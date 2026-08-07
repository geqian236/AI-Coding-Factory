"""
tests/contract/test_cargo_target_isolation.py

GPT 第九轮 P0：共享 Cargo target 破坏跨 worktree 验收隔离的回归测试。

第九轮 REVISE 根因：dev.ps1 / Set-RustGnuEnv / check.ps1 曾把所有 worktree 的
CARGO_TARGET_DIR 都指向同一个 <DATA_ROOT>\\cargo-target；而 Rust 测试二进制通过
env!("CARGO_MANIFEST_DIR")（见 crates/factory-contracts/tests/event_vectors.rs）把
**编译期 worktree 路径**烧进自身并据此读 contracts/golden/*.json。共享 target 下
worktree B 会跑 worktree A 编译的二进制：A 被删则 B 因路径消失假失败；A 尚在则 B
实际执行另一 checkout 的代码、读其 golden，产出错误验收证据。

修复：scripts/_worktree-target.ps1 的 Get-WorktreeTargetDir 按**规范化 worktree 根**
的 SHA-256 摘要派生 <DATA_ROOT>\\cargo-target\\<digest>，每个 checkout 独立。
本测试锁死该隔离不回退：两个不同 worktree 根 + **同一** DATA_ROOT 必须映射到
**不同** target namespace，且都在共享 cargo-target 根下、对同一路径确定性一致、
大小写/尾斜杠规范化到同一摘要。

测试通过 PowerShell subprocess 调用 Get-WorktreeTargetDir（单一真源，与 5 个调用方
同一函数），仅在 Windows 上运行（PS 5.1 / pwsh 7 可用）；CI windows-probes job 执行。
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER_PS1 = REPO_ROOT / "scripts" / "_worktree-target.ps1"
TEST_PS1 = REPO_ROOT / "scripts" / "test.ps1"

# 固定的合成 DATA_ROOT（含 CJK，复刻真实项目根形态；不落地、仅参与纯字符串摘要）。
DATA_ROOT = r"D:\codex项目\AI-Coding-Factory-Data\dev"
SHARED_CARGO_TARGET = DATA_ROOT + r"\cargo-target"


def _target_dir(worktree_root: str, data_root: str = DATA_ROOT) -> str:
    """通过 PS subprocess 调用 Get-WorktreeTargetDir，返回其派生的 target 路径。

    经 .NET UTF8 编码写子进程管道再回读：为避免 GBK 代码页在 stdout 上损坏 CJK 字节，
    用 [Convert]::ToBase64String(UTF8 bytes) 传出，Python 侧 base64 解码为 str，
    这样比较的是内存中真实的 .NET 路径字符串，与摘要计算所用字节完全同源。
    """
    ps_script = f"""
