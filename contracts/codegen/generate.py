"""
contracts/codegen/generate.py

确定性代码生成器：从 catalog.v1.json 读取 codegen=true 的 schema，
生成 TypeScript、Python、Rust 三语言类型定义。

算法版本：v1
支持 --check 标志：检查输出是否与当前磁盘文件一致，不一致则报错且不修改文件。
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

import jsonschema

# 仓库根目录（相对于本脚本所在 contracts/codegen/）
REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = REPO_ROOT / "contracts" / "codegen" / "catalog.v1.json"

# 生成目标路径（来自 catalog.v1.json outputTargets）
TS_OUT = REPO_ROOT / "packages" / "factory-contracts" / "src" / "generated" / "contracts.ts"
PY_OUT = REPO_ROOT / "apps" / "agent" / "src" / "factory_agent" / "contracts" / "generated" / "models.py"
RS_OUT = REPO_ROOT / "crates" / "factory-contracts" / "src" / "generated" / "contracts.rs"

# 计划哈希的运行时 validator 只能从这两份权威 schema 机械生成，不能复用普通类型生成的
# per-entry ERROR 注释降级路径。任一读取/解析/结构检查失败都必须阻断三语言生成。
RUN_SPEC_VALIDATOR_SCHEMA_PATH = "contracts/schemas/run-spec.v1.schema.json"
PLAN_REVISION_VALIDATOR_SCHEMA_PATH = "contracts/schemas/plan-revision.v1.schema.json"
KNOWN_VALIDATOR_FORMATS = frozenset({"date-time"})
_DRAFT7_SCHEMA_URI = "http://json-schema.org/draft-07/schema#"

# Draft7 中每类子 schema 的位置。按关键字感知地遍历，避免把 `properties` 中用户
# 自定义的 `format`、`$ref` 字段名误认为 schema keyword。
_SINGLE_SCHEMA_KEYWORDS = frozenset(
    {
        "additionalItems",
        "additionalProperties",
        "contains",
        "if",
        "then",
        "else",
        "not",
        "propertyNames",
    }
)
_SCHEMA_MAP_KEYWORDS = frozenset({"properties", "patternProperties", "definitions", "$defs"})
_SCHEMA_ARRAY_KEYWORDS = frozenset({"allOf", "anyOf", "oneOf"})


class ValidatorSchemaError(RuntimeError):
    """运行时 validator schema 不能安全嵌入生成物时的稳定失败分类。"""


class CodegenGenerationError(RuntimeError):
    """普通 catalog 类型生成失败时的稳定失败分类，禁止生成 ERROR 注释占位。"""

# JSON Schema → TypeScript 类型映射
TS_TYPE_MAP: dict[str, str] = {
    "string": "string",
    "integer": "number",
    "number": "number",
    "boolean": "boolean",
    "object": "Record<string, unknown>",
    "array": "unknown[]",
    "null": "null",
}

# JSON Schema → Python 类型映射
PY_TYPE_MAP: dict[str, str] = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "object": "dict[str, Any]",
    "array": "list[Any]",
    "null": "None",
}

# JSON Schema → Rust 类型映射
RS_TYPE_MAP: dict[str, str] = {
    "string": "String",
    "integer": "i64",
    "number": "f64",
    "boolean": "bool",
    "object": "serde_json::Value",
    "array": "Vec<serde_json::Value>",
    "null": "()",
}


def _is_utf8_encoding(encoding: object) -> bool:
    """仅接受无 BOM 的 UTF-8 编码标识，防止代码生成 CLI 在 ANSI 代码页截断中文。"""
    if not isinstance(encoding, str):
        return False
    return encoding.replace("_", "").replace("-", "").casefold() == "utf8"


def _configure_cli_text_stream_utf8(stream: object) -> None:
    """将一个 CLI 文本流固定为严格 UTF-8；未知流形态必须 fail-closed。

    生成器的成功、漂移和加载错误都可能包含中文或路径。必须在 argparse 与任何输出前
    配置 stdout/stderr；若宿主流不能明确确认 UTF-8/strict，就停止，不能留下半份生成
    报告。属性读取、重配置和状态回读的任何异常均归一化，避免把宿主异常正文泄露给 CLI。
    """
    try:
        # StringIO 只保存 Unicode 字符串，不经过外部编码器，是唯一可安全省略状态属性的流。
        if isinstance(stream, io.StringIO):
            return

        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="strict")

        # 即使 reconfigure 未抛错，也必须回读两项状态，拒绝 replace/ignore 等伪成功实现。
        encoding = getattr(stream, "encoding", None)
        errors = getattr(stream, "errors", None)
        if _is_utf8_encoding(encoding) and isinstance(errors, str) and errors == "strict":
            return
    except Exception as exc:  # noqa: BLE001 - 只暴露稳定分类，禁止泄露宿主异常正文。
        raise RuntimeError("CLI_TEXT_UTF8_CONFIGURATION_FAILED") from exc

    raise RuntimeError("CLI_TEXT_UTF8_CONFIGURATION_FAILED")


def _configure_cli_text_output_utf8() -> None:
    """在解析参数和输出生成结果前同时固定 stdout/stderr，保证证据编码一致。"""
    _configure_cli_text_stream_utf8(sys.stdout)
    _configure_cli_text_stream_utf8(sys.stderr)


def _emit_cli_text_encoding_failure() -> bool:
    """尝试输出稳定 ASCII 失败分类；返回值显式记录 stderr 是否仍可写。"""
    try:
        sys.stderr.write("CLI_TEXT_UTF8_CONFIGURATION_FAILED\n")
        sys.stderr.flush()
        return True
    except Exception:  # noqa: BLE001 - 失败路径不允许 traceback 覆盖稳定分类。
        return False


def load_catalog() -> dict[str, Any]:
    """加载 codegen catalog，返回解析后的 dict。"""
    with CATALOG_PATH.open(encoding="utf-8") as f:
        return json.load(f)  # type: ignore[no-any-return]


def load_schema(schema_path_str: str) -> dict[str, Any]:
    """从 catalog 中的相对路径加载 JSON Schema。"""
    schema_path = REPO_ROOT / schema_path_str
    if not schema_path.exists():
        raise FileNotFoundError(f"Schema 文件不存在: {schema_path}")
    with schema_path.open(encoding="utf-8") as f:
        return json.load(f)  # type: ignore[no-any-return]


def _reject_duplicate_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """JSON object hook：任何层级重复 key 都必须在 schema 编译前稳定失败。"""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidatorSchemaError("VALIDATOR_SCHEMA_DUPLICATE_KEY")
        result[key] = value
    return result


def _reject_non_finite_json_constant(_constant: str) -> NoReturn:
    """拒绝 Python JSON 扩展常量，确保权威 schema 只接受标准 JSON 数字。"""
    raise ValidatorSchemaError("VALIDATOR_SCHEMA_INVALID_JSON")


def _walk_validator_subschema(value: object) -> None:
    """按 Draft7 schema keyword 遍历子 schema，拒绝外部 ref 与未知 format。

    不通用递归 dict 的全部 value：`properties`/`definitions` 是名称到 schema 的映射，
    其中的名称可合法地叫 `format` 或 `$ref`，不能被错当成 keyword。
    """
    if isinstance(value, bool):
        return
    if not isinstance(value, dict):
        # 子 schema 的非 object/bool 形态再交给 Draft7 meta-schema 分类，不能静默跳过。
        return

    reference = value.get("$ref")
    if reference is not None and (not isinstance(reference, str) or not reference.startswith("#")):
        raise ValidatorSchemaError("VALIDATOR_SCHEMA_EXTERNAL_REF")

    format_name = value.get("format")
    if format_name is not None and (
        not isinstance(format_name, str) or format_name not in KNOWN_VALIDATOR_FORMATS
    ):
        raise ValidatorSchemaError("VALIDATOR_SCHEMA_UNKNOWN_FORMAT")

    for keyword in _SINGLE_SCHEMA_KEYWORDS:
        if keyword in value:
            _walk_validator_subschema(value[keyword])

    for keyword in _SCHEMA_MAP_KEYWORDS:
        schema_map = value.get(keyword)
        if isinstance(schema_map, dict):
            for child_schema in schema_map.values():
                _walk_validator_subschema(child_schema)

    items = value.get("items")
    if isinstance(items, list):
        for child_schema in items:
            _walk_validator_subschema(child_schema)
    elif items is not None:
        _walk_validator_subschema(items)

    for keyword in _SCHEMA_ARRAY_KEYWORDS:
        schemas = value.get(keyword)
        if isinstance(schemas, list):
            for child_schema in schemas:
                _walk_validator_subschema(child_schema)

    dependencies = value.get("dependencies")
    if isinstance(dependencies, dict):
        for dependency in dependencies.values():
            # Draft7 dependency 可以是 string 数组；只有 schema 形态才继续遍历。
            if isinstance(dependency, (bool, dict)):
                _walk_validator_subschema(dependency)


def _validate_validator_schema_object(schema: dict[str, Any]) -> None:
    """验证权威/派生 schema 是精确 Draft7 且不含运行时可解析的外部资源。"""
    if schema.get("$schema") != _DRAFT7_SCHEMA_URI:
        raise ValidatorSchemaError("VALIDATOR_SCHEMA_UNSUPPORTED_DRAFT7")
    _walk_validator_subschema(schema)
    try:
        jsonschema.Draft7Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        raise ValidatorSchemaError("VALIDATOR_SCHEMA_INVALID_DRAFT7") from exc


def parse_validator_schema(raw: bytes) -> dict[str, Any]:
    """严格按无 BOM UTF-8→无重复键 JSON→精确 Draft7 解析 validator schema。"""
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValidatorSchemaError("VALIDATOR_SCHEMA_INVALID_UTF8")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValidatorSchemaError("VALIDATOR_SCHEMA_INVALID_UTF8") from exc
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_object_keys,
            parse_constant=_reject_non_finite_json_constant,
        )
    except ValidatorSchemaError:
        raise
    except json.JSONDecodeError as exc:
        raise ValidatorSchemaError("VALIDATOR_SCHEMA_INVALID_JSON") from exc
    if not isinstance(parsed, dict):
        raise ValidatorSchemaError("VALIDATOR_SCHEMA_ROOT_NOT_OBJECT")

    _validate_validator_schema_object(parsed)
    return parsed


def load_required_validator_schema(schema_path_str: str) -> tuple[dict[str, Any], str]:
    """读取一个权威 validator schema，并返回对象与原始字节 SHA-256。"""
    schema_path = REPO_ROOT / schema_path_str
    try:
        raw = schema_path.read_bytes()
    except OSError as exc:
        raise ValidatorSchemaError("VALIDATOR_SCHEMA_READ_FAILED") from exc
    schema = parse_validator_schema(raw)
    return schema, "sha256:" + hashlib.sha256(raw).hexdigest()


def build_plan_revision_digest_material_schema(plan_revision_schema: dict[str, Any]) -> dict[str, Any]:
    """从权威 PlanRevision schema 机械删除仅摘要排除的字段，得到 digest material schema。"""
    material = copy.deepcopy(plan_revision_schema)
    properties = material.get("properties")
    required = material.get("required")
    # 摘要派生不能把权威 digest 字段本身缺失误当作「已排除」：它必须原本同时受
    # properties 与 required 约束，之后才允许仅删除 digest/signature 两项。
    if (
        not isinstance(properties, dict)
        or not isinstance(required, list)
        or "planRevisionDigest" not in properties
        or "signature" not in properties
        or "planRevisionDigest" not in required
        or "signature" in required
        or required.count("planRevisionDigest") != 1
    ):
        raise ValidatorSchemaError("VALIDATOR_SCHEMA_DIGEST_MATERIAL_FAILED")

    # 两个排除字段必须先由 full schema 明确定义；这里精确删除，禁止 silent pop 掩盖源合同漂移。
    del properties["planRevisionDigest"]
    del properties["signature"]
    required.remove("planRevisionDigest")
    return material


def load_required_validator_schemas() -> dict[str, tuple[dict[str, Any], str]]:
    """一次性加载并派生所有运行时 validator schema；任一步失败均不返回半份 bundle。"""
    run_spec_schema, run_spec_sha256 = load_required_validator_schema(RUN_SPEC_VALIDATOR_SCHEMA_PATH)
    plan_revision_schema, plan_revision_sha256 = load_required_validator_schema(
        PLAN_REVISION_VALIDATOR_SCHEMA_PATH
    )
    digest_material_schema = build_plan_revision_digest_material_schema(plan_revision_schema)
    try:
        _validate_validator_schema_object(digest_material_schema)
    except ValidatorSchemaError as exc:
        raise ValidatorSchemaError("VALIDATOR_SCHEMA_DIGEST_MATERIAL_FAILED") from exc
    return {
        "runSpec": (run_spec_schema, run_spec_sha256),
        "planRevision": (plan_revision_schema, plan_revision_sha256),
        "planRevisionDigestMaterial": (digest_material_schema, plan_revision_sha256),
    }


def _embedded_schema_json(schema: dict[str, Any]) -> str:
    """生成标准且稳定的 JSON；任何不可序列化值都归一为无敏感信息的稳定错误。"""
    try:
        return json.dumps(
            schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValidatorSchemaError("VALIDATOR_SCHEMA_INVALID_JSON") from exc


def _embedded_schema_sha256(schema_json: str) -> str:
    """计算嵌入 JSON 字符串的 UTF-8 内容身份；它与权威原文件 SHA 各司其职。"""
    return "sha256:" + hashlib.sha256(schema_json.encode("utf-8")).hexdigest()


def _rust_raw_string(text: str) -> str:
    """选择不会与 schema 文本冲突的 Rust raw-string 定界符。"""
    hashes = "#"
    while f'"{hashes}' in text:
        hashes += "#"
    return f'r{hashes}"{text}"{hashes}'


def get_schema_properties(
    schema: dict[str, Any],
    definition_key: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """
    从 schema 提取属性定义和 required 字段列表。
    如果指定了 definition_key，则从 definitions 中提取对应定义。
    """
    target = schema
    if definition_key:
        defs = schema.get("definitions", schema.get("$defs", {}))
        if definition_key not in defs:
            raise KeyError(f"definitions 中未找到 '{definition_key}'")
        target = defs[definition_key]
    props: dict[str, Any] = target.get("properties", {})
    required: list[str] = target.get("required", [])
    return props, required


def prop_ts_type(prop: dict[str, Any]) -> str:
    """将 schema property 定义转换为 TypeScript 类型字符串。"""
    # oneOf/anyOf 含 null 表示可选类型
    if "oneOf" in prop or "anyOf" in prop:
        variants = prop.get("oneOf", prop.get("anyOf", []))
        types = {v.get("type", "unknown") for v in variants if isinstance(v, dict)}
        non_null = [TS_TYPE_MAP.get(t, "unknown") for t in types if t != "null"]
        if not non_null:
            return "null"
        result = " | ".join(sorted(non_null))
        if "null" in types:
            result += " | null"
        return result
    if "const" in prop:
        val = prop["const"]
        if isinstance(val, str):
            return f'"{val}"'
        return str(val).lower()
    prop_type = prop.get("type")
    if isinstance(prop_type, list):
        ts_types = [TS_TYPE_MAP.get(t, "unknown") for t in prop_type]
        return " | ".join(ts_types)
    if "enum" in prop:
        vals = prop["enum"]
        return " | ".join(f'"{v}"' if isinstance(v, str) else str(v) for v in vals)
    if prop_type == "array":
        items = prop.get("items", {})
        item_type = prop_ts_type(items) if items else "unknown"
        return f"{item_type}[]"
    if prop_type == "object":
        return "Record<string, unknown>"
    return TS_TYPE_MAP.get(str(prop_type), "unknown")


def prop_py_type(prop: dict[str, Any]) -> str:
    """将 schema property 定义转换为 Python 类型字符串。"""
    if "oneOf" in prop or "anyOf" in prop:
        variants = prop.get("oneOf", prop.get("anyOf", []))
        types = {v.get("type", "unknown") for v in variants if isinstance(v, dict)}
        non_null = [PY_TYPE_MAP.get(t, "Any") for t in types if t != "null"]
        if not non_null:
            return "None"
        base = non_null[0] if len(non_null) == 1 else f"Union[{', '.join(sorted(non_null))}]"
        if "null" in types:
            return f"Optional[{base}]"
        return base
    if "const" in prop:
        val = prop["const"]
        if isinstance(val, str):
            return f'Literal["{val}"]'
        return f"Literal[{val!r}]"
    prop_type = prop.get("type")
    if isinstance(prop_type, list):
        py_types = [PY_TYPE_MAP.get(t, "Any") for t in prop_type if t != "null"]
        base = py_types[0] if len(py_types) == 1 else f"Union[{', '.join(py_types)}]"
        if "null" in prop_type:
            return f"Optional[{base}]"
        return base
    if "enum" in prop:
        vals = prop["enum"]
        lit_vals = ", ".join(f'"{v}"' if isinstance(v, str) else repr(v) for v in vals)
        return f"Literal[{lit_vals}]"
    if prop_type == "array":
        items = prop.get("items", {})
        item_type = prop_py_type(items) if items else "Any"
        return f"list[{item_type}]"
    if prop_type == "object":
        return "dict[str, Any]"
    return PY_TYPE_MAP.get(str(prop_type), "Any")


def prop_rs_type(prop: dict[str, Any], optional: bool = False) -> str:
    """将 schema property 定义转换为 Rust 类型字符串。"""
    if "const" in prop:
        return "String"
    if "enum" in prop:
        return "String"
    if "oneOf" in prop or "anyOf" in prop:
        variants = prop.get("oneOf", prop.get("anyOf", []))
        types = {v.get("type", "unknown") for v in variants if isinstance(v, dict)}
        non_null = [RS_TYPE_MAP.get(t, "serde_json::Value") for t in types if t != "null"]
        has_null = "null" in types
        base = non_null[0] if len(non_null) == 1 else "serde_json::Value"
        if has_null or optional:
            return f"Option<{base}>"
        return base
    prop_type = prop.get("type")
    if isinstance(prop_type, list):
        non_null = [RS_TYPE_MAP.get(t, "serde_json::Value") for t in prop_type if t != "null"]
        base = non_null[0] if non_null else "serde_json::Value"
        if "null" in prop_type or optional:
            return f"Option<{base}>"
        return base
    if prop_type == "array":
        items = prop.get("items", {})
        item_type = prop_rs_type(items) if items else "serde_json::Value"
        rs = f"Vec<{item_type}>"
        return f"Option<{rs}>" if optional else rs
    if prop_type == "object":
        rs = "serde_json::Value"
        return f"Option<{rs}>" if optional else rs
    rs = RS_TYPE_MAP.get(str(prop_type), "serde_json::Value")
    return f"Option<{rs}>" if optional else rs


def load_codegen_entry_definition(entry: dict[str, Any]) -> tuple[str, dict[str, Any], list[str]]:
    """加载一个普通 catalog entry；任何失败都上抛稳定错误而非生成 ERROR 注释。"""
    try:
        name = entry["name"]
        schema_path = entry["schemaPath"]
        if not isinstance(name, str) or not isinstance(schema_path, str):
            raise TypeError("catalog entry name/schemaPath must be strings")
        schema = load_schema(schema_path)
        definition_key = entry.get("definitionKey")
        if definition_key is not None and not isinstance(definition_key, str):
            raise TypeError("catalog entry definitionKey must be string")
        props, required = get_schema_properties(schema, definition_key)
        return name, props, required
    except CodegenGenerationError:
        raise
    except Exception as exc:  # noqa: BLE001 - 普通生成失败不能泄露路径/底层正文，也不能降级。
        raise CodegenGenerationError("CODEGEN_GENERATION_FAILED") from exc


def render_codegen_entry(
    entry: dict[str, Any], renderer: Callable[[str, dict[str, Any], list[str]], str]
) -> str:
    """将单一普通 entry 渲染为目标语言文本；渲染细节异常也必须 fail-closed。"""
    try:
        name, props, required = load_codegen_entry_definition(entry)
        return renderer(name, props, required)
    except CodegenGenerationError:
        raise
    except Exception as exc:  # noqa: BLE001 - 不输出 schema/path/第三方异常正文。
        raise CodegenGenerationError("CODEGEN_GENERATION_FAILED") from exc


# ────────────────────────────── TypeScript ──────────────────────────────


def generate_ts_validator_schema_constants(validator_schemas: dict[str, tuple[dict[str, Any], str]]) -> list[str]:
    """生成 TypeScript 运行时 validator 常量；原始 SHA 与派生 material 同时被冻结。"""
    run_spec_schema, run_spec_sha256 = validator_schemas["runSpec"]
    plan_revision_schema, plan_revision_sha256 = validator_schemas["planRevision"]
    digest_material_schema, _ = validator_schemas["planRevisionDigestMaterial"]
    run_spec_json = _embedded_schema_json(run_spec_schema)
    plan_revision_json = _embedded_schema_json(plan_revision_schema)
    digest_material_json = _embedded_schema_json(digest_material_schema)
    return [
        "// 运行时 validator 由权威 schema 机械嵌入；禁止手写字段表。",
        f'export const RUN_SPEC_SCHEMA_SOURCE_SHA256 = "{run_spec_sha256}";',
        f"export const RUN_SPEC_SCHEMA_JSON = {json.dumps(run_spec_json, ensure_ascii=False)};",
        f'export const RUN_SPEC_SCHEMA_JSON_SHA256 = "{_embedded_schema_sha256(run_spec_json)}";',
        f'export const PLAN_REVISION_SCHEMA_SOURCE_SHA256 = "{plan_revision_sha256}";',
        f"export const PLAN_REVISION_SCHEMA_JSON = {json.dumps(plan_revision_json, ensure_ascii=False)};",
        f'export const PLAN_REVISION_SCHEMA_JSON_SHA256 = "{_embedded_schema_sha256(plan_revision_json)}";',
        "// 仅 planRevisionDigest/signature 从摘要 material 排除，其他字段仍由权威 schema 约束。",
        "export const PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON = "
        f"{json.dumps(digest_material_json, ensure_ascii=False)};",
        "export const PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON_SHA256 = "
        f'"{_embedded_schema_sha256(digest_material_json)}";',
        "",
    ]


def generate_ts_interface(name: str, props: dict[str, Any], required: list[str]) -> str:
    """生成单个 TypeScript interface 定义。"""
    lines = [f"/** {name} — 由 generate.py 自动生成，禁止手动修改 */"]
    lines.append(f"export interface {name} {{")
    for field, prop_def in props.items():
        ts_type = prop_ts_type(prop_def)
        optional_marker = "" if field in required else "?"
        desc = prop_def.get("description", "")
        if desc:
            lines.append(f"  /** {desc} */")
        lines.append(f"  {field}{optional_marker}: {ts_type};")
    lines.append("}")
    return "\n".join(lines)


def generate_typescript(
    entries: list[dict[str, Any]],
    validator_schemas: dict[str, tuple[dict[str, Any], str]] | None = None,
) -> str:
    """生成完整 TypeScript 文件内容。"""
    validator_schemas = validator_schemas or load_required_validator_schemas()
    sections = [
        "// 此文件由 contracts/codegen/generate.py 自动生成，禁止手动修改。",
        "// 源 schema: contracts/schemas/*.schema.json",
        "// 算法版本: v1",
        "",
        "/* eslint-disable */",
        "// @ts-nocheck",
        "",
    ]
    sections.extend(generate_ts_validator_schema_constants(validator_schemas))
    for entry in entries:
        iface = render_codegen_entry(entry, generate_ts_interface)
        sections.append(iface)
        sections.append("")
    return "\n".join(sections)


# ────────────────────────────── Python ──────────────────────────────


def generate_py_validator_schema_constants(validator_schemas: dict[str, tuple[dict[str, Any], str]]) -> list[str]:
    """生成 Python 原始 schema JSON；计划模块惰性解析，避免 import-time 逃逸。"""
    run_spec_schema, run_spec_sha256 = validator_schemas["runSpec"]
    plan_revision_schema, plan_revision_sha256 = validator_schemas["planRevision"]
    digest_material_schema, _ = validator_schemas["planRevisionDigestMaterial"]
    run_spec_json = _embedded_schema_json(run_spec_schema)
    plan_revision_json = _embedded_schema_json(plan_revision_schema)
    digest_material_json = _embedded_schema_json(digest_material_schema)
    return [
        "# 运行时 validator 由权威 schema 机械嵌入；禁止手写字段表。",
        f'RUN_SPEC_SCHEMA_SOURCE_SHA256: Final[str] = "{run_spec_sha256}"',
        f"RUN_SPEC_SCHEMA_JSON: Final[str] = {run_spec_json!r}",
        f'RUN_SPEC_SCHEMA_JSON_SHA256: Final[str] = "{_embedded_schema_sha256(run_spec_json)}"',
        f'PLAN_REVISION_SCHEMA_SOURCE_SHA256: Final[str] = "{plan_revision_sha256}"',
        f"PLAN_REVISION_SCHEMA_JSON: Final[str] = {plan_revision_json!r}",
        f'PLAN_REVISION_SCHEMA_JSON_SHA256: Final[str] = "{_embedded_schema_sha256(plan_revision_json)}"',
        "# 仅 planRevisionDigest/signature 从摘要 material 排除，其他字段仍由权威 schema 约束。",
        (
            "PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON: Final[str] = "
            f"{digest_material_json!r}"
        ),
        (
            "PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON_SHA256: Final[str] = "
            f'"{_embedded_schema_sha256(digest_material_json)}"'
        ),
        "",
    ]


def generate_py_class(name: str, props: dict[str, Any], required: list[str]) -> str:
    """生成单个 Python TypedDict 类定义，逐字段保留 schema 的 required 语义。"""
    lines = [
        f"class {name}(TypedDict, total=False):",
        '    """由 generate.py 自动生成，禁止手动修改。"""',
    ]
    if not props:
        lines.append("    pass")
        return "\n".join(lines)
    for field, prop_def in props.items():
        is_required = field in required
        # total=False 让未包装字段保持 optional；只有 schema.required 中的字段使用
        # Required[T]，避免生成类型把安全必填字段静默降级为可省略。
        py_type = prop_py_type(prop_def)
        if is_required:
            py_type = f"Required[{py_type}]"
        desc = prop_def.get("description", "")
        if desc:
            lines.append(f"    # {desc}")
        lines.append(f"    {field}: {py_type}")
    return "\n".join(lines)


def generate_python(
    entries: list[dict[str, Any]],
    validator_schemas: dict[str, tuple[dict[str, Any], str]] | None = None,
) -> str:
    """生成完整 Python 文件内容。"""
    validator_schemas = validator_schemas or load_required_validator_schemas()
    sections = [
        '"""',
        "此模块由 contracts/codegen/generate.py 自动生成，禁止手动修改。",
        "源 schema: contracts/schemas/*.schema.json",
        "算法版本: v1",
        '"""',
        "from typing import Any, Final, Literal, Optional, Required, TypedDict, Union",
        "",
        "__all__ = [",
    ]
    # 先验证全部普通 entry；若其中一个失败，不能先构造部分 Python 文件再落盘。
    names = [load_codegen_entry_definition(entry)[0] for entry in entries]
    names = [
        "RUN_SPEC_SCHEMA_SOURCE_SHA256",
        "RUN_SPEC_SCHEMA_JSON",
        "RUN_SPEC_SCHEMA_JSON_SHA256",
        "PLAN_REVISION_SCHEMA_SOURCE_SHA256",
        "PLAN_REVISION_SCHEMA_JSON",
        "PLAN_REVISION_SCHEMA_JSON_SHA256",
        "PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON",
        "PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON_SHA256",
        *names,
    ]
    for n in names:
        sections.append(f'    "{n}",')
    sections.append("]")
    sections.append("")
    sections.extend(generate_py_validator_schema_constants(validator_schemas))
    for entry in entries:
        cls = render_codegen_entry(entry, generate_py_class)
        sections.append(cls)
        sections.append("")
    return "\n".join(sections)


