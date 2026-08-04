"""
tests/contract/test_compatibility_manifest.py

Task 6 合同层测试：Compatibility Manifest 结构与 benchmark profile 合规性验证。

验证内容：
  - benchmark-profile.v1.json 存在且字段合法
  - 各概率分布之和为 1.0
  - compatibility-manifest schema 强制 synchronous=FULL
  - emit_manifest 模块正确计算 parameterTupleDigest
  - 缺少 SQLite spike receipt 时 fail closed
"""
from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_PROFILE_PATH = REPO_ROOT / "contracts" / "benchmarks" / "benchmark-profile.v1.json"
MANIFEST_SCHEMA_PATH = REPO_ROOT / "contracts" / "schemas" / "compatibility-manifest.v1.schema.json"


# ─────────────────────── session fixtures ───────────────────────


@pytest.fixture(scope="session")
def benchmark_profile() -> dict:  # type: ignore[type-arg]
    """加载 benchmark-profile.v1.json。"""
    assert BENCHMARK_PROFILE_PATH.exists(), f"benchmark-profile.v1.json 不存在: {BENCHMARK_PROFILE_PATH}"
    with BENCHMARK_PROFILE_PATH.open(encoding="utf-8") as f:
        return json.load(f)  # type: ignore[no-any-return]


@pytest.fixture(scope="session")
def manifest_schema() -> dict:  # type: ignore[type-arg]
    """加载 compatibility-manifest.v1.schema.json。"""
    assert MANIFEST_SCHEMA_PATH.exists(), f"compatibility-manifest.v1.schema.json 不存在: {MANIFEST_SCHEMA_PATH}"
    with MANIFEST_SCHEMA_PATH.open(encoding="utf-8") as f:
        return json.load(f)  # type: ignore[no-any-return]


# ─────────────────────── benchmark profile 存在性 ───────────────────────


def test_benchmark_profile_v1_exists() -> None:
    """benchmark-profile.v1.json 必须存在于 contracts/benchmarks/。"""
    assert BENCHMARK_PROFILE_PATH.exists(), f"benchmark-profile.v1.json 不存在: {BENCHMARK_PROFILE_PATH}"


def test_benchmark_profile_valid_json(benchmark_profile: dict) -> None:  # type: ignore[type-arg]
    """benchmark-profile.v1.json 必须是合法 JSON 对象。"""
    assert isinstance(benchmark_profile, dict), "benchmark profile 不是 JSON 对象"


# ─────────────────────── benchmark profile 字段 ─────────────────────────


def test_benchmark_profile_has_required_fields(benchmark_profile: dict) -> None:  # type: ignore[type-arg]
    """benchmark profile 必须包含所有必需字段。"""
    required = {
        "generatorId", "prngSeed", "taskDistribution", "eventTypeDistribution",
        "payloadSizeDistribution", "sustainedTargetEventsPerSecond",
        "sustainedToleranceFraction", "burstEventCount", "burstWindowMs", "secretFraction",
    }
    missing = required - set(benchmark_profile.keys())
    assert not missing, f"benchmark profile 缺少字段: {sorted(missing)}"


def test_benchmark_profile_generator_id(benchmark_profile: dict) -> None:  # type: ignore[type-arg]
    """generatorId 必须为 'factory-event-loadgen-v1'。"""
    assert benchmark_profile["generatorId"] == "factory-event-loadgen-v1", (
        f"generatorId 不匹配: {benchmark_profile['generatorId']}"
    )


def test_benchmark_profile_prng_seed(benchmark_profile: dict) -> None:  # type: ignore[type-arg]
    """prngSeed 必须为 20260803。"""
    assert benchmark_profile["prngSeed"] == 20260803, (
        f"prngSeed 不匹配: {benchmark_profile['prngSeed']}"
    )


def test_task_distribution_sums_to_one(benchmark_profile: dict) -> None:  # type: ignore[type-arg]
    """taskDistribution 概率之和必须为 1.0（误差 1e-9）。"""
    dist = benchmark_profile["taskDistribution"]
    total = sum(dist.values())
    assert abs(total - 1.0) < 1e-9, f"taskDistribution 之和为 {total}，应为 1.0。分布: {dist}"


def test_task_distribution_has_three_tasks(benchmark_profile: dict) -> None:  # type: ignore[type-arg]
    """taskDistribution 必须包含 t1/t2/t3 三个任务。"""
    dist = benchmark_profile["taskDistribution"]
    assert set(dist.keys()) == {"t1", "t2", "t3"}, f"taskDistribution 键集合不匹配: {set(dist.keys())}"


