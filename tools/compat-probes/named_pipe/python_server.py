#!/usr/bin/env python3
"""认证命名管道 —— 真·跨进程 server 端。

本 server 不再自测：它创建带当前用户 SID ACL 的命名管道，然后循环接受
**独立进程**(Rust client `named_pipe.exe`) 发起的两次连接，凭自身持有的
nonce 注册表做跨进程重放判定，并用 GetNamedPipeClientProcessId 记录每个
连接对端的真实 PID，作为"对端确为另一进程"的硬证据。

协议(byte pipe, 定长帧)：
  client -> server : MAGIC(4=b"NPB1") + nonce(32)          共 36 字节
  server -> client : verdict(1)  0x01=ACCEPT 0x00=REPLAY 0x02=BADPROTO

跨进程重放证明：nonce 由 Rust client 进程生成；client 同一次运行连两次，
第 1 次发新 nonce -> server 注册并回 ACCEPT；第 2 次重发同一 nonce ->
由 **server 进程**凭注册表判定 REPLAY 并回 0x00。判定发生在另一进程，
不是同进程 set 去重。

server 只写 server_receipt.json；最终 receipt.json 由 driver 合成两侧结论。
"""
import ctypes
import ctypes.wintypes
import datetime
import hashlib
import json
import logging
import os
import sys
import threading

# ── 协议常量 ──────────────────────────────────────────────────────────────────
PIPE_NAME = r"\\.\pipe\factory_auth_probe"
MAGIC = b"NPB1"
NONCE_SIZE = 32
FRAME_SIZE = len(MAGIC) + NONCE_SIZE  # 36
EXPECTED_CONNECTIONS = 2
SERVER_JOIN_TIMEOUT_S = 15.0  # accept 循环守护线程的最长等待，防止 client 缺席时永久阻塞

VERDICT_ACCEPT = 0x01
VERDICT_REPLAY = 0x00
VERDICT_BADPROTO = 0x02

# Win32 常量
PIPE_ACCESS_DUPLEX = 0x00000003
PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
INVALID_HANDLE_VALUE = ctypes.wintypes.HANDLE(-1).value
ERROR_PIPE_CONNECTED = 535  # ConnectNamedPipe 在 client 抢先连上时返回 0 且 err=535，属正常

# 结构化诊断日志：走 stderr，nonce 一律脱敏(仅打印 sha256 前缀)，driver 的
# PIPE_READY 握手走 stdout，两者互不干扰。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] named_pipe_server: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("named_pipe_server")

k32 = ctypes.windll.kernel32
adv = ctypes.windll.advapi32
wt = ctypes.wintypes

# 关键：Win64 上必须显式声明句柄型返回值/参数为 HANDLE(=c_void_p)。
# 否则 ctypes 默认按 c_int 处理，64 位句柄被截断成 32 位，坏句柄会让
# ConnectNamedPipe/ReadFile 永久阻塞 —— 原 2 分钟挂死的根因之一。
k32.CreateNamedPipeW.restype = wt.HANDLE
k32.CreateNamedPipeW.argtypes = [
    wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_void_p,
]
k32.ConnectNamedPipe.restype = wt.BOOL
k32.ConnectNamedPipe.argtypes = [wt.HANDLE, ctypes.c_void_p]
k32.DisconnectNamedPipe.argtypes = [wt.HANDLE]
k32.FlushFileBuffers.argtypes = [wt.HANDLE]
k32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
k32.WriteFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.GetLastError.restype = wt.DWORD
k32.GetCurrentProcess.restype = wt.HANDLE
# 跨进程铁证：取管道对端(client)的真实进程 ID。
k32.GetNamedPipeClientProcessId.restype = wt.BOOL
k32.GetNamedPipeClientProcessId.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]

