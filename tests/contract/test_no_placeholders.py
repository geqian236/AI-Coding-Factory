"""
tests/contract/test_no_placeholders.py

Task 8 门禁测试：扫描 docs/ 目录，确保无 TODO/FIXME/TBD/PLACEHOLDER 占位符。

此测试是 Phase 0 总门禁（check.ps1 第 9 项）的 Python 端验证，
确保所有文档在提交前已完成，无残余草稿标记。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs"

# 需要扫描的文档文件扩展名
DOC_EXTENSIONS = {".md", ".txt", ".rst"}

# 禁止出现的占位符模式（大小写不敏感）
PLACEHOLDER_PATTERN = re.compile(
    r"\b(TODO|FIXME|TBD|PLACEHOLDER)\b",
    re.IGNORECASE,
)

# 允许出现在以下子路径的文件中（超能力/规格文档是原始计划，允许占位符）
ALLOWLIST_PATHS = {
    "docs/superpowers",
}


def _is_allowlisted(path: Path) -> bool:
    """判断路径是否在允许列表中（相对于仓库根）。"""
    try:
        rel = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return False
    return any(rel.startswith(allowed) for allowed in ALLOWLIST_PATHS)


def collect_doc_files() -> list[Path]:
    """收集 docs/ 下所有需要扫描的文档文件（排除允许列表）。"""
    if not DOCS_DIR.exists():
        return []
    files = []
    for f in DOCS_DIR.rglob("*"):
        if f.is_file() and f.suffix in DOC_EXTENSIONS and not _is_allowlisted(f):
            files.append(f)
    return sorted(files)


def test_no_doc_placeholders() -> None:
    """docs/ 中所有文档（排除 superpowers/）不得包含 TODO/FIXME/TBD/PLACEHOLDER。"""
    doc_files = collect_doc_files()
    violations: list[str] = []

    for doc_path in doc_files:
        try:
            content = doc_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # 非 UTF-8 文件跳过
            continue

        for lineno, line in enumerate(content.splitlines(), start=1):
            if PLACEHOLDER_PATTERN.search(line):
                rel_path = str(doc_path.relative_to(REPO_ROOT)).replace("\\", "/")
                violations.append(f"{rel_path}:{lineno}: {line.strip()}")

    assert not violations, (
        f"docs/ 中发现 {len(violations)} 处占位符（TODO/FIXME/TBD/PLACEHOLDER）：\n"
        + "\n".join(f"  {v}" for v in violations[:20])
        + ("\n  ..." if len(violations) > 20 else "")
    )


def test_docs_protocols_dir_exists() -> None:
    """docs/protocols/ 目录必须存在（Task 8 要求）。"""
    protocols_dir = DOCS_DIR / "protocols"
    assert protocols_dir.exists(), f"docs/protocols/ 目录不存在: {protocols_dir}"
    assert protocols_dir.is_dir(), f"docs/protocols/ 不是目录: {protocols_dir}"


def test_docs_operations_dev_env_exists() -> None:
    """docs/operations/development-environment.md 必须存在（Task 8 要求）。"""
    dev_env = DOCS_DIR / "operations" / "development-environment.md"
    assert dev_env.exists(), f"development-environment.md 不存在: {dev_env}"


def test_docs_protocols_contracts_v1_exists() -> None:
    """docs/protocols/contracts-v1.md 必须存在（Task 8 要求）。"""
    contracts_v1 = DOCS_DIR / "protocols" / "contracts-v1.md"
    assert contracts_v1.exists(), f"contracts-v1.md 不存在: {contracts_v1}"


@pytest.mark.parametrize("doc_path", collect_doc_files())
def test_individual_doc_no_placeholder(doc_path: Path) -> None:
    """参数化：每个文档文件单独验证无占位符。"""
    try:
        content = doc_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        pytest.skip(f"无法以 UTF-8 读取 {doc_path}")
        return

    violations = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        if PLACEHOLDER_PATTERN.search(line):
            violations.append(f"第 {lineno} 行: {line.strip()}")

    rel_path = str(doc_path.relative_to(REPO_ROOT)).replace("\\", "/")
    assert not violations, (
        f"{rel_path} 含有占位符：\n" + "\n".join(f"  {v}" for v in violations)
    )
