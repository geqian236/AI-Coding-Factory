"""
scripts/_gate_checks.py

Phase 0 总门禁辅助脚本 — 实现 check.ps1 中难以用纯 PowerShell 完成的检查。

用法：
  python scripts/_gate_checks.py <check_name>

check_name 可选值：
  chinese-coverage   检查公共 Python 模块中文注释覆盖
  no-bare-print      检查非测试代码中无裸 print/console.log
  secret-scan        秘密扫描（token/password/private_key 赋值）
  c-drive-paths      C 盘路径引用扫描

算法版本: v1
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


# ──────────────────────────── 检查 4：中文注释覆盖 ────────────────────────────


def check_chinese_coverage() -> int:
    """
    扫描 apps/agent/src 下的非生成、非测试 Python 文件，
    确保每个含 def/class 的文件至少包含一个中文字符。
    fail-closed：apps/agent/src 不存在或为空即视为缺失，return 1。
    """
    src_dir = REPO_ROOT / "apps" / "agent" / "src"
    if not src_dir.exists():
        print(f"FAIL: 中文覆盖门禁要求 apps/agent/src 存在: {src_dir}", file=sys.stderr)
        return 1

    missing = []
    for py_file in sorted(src_dir.rglob("*.py")):
        # 排除 __pycache__、generated、测试文件
        parts = py_file.parts
        if any(p in parts for p in ("__pycache__", "generated")):
            continue
        name = py_file.name
        if name.startswith("test_") or name.endswith("_test.py"):
            continue
        try:
            source = py_file.read_text(encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            print(f"  警告: 无法读取 {py_file}: {e}", file=sys.stderr)
            continue
        # 检查是否有 def 或 class 语句
        has_def = any(
            line.strip().startswith(("def ", "class "))
            for line in source.splitlines()
        )
        if not has_def:
            continue
        # 检查是否含中文字符
        if not any("\u4e00" <= c <= "\u9fa5" for c in source):
            rel = str(py_file.relative_to(REPO_ROOT)).replace("\\", "/")
            missing.append(rel)

    if missing:
        print(
            f"FAIL: {len(missing)} 个文件缺少中文注释/docstring:",
            file=sys.stderr,
        )
        for m in missing:
            print(f"  {m}", file=sys.stderr)
        return 1

    print(f"OK: 中文覆盖检查通过（已扫描 {src_dir}）")
    return 0


# ──────────────────────────── 检查 5：无裸 print/console.log ────────────────────


def _is_cli_entrypoint(source: str) -> bool:
    """判断 Python 源文件是否为 CLI 入口点（使用 argparse 或 click）。
    CLI 入口点的 print() 用于向用户输出结果，属于允许的用法。
    """
    return "import argparse" in source or "from argparse" in source or "import click" in source


def check_no_bare_print() -> int:
    """
    扫描非测试源码，确保无裸 print()/console.log()。
    Python 文件在 apps/agent/src，TypeScript 文件在 packages/ 下。
    CLI 入口点文件（使用 argparse/click）中的 print() 为合法的用户输出，不计入违规。
    """
    print_pattern = re.compile(r"\bprint\s*\(")
    console_pattern = re.compile(r"\bconsole\.(log|info|warn|error)\s*\(")
    violations: list[str] = []

    # Python
    py_src = REPO_ROOT / "apps" / "agent" / "src"
    if py_src.exists():
        # fail-closed：目录存在但无 .py 文件同样视为违规（避免空目录伪装 PASS）
        py_files = sorted(py_src.rglob("*.py"))
        if not py_files:
            print(f"FAIL: apps/agent/src 存在但无 .py 源码", file=sys.stderr)
            return 1
        for py_file in py_files:
            parts = py_file.parts
            if any(p in parts for p in ("__pycache__", "generated")):
                continue
            name = py_file.name
            if name.startswith("test_") or name.endswith("_test.py"):
                continue
            try:
                source = py_file.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                continue
            # CLI 入口点文件中的 print() 用于用户输出，允许使用
            if _is_cli_entrypoint(source):
                continue
            for i, line in enumerate(source.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if print_pattern.search(line):
                    rel = str(py_file.relative_to(REPO_ROOT)).replace("\\", "/")
                    violations.append(f"{rel}:{i}: {stripped[:100]}")

    # TypeScript
    ts_src = REPO_ROOT / "packages"
    if ts_src.exists():
        ts_files_all = sorted(ts_src.rglob("*.ts"))
        # 排除 node_modules 与测试
        ts_files = [
            f for f in ts_files_all
            if "node_modules" not in f.parts
            and ".test." not in f.name
            and ".spec." not in f.name
        ]
        if not ts_files:
            # 没有受管 TS 源码（仅 node_modules）也算 fail-closed（防止空仓库伪装）
            print(f"FAIL: packages/ 下无受管 .ts 源码（仅 node_modules 不算）", file=sys.stderr)
            return 1
        for ts_file in ts_files:
            # node_modules 是第三方依赖,不属于本项目源码,跳过
            if "node_modules" in ts_file.parts:
                continue
            name = ts_file.name
            if ".test." in name or ".spec." in name:
                continue
            try:
                source = ts_file.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                continue
            for i, line in enumerate(source.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("//"):
                    continue
                if console_pattern.search(line):
                    rel = str(ts_file.relative_to(REPO_ROOT)).replace("\\", "/")
                    violations.append(f"{rel}:{i}: {stripped[:100]}")

    if violations:
        print(
            f"FAIL: 发现 {len(violations)} 处裸 print/console.log:",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        return 1

    print("OK: 未发现测试外的裸输出语句")
    return 0


# ──────────────────────────── 检查 6：秘密扫描 ──────────────────────────────────


def check_secret_scan() -> int:
    """
    扫描非测试源码，确保无实际秘密值赋值。
    匹配模式：<secret_name> = "<value>" 形式的赋值语句。
    """
    # 匹配 secret 名称赋给字符串字面量（跳过注释行）
    pattern = re.compile(
        r"\b(token|password|private_key|api_key|secret_key|secret)\s*=\s*['\"][^'\"]{4,}['\"]",
        re.IGNORECASE,
    )
    violations: list[str] = []

    search_dirs = [
        REPO_ROOT / "apps" / "agent" / "src",
        REPO_ROOT / "packages" / "factory-contracts" / "src",
        REPO_ROOT / "crates" / "factory-contracts" / "src",
    ]

    # fail-closed：所有 search_dir 必须存在；缺任一即视为扫描不全，整体红。
    missing_dirs = [str(d.relative_to(REPO_ROOT)) for d in search_dirs if not d.exists()]
    if missing_dirs:
        print(
            f"FAIL: 秘密扫描范围目录缺失: {', '.join(missing_dirs)}",
            file=sys.stderr,
        )
        return 1
    scanned_any = False
    for search_dir in search_dirs:
        for src_file in sorted(search_dir.rglob("*")):
            if not src_file.is_file():
                continue
            if src_file.suffix not in (".py", ".ts", ".rs"):
                continue
            parts = src_file.parts
            name = src_file.name
            if any(p in parts for p in ("__pycache__", "generated")):
                continue
            if name.startswith("test_") or name.endswith(("_test.py", ".spec.ts")):
                continue
            try:
                source = src_file.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                continue
            scanned_any = True
            for i, line in enumerate(source.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith(("#", "//", "/*", "*", "//!")):
                    continue
                if pattern.search(line):
                    rel = str(src_file.relative_to(REPO_ROOT)).replace("\\", "/")
                    violations.append(f"{rel}:{i}: {stripped[:100]}")

    if not scanned_any:
        print("FAIL: 秘密扫描未覆盖任何源码文件（目录存在但为空）", file=sys.stderr)
        return 1
    if violations:
        print(
            f"FAIL: 发现 {len(violations)} 处疑似秘密泄露:",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        return 1

    print("OK: 秘密扫描通过（0 命中）")
    return 0


# ──────────────────────────── 检查 7：C 盘路径 ──────────────────────────────────


def check_c_drive_paths() -> int:
    """
    扫描非测试源码，确保无可控 C 盘路径引用。
    C:\\Windows 和 C:\\Program Files 的只读引用允许例外。
    """
    pattern = re.compile(r"[Cc]:[/\\](?!(Windows|Program Files))", re.IGNORECASE)
    violations: list[str] = []

    search_dirs = [
        REPO_ROOT / "apps" / "agent" / "src",
        REPO_ROOT / "packages" / "factory-contracts" / "src",
        REPO_ROOT / "crates" / "factory-contracts" / "src",
    ]

    # fail-closed：所有 search_dir 必须存在
    missing_dirs = [str(d.relative_to(REPO_ROOT)) for d in search_dirs if not d.exists()]
    if missing_dirs:
        print(
            f"FAIL: C 盘路径扫描范围目录缺失: {', '.join(missing_dirs)}",
            file=sys.stderr,
        )
        return 1
    scanned_any = False
    for search_dir in search_dirs:
        for src_file in sorted(search_dir.rglob("*")):
            if not src_file.is_file():
                continue
            if src_file.suffix not in (".py", ".ts", ".rs"):
                continue
            parts = src_file.parts
            name = src_file.name
            if any(p in parts for p in ("__pycache__", "generated")):
                continue
            if name.startswith("test_") or name.endswith(("_test.py", ".spec.ts")):
                continue
            try:
                source = src_file.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                continue
            scanned_any = True
            for i, line in enumerate(source.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith(("#", "//", "/*", "*", "//!")):
                    continue
                if pattern.search(line):
                    rel = str(src_file.relative_to(REPO_ROOT)).replace("\\", "/")
                    violations.append(f"{rel}:{i}: {stripped[:100]}")

    if not scanned_any:
        print("FAIL: C 盘路径扫描未覆盖任何源码文件（目录存在但为空）", file=sys.stderr)
        return 1
    if violations:
        print(
            f"FAIL: 发现 {len(violations)} 处 C 盘路径引用:",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        return 1

    print("OK: C 盘路径检查通过")
    return 0


# ──────────────────────────── 主入口 ─────────────────────────────────────────


_CHECKS: dict[str, object] = {
    "chinese-coverage": check_chinese_coverage,
    "no-bare-print": check_no_bare_print,
    "secret-scan": check_secret_scan,
    "c-drive-paths": check_c_drive_paths,
}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in _CHECKS:
        print(
            f"Usage: python scripts/_gate_checks.py <check_name>\n"
            f"Available: {', '.join(_CHECKS)}",
            file=sys.stderr,
        )
        return 2
    check_fn = _CHECKS[sys.argv[1]]
    return check_fn()  # type: ignore[operator]


if __name__ == "__main__":
    sys.exit(main())
