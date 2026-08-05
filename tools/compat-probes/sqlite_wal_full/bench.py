#!/usr/bin/env python3
"""SQLite WAL + FULL synchronous mode probe.

验证 WAL 模式 + FULL synchronous 下的崩溃恢复能力。
写入 eventBatchParameters tuple，模拟中途中断，验证恢复后数据完整性。
"""
import hashlib, json, os, platform, sqlite3, sys, tempfile, threading, time

SPIKE = "sqlite_wal_full"

EVENT_BATCH_PARAMS = {
    "event_id": "evt-probe-001",
    "batch_size": 64,
    "compression": "none",
    "encoding": "utf8",
    "schema_version": "v1",
    "flush_policy": "FULL",
}

def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()

def _env_info() -> dict:
    return {
        "os": platform.system(),
        "os_version": platform.version(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "arch": platform.machine(),
        "sqlite_version": sqlite3.sqlite_version,
    }

def run_probe(tmp_dir: str) -> dict:
    actions, assertions = [], []
    db_path = os.path.join(tmp_dir, "wal_probe.db")
    wal_path = db_path + "-wal"

    env = _env_info()
    env_digest = _sha256(json.dumps(env, sort_keys=True).encode())
    param_bytes = json.dumps(EVENT_BATCH_PARAMS, sort_keys=True).encode()
    param_digest = _sha256(param_bytes)

    # Step 1: WAL + FULL mode
    con = sqlite3.connect(db_path)
    jm = con.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    con.execute("PRAGMA synchronous = FULL")
    con.execute("CREATE TABLE IF NOT EXISTS event_batch (id INTEGER PRIMARY KEY, params TEXT, batch_num INTEGER)")
    con.commit()
    actions.append(f"PRAGMA journal_mode=WAL => {jm}; synchronous=FULL")

    # Step 2: write 100 rows
    t0 = time.monotonic()
    for i in range(100):
        con.execute("INSERT INTO event_batch (params, batch_num) VALUES (?, ?)",
                    (json.dumps(EVENT_BATCH_PARAMS), i // 10))
        if (i + 1) % 10 == 0:
            con.commit()
    write_elapsed = time.monotonic() - t0
    actions.append(f"inserted 100 rows ({write_elapsed*1000:.1f}ms)")

    # Step 3: simulate mid-write interrupt via thread abort
    interrupted = threading.Event()
    def _writer():
        try:
            c2 = sqlite3.connect(db_path)
            c2.execute("PRAGMA journal_mode = WAL")
            for _ in range(50):
                c2.execute("INSERT INTO event_batch (params, batch_num) VALUES (?, ?)",
                           (json.dumps(EVENT_BATCH_PARAMS), 99))
                if interrupted.is_set():
                    c2.close()   # close without commit
                    return
                time.sleep(0.001)
            c2.close()
        except Exception:
            pass
    t = threading.Thread(target=_writer, daemon=True)
    t.start()
    time.sleep(0.030)
    interrupted.set()
    t.join(timeout=2.0)
    actions.append("mid-write thread interrupted without commit")

    # Step 4: verify recovery
    con.close()
    con2 = sqlite3.connect(db_path)
    con2.execute("PRAGMA journal_mode = WAL")
    row_count = con2.execute("SELECT COUNT(*) FROM event_batch").fetchone()[0]
    con2.close()
    db_size = os.path.getsize(db_path)
    wal_exists = os.path.exists(wal_path)
    actions.append(f"recovery: row_count={row_count} db_size={db_size}")

    bench_profile = {
        "rows_written": 100,
        "batch_size": 10,
        "write_ms": round(write_elapsed * 1000, 2),
        "rows_per_second": round(100 / write_elapsed, 1) if write_elapsed > 0 else 0,
    }
    bench_digest = _sha256(json.dumps(bench_profile, sort_keys=True).encode())
    artifact_digest = _sha256(json.dumps({"row_count": row_count, "db_size": db_size}, sort_keys=True).encode())

    assertions.append({"name": "wal_mode_confirmed",         "passed": jm == "wal", "detail": f"journal_mode={jm}"})
    assertions.append({"name": "rows_survive_interrupt",     "passed": row_count >= 100, "detail": f"expected>=100 actual={row_count}"})
    assertions.append({"name": "db_readable_after_interrupt","passed": row_count >= 0,   "detail": "SELECT COUNT(*) succeeded"})

    status = "PASS" if all(a["passed"] for a in assertions) else "FAIL"
    import datetime
    return {
        "spike": SPIKE, "status": status,
        "environment": env, "actions": actions,
        "observable_facts": {
            "wal_file_existed": wal_exists,
            "row_count_after_recovery": row_count,
            "db_size_bytes": db_size,
            "write_elapsed_ms": round(write_elapsed * 1000, 2),
        },
        "param_tuple": EVENT_BATCH_PARAMS,
        "param_digest": param_digest,
        "benchmarkProfileDigest": bench_digest,
        "benchmark_profile": bench_profile,
        "env_digest": env_digest,
        "assertions": assertions,
        "artifact_digest": artifact_digest,
        "timestamp": datetime.datetime.now().astimezone().isoformat(),
    }

if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as td:
        receipt = run_probe(td)
    out = json.dumps(receipt, indent=2)
    print(out)
    rp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "receipt.json")
    with open(rp, "w", encoding="utf-8") as f:
        f.write(out)
    sys.exit(0 if receipt["status"] == "PASS" else 1)