//! crates/factory-contracts/tests/plan_vectors.rs
//!
//! Task 3 跨语言 golden vector 测试（Rust 端）。
//! 从 repo 根的 contracts/golden/*.json 读取与 Python / TypeScript 完全相同的
//! 冻结向量，断言 Rust 实现产出字节级一致的 canonical bytes 与 hash。

use factory_contracts::canonical::{canonicalize_json_text, CanonicalJsonError};
use factory_contracts::plan::{
    barrier_id, build_semantic_projection, plan_revision_digest, semantic_plan_hash, PlanHashError,
};
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

/// 通过共享 golden 名称定位基准对象；测试配置错误不能静默退化为通过。
fn find_named_case<'a>(cases: &'a [Value], name: &str) -> &'a Value {
    cases
        .iter()
        .find(|case| case["name"].as_str() == Some(name))
        .unwrap_or_else(|| panic!("missing golden base case: {name}"))
}

/// 在独立 payload 副本上应用一条 golden 变异，避免同组反例互相污染。
fn apply_golden_mutation(mut payload: Value, mutation: &Value) -> Value {
    let path = mutation["path"]
        .as_array()
        .expect("mutation path must be array");
    assert!(!path.is_empty(), "mutation path must be non-empty");

    // 路径段可定位对象键或数组下标，保持 DAG 反例由单一跨语言 golden 驱动。
    let mut target = &mut payload;
    for segment in &path[..path.len() - 1] {
        let key = segment.as_str().expect("path segment must be string");
        target = match target {
            Value::Object(object) => object
                .get_mut(key)
                .unwrap_or_else(|| panic!("mutation path is absent: {key}")),
            Value::Array(array) => {
                let index = key
                    .parse::<usize>()
                    .unwrap_or_else(|_| panic!("mutation array index is invalid: {key}"));
                array
                    .get_mut(index)
                    .unwrap_or_else(|| panic!("mutation array index is absent: {key}"))
            }
            _ => panic!("mutation path is not a container: {key}"),
        };
    }
    let leaf = path
        .last()
        .and_then(Value::as_str)
        .expect("leaf must be string");
    match mutation["op"].as_str().expect("mutation op must be string") {
        "remove" => {
            if let Some(object) = target.as_object_mut() {
                object.remove(leaf);
            } else if let Some(array) = target.as_array_mut() {
                let index = leaf
                    .parse::<usize>()
                    .unwrap_or_else(|_| panic!("mutation array index is invalid: {leaf}"));
                array.remove(index);
            } else {
                panic!("mutation target must be a container");
            }
        }
        "replace" => {
            if let Some(object) = target.as_object_mut() {
                object.insert(leaf.to_string(), mutation["value"].clone());
            } else if let Some(array) = target.as_array_mut() {
                let index = leaf
                    .parse::<usize>()
                    .unwrap_or_else(|_| panic!("mutation array index is invalid: {leaf}"));
                array[index] = mutation["value"].clone();
            } else {
                panic!("mutation target must be a container");
            }
        }
        op => panic!("unsupported golden mutation: {op}"),
    }
    payload
}

/// 根据共享声明式变异构造原始输入；单个黄金用例可含多条变异而不新增测试节点。
fn materialize_mutation_cases(
    case: &Value,
    source_cases: &[Value],
    payload_key: &str,
) -> Vec<(String, Value)> {
    if let Some(input) = case.get("input") {
        return vec![(
            (case["name"].as_str().expect("case name must be string")).to_owned(),
            input.clone(),
        )];
    }

    let base_name = case["base"].as_str().expect("base name must be string");
    let base_payload = &find_named_case(source_cases, base_name)[payload_key];
    let mutations = match case.get("mutations") {
        Some(Value::Array(items)) if !items.is_empty() => items.iter().collect::<Vec<_>>(),
        Some(_) => panic!("golden mutations must be a non-empty array"),
        None => vec![&case["mutation"]],
    };
    let case_name = case["name"].as_str().expect("case name must be string");
    mutations
        .into_iter()
        .enumerate()
        .map(|(index, mutation)| {
            // legacy 单 mutation 允许省略子名称并回退到序号；显式提供的值仍必须是非空字符串。
            let label = match mutation.get("name") {
                None => index.to_string(),
                Some(value) => value
                    .as_str()
                    .unwrap_or_else(|| {
                        panic!("golden mutation {case_name}:{index} name must be a string")
                    })
                    .to_owned(),
            };
            assert!(!label.is_empty(), "golden mutation name must be non-empty");
            (
                format!("{case_name}:{label}"),
                apply_golden_mutation(base_payload.clone(), mutation),
            )
        })
        .collect()
}

