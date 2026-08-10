"""factory_agent.config — D 盘根路径绑定与全局配置。

Phase 0 仅提供路径常量和 StorageLocationContract 封装；
不实现业务目录创建或 WSL/Docker 迁移逻辑（Phase 1+）。

D 盘目录约定：
  D_ROOT       = D:\\codex项目
  DATA_DIR     = D:\\codex项目\\AI-Coding-Factory-Data
  DEV_DIR      = D:\\codex项目\\AI-Coding-Factory-Data\\dev

强制约束（P0-7 fail-closed）：
  ``FACTORY_D_ROOT`` 环境变量或 ``get_d_root`` 入参覆盖的值，*必须* 经过
  ``validate_storage_path`` 的全部 8 条规则（含项目根前缀 + reparse 跨根
  逃逸）通过后才被采纳。仅当原始 ``D_ROOT`` 常量路径本身才允许跳过校验
  ——常量在源码里固化，是可信基线；外部输入都是不可信的，必须 fail closed。
"""

from __future__ import annotations

import os
from pathlib import Path

from factory_agent.errors import ConfigurationError, StorageViolationError
from factory_agent.security.storage_contract import ValidationReceipt, validate_storage_path

# ── D 盘路径常量 ──────────────────────────────────────────────────────────────

D_ROOT: Path = Path("D:/codex项目")
"""D 盘工作根目录，所有 Factory Agent 数据必须位于此目录或其子目录。"""

DATA_DIR: Path = D_ROOT / "AI-Coding-Factory-Data"
"""应用数据目录（缓存、运行时产物等）。"""

DEV_DIR: Path = DATA_DIR / "dev"
"""开发环境目录（CARGO_HOME、UV_CACHE、pnpm 等工具缓存均绑定于此）。"""


def _coerce_d_root(raw: str | os.PathLike[str]) -> Path:
    """将覆盖值解析为绝对 Path，解析失败抛 ConfigurationError。

    与 ``Path(raw).resolve()`` 相比，本函数保留 ``os.path.abspath``
    风格的纯词法归一化（不跟随 reparse point），避免先把
    ``D:\\other`` 解析成 ``D:\\codex项目\\other`` 之类的目标再误判为合规。
    实际根前缀校验交给 ``validate_storage_path``。

    Args:
        raw: 来自 override 参数或环境变量的原始路径。

    Returns:
        已词法归一化的绝对 Path。

    Raises:
        ConfigurationError: 路径无法解析时。
    """
    try:
        return Path(os.path.abspath(os.fspath(raw)))
    except Exception as exc:
        raise ConfigurationError(f"无法解析 D 盘根路径：{exc}") from exc


def get_d_root(override: str | os.PathLike[str] | None = None) -> Path:
    """获取 D 盘根路径，优先使用环境变量 FACTORY_D_ROOT 覆盖。

    优先级：
      1. 函数参数 override
      2. 环境变量 FACTORY_D_ROOT
      3. 内置常量 D_ROOT（"D:\\codex项目"）

    不变量：所有候选值（含内置 ``D_ROOT`` 常量、``FACTORY_D_ROOT``
    环境变量和 ``override`` 参数）一律通过 ``validate_storage_path``
    的完整 8 条规则（盘符 / 项目根前缀 / 卷类型 / SUBST / reparse /
    跨根逃逸），否则视为不可信输入并抛 ``ConfigurationError``（fail closed）。

    早期实现对内置常量 ``D_ROOT`` 跳过校验（理由：常量路径是唯一可信基线）。
    但若该常量本身被替换为 junction/SUBST 目标，攻击者可把项目路径
    重定向到 D:\other 之外，绕过项目根前缀。GPT 审核指明此漏洞。
    本修订一律校验；常量路径 ``D:/codex项目`` 是项目自己创建的目录，
    校验对启动性能无显著影响。

    Args:
        override: 可选路径字符串，覆盖环境变量和内置常量。

    Returns:
        已校验的绝对 Path 对象。

    Raises:
        ConfigurationError: 覆盖值无法解析或未通过存储位置合同时。
    """
    raw_value: str | os.PathLike[str] | None = override
    if raw_value is None:
        env_value = os.environ.get("FACTORY_D_ROOT")
        raw_value = env_value if env_value else D_ROOT

    candidate = _coerce_d_root(raw_value)
    # 所有候选值（含内置常量）一律经存储合同校验（fail closed）。
    # 把 ``StorageViolationError`` 翻译为 ``ConfigurationError``，
    # 因为对调用方而言这是「配置被拒」而非「存储子系统内部错误」。
    try:
        validate_storage_path(candidate)
    except StorageViolationError as exc:
        raise ConfigurationError(
            f"FACTORY_D_ROOT 候选值 '{raw_value}' 不满足 D 盘存储位置合同：{exc}"
            ) from exc
    return candidate


def validate_d_root(path: Path | None = None) -> ValidationReceipt:
    """验证 D 盘根路径满足存储位置合同。

    Args:
        path: 待验证路径，默认使用 get_d_root() 的返回值。

    Returns:
        ValidationReceipt：通过验证时的可观察事实回执。

    Raises:
        StorageViolationError: 路径不满足 D 盘固定卷要求。
        ConfigurationError:    路径无法解析或覆盖值不合规时。
    """
    target = path or get_d_root()
    return validate_storage_path(target)
