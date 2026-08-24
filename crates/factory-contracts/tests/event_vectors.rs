//! crates/factory-contracts/tests/event_vectors.rs
//!
//! Task 4 跨语言 golden vector 测试（Rust 端）。
//! 从 repo 根的 contracts/golden/event-hash.v2.json 与 prepared-batch.v2.json
//! 读取与 Python / TypeScript 完全相同的冻结向量，断言 Rust 物化器产出
//! 字节级一致的 eventId、payloadDigest、DurableEventV2 链与 canonical bytes。

use factory_contracts::canonical::canonicalize_value;
use factory_contracts::event::{
    event_id, materialize_batch, payload_digest, redaction_manifest_digest, EventHashError,
};
use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::fs;
use std::path::PathBuf;
use unicode_normalization::UnicodeNormalization;

/// 定位 repo 根的 contracts/golden 目录。
/// CARGO_MANIFEST_DIR = <repo>/crates/factory-contracts，上溯两级到 repo 根。
fn golden_dir() -> PathBuf {
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    manifest
        .parent()
        .and_then(|p| p.parent())
        .expect("无法定位 repo 根")
        .join("contracts")
        .join("golden")
}

/// 读取并解析一个 golden JSON 文件。
fn load_golden(name: &str) -> Value {
    let path = golden_dir().join(name);
    let text = fs::read_to_string(&path).unwrap_or_else(|e| panic!("读取 {path:?} 失败：{e}"));
    serde_json::from_str(&text).unwrap_or_else(|e| panic!("解析 {path:?} 失败：{e}"))
}
const EXPECTED_REJECT_MUTATION_NAMES: &[&str] = &[
    "input-root-unknown",
    "event-missing-run-id",
    "event-writer-field",
    "event-coarse-type",
    "event-nested-process-unknown",
    "event-digest-uppercase",
    "event-frame-digest-uppercase",
    "process-runtime-wsl-process",
    "process-runtime-container",
    "process-wsl-docker-missing-container",
    "process-local-missing-job",
    "process-local-unexpected-wsl",
    "source-span-equal-range",
    "source-span-reversed-range",
    "source-span-none-with-offset",
    "source-span-bool",
    "source-span-unsafe-integer",
    "sanitized-span-equal-range",
    "sanitized-span-reversed-range",
    "sanitized-span-bool",
    "redaction-range-equal",
    "redaction-range-reversed",
    "redaction-range-bool",
    "redaction-replacement-nonfrozen",
    "redaction-replacement-uppercase-tag",
    "redaction-nested-unknown",
    "wall-time-invalid",
    "ingested-at-invalid",
    "process-start-time-invalid",
    "head-task-seq-bool",
    "head-task-seq-unsafe-integer",
    "source-seq-bool",
    "source-seq-unsafe-integer",
    "batch-ordinal-bool",
    "batch-ordinal-unsafe-integer",
    "run-seq-bool",
    "run-seq-unsafe-integer",
    "monotonic-time-bool",
    "monotonic-time-unsafe-integer",
    "pid-bool",
    "pid-unsafe-integer",
    "checked-task-seq-overflow",
    "anchor-event-digest-drift",
    "anchor-task-seq-drift",
    "empty-events",
    "duplicate-ingest-id",
    "nfc-equivalent-ingest-id",
    "multi-task-slice",
    "model-summary-fail-closed",
    "state-changed-adapter-lane",
    "event-authoritative-durability-class",
    "missing-source-seq",
    "missing-source-span",
    "missing-process-identity",
    "stream-event-null-stream-id",
    "anchor-unknown-field",
    "batch-ordinal-negative",
    "batch-ordinal-nonmonotonic",
    "missing-coordinator-field",
    "half-null-input-head",
    "digest-pattern-short",
    "digest-pattern-wrong-prefix",
    "sanitized-span-unsafe-integer",
    "redaction-range-unsafe-integer",
];
const EXPECTED_INTEGER_CASE_NAMES: &[&str] = &[
    "integer-one",
    "decimal-integral-one",
    "exponent-integral-one",
    "negative-zero",
    "fractional-one-point-five",
    "nan",
    "positive-infinity",
    "negative-infinity",
    "unsafe-positive-integer",
    "boolean-true",
];
const EXPECTED_SOURCE_SPAN_CASE_NAMES: &[&str] = &[
    "provider-bytes-byte-range",
    "master-provider-bytes-frame-null",
    "provider-chars-byte-range",
    "provider-chars-field-null",
    "provider-bytes-none-null",
    "none-null",
    "provider-bytes-byte-start-null",
    "provider-bytes-byte-equal-range",
    "provider-bytes-frame-start-present",
    "provider-chars-field-end-present",
    "provider-bytes-none-end-present",
    "none-frame-precision",
    "none-start-present",
    "legacy-coordinate-byte",
];