def test_event_type_distribution_sums_to_one(benchmark_profile: dict) -> None:  # type: ignore[type-arg]
    """eventTypeDistribution 概率之和必须为 1.0（误差 1e-9）。"""
    dist = benchmark_profile["eventTypeDistribution"]
    total = sum(dist.values())
    assert abs(total - 1.0) < 1e-9, f"eventTypeDistribution 之和为 {total}，应为 1.0。分布: {dist}"


def test_event_type_distribution_has_four_types(benchmark_profile: dict) -> None:  # type: ignore[type-arg]
    """eventTypeDistribution 必须包含 stream/provider_semantic/tool/state 四种类型。"""
    dist = benchmark_profile["eventTypeDistribution"]
    expected = {"stream", "provider_semantic", "tool", "state"}
    assert set(dist.keys()) == expected, f"eventTypeDistribution 键集合不匹配: {set(dist.keys())}"


def test_payload_distribution_fractions_sum_to_one(benchmark_profile: dict) -> None:  # type: ignore[type-arg]
    """payloadSizeDistribution fraction 之和必须为 1.0（误差 1e-9）。"""
    total = sum(e["fraction"] for e in benchmark_profile["payloadSizeDistribution"])
    assert abs(total - 1.0) < 1e-9, f"payloadSizeDistribution fraction 之和为 {total}，应为 1.0"


def test_payload_distribution_has_three_buckets(benchmark_profile: dict) -> None:  # type: ignore[type-arg]
    """payloadSizeDistribution 必须有三个 size bucket（512/704/1024 bytes）。"""
    dist = benchmark_profile["payloadSizeDistribution"]
    assert len(dist) == 3, f"payloadSizeDistribution 应有 3 个条目，实际 {len(dist)}"
    sizes = {e["bytes"] for e in dist}
    assert sizes == {512, 704, 1024}, f"bytes 集合不匹配: {sizes}"


def test_benchmark_profile_burst_config(benchmark_profile: dict) -> None:  # type: ignore[type-arg]
    """burstEventCount=12000，burstWindowMs=100。"""
    assert benchmark_profile["burstEventCount"] == 12000
    assert benchmark_profile["burstWindowMs"] == 100


def test_benchmark_profile_sustained_config(benchmark_profile: dict) -> None:  # type: ignore[type-arg]
    """sustainedTargetEventsPerSecond=2000，sustainedToleranceFraction=0.005。"""
    assert benchmark_profile["sustainedTargetEventsPerSecond"] == 2000
    assert abs(benchmark_profile["sustainedToleranceFraction"] - 0.005) < 1e-9


# ─────────────────────── manifest schema 测试 ────────────────────────────────


def test_manifest_schema_requires_synchronous_full(manifest_schema: dict) -> None:  # type: ignore[type-arg]
    """compatibility-manifest schema 的 synchronous 字段必须 const='FULL'。"""
    sync_prop = manifest_schema["properties"]["eventBatchParameters"]["properties"]["synchronous"]
    assert sync_prop.get("const") == "FULL", f"synchronous 必须 const='FULL'，实际: {sync_prop}"


def test_manifest_schema_event_batch_parameters_required(manifest_schema: dict) -> None:  # type: ignore[type-arg]
    """eventBatchParameters 必须声明所有必需子字段。"""
    ebp_schema = manifest_schema["properties"]["eventBatchParameters"]
    required = set(ebp_schema.get("required", []))
    expected = {
        "maxBatchEvents", "maxBatchBytes", "maxBatchAgeMs",
        "synchronous", "parameterTupleDigest", "sqliteSpikeReceiptDigest",
    }
    missing = expected - required
    assert not missing, f"eventBatchParameters 缺少 required 字段: {sorted(missing)}"


def test_manifest_schema_additional_properties_false(manifest_schema: dict) -> None:  # type: ignore[type-arg]
    """compatibility-manifest schema 顶层必须 additionalProperties=false。"""
    assert manifest_schema.get("additionalProperties") is False, (
        "compatibility-manifest.v1 schema 缺少 additionalProperties:false"
    )


def test_manifest_schema_event_batch_additional_properties_false(manifest_schema: dict) -> None:  # type: ignore[type-arg]
    """eventBatchParameters 对象也必须 additionalProperties=false。"""
    ebp = manifest_schema["properties"]["eventBatchParameters"]
    assert ebp.get("additionalProperties") is False, "eventBatchParameters 缺少 additionalProperties:false"


