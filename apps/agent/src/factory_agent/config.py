"""factory_agent.config — D 盘根路径绑定与全局配置。

Phase 0 仅提供路径常量和 StorageLocationContract 封装；
不实现业务目录创建或 WSL/Docker 迁移逻辑（Phase 1+）。

D 盘目录约定：
  D_ROOT       = D:\\codex项目
  DATA_DIR     = D:\\codex项目\\AI-Coding-Factory-Data
  DEV_DIR      = D:\\codex项目\\AI-Coding-Factory-Data\\dev
"""

from __future__ import annotations

import os
from pathlib import Path

from factory_agent.errors import ConfigurationError
from factory_agent.security.storage_contract import ValidationReceipt, validate_storage_path

# ── D 盘路径常量 ──────────────────────────────────────────────────────────────

D_ROOT: Path = Path("D:/codex项目")
"""D 盘工作根目录，所有 Factory Agent 数据必须位于此目录或其子目录。"""

DATA_DIR: Path = D_ROOT / "AI-Coding-Factory-Data"
"""应用数据目录（缓存、运行时产物等）。"""

DEV_DIR: Path = DATA_DIR / "dev"
"""开发环境目录（CARGO_HOME、UV_CACHE、pnpm 等工具缓存均绑定于此）。"""


def get_d_root(override: str | None = None) -> Path:
    """获取 D 盘根路径，优先使用环境变量 FACTORY_D_ROOT 覆盖。

    优先级：
      1. 函数参数 override
      2. 环境变量 FACTORY_D_ROOT
      3. 内置常量 D_ROOT（"D:\\codex项目"）

    Args:
        override: 可选路径字符串，覆盖环境变量和内置常量。

    Returns:
        已解析的绝对 Path 对象。

    Raises:
        ConfigurationError: 路径无法解析时。
    """
    raw = override or os.environ.get("FACTORY_D_ROOT") or str(D_ROOT)
    try:
        return Path(raw).resolve()
    except Exception as exc:
        raise ConfigurationError(f"无法解析 D 盘根路径：{exc}") from exc


def validate_d_root(path: Path | None = None) -> ValidationReceipt:
    """验证 D 盘根路径满足存储位置合同。

    Args:
        path: 待验证路径，默认使用 get_d_root() 的返回值。

    Returns:
        ValidationReceipt：通过验证时的可观察事实回执。

    Raises:
        StorageViolationError: 路径不满足 D 盘固定卷要求。
        ConfigurationError:    路径无法解析时。
    """
    target = path or get_d_root()
    return validate_storage_path(target)
