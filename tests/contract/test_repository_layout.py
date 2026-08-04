"""
仓库布局合同测试 — 验证 monorepo 基线文件全部存在。

Task 1 验收门禁：所有工程配置文件、脚本和 GitHub Actions workflow 必须在
仓库根目录可见，否则此测试失败，阻止 Task 1 提交。
"""
from pathlib import Path


def test_required_workspace_files_exist() -> None:
    """验证必须存在的 workspace 文件集合是当前仓库文件树的子集。"""
    repo_root = Path(__file__).resolve().parents[2]
    required = {
        "package.json",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        "Cargo.toml",
        "Cargo.lock",
        "rust-toolchain.toml",
        "pyproject.toml",
        "uv.lock",
        ".node-version",
        ".github/workflows/plan-validation.yml",
        ".github/workflows/ci.yml",
        ".github/pull_request_template.md",
        "scripts/bootstrap-dev.ps1",
        "scripts/dev.ps1",
        "scripts/check.ps1",
        "scripts/test.ps1",
    }
    # 收集仓库中所有文件的相对路径（统一为正斜杠）
    actual = {
        str(path.relative_to(repo_root)).replace("\\", "/")
        for path in repo_root.rglob("*")
        if path.is_file()
    }
    missing = required - actual
    assert not missing, (
        f"以下必须文件缺失（共 {len(missing)} 项）：\n"
        + "\n".join(sorted(missing))
    )
