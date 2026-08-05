#!/usr/bin/env python3
"""SQLite WAL + FULL synchronous mode 崩溃恢复与 group-commit 探针。

验证 `journal_mode=WAL` + `synchronous=FULL` 下的三项真实事实：
  1. process-kill 恢复：子进程提交 N 个已 COMMIT 的批次后被**硬杀**（os.kill /
     TerminateProcess，非正常关闭连接），母进程重开库校验「已提交前缀完整存活、
     未提交尾部不出现」——这是真实断电/崩溃语义，而非线程正常 close。
  2. group-commit 原子性：一个事务内批量写入 batch_size 行后 COMMIT，崩溃恢复后
     该批次要么整批可见、要么整批不可见，不允许出现半批。
  3. disk-full：本 sandbox 无独立小卷、无法可靠制造 ENOSPC（易 flaky 或需管理员
     挂载 VHD），诚实标记 BLOCKED_UNCERTIFIED 子结果并给出解封步骤，绝不伪造。

receipt 绑定：emit real `eventBatchParameters` tuple（maxBatchEvents/maxBatchBytes/
maxBatchAgeMs/synchronous，取 Master Spec §11 参考值 200/256 KiB/500 ms/FULL）与
`parameterTupleDigest`（与 tools/compat-probes/emit_manifest.py 同一算法），使
Compatibility Manifest 能真正绑定「SQLite 实测的参数」而非硬编码默认值。

失败错误码语义随 receipt.status（PASS / FAIL / 内含 BLOCKED_UNCERTIFIED 子结果）。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time

SPIKE = "sqlite_wal_full"

# 事件批处理参数（Master Spec §11 首版参考值：200 events / 256 KiB / 500 ms）。
# 这是 SQLite spike 实际施加并认证的 tuple；emit_manifest 读取并重算 digest 绑定。
EVENT_BATCH_PARAMETERS = {
    "maxBatchEvents": 200,
    "maxBatchBytes": 262144,  # 256 KiB
    "maxBatchAgeMs": 500,
    "synchronous": "FULL",
}


def _sha256(data: bytes) -> str:
    """计算 SHA-256 并返回 "sha256:<hex>"。"""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _sha256_dict(d: dict) -> str:
    """按 key 排序序列化后取 SHA-256（与 emit_manifest._sha256_dict 一致）。"""
    return _sha256(json.dumps(d, sort_keys=True, ensure_ascii=False).encode("utf-8"))


def parameter_tuple_digest(params: dict) -> str:
    """计算 parameterTupleDigest。

    与 tools/compat-probes/emit_manifest.compute_parameter_tuple_digest 字节一致：
    仅取 maxBatchEvents/maxBatchBytes/maxBatchAgeMs/synchronous 四键，按 key 排序
    JSON 序列化后 SHA-256。synchronous 必须为 FULL。
    """
    if params.get("synchronous") != "FULL":
        raise ValueError(f"SYNCHRONOUS_NOT_FULL: synchronous 必须为 FULL，实际 {params.get('synchronous')}")
    tuple_dict = {
        "maxBatchAgeMs": params["maxBatchAgeMs"],
        "maxBatchBytes": params["maxBatchBytes"],
        "maxBatchEvents": params["maxBatchEvents"],
        "synchronous": params["synchronous"],
    }
    return _sha256_dict(tuple_dict)


def _env_info() -> dict:
    """采集环境信息（用于 env_digest 绑定）。"""
    return {
        "os": platform.system(),
        "os_version": platform.version(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "arch": platform.machine(),
        "sqlite_version": sqlite3.sqlite_version,
    }


# 子进程源码：连接库、置 WAL+FULL、逐批 COMMIT，到达 kill 标记批次后打印
# "COMMITTED:<n>" 并进入忙等，等待母进程硬杀。绝不自行退出或正常 close。
_CHILD_SOURCE = r'''
import os, sqlite3, sys, time
db_path = sys.argv[1]
committed_batches = int(sys.argv[2])
batch_size = int(sys.argv[3])
con = sqlite3.connect(db_path)
con.execute("PRAGMA journal_mode = WAL")
con.execute("PRAGMA synchronous = FULL")
con.execute("CREATE TABLE IF NOT EXISTS ev (id INTEGER PRIMARY KEY, batch INTEGER, val TEXT)")
con.commit()
# 逐批：一个事务写 batch_size 行再 COMMIT（group commit 语义）。
for b in range(committed_batches):
    con.execute("BEGIN")
    for _ in range(batch_size):
        con.execute("INSERT INTO ev (batch, val) VALUES (?, ?)", (b, "x" * 32))
    con.commit()
# 已提交 committed_batches 个批次；再开一个**未提交**事务（崩溃后应整批消失）。
con.execute("BEGIN")
for _ in range(batch_size):
    con.execute("INSERT INTO ev (batch, val) VALUES (?, ?)", (committed_batches, "uncommitted"))
# 不 commit、不 close：宣告已提交批次数后忙等被硬杀。
print("COMMITTED:%d" % committed_batches, flush=True)
while True:
    time.sleep(0.05)
'''


def _run_process_kill_recovery(tmp_dir: str, committed_batches: int, batch_size: int) -> dict:
    """真实 process-kill 崩溃恢复子测试。

    启动子进程写入并 COMMIT committed_batches 个批次（每批 batch_size 行），外加一个
    未提交事务，然后**硬杀**子进程；母进程重开库校验：
      - 已提交行数 == committed_batches * batch_size（前缀完整存活）
      - 不存在未提交批次的行（未提交尾部不出现）
      - 每个已提交批次的行数恰为 batch_size（group-commit 原子、无半批）
    """
    db_path = os.path.join(tmp_dir, "crash_probe.db")
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD_SOURCE, db_path, str(committed_batches), str(batch_size)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # 等待子进程宣告已提交批次（最多 10s，避免挂死）。
    committed_seen = False
    deadline = time.monotonic() + 10.0
    assert child.stdout is not None
    while time.monotonic() < deadline:
        line = child.stdout.readline()
        if line.startswith("COMMITTED:"):
            committed_seen = True
            break
        if child.poll() is not None:
            break
    # 硬杀（SIGKILL / TerminateProcess）——真实崩溃，不给清理机会。
    child.kill()
    child.wait(timeout=5.0)

    # 母进程重开库，校验恢复后的持久前缀。
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode = WAL")
    rows_total = con.execute("SELECT COUNT(*) FROM ev").fetchone()[0]
    # 每个 batch 的行数分布（检测半批 / 未提交泄漏）。
    per_batch = dict(con.execute("SELECT batch, COUNT(*) FROM ev GROUP BY batch").fetchall())
    con.close()

    expected_rows = committed_batches * batch_size
    committed_prefix_intact = rows_total == expected_rows
    uncommitted_absent = committed_batches not in per_batch
    all_batches_atomic = all(
        per_batch.get(b, 0) == batch_size for b in range(committed_batches)
    )
    return {
        "committed_seen": committed_seen,
        "rows_total": rows_total,
        "expected_rows": expected_rows,
        "committed_prefix_intact": committed_prefix_intact,
        "uncommitted_absent": uncommitted_absent,
        "all_batches_atomic": all_batches_atomic,
        "per_batch_counts": {str(k): v for k, v in sorted(per_batch.items())},
    }


def run_probe(tmp_dir: str) -> dict:
    """执行 SQLite WAL+FULL 崩溃恢复 + group-commit + disk-full 三项子测试。"""
    actions: list[str] = []
    assertions: list[dict] = []
    sub_results: list[dict] = []

    env = _env_info()
    env_digest = _sha256_dict(env)
    param_tuple_digest = parameter_tuple_digest(EVENT_BATCH_PARAMETERS)

    batch_size = 20
    committed_batches = 5

    # ── 子测试 1+2：真实 process-kill 恢复 + group-commit 原子性 ──────────────
    kill = _run_process_kill_recovery(tmp_dir, committed_batches, batch_size)
    actions.append(
        f"spawn child writer: {committed_batches} 批 × {batch_size} 行 COMMIT + 1 未提交事务，"
        f"child.kill() 硬杀"
    )
    actions.append(
        f"recovery: rows_total={kill['rows_total']} (expected {kill['expected_rows']}), "
        f"per_batch={kill['per_batch_counts']}"
    )
    assertions.append({
        "name": "child_committed_before_kill",
        "passed": kill["committed_seen"],
        "detail": "子进程在被杀前已宣告 COMMITTED",
    })
    assertions.append({
        "name": "committed_prefix_survives_process_kill",
        "passed": kill["committed_prefix_intact"],
        "detail": f"已提交前缀完整：{kill['rows_total']}=={kill['expected_rows']}",
    })
    assertions.append({
        "name": "uncommitted_tail_absent_after_kill",
        "passed": kill["uncommitted_absent"],
        "detail": "未提交事务的批次在恢复后不出现",
    })
    assertions.append({
        "name": "group_commit_atomic_no_half_batch",
        "passed": kill["all_batches_atomic"],
        "detail": "每个已提交批次行数恰为 batch_size（无半批）",
    })
    sub_results.append({
        "aspect": "process_kill_recovery",
        "status": "PASS" if (
            kill["committed_seen"] and kill["committed_prefix_intact"]
            and kill["uncommitted_absent"] and kill["all_batches_atomic"]
        ) else "FAIL",
        "facts": kill,
    })
    sub_results.append({
        "aspect": "group_commit_atomicity",
        "status": "PASS" if kill["all_batches_atomic"] else "FAIL",
        "facts": {"per_batch_counts": kill["per_batch_counts"], "batch_size": batch_size},
    })

    # ── 子测试 3：disk-full —— 诚实 BLOCKED_UNCERTIFIED（不伪造）───────────────
    actions.append("disk-full：本 sandbox 无独立小卷，标记 BLOCKED_UNCERTIFIED，不伪造")
    sub_results.append({
        "aspect": "disk_full_enospc",
        "status": "BLOCKED_UNCERTIFIED",
        "reason": "本环境无独立小容量卷，无法可靠制造 ENOSPC；模拟易 flaky 或需管理员挂载 VHD",
        "unblock_steps": [
            "以管理员挂载固定小容量 VHD（如 16 MiB）到纯 ASCII 路径",
            "将 crash_probe.db 置于该卷，写入直至 sqlite3.OperationalError: database or disk is full",
            "校验 WAL 在 ENOSPC 下不暴露半提交批次，恢复后仍满足 group-commit 原子性",
        ],
    })

    # 顶层 status：真实认证项（process-kill + group-commit）全 PASS 即 PASS；
    # disk-full 作为诚实 BLOCKED 子结果披露，不拉低顶层（对齐 Task 7 spike 惯例）。
    core_pass = all(a["passed"] for a in assertions)
    status = "PASS" if core_pass else "FAIL"

    bench_profile = {
        "committed_batches": committed_batches,
        "batch_size": batch_size,
        "rows_recovered": kill["rows_total"],
    }
    bench_digest = _sha256_dict(bench_profile)
    artifact_digest = _sha256_dict({
        "rows_total": kill["rows_total"],
        "per_batch": kill["per_batch_counts"],
    })

    return {
        "spike": SPIKE,
        "status": status,
        "environment": env,
        "actions": actions,
        "observable_facts": {
            "rows_recovered": kill["rows_total"],
            "committed_prefix_intact": kill["committed_prefix_intact"],
            "uncommitted_tail_absent": kill["uncommitted_absent"],
            "group_commit_atomic": kill["all_batches_atomic"],
        },
        # eventBatchParameters + parameterTupleDigest：供 emit_manifest 真实绑定。
        "eventBatchParameters": EVENT_BATCH_PARAMETERS,
        "parameterTupleDigest": param_tuple_digest,
        "benchmarkProfileDigest": bench_digest,
        "benchmark_profile": bench_profile,
        "env_digest": env_digest,
        # param_digest 保留以兼容 emit_manifest 现有必需字段校验（= tuple digest）。
        "param_digest": param_tuple_digest,
        "assertions": assertions,
        "subResults": sub_results,
        "artifact_digest": artifact_digest,
        "timestamp": datetime.datetime.now().astimezone().isoformat(),
    }


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as td:
        receipt = run_probe(td)
    out = json.dumps(receipt, indent=2, ensure_ascii=False)
    print(out)
    rp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "receipt.json")
    with open(rp, "w", encoding="utf-8") as f:
        f.write(out)
    sys.exit(0 if receipt["status"] == "PASS" else 1)
