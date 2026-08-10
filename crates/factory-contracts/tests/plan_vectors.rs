//! crates/factory-contracts/tests/plan_vectors.rs
//!
//! Task 3 跨语言 golden vector 测试（Rust 端）。
//! 从 repo 根的 contracts/golden/*.json 读取与 Python / TypeScript 完全相同的
//! 冻结向量，断言 Rust 实现产出字节级一致的 canonical bytes 与 hash。

use factory_contracts::canonical::{canonicalize_json_text, CanonicalJsonError};
use factory_contracts::plan::{barrier_id, plan_revision_digest, semantic_plan_hash};
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
fn canonical_json_accept_vectors() {
    // 接受用例：inputJsonText 经 canonicalize_json_text 得到 expectedCanonical。
    let golden = load_golden("canonical-json.v1.json");
    let cases = golden["accept"].as_array().expect("accept 必须为数组");
    assert!(!cases.is_empty(), "accept 用例不得为空");
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let input = case["inputJsonText"].as_str().unwrap();
        let expected = case["expectedCanonical"].as_str().unwrap();
        let actual = canonicalize_json_text(input)
            .unwrap_or_else(|e| panic!("accept {name} 意外失败：{e}"));
        let actual_str = String::from_utf8(actual).unwrap();
        assert_eq!(actual_str, expected, "accept 用例 {name} 字节不一致");
    }
}

#[test]
fn canonical_json_reject_vectors() {
    // 拒绝用例：canonicalize_json_text 必须返回 Err，错误码为 canonical-json-error。
    let golden = load_golden("canonical-json.v1.json");
    let cases = golden["reject"].as_array().expect("reject 必须为数组");
    assert!(!cases.is_empty(), "reject 用例不得为空");
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let input = case["inputJsonText"].as_str().unwrap();
        let result = canonicalize_json_text(input);
        assert!(result.is_err(), "reject 用例 {name} 应当失败但通过了");
    }
    // 稳定错误码回归。
    assert_eq!(CanonicalJsonError::ERROR_CODE, "canonical-json-error");
}

#[test]
fn semantic_plan_hash_vectors() {
    // semanticPlanHash：plan 全量对象经字段投影 + canonicalize + SHA-256。
    let golden = load_golden("plan-hash.v1.json");
    let cases = golden["semanticPlanHash"].as_array().unwrap();
    assert!(!cases.is_empty());
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let plan = &case["plan"];
        let expected = case["expected"].as_str().unwrap();
        let actual = semantic_plan_hash(plan)
            .unwrap_or_else(|e| panic!("semanticPlanHash {name} 失败：{e}"));
        assert_eq!(actual, expected, "semanticPlanHash {name} 不一致");
    }
}

#[test]
fn semantic_plan_hash_stable_across_excluded_fields() {
    // 不变量：语义相同、仅排除字段不同 → semanticPlanHash 必须相同。
    let golden = load_golden("plan-hash.v1.json");
    let inv = &golden["invariants"]["sameSemanticsShareHash"];
    let a_name = inv["a"].as_str().unwrap();
    let b_name = inv["b"].as_str().unwrap();
    let cases = golden["semanticPlanHash"].as_array().unwrap();
    let find = |n: &str| -> String {
        let c = cases.iter().find(|c| c["name"].as_str() == Some(n)).unwrap();
        semantic_plan_hash(&c["plan"]).unwrap()
    };
    assert_eq!(find(a_name), find(b_name), "语义相同应共享 semanticPlanHash");
}

#[test]
fn plan_revision_digest_vectors() {
    // planRevisionDigest：完整修订对象（排除自身值与签名）经 canonicalize + SHA-256。
    let golden = load_golden("plan-hash.v1.json");
    let cases = golden["planRevisionDigest"].as_array().unwrap();
    assert!(!cases.is_empty());
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let revision = &case["revision"];
        let expected = case["expected"].as_str().unwrap();
        let actual = plan_revision_digest(revision)
            .unwrap_or_else(|e| panic!("planRevisionDigest {name} 失败：{e}"));
        assert_eq!(actual, expected, "planRevisionDigest {name} 不一致");
    }
}

#[test]
fn plan_revision_digest_differs_across_lineage() {
    // 不变量：语义相同但谱系不同 → planRevisionDigest 必须不同。
    let golden = load_golden("plan-hash.v1.json");
    let inv = &golden["invariants"]["differentLineageDiffersDigest"];
    let a_name = inv["a"].as_str().unwrap();
    let b_name = inv["b"].as_str().unwrap();
    let cases = golden["planRevisionDigest"].as_array().unwrap();
    let find = |n: &str| -> String {
        let c = cases.iter().find(|c| c["name"].as_str() == Some(n)).unwrap();
        plan_revision_digest(&c["revision"]).unwrap()
    };
    assert_ne!(find(a_name), find(b_name), "不同谱系应得到不同 planRevisionDigest");
}

#[test]
fn barrier_id_vectors() {
    // barrierId：域分离数组 canonicalize + SHA-256，前缀 "bar_"。
    let golden = load_golden("barrier-id.v1.json");
    let cases = golden["cases"].as_array().unwrap();
    assert!(!cases.is_empty());
    let mut seen = std::collections::HashSet::new();
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let run_id = case["runId"].as_str().unwrap();
        let digest = case["planRevisionDigest"].as_str().unwrap();
        let phase = case["businessPhase"].as_str().unwrap();
        let ordinal = case["barrierOrdinal"].as_i64().unwrap();
        let expected = case["expected"].as_str().unwrap();
        let actual = barrier_id(run_id, digest, phase, ordinal)
            .unwrap_or_else(|e| panic!("barrierId {name} 失败：{e}"));
        assert_eq!(actual, expected, "barrierId {name} 不一致");
        seen.insert(actual);
    }
    // 不同输入必须产生不同 barrierId。
    assert_eq!(seen.len(), cases.len(), "barrierId 出现碰撞");
}
