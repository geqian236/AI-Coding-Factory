#!/usr/bin/env python3
"""Git Object Bridge 兼容性探针。

验证 Windows 只读 Git Adapter 的核心假设（master spec GIT-001 / DOD-GIT-001）：
  1. 以 GIT_OPTIONAL_LOCKS=0 从用户源仓库生成 bundle，并记录 bundle digest；
  2. 将 bundle 导入独立 bare mirror，从 mirror 创建任务 worktree（要求原生
     `worktree add` 成功，不接受退化 clone 冒充）；
  3. 独立 pack 往返：`pack-objects` 打出 packfile → `index-pack --verify` 校验
     pack 自洽与全对象 SHA 完整性，记录 pack digest；
  4. 用户 workspace 零污染：bridge 全流程前后各采一次源仓库完整指纹（工作区
     文件、git status、HEAD、分支、全部 refs、对象计数、index 摘要），断言两次
     快照完全相等。

所有写操作（mirror / worktree / pack 校验）都落在源仓库之外的临时目录，用以
证明只读 bridge 不触碰用户 index / worktree / branch / ref。任一断言不成立即
FAIL，绝不用恒真断言冒充通过。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile

SPIKE = "git_object_bridge"


def _sha256(data: bytes) -> str:
    """返回带 `sha256:` 前缀的十六进制摘要。"""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _run(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    """运行 git 命令（文本模式）。

    强制 `GIT_OPTIONAL_LOCKS=0`：只读操作不写用户 index，是零污染的前提。
    显式 UTF-8 解码，避免 Windows GBK 本地编码吞掉中文输出。
    """
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=cwd,
        check=False,
    )


def _env_info() -> dict[str, str]:
    """采集环境指纹字段（写入 receipt 的 environment）。"""
    git_v = _run(["git", "--version"]).stdout.strip()
    return {
        "os": platform.system(),
        "os_version": platform.version(),
        "python_version": platform.python_version(),
        "git_version": git_v,
        "arch": platform.machine(),
    }


def _repo_fingerprint(repo: str) -> dict[str, object]:
    """采集源仓库完整状态指纹，用于零污染断言。

    覆盖：工作区文件列表（排除 .git）、git status、HEAD、分支、全部 refs、
    对象计数、以及 .git/index 内容摘要。bridge 全流程前后各采一次，必须完全
    相等，才能证明只读 bridge 未污染用户 index / worktree / branch / ref。
    """

    def g(sub: list[str]) -> str:
        return _run(["git", "-C", repo, *sub]).stdout

    worktree_files: list[str] = []
    for root, dirs, files in os.walk(repo):
        if ".git" in dirs:
            dirs.remove(".git")  # 不递归 .git 内部
        for fn in files:
            rel = os.path.relpath(os.path.join(root, fn), repo).replace("\\", "/")
            worktree_files.append(rel)

    index_path = os.path.join(repo, ".git", "index")
    if os.path.exists(index_path):
        with open(index_path, "rb") as f:
            index_digest = _sha256(f.read())
    else:
        index_digest = "none"

    return {
        "worktree_files": sorted(worktree_files),
        "status": g(["status", "--porcelain"]),
        "head": g(["rev-parse", "HEAD"]).strip(),
        "branches": g(["branch", "--format=%(refname) %(objectname)"]),
        "refs": g(["show-ref"]),
        "count_objects": g(["count-objects", "-v"]),
        "index_digest": index_digest,
    }


def run_probe(tmp_dir: str) -> dict[str, object]:
    """执行 Git Object Bridge 探针并返回机器可读 receipt。"""
    actions: list[str] = []
    assertions: list[dict[str, object]] = []

    # 1) 创建用户源仓库并提交一个哨兵文件
    src_repo = os.path.join(tmp_dir, "src_repo")
    os.makedirs(src_repo)
    _run(["git", "init", src_repo], cwd=tmp_dir)
    _run(["git", "-C", src_repo, "config", "user.email", "probe@factory.local"])
    _run(["git", "-C", src_repo, "config", "user.name", "Probe"])
    sentinel_file = os.path.join(src_repo, "sentinel.txt")
    with open(sentinel_file, "w", encoding="utf-8") as f:
        f.write("PROBE_SENTINEL_v1\n")
    _run(["git", "-C", src_repo, "add", "sentinel.txt"])
    _run(["git", "-C", src_repo, "commit", "-m", "probe commit"])
    head = _run(["git", "-C", src_repo, "rev-parse", "HEAD"]).stdout.strip()
    actions.append(f"created source repo, HEAD={head[:12]}")

    # 零污染基线：在任何 bridge 操作之前采一次完整指纹
    fp_before = _repo_fingerprint(src_repo)

    # 2) 以 GIT_OPTIONAL_LOCKS=0 生成 bundle
    bundle_path = os.path.join(tmp_dir, "probe.bundle")
    r_bundle = _run(["git", "-C", src_repo, "bundle", "create", bundle_path, "--all"])
    bundle_ok = r_bundle.returncode == 0 and os.path.exists(bundle_path)
    bundle_size = os.path.getsize(bundle_path) if bundle_ok else 0
    if bundle_ok:
        with open(bundle_path, "rb") as f:
            bundle_digest = _sha256(f.read())
    else:
        bundle_digest = "none"
    actions.append(
        f"git bundle create: ok={bundle_ok} size={bundle_size}B digest={bundle_digest[:20]}"
    )

    # 3) 导入独立 bare mirror
    mirror_path = os.path.join(tmp_dir, "mirror.git")
    r_mirror = _run(["git", "clone", "--bare", bundle_path, mirror_path])
    mirror_ok = r_mirror.returncode == 0
    actions.append(f"clone bare from bundle: ok={mirror_ok}")

    # 4) 从 mirror 原生 worktree add（退化 clone 仅作诊断，不使断言转绿）
    wt_path = os.path.join(tmp_dir, "worktree")
    r_wt = _run(["git", "-C", mirror_path, "worktree", "add", wt_path, "HEAD"])
    worktree_add_ok = r_wt.returncode == 0
    clone_fallback_used = False
    if not worktree_add_ok:
        os.makedirs(wt_path, exist_ok=True)
        r_fallback = _run(["git", "clone", mirror_path, wt_path])
        clone_fallback_used = r_fallback.returncode == 0
    actions.append(
        f"worktree add native={worktree_add_ok} clone_fallback={clone_fallback_used}"
    )

    # 5) 哨兵内容往返校验
    wt_sentinel = os.path.join(wt_path, "sentinel.txt")
    sentinel_ok = False
    if os.path.exists(wt_sentinel):
        with open(wt_sentinel, encoding="utf-8") as f:
            sentinel_ok = f.read().strip() == "PROBE_SENTINEL_v1"
    actions.append(f"sentinel verified in worktree: {sentinel_ok}")

    # 6) 独立 pack 往返：pack-objects 打包 → index-pack --verify 校验
    pack_dir = os.path.join(tmp_dir, "packs")
    os.makedirs(pack_dir)
    rev = _run(["git", "-C", src_repo, "rev-list", "--all", "--objects"])
    obj_input = "".join(
        line.split()[0] + "\n" for line in rev.stdout.splitlines() if line.strip()
    )
    pack_prefix = os.path.join(pack_dir, "probe")
    pp = subprocess.run(
        ["git", "-C", src_repo, "pack-objects", pack_prefix],
        input=obj_input,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        check=False,
    )
    pack_name = pp.stdout.strip().splitlines()[-1] if pp.stdout.strip() else ""
    pack_file = f"{pack_prefix}-{pack_name}.pack"
    pack_ok = pp.returncode == 0 and bool(pack_name) and os.path.exists(pack_file)
    if pack_ok:
        with open(pack_file, "rb") as f:
            pack_digest = _sha256(f.read())
    else:
        pack_digest = "none"
    # index-pack --verify 真实校验 pack 自洽与全对象 SHA 完整性
    r_verify = _run(["git", "index-pack", "--verify", pack_file]) if pack_ok else None
    pack_verify_ok = r_verify is not None and r_verify.returncode == 0
    actions.append(
        f"pack roundtrip: pack_ok={pack_ok} verify_ok={pack_verify_ok} "
        f"digest={pack_digest[:20]}"
    )

    # 7) 零污染终态：全流程结束后再采一次指纹，断言与基线完全相等
    fp_after = _repo_fingerprint(src_repo)
    zero_pollution = fp_before == fp_after
    polluted_keys = [k for k in fp_before if fp_before.get(k) != fp_after.get(k)]
    actions.append(
        f"user workspace zero pollution: {zero_pollution}"
        + (f" (differing keys: {polluted_keys})" if polluted_keys else "")
    )

    assertions.append(
        {"name": "bundle_created", "passed": bundle_ok, "detail": f"size={bundle_size}B"}
    )
    assertions.append(
        {"name": "mirror_import_ok", "passed": mirror_ok, "detail": "bare clone from bundle"}
    )
    assertions.append(
        {
            "name": "worktree_add_native",
            "passed": worktree_add_ok,
            "detail": f"native worktree add (clone_fallback={clone_fallback_used})",
        }
    )
    assertions.append(
        {
            "name": "sentinel_survives_roundtrip",
            "passed": sentinel_ok,
            "detail": "probe commit content verified in worktree",
        }
    )
    assertions.append(
        {"name": "pack_created", "passed": pack_ok, "detail": f"digest={pack_digest[:24]}"}
    )
    assertions.append(
        {
            "name": "pack_verify_index_pack",
            "passed": pack_verify_ok,
            "detail": "git index-pack --verify 校验 pack 自洽与全对象完整性",
        }
    )
    assertions.append(
        {
            "name": "user_workspace_zero_pollution",
            "passed": zero_pollution,
            "detail": f"源仓库全流程前后指纹一致；differing={polluted_keys}",
        }
    )

    status = "PASS" if all(a["passed"] for a in assertions) else "FAIL"
    env = _env_info()
    env_digest = _sha256(json.dumps(env, sort_keys=True).encode())

    return {
        "spike": SPIKE,
        "status": status,
        "environment": env,
        "actions": actions,
        "observable_facts": {
            "head_sha": head,
            "bundle_size_bytes": bundle_size,
            "bundle_digest": bundle_digest,
            "bundle_ok": bundle_ok,
            "mirror_ok": mirror_ok,
            "worktree_add_native": worktree_add_ok,
            "clone_fallback_used": clone_fallback_used,
            "sentinel_ok": sentinel_ok,
            "pack_ok": pack_ok,
            "pack_digest": pack_digest,
            "pack_verify_ok": pack_verify_ok,
            "zero_pollution": zero_pollution,
            "polluted_keys": polluted_keys,
        },
        "assertions": assertions,
        "artifact_digest": bundle_digest,
        "env_digest": env_digest,
        "timestamp": datetime.datetime.now().astimezone().isoformat(),
    }


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as td:
        receipt = run_probe(td)
    receipt_json = json.dumps(receipt, indent=2, ensure_ascii=False)
    rp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "receipt.json")
    with open(rp, "w", encoding="utf-8") as f:
        f.write(receipt_json)
    # 控制台回显：Windows GBK 控制台可能无法编码中文，失败则退化为 ASCII 转义
    try:
        print(receipt_json)
    except UnicodeEncodeError:
        print(json.dumps(receipt, indent=2, ensure_ascii=True))
    sys.exit(0 if receipt["status"] == "PASS" else 1)