# ────────────────────────────── Rust ──────────────────────────────


def generate_rs_validator_schema_constants(validator_schemas: dict[str, tuple[dict[str, Any], str]]) -> list[str]:
    """生成 Rust 运行时 validator 常量；JSON 保持原始 schema 结构并在运行时解析。"""
    run_spec_schema, run_spec_sha256 = validator_schemas["runSpec"]
    plan_revision_schema, plan_revision_sha256 = validator_schemas["planRevision"]
    digest_material_schema, _ = validator_schemas["planRevisionDigestMaterial"]
    run_spec_json = _embedded_schema_json(run_spec_schema)
    plan_revision_json = _embedded_schema_json(plan_revision_schema)
    digest_material_json = _embedded_schema_json(digest_material_schema)
    return [
        "// 运行时 validator 由权威 schema 机械嵌入；禁止手写字段表。",
        "pub const RUN_SPEC_SCHEMA_SOURCE_SHA256: &str =\n"
        f'    "{run_spec_sha256}";',
        f"pub const RUN_SPEC_SCHEMA_JSON: &str = {_rust_raw_string(run_spec_json)};",
        "pub const RUN_SPEC_SCHEMA_JSON_SHA256: &str =\n"
        f'    "{_embedded_schema_sha256(run_spec_json)}";',
        "pub const PLAN_REVISION_SCHEMA_SOURCE_SHA256: &str =\n"
        f'    "{plan_revision_sha256}";',
        (
            "pub const PLAN_REVISION_SCHEMA_JSON: &str = "
            f"{_rust_raw_string(plan_revision_json)};"
        ),
        "pub const PLAN_REVISION_SCHEMA_JSON_SHA256: &str =\n"
        f'    "{_embedded_schema_sha256(plan_revision_json)}";',
        "// 仅 planRevisionDigest/signature 从摘要 material 排除，其他字段仍由权威 schema 约束。",
        (
            "pub const PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON: &str = "
            f"{_rust_raw_string(digest_material_json)};"
        ),
        "pub const PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON_SHA256: &str =\n"
        f'    "{_embedded_schema_sha256(digest_material_json)}";',
        "",
    ]


