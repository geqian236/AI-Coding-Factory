"""
contracts/codegen/generate.py

确定性代码生成器：从 catalog.v1.json 读取 codegen=true 的 schema，
生成 TypeScript、Python、Rust 三语言类型定义。

算法版本：v1
支持 --check 标志：检查输出是否与当前磁盘文件一致，不一致则报错且不修改文件。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import textwrap
from pathlib import Path
from typing import Any

# 仓库根目录（相对于本脚本所在 contracts/codegen/）
REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = REPO_ROOT / "contracts" / "codegen" / "catalog.v1.json"

# 生成目标路径（来自 catalog.v1.json outputTargets）
TS_OUT = REPO_ROOT / "packages" / "factory-contracts" / "src" / "generated" / "contracts.ts"
PY_OUT = REPO_ROOT / "apps" / "agent" / "src" / "factory_agent" / "contracts" / "generated" / "models.py"
RS_OUT = REPO_ROOT / "crates" / "factory-contracts" / "src" / "generated" / "contracts.rs"

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


def prop_py_type(prop: dict[str, Any], optional: bool = False) -> str:
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
    result = PY_TYPE_MAP.get(str(prop_type), "Any")
    if optional:
        return f"Optional[{result}]"
    return result


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


# ────────────────────────────── TypeScript ──────────────────────────────


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


def generate_typescript(entries: list[dict[str, Any]]) -> str:
    """生成完整 TypeScript 文件内容。"""
    sections = [
        "// 此文件由 contracts/codegen/generate.py 自动生成，禁止手动修改。",
        "// 源 schema: contracts/schemas/*.schema.json",
        "// 算法版本: v1",
        "",
        "/* eslint-disable */",
        "// @ts-nocheck",
        "",
    ]
    for entry in entries:
        try:
            schema = load_schema(entry["schemaPath"])
            definition_key: str | None = entry.get("definitionKey")
            props, required = get_schema_properties(schema, definition_key)
            iface = generate_ts_interface(entry["name"], props, required)
            sections.append(iface)
            sections.append("")
        except Exception as exc:  # noqa: BLE001
            sections.append(f"// ERROR generating {entry['name']}: {exc}")
            sections.append("")
    return "\n".join(sections)


# ────────────────────────────── Python ──────────────────────────────


def generate_py_class(name: str, props: dict[str, Any], required: list[str]) -> str:
    """生成单个 Python TypedDict 类定义。"""
    lines = [
        f"class {name}(TypedDict, total=False):",
        f'    """由 generate.py 自动生成，禁止手动修改。"""',
    ]
    if not props:
        lines.append("    pass")
        return "\n".join(lines)
    for field, prop_def in props.items():
        is_required = field in required
        py_type = prop_py_type(prop_def, optional=not is_required)
        desc = prop_def.get("description", "")
        if desc:
            lines.append(f"    # {desc}")
        lines.append(f"    {field}: {py_type}")
    return "\n".join(lines)


def generate_python(entries: list[dict[str, Any]]) -> str:
    """生成完整 Python 文件内容。"""
    sections = [
        '"""',
        "此模块由 contracts/codegen/generate.py 自动生成，禁止手动修改。",
        "源 schema: contracts/schemas/*.schema.json",
        "算法版本: v1",
        '"""',
        "from __future__ import annotations",
        "",
        "from typing import Any, Literal, Optional, TypedDict, Union",
        "",
        "__all__ = [",
    ]
    names = [e["name"] for e in entries]
    for n in names:
        sections.append(f'    "{n}",')
    sections.append("]")
    sections.append("")
    for entry in entries:
        try:
            schema = load_schema(entry["schemaPath"])
            definition_key: str | None = entry.get("definitionKey")
            props, required = get_schema_properties(schema, definition_key)
            cls = generate_py_class(entry["name"], props, required)
            sections.append(cls)
            sections.append("")
        except Exception as exc:  # noqa: BLE001
            sections.append(f"# ERROR generating {entry['name']}: {exc}")
            sections.append("")
    return "\n".join(sections)


# ────────────────────────────── Rust ──────────────────────────────


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


def generate_rust(entries: list[dict[str, Any]]) -> str:
    """生成完整 Rust 文件内容。"""
    sections = [
        "//! 此模块由 contracts/codegen/generate.py 自动生成，禁止手动修改。",
        "//! 源 schema: contracts/schemas/*.schema.json",
        "//! 算法版本: v1",
        "",
        "#![allow(dead_code)]",
        "#![allow(unused_imports)]",
        "",
    ]
    for entry in entries:
        try:
            schema = load_schema(entry["schemaPath"])
            definition_key: str | None = entry.get("definitionKey")
            props, required = get_schema_properties(schema, definition_key)
            struct_def = generate_rs_struct(entry["name"], props, required)
            sections.append(struct_def)
            sections.append("")
        except Exception as exc:  # noqa: BLE001
            sections.append(f"// ERROR generating {entry['name']}: {exc}")
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

    # 生成各语言内容
    ts_content = generate_typescript(codegen_entries)
    py_content = generate_python(codegen_entries)
    rs_content = generate_rust(codegen_entries)

    # 写入或检查
    ok = True
    ok = write_or_check(TS_OUT, ts_content, check_mode) and ok
    ok = write_or_check(PY_OUT, py_content, check_mode) and ok
    ok = write_or_check(RS_OUT, rs_content, check_mode) and ok

    if check_mode:
        if ok:
            print("[OK] 三语言生成树无漂移。")
        else:
            print("[FAIL] 检测到漂移，请重新运行 generate.py 更新输出。", file=sys.stderr)
        return 0 if ok else 1

    print("[DONE] 三语言类型文件生成完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