# advapi32 同样必须声明类型：GetCurrentProcess() 返回伪句柄 0xFFFFFFFFFFFFFFFF，
# 若 OpenProcessToken 无 argtypes，ctypes 默认按 c_int 编组 → OverflowError，
# 被 except 吞成 ACL_FALLBACK(伪造 SID ACL 通过)。声明类型后 SID ACL 真正生效。
adv.OpenProcessToken.restype = wt.BOOL
adv.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
adv.GetTokenInformation.restype = wt.BOOL
adv.GetTokenInformation.argtypes = [
    wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
]
adv.ConvertSidToStringSidW.restype = wt.BOOL
adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.LPWSTR)]
adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wt.BOOL
adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    wt.LPCWSTR, wt.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    """Win32 SECURITY_ATTRIBUTES —— 用真实结构体替代手工 struct.pack，避免对齐错误。"""

    _fields_ = [
        ("nLength", wt.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wt.BOOL),
    ]


def get_current_user_sid_string() -> str:
    """返回当前进程用户的 SID 字符串(如 S-1-5-21-...)。"""
    token = ctypes.wintypes.HANDLE()
    adv.OpenProcessToken(k32.GetCurrentProcess(), 0x0008, ctypes.byref(token))  # TOKEN_QUERY
    size = ctypes.c_ulong(0)
    adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser=1
    buf = (ctypes.c_byte * size.value)()
    adv.GetTokenInformation(token, 1, buf, size, ctypes.byref(size))
    # TOKEN_USER 起始即 TOKEN_USER.User.Sid 指针
    sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
    sid_str_ptr = ctypes.c_wchar_p()
    adv.ConvertSidToStringSidW(ctypes.cast(sid_ptr, ctypes.c_void_p), ctypes.byref(sid_str_ptr))
    result = sid_str_ptr.value
    k32.CloseHandle(token)
    return result or "SID_UNKNOWN"


def create_pipe_with_current_user_acl() -> "tuple[int | None, str, bool]":
    """创建仅允许当前用户访问的命名管道(SDDL ACL)。

    返回 (handle, sid, acl_applied)：acl_applied 如实反映 SID ACL 是否真正生效，
    绝不伪造。SDDL 构造失败时回退为无显式 ACL 的管道并置 acl_applied=False，
    使 pipe_created_with_sid_acl 断言据实通过/失败。
    """
    try:
        sid = get_current_user_sid_string()
        # SDDL: D:(A;;GRGW;;;current_user_sid) — 仅当前用户允许 GenericRead+GenericWrite。
        sddl = f"D:(A;;GRGW;;;{sid})"
        sd = ctypes.c_void_p()
        if adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(sd), None):
            sec_attrs = SECURITY_ATTRIBUTES()
            sec_attrs.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
            sec_attrs.lpSecurityDescriptor = sd
            sec_attrs.bInheritHandle = False
            h = k32.CreateNamedPipeW(
                PIPE_NAME,
                PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE,
                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
                1, 4096, 4096, 0,
                ctypes.byref(sec_attrs),
            )
            acl_applied = h is not None and h != INVALID_HANDLE_VALUE
            return h, sid, acl_applied
        # SDDL 转换失败：回退为无显式 ACL 的管道，如实标记 acl_applied=False。
        h = k32.CreateNamedPipeW(PIPE_NAME, PIPE_ACCESS_DUPLEX, PIPE_TYPE_BYTE, 1, 4096, 4096, 0, None)
        return h, sid, False
    except Exception as e:  # noqa: BLE001 —— 回退路径必须如实标记失败，不得伪装成功
        h = k32.CreateNamedPipeW(PIPE_NAME, PIPE_ACCESS_DUPLEX, PIPE_TYPE_BYTE, 1, 4096, 4096, 0, None)
        return h, f"ACL_FALLBACK:{e}", False


class NonceRegistry:
    """跨进程防重放 nonce 注册表(server 进程独占持有)。"""

    def __init__(self) -> None:
        self._seen: set = set()
        self._lock = threading.Lock()

    def register(self, nonce: bytes) -> bool:
        """返回 True 表示首见 nonce(允许)；False 表示重放(拒绝)。"""
        digest = hashlib.sha256(nonce).hexdigest()
        with self._lock:
            if digest in self._seen:
                return False
            self._seen.add(digest)
            return True


