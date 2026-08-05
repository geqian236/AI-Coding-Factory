//! crates/factory-contracts/tests/event_vectors.rs
//!
//! Task 4 跨语言 golden vector 测试（Rust 端）。
//! 从 repo 根的 contracts/golden/event-hash.v2.json 与 prepared-batch.v2.json
//! 读取与 Python / TypeScript 完全相同的冻结向量，断言 Rust 物化器产出
//! 字节级一致的 eventId、payloadDigest、DurableEventV2 链与 canonical bytes。

use factory_contracts::canonical::canonicalize_value;
use factory_contracts::event::{event_id, materialize_batch, payload_digest, EventHashError};
use serde_json::Value;
use std::fs;
use std::path::PathBuf;

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
    let case = cases.iter().find(|c| c["name"].as_str() == Some(a_name)).unwrap();
    let ingest = case["ingestEventId"].as_str().unwrap();
    assert_eq!(event_id(ingest).unwrap(), event_id(ingest).unwrap());
}

#[test]
fn payload_digest_vectors() {
    // payloadDigest：脱敏 payload 的 JCS 摘要，输出必须等于冻结值。
    let golden = load_golden("event-hash.v2.json");
    let cases = golden["payloadDigest"].as_array().expect("payloadDigest 必须为数组");
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
fn materialize_vectors() {
    // materialize_batch：物化整批事件，逐字段必须等于冻结 DurableEventV2 列表。
    let golden = load_golden("event-hash.v2.json");
    let cases = golden["materialize"].as_array().expect("materialize 必须为数组");
    assert!(!cases.is_empty());
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let batch = &case["batch"];
        let anchor = &case["anchor"];
        let durable_at = case["durableAt"].as_str().unwrap();
        let expected = case["expected"].clone();
        let result = materialize_batch(batch, anchor, durable_at)
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
    let find = |n: &str| cases.iter().find(|c| c["name"].as_str() == Some(n)).unwrap();
    let a = find(a_name);
    let b = find(b_name);
    let ra = materialize_batch(&a["batch"], &a["anchor"], a["durableAt"].as_str().unwrap()).unwrap();
    let rb = materialize_batch(&b["batch"], &b["anchor"], b["durableAt"].as_str().unwrap()).unwrap();
    assert_eq!(ra[0]["taskSeq"].as_i64(), Some(0));
    assert_eq!(rb[0]["taskSeq"].as_i64(), Some(0));
    // previousEventDigest 单链：两条独立链的 genesis eventDigest 必须不同（payload 不同）
    assert_ne!(ra[0]["eventDigest"], rb[0]["eventDigest"], "两条独立链 eventDigest 不应相同");
    // 两者 previousEventDigest 都是固定全零 predecessor（genesis）
    assert_eq!(
        ra[0]["previousEventDigest"].as_str().unwrap(),
        "sha256:0000000000000000000000000000000000000000000000000000000000000000"
    );
    assert_eq!(
        rb[0]["previousEventDigest"].as_str().unwrap(),
        "sha256:0000000000000000000000000000000000000000000000000000000000000000"
    );
}

#[test]
fn materialize_reject_vectors() {
    // 拒绝向量：前驱漂移、竞争抢占、空批、重复 ID、taskId 不一致、未知/缺字段必须 fail closed。
    let golden = load_golden("event-hash.v2.json");
    let cases = golden["reject"].as_array().expect("reject 必须为数组");
    assert!(!cases.is_empty());
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let batch = &case["batch"];
        let anchor = &case["anchor"];
        let durable_at = case["durableAt"].as_str().unwrap();
        let result = materialize_batch(batch, anchor, durable_at);
        assert!(result.is_err(), "reject 用例 {name} 应当失败但通过了");
    }
    // 稳定错误码回归。
    assert_eq!(EventHashError::ERROR_CODE, "event-hash-error");
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
}
