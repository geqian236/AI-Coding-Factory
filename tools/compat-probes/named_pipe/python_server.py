#!/usr/bin/env python3
"""Windows Named Pipe server with current-user SID ACL.

验证：创建带当前用户 SID 访问控制的命名管道，执行 nonce 交换，拒绝重放攻击。
"""
import ctypes, ctypes.wintypes, hashlib, json, os, secrets, struct, sys, time
import threading

PIPE_NAME = r"\\.\pipe\factory_auth_probe"
NONCE_SIZE = 32

# Win32 constants
PIPE_ACCESS_DUPLEX         = 0x00000003
PIPE_TYPE_BYTE             = 0x00000000
PIPE_READMODE_BYTE         = 0x00000000
PIPE_WAIT                  = 0x00000000
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
GENERIC_READ               = 0x80000000
GENERIC_WRITE              = 0x40000000
OPEN_EXISTING              = 3
INVALID_HANDLE_VALUE       = ctypes.wintypes.HANDLE(-1).value
SECURITY_DESCRIPTOR_MIN_LENGTH = 20

k32 = ctypes.windll.kernel32
adv = ctypes.windll.advapi32

# 关键：Win64 上必须显式声明句柄型返回值/参数为 HANDLE(=c_void_p)。
# 否则 ctypes 默认按 c_int 处理，64 位句柄被截断成 32 位，
# CreateFileW 返回的坏句柄使 client 实际未连接、server 的 ConnectNamedPipe
# 永远阻塞，而非守护线程又阻止进程退出 —— 表现为整体挂死。
wt = ctypes.wintypes
k32.CreateNamedPipeW.restype = wt.HANDLE
k32.CreateNamedPipeW.argtypes = [
    wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_void_p,
]
k32.CreateFileW.restype = wt.HANDLE
k32.CreateFileW.argtypes = [
    wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE,
]
k32.ConnectNamedPipe.restype = wt.BOOL
k32.ConnectNamedPipe.argtypes = [wt.HANDLE, ctypes.c_void_p]
k32.DisconnectNamedPipe.argtypes = [wt.HANDLE]
k32.FlushFileBuffers.argtypes = [wt.HANDLE]
k32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
k32.WriteFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.GetCurrentProcess.restype = wt.HANDLE

# advapi32 同样必须声明类型：GetCurrentProcess() 返回伪句柄 0xFFFFFFFFFFFFFFFF，
# 若 OpenProcessToken 无 argtypes，ctypes 默认按 c_int 编组该值 → OverflowError，
# 被 except 吞成 ACL_FALLBACK（伪造 SID ACL 通过）。声明类型后 SID ACL 真正生效。
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
    """返回当前进程用户的 SID 字符串（如 S-1-5-21-...）"""
    token = ctypes.wintypes.HANDLE()
    adv.OpenProcessToken(k32.GetCurrentProcess(), 0x0008, ctypes.byref(token))  # TOKEN_QUERY
    size = ctypes.c_ulong(0)
    adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser=1
    buf = (ctypes.c_byte * size.value)()
    adv.GetTokenInformation(token, 1, buf, size, ctypes.byref(size))
    # TOKEN_USER starts with TOKEN_USER.User.Sid pointer
    sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
    sid_str_ptr = ctypes.c_wchar_p()
    adv.ConvertSidToStringSidW(ctypes.cast(sid_ptr, ctypes.c_void_p), ctypes.byref(sid_str_ptr))
    result = sid_str_ptr.value
    ctypes.windll.kernel32.CloseHandle(token)
    return result or "SID_UNKNOWN"

def create_pipe_with_current_user_acl():
    """创建仅允许当前用户访问的命名管道（SDDL ACL）。

    返回 (handle, sid, acl_applied)：acl_applied 如实反映 SID ACL 是否真正生效，
    绝不伪造。SDDL 构造失败时回退为无显式 ACL 的管道并置 acl_applied=False，
    使 pipe_created_with_sid_acl 断言据实通过/失败。
    """
    try:
        sid = get_current_user_sid_string()
        # SDDL: D:(A;;GRGW;;;current_user_sid) — 仅当前用户允许 GenericRead+GenericWrite。
        sddl = f"D:(A;;GRGW;;;{sid})"
        sd = ctypes.c_void_p()
        if adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(sd), None):
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
            # 句柄有效才算 ACL 真正生效。
            acl_applied = h is not None and h != INVALID_HANDLE_VALUE
            return h, sid, acl_applied
        # SDDL 转换失败：回退为无显式 ACL 的管道，如实标记 acl_applied=False。
        h = k32.CreateNamedPipeW(PIPE_NAME, PIPE_ACCESS_DUPLEX, PIPE_TYPE_BYTE, 1, 4096, 4096, 0, None)
        return h, sid, False
    except Exception as e:
        h = k32.CreateNamedPipeW(PIPE_NAME, PIPE_ACCESS_DUPLEX, PIPE_TYPE_BYTE, 1, 4096, 4096, 0, None)
        return h, f"ACL_FALLBACK:{e}", False

class NonceRegistry:
    """防重放 nonce 注册表"""
    def __init__(self):
        self._seen: set = set()
        self._lock = threading.Lock()

    def register(self, nonce: bytes) -> bool:
        """返回 True 表示新 nonce（允许）；False 表示重放（拒绝）"""
        h = hashlib.sha256(nonce).hexdigest()
        with self._lock:
            if h in self._seen:
                return False
            self._seen.add(h)
            return True

_registry = NonceRegistry()

