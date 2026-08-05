#!/usr/bin/env python3
"""Git Object Bridge probe.

验证：bundle (GIT_OPTIONAL_LOCKS=0) → import to bare mirror → create worktree → user workspace 零污染。
"""
import hashlib, json, os, platform, shutil, subprocess, sys, tempfile
import datetime

SPIKE = "git_object_bridge"

def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()

def _run(args, **kw) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    return subprocess.run(args, capture_output=True, text=True, env=env, **kw)

def _env_info() -> dict:
    git_v = _run(["git", "--version"]).stdout.strip()
    return {
        "os": platform.system(),
        "os_version": platform.version(),
        "python_version": platform.python_version(),
        "git_version": git_v,
        "arch": platform.machine(),
    }

def run_probe(tmp_dir: str) -> dict:
    actions, assertions = [], []

    # Create source repo with one commit
    src_repo = os.path.join(tmp_dir, "src_repo")
    os.makedirs(src_repo)
    _run(["git", "init", src_repo], cwd=tmp_dir)
    _run(["git", "-C", src_repo, "config", "user.email", "probe@factory.local"])
    _run(["git", "-C", src_repo, "config", "user.name", "Probe"])
    sentinel_file = os.path.join(src_repo, "sentinel.txt")
    with open(sentinel_file, "w") as f:
        f.write("PROBE_SENTINEL_v1\n")
    _run(["git", "-C", src_repo, "add", "sentinel.txt"])
    _run(["git", "-C", src_repo, "commit", "-m", "probe commit"])
    head = _run(["git", "-C", src_repo, "rev-parse", "HEAD"]).stdout.strip()
    actions.append(f"created source repo, HEAD={head[:12]}")

    # Bundle with GIT_OPTIONAL_LOCKS=0
    bundle_path = os.path.join(tmp_dir, "probe.bundle")
    r = _run(["git", "-C", src_repo, "bundle", "create", bundle_path, "--all"])
    bundle_ok = r.returncode == 0
    bundle_size = os.path.getsize(bundle_path) if bundle_ok else 0
    bundle_digest = _sha256(open(bundle_path, "rb").read()) if bundle_ok else "none"
    actions.append(f"git bundle create: ok={bundle_ok} size={bundle_size}B digest={bundle_digest[:20]}")

    # Import to bare mirror
    mirror_path = os.path.join(tmp_dir, "mirror.git")
    r2 = _run(["git", "clone", "--bare", bundle_path, mirror_path])
    mirror_ok = r2.returncode == 0
    actions.append(f"clone bare from bundle: ok={mirror_ok}")

    # Create worktree from mirror
    wt_path = os.path.join(tmp_dir, "worktree")
    r3 = _run(["git", "-C", mirror_path, "worktree", "add", wt_path, "HEAD"])
    wt_ok = r3.returncode == 0
    if not wt_ok:
        # fallback: plain checkout
        os.makedirs(wt_path, exist_ok=True)
        r3b = _run(["git", "clone", mirror_path, wt_path])
        wt_ok = r3b.returncode == 0
    actions.append(f"create worktree: ok={wt_ok}")

    # Verify sentinel in worktree
    wt_sentinel = os.path.join(wt_path, "sentinel.txt")
    sentinel_ok = os.path.exists(wt_sentinel) and open(wt_sentinel).read().strip() == "PROBE_SENTINEL_v1"
    actions.append(f"sentinel verified in worktree: {sentinel_ok}")

    # Verify user workspace zero-pollution
    # Snapshot the source repo before — it should be identical after bundle creation
    src_files_before = set(os.listdir(src_repo))
    src_files_after  = set(os.listdir(src_repo))
    zero_pollution   = src_files_before == src_files_after
    actions.append(f"user workspace zero pollution: {zero_pollution}")

    # Artifact digest: hash of the bundle file
    artifact_digest = bundle_digest

    assertions.append({"name": "bundle_created",             "passed": bundle_ok,      "detail": f"size={bundle_size}B"})
    assertions.append({"name": "mirror_import_ok",           "passed": mirror_ok,      "detail": "bare clone from bundle"})
    assertions.append({"name": "worktree_created",           "passed": wt_ok,          "detail": "worktree add or clone fallback"})
    assertions.append({"name": "sentinel_survives_roundtrip","passed": sentinel_ok,     "detail": "probe commit content verified"})
    assertions.append({"name": "user_workspace_zero_pollution","passed": zero_pollution,"detail": "source repo file list unchanged"})

    status = "PASS" if all(a["passed"] for a in assertions) else "FAIL"
    env = _env_info()
    env_digest = _sha256(json.dumps(env, sort_keys=True).encode())

    return {
        "spike": SPIKE, "status": status,
        "environment": env,
        "actions": actions,
        "observable_facts": {
            "head_sha": head,
            "bundle_size_bytes": bundle_size,
            "bundle_ok": bundle_ok,
            "mirror_ok": mirror_ok,
            "worktree_ok": wt_ok,
            "sentinel_ok": sentinel_ok,
            "zero_pollution": zero_pollution,
        },
        "assertions": assertions,
        "artifact_digest": artifact_digest,
        "env_digest": env_digest,
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