/// 解析共享 RFC 6901 pointer，并解码 ~0/~1。
fn pointer_parts(pointer: &str) -> Vec<String> {
    assert!(pointer.starts_with('/'), "mutation pointer 必须为绝对路径");
    pointer[1..]
        .split('/')
        .map(|part| part.replace("~1", "/").replace("~0", "~"))
        .collect()
}

/// 递归定位 pointer 的父容器，供单 mutation 原地执行。
fn parent_at_mut<'a>(value: &'a mut Value, parts: &[String]) -> &'a mut Value {
    if parts.is_empty() {
        return value;
    }
    match value {
        Value::Array(items) => {
            let index = parts[0].parse::<usize>().expect("array pointer 必须是索引");
            parent_at_mut(&mut items[index], &parts[1..])
        }
        Value::Object(object) => {
            let next = object.get_mut(&parts[0]).expect("pointer 字段必须存在");
            parent_at_mut(next, &parts[1..])
        }
        _ => panic!("pointer 中间节点必须为 object/array"),
    }
}

/// 执行一个 add/remove/replace，并返回可精确恢复的逆操作。
fn apply_pointer_mutation(document: &mut Value, mutation: &Value) -> Value {
    let operation = mutation["operation"]
        .as_str()
        .expect("operation 必须为字符串");
    let path = mutation["path"].as_str().expect("path 必须为字符串");
    let parts = pointer_parts(path);
    let (parent_parts, leaf_parts) = parts.split_at(parts.len() - 1);
    let leaf = &leaf_parts[0];
    let parent = parent_at_mut(document, parent_parts);

    if operation == "add" {
        let value = mutation["value"].clone();
        return match parent {
            Value::Array(items) => {
                let index = if leaf == "-" {
                    items.len()
                } else {
                    leaf.parse::<usize>().expect("array pointer 必须是索引")
                };
                items.insert(index, value);
                let mut inverse_parts = parent_parts.to_vec();
                inverse_parts.push(index.to_string());
                json!({"operation": "remove", "path": format!("/{}", inverse_parts.join("/"))})
            }
            Value::Object(object) => {
                assert!(!object.contains_key(leaf), "add mutation 不得覆盖字段");
                object.insert(leaf.clone(), value);
                json!({"operation": "remove", "path": path})
            }
            _ => panic!("add parent 必须为 object/array"),
        };
    }

    let previous = match parent {
        Value::Array(items) => {
            let index = leaf.parse::<usize>().expect("array pointer 必须是索引");
            if operation == "remove" {
                items.remove(index)
            } else {
                assert_eq!(operation, "replace");
                std::mem::replace(&mut items[index], mutation["value"].clone())
            }
        }
        Value::Object(object) => {
            if operation == "remove" {
                object.remove(leaf).expect("remove 字段必须存在")
            } else {
                assert_eq!(operation, "replace");
                let slot = object.get_mut(leaf).expect("replace 字段必须存在");
                std::mem::replace(slot, mutation["value"].clone())
            }
        }
        _ => panic!("mutation parent 必须为 object/array"),
    };
    json!({
        "operation": if operation == "remove" { "add" } else { "replace" },
        "path": path,
        "value": previous,
    })
}