def read_exact(h_pipe: int, n: int) -> bytes:
    """从管道精确读取 n 字节；对端关闭或读到 0 字节则提前返回已读部分。"""
    buf = bytearray()
    remaining = n
    while remaining > 0:
        chunk = (ctypes.c_ubyte * remaining)()  # c_ubyte(无符号)：>127 字节转 bytes 才不越界
        got = wt.DWORD(0)
        ok = k32.ReadFile(h_pipe, chunk, remaining, ctypes.byref(got), None)
        if not ok or got.value == 0:
            break
        buf.extend(bytes(chunk[: got.value]))
        remaining -= got.value
    return bytes(buf)


def write_all(h_pipe: int, data: bytes) -> int:
    """向管道写出全部字节，返回实际写入数。"""
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    written = wt.DWORD(0)
    k32.WriteFile(h_pipe, buf, len(data), ctypes.byref(written), None)
    return written.value


def serve_connections(h_pipe: int, server_pid: int, registry: NonceRegistry, results: list, expected: int) -> None:
    """accept 循环：串行服务 expected 个独立进程连接。

    每个连接：ConnectNamedPipe 等待 -> 取对端 PID -> 读 36 字节帧 ->
    校验 MAGIC -> 查注册表得裁决 -> 回 1 字节 -> DisconnectNamedPipe 复用管道实例。
    """
    for i in range(expected):
        connected = k32.ConnectNamedPipe(h_pipe, None)
        err = k32.GetLastError()
        if not connected and err != ERROR_PIPE_CONNECTED:
            log.error("connection[%d] ConnectNamedPipe failed err=%d", i, err)
            results.append({"index": i, "error": f"ConnectNamedPipe failed: {err}"})
            k32.DisconnectNamedPipe(h_pipe)
            continue

        # 取对端真实 PID —— 与 server_pid 不同即证明对端是独立进程。
        client_pid = wt.DWORD(0)
        k32.GetNamedPipeClientProcessId(h_pipe, ctypes.byref(client_pid))

        req = read_exact(h_pipe, FRAME_SIZE)
        if len(req) < FRAME_SIZE or req[: len(MAGIC)] != MAGIC:
            verdict = VERDICT_BADPROTO
            nonce_hex = ""
            nonce_digest = ""
        else:
            nonce = req[len(MAGIC):FRAME_SIZE]
            nonce_hex = nonce.hex()
            nonce_digest = hashlib.sha256(nonce).hexdigest()
            fresh = registry.register(nonce)
            verdict = VERDICT_ACCEPT if fresh else VERDICT_REPLAY

        write_all(h_pipe, bytes([verdict]))
        k32.FlushFileBuffers(h_pipe)
        k32.DisconnectNamedPipe(h_pipe)

        # 日志脱敏：只记 nonce 的 sha256 前缀，绝不打印原始 nonce。
        log.info(
            "connection[%d] client_pid=%d verdict=0x%02x nonce_sha256=%s",
            i, client_pid.value, verdict, (nonce_digest[:16] or "<badproto>"),
        )
        results.append({
            "index": i,
            "client_pid": client_pid.value,
            "nonce_hex": nonce_hex,
            "nonce_sha256": nonce_digest,
            "verdict": verdict,
        })