/// 只断言稳定分类，避免把输入正文或第三方校验器细节固定进错误输出。
fn is_stable_plan_hash_error<T>(
    result: Result<T, PlanHashError>,
    expected_error_code: &str,
) -> bool {
    match result {
        Ok(_) => false,
        Err(error) => {
            PlanHashError::ERROR_CODE == expected_error_code
                && error.to_string().starts_with("[plan-hash-error]")
        }
    }
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
        let actual =
            canonicalize_json_text(input).unwrap_or_else(|e| panic!("accept {name} 意外失败：{e}"));
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
        let c = cases
            .iter()
            .find(|c| c["name"].as_str() == Some(n))
            .unwrap();
        semantic_plan_hash(&c["plan"]).unwrap()
    };
    assert_eq!(
        find(a_name),
        find(b_name),
        "语义相同应共享 semanticPlanHash"
    );
}

#[test]
fn run_spec_invalid_vectors_reject_before_projection_and_hash() {
    // RunSpec 先经过权威 schema；每个非法 wire 都不能产生 projection 或 hash。
    let golden = load_golden("plan-hash.v1.json");
    let source_cases = golden["semanticPlanHash"].as_array().unwrap();
    let invalid_cases = golden["invalidRunSpec"].as_array().unwrap();
    assert!(!invalid_cases.is_empty());

    // legacy 单 mutation 可省略子名称并稳定回退到序号；显式空名仍是损坏的 golden 元数据。
    let legacy_case = find_named_case(invalid_cases, "missing-base-commit");
    let legacy_materialized = materialize_mutation_cases(legacy_case, source_cases, "plan");
    assert_eq!(legacy_materialized[0].0, "missing-base-commit:0");
    let mut empty_name_case =
        find_named_case(invalid_cases, "invalid-base-commit-or-zero-sentinel").clone();
    empty_name_case["mutations"][0]["name"] = Value::String(String::new());
    let empty_name_rejected = std::panic::catch_unwind(|| {
        materialize_mutation_cases(&empty_name_case, source_cases, "plan")
    });
    assert!(
        empty_name_rejected.is_err(),
        "显式空 mutation name 必须 fail closed"
    );

    let mut unexpectedly_accepted = Vec::new();
    for case in invalid_cases {
        let expected = case["expectedErrorCode"].as_str().unwrap();
        for (name, input) in materialize_mutation_cases(case, source_cases, "plan") {
            if !is_stable_plan_hash_error(build_semantic_projection(&input), expected) {
                unexpectedly_accepted.push(format!("{name}:projection"));
            }
            if !is_stable_plan_hash_error(semantic_plan_hash(&input), expected) {
                unexpectedly_accepted.push(format!("{name}:hash"));
            }
        }
    }
    assert!(
        unexpectedly_accepted.is_empty(),
        "非法 RunSpec 未被拒绝：{}",
        unexpectedly_accepted.join(", ")
    );
}