/// 使用共享真实 input/anchor envelope 调用 Rust 物化器。
fn materialize_fixture(fixture: &Value) -> Result<Vec<Value>, EventHashError> {
    materialize_batch(&fixture["input"], &fixture["anchor"])
}

/// 读取首事件字段或后继 previousEventDigest，供摘要 mutation 对比。
fn observable(events: &[Value], label: &str) -> Value {
    if label == "successor.previousEventDigest" {
        events[1]["previousEventDigest"].clone()
    } else {
        events[0][label].clone()
    }
}

#[test]
fn event_id_vectors() {
    // eventId：幂等身份，仅由 ingestEventId 决定，输出必须等于冻结值。
    let golden = load_golden("event-hash.v2.json");
    let cases = golden["eventId"].as_array().expect("eventId 必须为数组");
    assert!(!cases.is_empty());
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let ingest = case["ingestEventId"].as_str().unwrap();
        let expected = case["expected"].as_str().unwrap();
        let actual = event_id(ingest).unwrap_or_else(|e| panic!("eventId {name} 失败：{e}"));
        assert_eq!(actual, expected, "eventId {name} 不一致");
    }
}

#[test]
fn event_id_idempotent() {
    // 不变量：同一 ingestEventId 多次计算必须得到同一 eventId（崩溃重试幂等）。
    let golden = load_golden("event-hash.v2.json");
    let inv = &golden["invariants"]["idempotentEventId"];
    let a_name = inv["a"].as_str().unwrap();
    let cases = golden["eventId"].as_array().unwrap();
    let case = cases
        .iter()
        .find(|c| c["name"].as_str() == Some(a_name))
        .unwrap();
    let ingest = case["ingestEventId"].as_str().unwrap();
    assert_eq!(event_id(ingest).unwrap(), event_id(ingest).unwrap());
}

#[test]
fn payload_digest_vectors() {
    // payloadDigest：脱敏 payload 的 JCS 摘要，输出必须等于冻结值。
    let golden = load_golden("event-hash.v2.json");
    let cases = golden["payloadDigest"]
        .as_array()
        .expect("payloadDigest 必须为数组");
    assert!(!cases.is_empty());
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let payload = &case["payload"];
        let expected = case["expected"].as_str().unwrap();
        let actual =
            payload_digest(payload).unwrap_or_else(|e| panic!("payloadDigest {name} 失败：{e}"));
        assert_eq!(actual, expected, "payloadDigest {name} 不一致");
    }
}

#[test]
fn mathematical_integer_semantics() {
    // 合法 JSON 数字按 Draft7 数学整数判定；非 JSON 的 NaN/Infinity 在解析边界即拒绝。
    let golden = load_golden("event-hash.v2.json");
    let cases = golden["integerSemantics"].as_array().unwrap();
    let names = cases
        .iter()
        .map(|case| case["name"].as_str().unwrap())
        .collect::<BTreeSet<_>>();
    assert_eq!(names.len(), cases.len());
    assert_eq!(
        names,
        EXPECTED_INTEGER_CASE_NAMES
            .iter()
            .copied()
            .collect::<BTreeSet<_>>()
    );

    for case in cases {
        let parsed = serde_json::from_str::<Value>(case["json"].as_str().unwrap());
        if parsed.is_err() {
            assert!(!case["accepted"].as_bool().unwrap());
            continue;
        }
        let mut fixture = golden["rejectBase"].clone();
        fixture["input"]["events"][0]["sourceSeq"] = parsed.unwrap();
        if case["accepted"].as_bool().unwrap() {
            let events = materialize_fixture(&fixture).unwrap();
            assert_eq!(
                events[0]["sourceSeq"].as_u64(),
                case["expected"].as_u64(),
                "integer case {} 未规范化为整数输出",
                case["name"]
            );
        } else {
            assert!(
                materialize_fixture(&fixture).is_err(),
                "integer case {} 应拒绝",
                case["name"]
            );
        }
    }
}