def to_snake_case(name: str) -> str:
    """将 camelCase/PascalCase 字段名转为 snake_case。"""
    import re
    s1 = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def generate_rs_struct(name: str, props: dict[str, Any], required: list[str]) -> str:
    """生成单个 Rust struct 定义（使用 serde 注解）。"""
    lines = [
        "/// 由 generate.py 自动生成，禁止手动修改。",
        "#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]",
        "#[serde(rename_all = \"camelCase\")]",
        "#[serde(deny_unknown_fields)]",
        f"pub struct {name} {{",
    ]
    for field, prop_def in props.items():
        is_optional = field not in required
        rs_type = prop_rs_type(prop_def, optional=is_optional)
        snake = to_snake_case(field)
        desc = prop_def.get("description", "")
        if desc:
            lines.append(f"    /// {desc}")
        if snake != field:
            lines.append(f'    #[serde(rename = "{field}")]')
        if is_optional:
            lines.append("    #[serde(skip_serializing_if = \"Option::is_none\")]")
        lines.append(f"    pub {snake}: {rs_type},")
    lines.append("}")
    return "\n".join(lines)


def generate_rust(
    entries: list[dict[str, Any]],
    validator_schemas: dict[str, tuple[dict[str, Any], str]] | None = None,
) -> str:
    """生成完整 Rust 文件内容。"""
    validator_schemas = validator_schemas or load_required_validator_schemas()
    sections = [
        "//! 此模块由 contracts/codegen/generate.py 自动生成，禁止手动修改。",
        "//! 源 schema: contracts/schemas/*.schema.json",
        "//! 算法版本: v1",
        "",
        "#![allow(dead_code)]",
        "#![allow(unused_imports)]",
        "",
    ]
    sections.extend(generate_rs_validator_schema_constants(validator_schemas))
    for entry in entries:
        struct_def = render_codegen_entry(entry, generate_rs_struct)
        sections.append(struct_def)
        sections.append("")
    return "\n".join(sections)