def build_receipt(sid: str, acl_applied: bool, server_pid: int, results: list, timed_out: bool) -> dict:
    """据 accept 循环的真实结果构建 server_receipt(断言全部来自可观测事实)。"""
    conns = [r for r in results if "verdict" in r]
    first = conns[0] if len(conns) >= 1 else {}
    second = conns[1] if len(conns) >= 2 else {}
    client_pids = [r["client_pid"] for r in conns]

    # 断言 1：SID ACL 真正生效(如实反映 acl_applied)。
    # 断言 2：所有对端 PID 均非 0 且 != server_pid —— 铁证对端是独立进程。
    all_cross_process = len(conns) >= 1 and all(p != 0 and p != server_pid for p in client_pids)
    # 断言 3：首个 nonce 被 ACCEPT。
    first_accepted = first.get("verdict") == VERDICT_ACCEPT
    # 断言 4：第二次同一 nonce 被 server 跨进程判为 REPLAY。
    same_nonce = bool(first.get("nonce_hex")) and second.get("nonce_hex") == first.get("nonce_hex")
    replay_rejected = second.get("verdict") == VERDICT_REPLAY and same_nonce

    assertions = [
        {"name": "pipe_created_with_sid_acl", "passed": bool(acl_applied),
         "detail": f"SID={sid} acl_applied={acl_applied}"},
        {"name": "client_is_separate_process", "passed": all_cross_process,
         "detail": f"server_pid={server_pid} client_pids={client_pids}"},
        {"name": "first_nonce_accepted", "passed": first_accepted,
         "detail": f"verdict=0x{first.get('verdict', -1):02x} nonce_sha256={first.get('nonce_sha256', '')[:16]}"},
        {"name": "replay_rejected_cross_process", "passed": replay_rejected,
         "detail": (f"2nd verdict=0x{second.get('verdict', -1):02x} "
                    f"same_nonce={same_nonce} judged_by_pid={server_pid}")},
    ]
    status = "PASS" if (not timed_out and all(a["passed"] for a in assertions)) else "FAIL"

    # artifact_digest 覆盖真实可观测结果，供交叉核验。
    artifact_bytes = json.dumps(
        {"sid": sid, "server_pid": server_pid,
         "connections": [{"client_pid": c["client_pid"], "verdict": c["verdict"],
                          "nonce_sha256": c["nonce_sha256"]} for c in conns]},
        sort_keys=True,
    ).encode()
    artifact_digest = "sha256:" + hashlib.sha256(artifact_bytes).hexdigest()

    return {
        "spike": "named_pipe_server",
        "status": status,
        "environment": {
            "os": "Windows",
            "python_version": sys.version.split()[0],
            "sid": sid,
            "probe_backend": "python_ctypes_win32",
        },
        "observable_facts": {
            "pipe_name": PIPE_NAME,
            "sid": sid,
            "acl_applied": bool(acl_applied),
            "server_pid": server_pid,
            "expected_connections": EXPECTED_CONNECTIONS,
            "served_connections": len(conns),
            "timed_out": timed_out,
            "connections": conns,
        },
        "assertions": assertions,
        "artifact_digest": artifact_digest,
        "timestamp": datetime.datetime.now().astimezone().isoformat(),
    }


def main() -> None:
    server_pid = os.getpid()
    h_pipe, sid, acl_applied = create_pipe_with_current_user_acl()

    receipt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server_receipt.json")

    if h_pipe is None or h_pipe == INVALID_HANDLE_VALUE:
        err = k32.GetLastError()
        log.error("CreateNamedPipeW failed err=%d", err)
        receipt = {
            "spike": "named_pipe_server",
            "status": "ERROR",
            "error": f"CreateNamedPipeW failed: {err}",
            "timestamp": datetime.datetime.now().astimezone().isoformat(),
        }
        with open(receipt_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(receipt, indent=2))
        # PIPE_READY 未发出，driver 不会启动 client。
        sys.exit(1)

    log.info("pipe created server_pid=%d acl_applied=%s sid=%s", server_pid, acl_applied, sid)
    # 管道创建后即处于 listening 状态；通知 driver 可以启动 client 进程。
    print("PIPE_READY", flush=True)

    registry = NonceRegistry()
    results: list = []
    # 守护线程跑 accept 循环：即便某次 ConnectNamedPipe/ReadFile 意外阻塞，
    # 主线程仍可在超时后退出，进程不会永久卡死。
    worker = threading.Thread(
        target=serve_connections,
        args=(h_pipe, server_pid, registry, results, EXPECTED_CONNECTIONS),
        daemon=True,
    )
    worker.start()
    worker.join(timeout=SERVER_JOIN_TIMEOUT_S)
    timed_out = worker.is_alive()
    if timed_out:
        log.error("accept loop timed out after %.1fs (client absent?)", SERVER_JOIN_TIMEOUT_S)
    k32.CloseHandle(h_pipe)

    receipt = build_receipt(sid, acl_applied, server_pid, results, timed_out)
    out = json.dumps(receipt, indent=2)
    with open(receipt_path, "w", encoding="utf-8") as f:
        f.write(out)
    print(out)
    log.info("server done status=%s served=%d", receipt["status"],
             receipt["observable_facts"]["served_connections"])
    sys.exit(0 if receipt["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