#[test]
fn conservative_v1_source_span_semantics() {
    // coordinate 与 mappingPrecision 是独立维度；仅 transport+byte 携带递增范围。
    let golden = load_golden("event-hash.v2.json");
    let cases = golden["sourceSpanSemantics"].as_array().unwrap();
    let names = cases
        .iter()
        .map(|case| case["name"].as_str().unwrap())
        .collect::<BTreeSet<_>>();
    assert_eq!(names.len(), cases.len());
    assert_eq!(
        names,
        EXPECTED_SOURCE_SPAN_CASE_NAMES
            .iter()
            .copied()
            .collect::<BTreeSet<_>>()
    );
    for case in cases {
        if case["accepted"].as_bool().unwrap() {
            let mut fixture = golden["rejectBase"].clone();
            fixture["input"]["events"][0]["sourceTransportSpan"] = case["span"].clone();
            let events = materialize_fixture(&fixture).unwrap();
            assert_eq!(events[0]["sourceTransportSpan"], case["span"]);
        } else {
            let base_case_name = case["baseCase"].as_str().unwrap();
            let base_span = cases
                .iter()
                .find(|candidate| candidate["name"].as_str() == Some(base_case_name))
                .expect("负例必须引用共享合法 source span")["span"]
                .clone();
            let mut base_fixture = golden["rejectBase"].clone();
            base_fixture["input"]["events"][0]["sourceTransportSpan"] = base_span.clone();
            assert_eq!(
                materialize_fixture(&base_fixture).unwrap()[0]["sourceTransportSpan"],
                base_span
            );

            let mut mutated_span = base_span.clone();
            let inverse = apply_pointer_mutation(&mut mutated_span, &case["mutation"]);
            let mut fixture = golden["rejectBase"].clone();
            fixture["input"]["events"][0]["sourceTransportSpan"] = mutated_span.clone();
            assert!(
                materialize_fixture(&fixture).is_err(),
                "span case {} 应拒绝",
                case["name"]
            );
            apply_pointer_mutation(&mut mutated_span, &inverse);
            assert_eq!(mutated_span, base_span);
        }
    }
}

#[test]
fn materialize_vectors() {
    // materialize_batch：物化整批事件，逐字段必须等于冻结 DurableEventV2 列表。
    let golden = load_golden("event-hash.v2.json");
    let cases = golden["materialize"]
        .as_array()
        .expect("materialize 必须为数组");
    assert!(!cases.is_empty());
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let input = &case["input"];
        let expected = case["expected"].clone();
        let result = materialize_batch(input, &case["anchor"])
            .unwrap_or_else(|e| panic!("materialize {name} 失败：{e}"));
        assert_eq!(Value::Array(result), expected, "materialize {name} 不一致");
    }
}

#[test]
fn interleaved_tasks_independent() {
    // 不变量：多 Task 交错，taskA 与 taskB 各自从 genesis 起链，taskSeq 与 eventDigest 互不影响。
    let golden = load_golden("event-hash.v2.json");
    let inv = &golden["invariants"]["interleavedTasksIndependent"];
    let a_name = inv["taskA"].as_str().unwrap();
    let b_name = inv["taskB"].as_str().unwrap();
    let cases = golden["materialize"].as_array().unwrap();
    let find = |n: &str| {
        cases
            .iter()
            .find(|c| c["name"].as_str() == Some(n))
            .unwrap()
    };
    let a = find(a_name);
    let b = find(b_name);
    let ra = materialize_batch(&a["input"], &a["anchor"]).unwrap();
    let rb = materialize_batch(&b["input"], &b["anchor"]).unwrap();
    assert_eq!(ra[0]["taskSeq"].as_i64(), Some(0));
    assert_eq!(rb[0]["taskSeq"].as_i64(), Some(0));
    // previousEventDigest 单链：两条独立链的 genesis eventDigest 必须不同（payload 不同）
    assert_ne!(
        ra[0]["eventDigest"], rb[0]["eventDigest"],
        "两条独立链 eventDigest 不应相同"
    );
    // 两者 previousEventDigest 都是固定全零 predecessor（genesis）
    assert_eq!(
        ra[0]["previousEventDigest"].as_str().unwrap(),
        "sha256:0000000000000000000000000000000000000000000000000000000000000000"
    );
    assert_eq!(
        rb[0]["previousEventDigest"].as_str().unwrap(),
        "sha256:0000000000000000000000000000000000000000000000000000000000000000"
    );
    // 两个单 Task slice 来自同一真实 manifest，ordinal 在批内全局交错。
    assert_eq!(
        a["input"]["preparedBatchId"].as_str(),
        Some("batch-multi-0")
    );
    assert_eq!(
        b["input"]["preparedBatchId"].as_str(),
        Some("batch-multi-0")
    );
    assert_eq!(
        ra.iter()
            .map(|event| event["batchOrdinal"].as_i64())
            .collect::<Vec<_>>(),
        vec![Some(0), Some(2)]
    );
    assert_eq!(
        rb.iter()
            .map(|event| event["batchOrdinal"].as_i64())
            .collect::<Vec<_>>(),
        vec![Some(1)]
    );
}