# ────────────────────────────── main ──────────────────────────────


def content_hash(content: str) -> str:
    """计算内容 SHA-256 哈希，用于漂移检测。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def write_or_check(path: Path, content: str, check_mode: bool) -> bool:
    """
    check_mode=False 时写入文件并返回 True。
    check_mode=True 时比较内容，不一致返回 False（不修改文件）。
    """
    if check_mode:
        if not path.exists():
            print(f"[DRIFT] 文件不存在: {path}", file=sys.stderr)
            return False
        existing = path.read_text(encoding="utf-8")
        if existing != content:
            print(f"[DRIFT] 文件内容已漂移: {path}", file=sys.stderr)
            print(f"  期望 SHA-256: {content_hash(content)}", file=sys.stderr)
            print(f"  实际 SHA-256: {content_hash(existing)}", file=sys.stderr)
            return False
        return True
    # 写入模式：确保父目录存在，写入内容
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    print(f"[OK] 已写入: {path}")
    return True


def main() -> int:
    """主入口。返回 0 表示成功，非零表示失败。"""
    try:
        # --help、成功与漂移路径都会输出中文；先由入口独立建立严格 UTF-8。
        _configure_cli_text_output_utf8()
    except RuntimeError:
        _emit_cli_text_encoding_failure()
        return 2

    parser = argparse.ArgumentParser(
        description="确定性代码生成器：从 catalog.v1.json 生成三语言类型定义。"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="检查模式：校验输出是否与磁盘一致，不一致则失败且不修改文件。",
    )
    args = parser.parse_args()
    check_mode: bool = args.check

    # 加载 catalog
    try:
        catalog = load_catalog()
    except Exception as exc:
        print(f"[ERROR] 无法加载 catalog: {exc}", file=sys.stderr)
        return 1

    # 筛选 codegen=true 的条目
    codegen_entries = [s for s in catalog.get("schemas", []) if s.get("codegen") is True]
    if not codegen_entries:
        print("[ERROR] catalog 中没有 codegen=true 的条目", file=sys.stderr)
        return 1

    # 先在内存中严格加载 schema 并生成三语言完整内容；任一 validator 失败时绝不进入写入阶段。
    try:
        validator_schemas = load_required_validator_schemas()
        ts_content = generate_typescript(codegen_entries, validator_schemas)
        py_content = generate_python(codegen_entries, validator_schemas)
        rs_content = generate_rust(codegen_entries, validator_schemas)
    except ValidatorSchemaError as exc:
        print(f"[ERROR] 运行时 validator schema 生成失败: {exc}", file=sys.stderr)
        return 1
    except CodegenGenerationError:
        print("[ERROR] CODEGEN_GENERATION_FAILED", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - 内存生成阶段不能把未分类异常降级成部分写入。
        print("[ERROR] CODEGEN_GENERATION_FAILED", file=sys.stderr)
        return 1

    # 写入或检查
    try:
        results = (
            write_or_check(TS_OUT, ts_content, check_mode),
            write_or_check(PY_OUT, py_content, check_mode),
            write_or_check(RS_OUT, rs_content, check_mode),
        )
    except Exception:  # noqa: BLE001 - I/O 中途失败不得输出 [DONE]；后续 --check 会报告剩余漂移。
        print("[ERROR] CODEGEN_WRITE_FAILED", file=sys.stderr)
        return 1
    ok = all(results)

    if check_mode:
        if ok:
            print("[OK] 三语言生成树无漂移。")
        else:
            print("[FAIL] 检测到漂移，请重新运行 generate.py 更新输出。", file=sys.stderr)
        return 0 if ok else 1

    if not ok:
        print("[FAIL] 生成写入未完整完成，请使用 --check 定位漂移。", file=sys.stderr)
        return 1
    print("[DONE] 三语言类型文件生成完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