def run_server_session(h_pipe, nonce: bytes, results: list):
    """处理一个 pipe 连接：发送 nonce，等待 echo，验证防重放"""
    connected = k32.ConnectNamedPipe(h_pipe, None)
    if not connected and k32.GetLastError() != 535:  # ERROR_PIPE_CONNECTED
        results.append({"error": f"ConnectNamedPipe failed: {k32.GetLastError()}"})
        return

    # Send nonce to client
    written = ctypes.c_ulong(0)
    k32.WriteFile(h_pipe, nonce, len(nonce), ctypes.byref(written), None)

    # Read client echo。必须用 c_ubyte（无符号）：c_byte 为 -128..127，
    # nonce 中任何 >127 的字节转 bytes() 会抛 ValueError（超出 range(0,256)）。
    buf = (ctypes.c_ubyte * 64)()
    read_bytes = ctypes.c_ulong(0)
    k32.ReadFile(h_pipe, buf, 64, ctypes.byref(read_bytes), None)
    echo = bytes(buf[:read_bytes.value])

    k32.FlushFileBuffers(h_pipe)
    k32.DisconnectNamedPipe(h_pipe)

    nonce_ok = (echo == nonce)
    results.append({
        "nonce_sent": nonce.hex(),
        "echo_received": echo.hex(),
        "nonce_exchange_ok": nonce_ok,
    })

def main():
    import datetime
    h_pipe, sid, acl_applied = create_pipe_with_current_user_acl()
    results = []
    actions = []
    assertions = []

    if h_pipe is None or h_pipe == INVALID_HANDLE_VALUE:
        receipt = {
            "spike": "named_pipe",
            "status": "ERROR",
            "error": f"CreateNamedPipeW failed: {k32.GetLastError()}",
            "timestamp": datetime.datetime.now().astimezone().isoformat(),
        }
        print(json.dumps(receipt, indent=2))
        sys.exit(1)

    actions.append(f"CreateNamedPipe with SID={sid}")

    # Generate nonce
    nonce = secrets.token_bytes(NONCE_SIZE)
    actions.append(f"generated nonce[{len(nonce)}]={nonce.hex()[:16]}...")

    # Run server in thread, then connect as client to self-test。
    # daemon=True：即便 ConnectNamedPipe/ReadFile 意外阻塞，主线程仍可退出，
    # 进程不会因非 daemon 线程挂在 Win32 阻塞调用上而永久卡死（原 2 分钟超时根因之一）。
    server_thread = threading.Thread(
        target=run_server_session, args=(h_pipe, nonce, results), daemon=True
    )
    server_thread.start()
    time.sleep(0.05)

    # Client side
    h_client = k32.CreateFileW(PIPE_NAME, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
    client_ok = (h_client is not None) and (h_client != INVALID_HANDLE_VALUE)
    actions.append(f"client connect: ok={client_ok}")

    if client_ok:
        # Read nonce from server
        rx_buf = (ctypes.c_ubyte * 64)()
        rx_count = ctypes.c_ulong(0)
        k32.ReadFile(h_client, rx_buf, 64, ctypes.byref(rx_count), None)
        rx_nonce = bytes(rx_buf[:rx_count.value])

        # Echo back
        written = ctypes.c_ulong(0)
        k32.WriteFile(h_client, rx_nonce, len(rx_nonce), ctypes.byref(written), None)
        k32.CloseHandle(h_client)
        actions.append(f"client echoed {rx_count.value} bytes")

    server_thread.join(timeout=3.0)
    k32.CloseHandle(h_pipe)

    session = results[0] if results else {}
    nonce_ok = session.get("nonce_exchange_ok", False)

    # Replay rejection test
    replay_allowed = _registry.register(nonce)  # first registration — should succeed
    replay_rejected = not _registry.register(nonce)  # second — should be rejected
    actions.append(f"replay test: first_ok={replay_allowed} second_rejected={replay_rejected}")

    # ACL 断言必须如实反映 SDDL 是否真正生效（acl_applied），禁止硬编码 passed=True。
    assertions.append({"name": "pipe_created_with_sid_acl", "passed": acl_applied, "detail": f"SID={sid} acl_applied={acl_applied}"})
    assertions.append({"name": "nonce_exchange_succeeds",    "passed": nonce_ok, "detail": "client echoed correct nonce"})
    assertions.append({"name": "replay_rejected",            "passed": replay_rejected, "detail": "duplicate nonce hash rejected"})

    status = "PASS" if all(a["passed"] for a in assertions) else "FAIL"
    env = {
        "os": "Windows", "python_version": sys.version.split()[0],
        "sid": sid, "probe_backend": "python_ctypes_win32",
    }
    artifact_bytes = json.dumps({"nonce_hex": nonce.hex(), "sid": sid}, sort_keys=True).encode()
    artifact_digest = "sha256:" + hashlib.sha256(artifact_bytes).hexdigest()

    receipt = {
        "spike": "named_pipe", "status": status,
        "environment": env, "actions": actions,
        "observable_facts": {
            "pipe_name": PIPE_NAME,
            "sid": sid,
            "acl_applied": acl_applied,
            "nonce_size_bytes": len(nonce),
            "nonce_exchange_ok": nonce_ok,
            "replay_rejected": replay_rejected,
        },
        "assertions": assertions,
        "artifact_digest": artifact_digest,
        "timestamp": datetime.datetime.now().astimezone().isoformat(),
    }
    out = json.dumps(receipt, indent=2)
    print(out)
    rp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "receipt.json")
    with open(rp, "w", encoding="utf-8") as f:
        f.write(out)
    sys.exit(0 if status == "PASS" else 1)

if __name__ == "__main__":
    main()