#[test]
fn single_pointer_mutation_vectors() {
    // base -> 单 mutation -> 稳定错误 -> inverse -> base，三语言必须消费同一 exact-set。
    let golden = load_golden("event-hash.v2.json");
    let base = golden["rejectBase"].clone();
    materialize_fixture(&base).expect("合法 rejectBase 必须通过");

    let mutations = golden["rejectMutations"]
        .as_array()
        .expect("rejectMutations 必须为数组");
    let names = mutations
        .iter()
        .map(|item| item["name"].as_str().expect("mutation name 必须存在"))
        .collect::<Vec<_>>();
    assert_eq!(
        names.iter().copied().collect::<BTreeSet<_>>().len(),
        names.len()
    );
    assert_eq!(
        names.iter().copied().collect::<BTreeSet<_>>(),
        EXPECTED_REJECT_MUTATION_NAMES
            .iter()
            .copied()
            .collect::<BTreeSet<_>>()
    );

    for mutation in golden["validVariants"].as_array().unwrap() {
        let mut candidate = base.clone();
        let inverse = apply_pointer_mutation(&mut candidate, mutation);
        materialize_fixture(&candidate).expect("合法 process variant 必须通过");
        apply_pointer_mutation(&mut candidate, &inverse);
        assert_eq!(candidate, base);
        materialize_fixture(&candidate).expect("inverse 后 base 必须再次通过");
    }

    for mutation in mutations {
        let name = mutation["name"].as_str().unwrap();
        let mut candidate = base.clone();
        let inverse = apply_pointer_mutation(&mut candidate, mutation);
        let error = match materialize_fixture(&candidate) {
            Err(error) => error,
            Ok(_) => panic!("mutation {name} 应拒绝但通过"),
        };
        assert_eq!(EventHashError::ERROR_CODE, "event-hash-error");
        assert!(!error.to_string().contains("must-not-appear"));
        apply_pointer_mutation(&mut candidate, &inverse);
        assert_eq!(candidate, base);
        materialize_fixture(&candidate).expect("inverse 后 base 必须再次通过");
    }
}