#[test]
fn shared_rfc3339_date_time_subset_is_enforced_for_both_contracts() {
    // 权威 schema 的 ASCII pattern 与同名 format 必须一起收敛三端日期语义。
    let golden = load_golden("plan-hash.v1.json");
    let run_spec = &golden["semanticPlanHash"][0]["plan"];
    let revision = &golden["planRevisionDigest"][0]["revision"];

    for case in golden["rfc3339DateTime"]["valid"].as_array().unwrap() {
        let value = case["value"].as_str().unwrap();
        let mut valid_run_spec = run_spec.clone();
        valid_run_spec["createdAt"] = Value::String(value.to_owned());
        let mut valid_revision = revision.clone();
        valid_revision["createdAt"] = Value::String(value.to_owned());
        assert!(
            semantic_plan_hash(&valid_run_spec).is_ok(),
            "RunSpec valid: {value}"
        );
        assert!(
            plan_revision_digest(&valid_revision).is_ok(),
            "PlanRevision valid: {value}"
        );
    }

    let mut unexpectedly_accepted = Vec::new();
    for case in golden["rfc3339DateTime"]["invalid"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let value = case["value"].as_str().unwrap();
        let mut invalid_run_spec = run_spec.clone();
        invalid_run_spec["createdAt"] = Value::String(value.to_owned());
        let mut invalid_revision = revision.clone();
        invalid_revision["createdAt"] = Value::String(value.to_owned());
        if !is_stable_plan_hash_error(
            build_semantic_projection(&invalid_run_spec),
            PlanHashError::ERROR_CODE,
        ) {
            unexpectedly_accepted.push(format!("runspec:{name}:projection"));
        }
        if !is_stable_plan_hash_error(
            semantic_plan_hash(&invalid_run_spec),
            PlanHashError::ERROR_CODE,
        ) {
            unexpectedly_accepted.push(format!("runspec:{name}:hash"));
        }
        if !is_stable_plan_hash_error(
            plan_revision_digest(&invalid_revision),
            PlanHashError::ERROR_CODE,
        ) {
            unexpectedly_accepted.push(format!("revision:{name}"));
        }
    }
    assert!(
        unexpectedly_accepted.is_empty(),
        "非法 RFC3339 子集值未被拒绝：{}",
        unexpectedly_accepted.join(", ")
    );
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
    // 不变量：谱系或任一 DAG material 变化都必须改变 planRevisionDigest。
    let golden = load_golden("plan-hash.v1.json");
    let inv = &golden["invariants"]["differentLineageDiffersDigest"];
    let a_name = inv["a"].as_str().unwrap();
    let b_name = inv["b"].as_str().unwrap();
    let cases = golden["planRevisionDigest"].as_array().unwrap();
    let find = |n: &str| -> String {
        let c = cases
            .iter()
            .find(|c| c["name"].as_str() == Some(n))
            .unwrap();
        plan_revision_digest(&c["revision"]).unwrap()
    };
    assert_ne!(
        find(a_name),
        find(b_name),
        "不同谱系应得到不同 planRevisionDigest"
    );

    let dag_inv = &golden["invariants"]["differentDagMaterialDiffersDigest"];
    let base_name = dag_inv["base"].as_str().unwrap();
    let baseline = cases
        .iter()
        .find(|case| case["name"].as_str() == Some(base_name))
        .unwrap()["revision"]
        .clone();
    let changed = apply_golden_mutation(baseline.clone(), &dag_inv["mutation"]);
    assert_ne!(
        plan_revision_digest(&baseline).unwrap(),
        plan_revision_digest(&changed).unwrap(),
        "DAG material 变化必须改变 planRevisionDigest"
    );
}

#[test]
fn plan_revision_invalid_vectors_reject_before_digest() {
    // 仅 planRevisionDigest/signature 可在摘要前剥离，其余字段仍需完整 schema 验证。
    let golden = load_golden("plan-hash.v1.json");
    let source_cases = golden["planRevisionDigest"].as_array().unwrap();
    let invalid_cases = golden["invalidPlanRevision"].as_array().unwrap();
    assert!(!invalid_cases.is_empty());
    let mut unexpectedly_accepted = Vec::new();
    for case in invalid_cases {
        let expected = case["expectedErrorCode"].as_str().unwrap();
        for (name, input) in materialize_mutation_cases(case, source_cases, "revision") {
            if !is_stable_plan_hash_error(plan_revision_digest(&input), expected) {
                unexpectedly_accepted.push(name);
            }
        }
    }
    assert!(
        unexpectedly_accepted.is_empty(),
        "非法 PlanRevision 未被拒绝：{}",
        unexpectedly_accepted.join(", ")
    );
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