. '{HELPER_PS1}'
$t = Get-WorktreeTargetDir -WorktreeRoot '{worktree_root}' -DataRoot '{data_root}'
$b = [System.Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($t))
Write-Output "B64=$b"
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO_ROOT), timeout=30,
    )
    assert result.returncode == 0, (
        f"Get-WorktreeTargetDir 调用失败: rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    line = next((ln for ln in result.stdout.splitlines() if ln.startswith("B64=")), None)
    assert line is not None, f"未取到 B64= 输出行, stdout={result.stdout!r}"
    return base64.b64decode(line[len("B64="):]).decode("utf-8")


@pytest.mark.skipif(sys.platform != "win32", reason="Get-WorktreeTargetDir 需 Windows PowerShell")
def test_two_worktrees_share_data_root_but_differ_in_target_namespace() -> None:
    """两 worktree 共用同一 DATA_ROOT，但 target namespace 必须不同（隔离核心断言）。

    这正是第九轮 reviewer 的复现场景：两个干净 detached worktree 共享数据根。修复后
    二者的 CARGO_TARGET_DIR 落在同一个 cargo-target 下的**不同**摘要子目录，故一个
    worktree 绝不会跑到另一个编译的二进制或读其 golden。
    """
    a = _target_dir(r"D:\codex项目\.codex-worktrees\factory-phase-0")
    b = _target_dir(r"D:\codex项目\.codex-worktrees\factory-phase-0-r9b")

    # 都在共享 cargo-target 根下（共用 DATA_ROOT，工具链/缓存仍共享，仅产物隔离）。
    assert a.startswith(SHARED_CARGO_TARGET + "\\"), f"A 不在共享 cargo-target 下: {a}"
    assert b.startswith(SHARED_CARGO_TARGET + "\\"), f"B 不在共享 cargo-target 下: {b}"
    # namespace 必须不同：否则回退到共享 target，隔离失效。
    assert a != b, f"两 worktree 的 target 相同（隔离失效）: {a}"
    # 摘要段（cargo-target 之后的叶子）必须不同。
    leaf_a = a[len(SHARED_CARGO_TARGET) + 1:]
    leaf_b = b[len(SHARED_CARGO_TARGET) + 1:]
    assert leaf_a != leaf_b, f"摘要叶子相同: {leaf_a}"
    assert len(leaf_a) == 12 and len(leaf_b) == 12, f"摘要应为 12 hex: {leaf_a} / {leaf_b}"


@pytest.mark.skipif(sys.platform != "win32", reason="Get-WorktreeTargetDir 需 Windows PowerShell")
def test_target_namespace_is_deterministic_and_trailing_slash_normalized() -> None:
    """同一 worktree 根确定性一致；尾斜杠规范化到同一 namespace。

    确定性保证同一 worktree 的 cargo build 与 wrapper 取 exe 命中同一 target
    （所有调用方都从各自 $PSScriptRoot 派生同一 worktree 根，路径来源一致）；
    尾斜杠规范化（GetFullPath + TrimEnd）保证 ...\\tree 与 ...\\tree\\ 视为同一树。
    """
    canonical = _target_dir(r"D:\codex项目\.codex-worktrees\factory-phase-0")
    again = _target_dir(r"D:\codex项目\.codex-worktrees\factory-phase-0")
    trailing = _target_dir("D:\\codex项目\\.codex-worktrees\\factory-phase-0\\")

    assert canonical == again, f"非确定性: {canonical} != {again}"
    assert canonical == trailing, (
        f"尾斜杠未规范化到同一 namespace: {canonical} != {trailing}"
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Get-WorktreeTargetDir 需 Windows PowerShell")
def test_case_variant_paths_get_distinct_namespaces() -> None:
    """GPT 第十轮 P1：大小写不同的路径必须映射到**不同** namespace（碰撞修复）。

    旧实现无条件 ToLowerInvariant()，在启用 case sensitivity 的 Windows 目录下会把
    两个真实不同的 worktree（如 .../tree 与 .../TREE）折叠到同一 target namespace →
    互相污染编译产物。修复后摘要基于**未强制小写**的规范化路径，故大小写不同的根
    得到不同摘要。所有调用方从同一 $PSScriptRoot 源派生，天然大小写一致，不会因此
    分裂同一棵树。
    """
    lower = _target_dir(r"D:\codex项目\.codex-worktrees\factory-phase-0")
    upper = _target_dir(r"D:\CODEX项目\.codex-worktrees\FACTORY-PHASE-0")

    assert lower != upper, (
        f"大小写不同的路径不应折叠到同一 namespace（碰撞）: {lower} == {upper}"
    )


TEST_PS1 = REPO_ROOT / "scripts" / "test.ps1"


def _project_data_root() -> str | None:
    """从 REPO_ROOT 向上找含 AI-Coding-Factory-Data 的项目根，返回 <root>\\AI-Coding-Factory-Data\\dev。

    与 test.ps1 / check.ps1 内的项目根定位逻辑同源。找不到返回 None（测试将 skip）。
    """
    probe = REPO_ROOT
    while True:
        if (probe / "AI-Coding-Factory-Data").exists():
            return str(probe / "AI-Coding-Factory-Data" / "dev")
        parent = probe.parent
        if parent == probe:
            return None
        probe = parent


@pytest.mark.skipif(sys.platform != "win32", reason="test.ps1 是 Windows PowerShell 入口")
def test_test_ps1_overrides_inherited_stale_cargo_target_dir() -> None:
    """GPT 第十轮 P1（调用方级回归）：test.ps1 必须**强制**用 per-worktree namespace
    覆盖任何继承的（尤其是旧版**共享**）CARGO_TARGET_DIR，绝不沿用可疑继承值。

    复现威胁：父进程残留 CARGO_TARGET_DIR=<DATA_ROOT>\\cargo-target（旧版共享目录）。
    若 test.ps1 不覆盖，它的 cargo test 会跑到另一 worktree 编译的二进制并读其 golden。
    本测试给子进程注入一个 bogus 共享值，调用 `test.ps1 -PrintTargetDir`（只设 namespace
    并打印、不跑 cargo），断言打印值 == Get-WorktreeTargetDir(REPO_ROOT, 真实 DataRoot)，
    且 != 注入的 bogus 值，证明覆盖生效。
    """
    data_root = _project_data_root()
    if data_root is None:
        pytest.skip("REPO_ROOT 无 AI-Coding-Factory-Data 祖先（非真实项目布局）")

    # 期望值：test.ps1 应把 CARGO_TARGET_DIR 设为本 worktree 根 + 真实 DataRoot 的派生 target。
    expected = _target_dir(str(REPO_ROOT), data_root=data_root)

    # 注入一个 bogus 的旧版**共享** target 作为继承值（test.ps1 必须覆盖它）。
    bogus_shared = data_root + r"\cargo-target"
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = bogus_shared

    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(TEST_PS1), "-PrintTargetDir"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO_ROOT), timeout=60, env=env,
    )
    assert result.returncode == 0, (
        f"test.ps1 -PrintTargetDir 失败: rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    line = next((ln for ln in result.stdout.splitlines() if ln.startswith("TARGETDIR_B64=")), None)
    assert line is not None, f"未取到 TARGETDIR_B64= 输出行, stdout={result.stdout!r}"
    actual = base64.b64decode(line[len("TARGETDIR_B64="):]).decode("utf-8")

    assert actual == expected, (
        f"test.ps1 未把 CARGO_TARGET_DIR 设为 per-worktree namespace: "
        f"actual={actual} expected={expected}"
    )
    assert actual != bogus_shared, (
        f"test.ps1 沿用了继承的共享 CARGO_TARGET_DIR（未隔离）: {actual}"
    )