#[test]
fn independent_digest_invariants() {
    // 摘要必须从实际字段独立重算；eventDigest 去自身，后继严格绑定前驱。
    let golden = load_golden("event-hash.v2.json");
    let fixture = golden["rejectBase"].clone();
    let baseline = materialize_fixture(&fixture).unwrap();
    let expected_payload_digest = payload_digest(&baseline[0]["payload"]).unwrap();
    let expected_redaction_digest = redaction_manifest_digest(&baseline[0]["redactions"]).unwrap();
    assert_eq!(
        baseline[0]["payloadDigest"].as_str(),
        Some(expected_payload_digest.as_str())
    );
    assert_eq!(
        baseline[0]["redactionManifestDigest"].as_str(),
        Some(expected_redaction_digest.as_str())
    );
    let mut digest_material = baseline[0].as_object().unwrap().clone();
    digest_material.remove("eventDigest");
    let expected_event_digest = payload_digest(&Value::Object(digest_material)).unwrap();
    assert_eq!(
        baseline[0]["eventDigest"].as_str(),
        Some(expected_event_digest.as_str())
    );
    assert_eq!(
        baseline[0]["previousEventDigest"],
        fixture["anchor"]["committedEventDigest"]
    );
    assert_eq!(
        baseline[1]["previousEventDigest"],
        baseline[0]["eventDigest"]
    );

    for mutation in golden["digestInvariants"].as_array().unwrap() {
        let mut candidate = fixture.clone();
        let inverse = apply_pointer_mutation(&mut candidate, mutation);
        let changed = materialize_fixture(&candidate).unwrap();
        for label in mutation["changes"].as_array().unwrap() {
            let label = label.as_str().unwrap();
            assert_ne!(observable(&changed, label), observable(&baseline, label));
        }
        for label in mutation["preserves"].as_array().unwrap() {
            let label = label.as_str().unwrap();
            assert_eq!(observable(&changed, label), observable(&baseline, label));
        }
        assert_eq!(changed[1]["previousEventDigest"], changed[0]["eventDigest"]);
        apply_pointer_mutation(&mut candidate, &inverse);
        assert_eq!(materialize_fixture(&candidate).unwrap(), baseline);
    }
}