# ─────────────────────── emit_manifest 模块测试 ──────────────────────────────
# emit_manifest_module fixture 由 tests/conftest.py 提供（使用 importlib 绕过目录连字符）


def test_compute_parameter_tuple_digest_is_deterministic(emit_manifest_module: ModuleType) -> None:
    """parameterTupleDigest 对相同输入必须确定性输出。"""
    fn = emit_manifest_module.compute_parameter_tuple_digest
    d1 = fn(1000, 1_048_576, 100, "FULL")
    d2 = fn(1000, 1_048_576, 100, "FULL")
    assert d1 == d2, "parameterTupleDigest 对相同输入应确定性输出"


def test_compute_parameter_tuple_digest_is_sha256_prefixed(emit_manifest_module: ModuleType) -> None:
    """parameterTupleDigest 必须以 'sha256:' 开头。"""
    digest = emit_manifest_module.compute_parameter_tuple_digest(1000, 1_048_576, 100, "FULL")
    assert digest.startswith("sha256:"), f"parameterTupleDigest 格式不对: {digest}"


def test_compute_parameter_tuple_digest_rejects_non_full(emit_manifest_module: ModuleType) -> None:
    """synchronous != 'FULL' 时必须拒绝（SYNCHRONOUS_NOT_FULL 错误）。"""
    with pytest.raises(ValueError, match="SYNCHRONOUS_NOT_FULL"):
        emit_manifest_module.compute_parameter_tuple_digest(1000, 1_048_576, 100, "WAL")


def test_emit_manifest_rejects_missing_spike_receipt(
    emit_manifest_module: ModuleType,
    tmp_path: Path,
) -> None:
    """spike receipt 不存在时 emit_manifest 必须 fail closed（FileNotFoundError）。"""
    with pytest.raises(FileNotFoundError, match="SPIKE_RECEIPT_MISSING"):
        emit_manifest_module.emit_manifest(spike_receipt_path=tmp_path / "nonexistent.json")


def test_emit_manifest_rejects_failed_spike_receipt(
    emit_manifest_module: ModuleType,
    tmp_path: Path,
) -> None:
    """spike receipt status != PASS 时 emit_manifest 必须 fail closed（ValueError）。"""
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps({"status": "FAIL", "env_digest": "sha256:abc", "param_digest": "sha256:def"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="SPIKE_NOT_PASS"):
        emit_manifest_module.emit_manifest(spike_receipt_path=bad)


def test_emit_manifest_produces_valid_structure(
    emit_manifest_module: ModuleType,
    tmp_path: Path,
) -> None:
    """有效 spike receipt 应产生包含全部必需顶级字段的 manifest。"""
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps({
            "status": "PASS",
            "env_digest": "sha256:" + "a" * 64,
            "param_digest": "sha256:" + "b" * 64,
            "assertions": [{"name": "wal_ok", "passed": True}],
        }),
        encoding="utf-8",
    )
    manifest = emit_manifest_module.emit_manifest(spike_receipt_path=receipt)

    for key in ("manifestId", "manifestSchemaDigest", "eventBatchParameters",
                "benchmarkProfileDigest", "sqliteSpikeReceiptDigest",
                "stageCapeabilityMapDigest", "nodeCapabilityMapDigest",
                "environmentDigest", "createdAt"):
        assert key in manifest, f"manifest 缺少顶级字段: {key}"

    assert manifest["eventBatchParameters"]["synchronous"] == "FULL"
    assert manifest["eventBatchParameters"]["parameterTupleDigest"].startswith("sha256:")


def test_emit_manifest_parameter_tuple_digest_consistent(
    emit_manifest_module: ModuleType,
    tmp_path: Path,
) -> None:
    """emit_manifest 计算的 parameterTupleDigest 必须与独立调用 compute_parameter_tuple_digest 一致。"""
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps({"status": "PASS", "env_digest": "sha256:env", "param_digest": "sha256:param"}),
        encoding="utf-8",
    )
    manifest = emit_manifest_module.emit_manifest(spike_receipt_path=receipt)

    expected = emit_manifest_module.compute_parameter_tuple_digest(
        emit_manifest_module.DEFAULT_MAX_BATCH_EVENTS,
        emit_manifest_module.DEFAULT_MAX_BATCH_BYTES,
        emit_manifest_module.DEFAULT_MAX_BATCH_AGE_MS,
        emit_manifest_module.SYNCHRONOUS_MODE,
    )
    assert manifest["eventBatchParameters"]["parameterTupleDigest"] == expected
