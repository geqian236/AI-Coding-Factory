"""
tests/conftest.py

pytest 全局 fixture 配置。
将 apps/agent/src 加入 sys.path，使 factory_agent 包可直接导入。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _ensure_factory_agent_on_path() -> None:
    """确保 apps/agent/src 在 sys.path 中以便导入 factory_agent。"""
    agent_src = str(REPO_ROOT / "apps" / "agent" / "src")
    if agent_src not in sys.path:
        sys.path.insert(0, agent_src)


# 在 conftest 加载时立即执行
_ensure_factory_agent_on_path()


def load_module_from_file(module_name: str, file_path: Path) -> ModuleType:
    """
    使用 importlib 从绝对路径加载 Python 模块（适用于目录名含连字符等无法直接 import 的情况）。
    已加载过的同名模块会被复用（缓存在 sys.modules）。
    """
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法从 {file_path} 加载模块 {module_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="session")
def emit_manifest_module() -> ModuleType:
    """加载 tools/compat-probes/emit_manifest.py 模块（目录含连字符，需 importlib 加载）。"""
    return load_module_from_file(
        "emit_manifest",
        REPO_ROOT / "tools" / "compat-probes" / "emit_manifest.py",
    )