#[test]
fn prepared_batch_canonical_vectors() {
    // PreparedBatchV2 canonical 字节：JCS 规范化输出必须等于冻结值。
    let golden = load_golden("prepared-batch.v2.json");
    let cases = golden["cases"].as_array().expect("cases 必须为数组");
    assert!(!cases.is_empty());
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let batch = &case["batch"];
        let expected = case["expectedCanonical"].as_str().unwrap();
        let bytes = canonicalize_value(batch)
            .unwrap_or_else(|e| panic!("prepared-batch {name} 规范化失败：{e}"));
        let actual = String::from_utf8(bytes).unwrap();
        assert_eq!(actual, expected, "prepared-batch {name} 字节不一致");
    }

    let single = cases
        .iter()
        .find(|case| case["name"].as_str() == Some("genesis-single-task-manifest"))
        .unwrap();
    assert_eq!(single["batch"]["firstBatchOrdinal"].as_u64(), Some(0));
    assert_eq!(single["batch"]["lastBatchOrdinal"].as_u64(), Some(1));
    assert_eq!(single["batch"]["eventCount"].as_u64(), Some(2));
    assert_eq!(
        single["batch"]["orderedIngestIds"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
}

#[test]
fn multi_task_slices_union_matches_prepared_batch_manifest() {
    // 多 Task slice 仅是非 wire 输入；按全局 ordinal union 后必须精确还原 manifest 顺序。
    let event_golden = load_golden("event-hash.v2.json");
    let batch_golden = load_golden("prepared-batch.v2.json");
    let manifest = &batch_golden["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|case| case["name"].as_str() == Some("multi-task-interleaved-manifest"))
        .unwrap()["batch"];
    let mut events = event_golden["materialize"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|case| {
            case["input"]["preparedBatchId"].as_str() == manifest["preparedBatchId"].as_str()
        })
        .flat_map(|case| case["input"]["events"].as_array().unwrap().iter().cloned())
        .collect::<Vec<_>>();
    events.sort_by_key(|event| event["batchOrdinal"].as_u64().unwrap());
    assert_eq!(
        events
            .iter()
            .map(|event| event["batchOrdinal"].as_u64().unwrap())
            .collect::<Vec<_>>(),
        vec![0, 1, 2]
    );
    assert_eq!(
        events
            .iter()
            .map(|event| event["ingestEventId"].clone())
            .collect::<Vec<_>>(),
        manifest["orderedIngestIds"].as_array().unwrap().clone()
    );
    let normalized_ids = events
        .iter()
        .map(|event| {
            event["ingestEventId"]
                .as_str()
                .unwrap()
                .nfc()
                .collect::<String>()
        })
        .collect::<Vec<_>>();
    assert_eq!(
        normalized_ids.iter().collect::<BTreeSet<_>>().len(),
        normalized_ids.len()
    );
    assert_eq!(
        normalized_ids,
        manifest["orderedIngestIds"]
            .as_array()
            .unwrap()
            .iter()
            .map(|value| value.as_str().unwrap().nfc().collect::<String>())
            .collect::<Vec<_>>()
    );
    assert_eq!(
        events
            .iter()
            .map(|event| event["taskId"].as_str().unwrap())
            .collect::<BTreeSet<_>>(),
        manifest["perTaskExpectedHeads"]
            .as_array()
            .unwrap()
            .iter()
            .map(|head| head["taskId"].as_str().unwrap())
            .collect::<BTreeSet<_>>()
    );
}

#[test]
fn prepared_batch_semantic_mutations() {
    // Draft7 后仍须执行计数、ordinal 连续、head pair 与 Task head 唯一性。
    let golden = load_golden("prepared-batch.v2.json");
    let base = golden["rejectBase"].clone();
    factory_contracts::event::validate_prepared_batch_manifest(&base)
        .expect("合法 PreparedBatch rejectBase 必须通过");
    let expected_names = [
        "prepared-batch-event-count-mismatch",
        "prepared-batch-ordered-ingest-length-mismatch",
        "prepared-batch-global-ordinal-gap",
        "prepared-batch-half-null-head",
        "prepared-batch-duplicate-task-head",
        "prepared-batch-reversed-task-range",
        "prepared-batch-event-count-bool",
        "prepared-batch-writer-epoch-unsafe-integer",
        "prepared-batch-nfc-equivalent-ingest-id",
    ];
    let mutations = golden["rejectMutations"].as_array().unwrap();
    let names = mutations
        .iter()
        .map(|mutation| mutation["name"].as_str().unwrap())
        .collect::<Vec<_>>();
    assert_eq!(
        names.iter().copied().collect::<BTreeSet<_>>().len(),
        names.len()
    );
    assert_eq!(
        names.iter().copied().collect::<BTreeSet<_>>(),
        expected_names.iter().copied().collect::<BTreeSet<_>>()
    );
    for mutation in mutations {
        let mut candidate = base.clone();
        let inverse = apply_pointer_mutation(&mut candidate, mutation);
        let error = factory_contracts::event::validate_prepared_batch_manifest(&candidate)
            .expect_err("非法 PreparedBatch mutation 必须拒绝");
        assert_eq!(EventHashError::ERROR_CODE, "event-hash-error");
        assert!(!error.to_string().contains("must-not-appear"));
        apply_pointer_mutation(&mut candidate, &inverse);
        assert_eq!(candidate, base);
        factory_contracts::event::validate_prepared_batch_manifest(&candidate)
            .expect("inverse 后 PreparedBatch base 必须再次通过");
    }
}

#[test]
fn prepared_batch_segment_source_span_semantics() {
    // segment 仅 byte 精度允许非空递增 sourceSpan，其余三种精度必须显式 null。
    let golden = load_golden("prepared-batch.v2.json");
    let cases = golden["segmentSpanSemantics"].as_array().unwrap();
    assert_eq!(cases.len(), 9);
    for case in cases {
        let mut candidate = golden["rejectBase"].clone();
        candidate["segmentDigests"][0]["mappingPrecision"] = case["mappingPrecision"].clone();
        candidate["segmentDigests"][0]["sourceSpan"] = case["sourceSpan"].clone();
        if case["accepted"].as_bool().unwrap() {
            factory_contracts::event::validate_prepared_batch_manifest(&candidate)
                .expect("合法 segment span 必须通过");
        } else {
            assert!(
                factory_contracts::event::validate_prepared_batch_manifest(&candidate).is_err(),
                "segment span case {} 应拒绝",
                case["name"]
            );
        }
    }
}
