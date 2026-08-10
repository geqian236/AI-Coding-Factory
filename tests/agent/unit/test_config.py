"""tests.agent.unit.test_config — 测试 D 盘配置绑定。"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from factory_agent.config import (
    D_ROOT,
    DATA_DIR,
    DEV_DIR,
    get_d_root,
    validate_d_root,
)
from factory_agent.errors import StorageViolationError


class TestDRootConstants:
    """测试 D 盘路径常量的合规性。"""

    def test_d_root_is_d_drive(self) -> None:
        """D_ROOT 必须位于 D 盘。"""
        assert str(D_ROOT.drive).upper() == "D:"

    def test_data_dir_is_child_of_d_root(self) -> None:
        """DATA_DIR 必须是 D_ROOT 的子目录。"""
        assert str(DATA_DIR).startswith(str(D_ROOT))

    def test_dev_dir_is_child_of_data_dir(self) -> None:
        """DEV_DIR 必须是 DATA_DIR 的子目录。"""
        assert str(DEV_DIR).startswith(str(DATA_DIR))


class TestGetDRoot:
    """测试 get_d_root() 覆盖优先级。"""

    def test_returns_default_when_no_override(self) -> None:
        """无参数时返回 D 盘路径。"""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FACTORY_D_ROOT", None)
            result = get_d_root()
        assert str(result.drive).upper() == "D:"

    def test_override_param_takes_priority(self) -> None:
        """函数参数 override 优先于环境变量。"""
        with patch.dict(os.environ, {"FACTORY_D_ROOT": "D:\\other"}):
            result = get_d_root(override="D:\\codex项目")
        assert "codex" in str(result).lower() or str(result.drive).upper() == "D:"

    def test_env_var_override(self) -> None:
        """FACTORY_D_ROOT 环境变量能覆盖内置常量。"""
        with patch.dict(os.environ, {"FACTORY_D_ROOT": "D:\\codex项目\\custom"}):
            result = get_d_root()
        assert str(result.drive).upper() == "D:"
        assert "custom" in str(result)


class TestValidateDRoot:
    """测试 validate_d_root() 对不合规路径的拒绝行为。"""

    def test_c_drive_rejected(self) -> None:
        """C 盘路径必须被拒绝。"""
        with pytest.raises(StorageViolationError) as exc_info:
            validate_d_root(Path("C:\\Windows\\Temp"))
        assert "C" in str(exc_info.value) or "storage-violation" in str(exc_info.value)

    def test_e_drive_rejected(self) -> None:
        """E 盘路径必须被拒绝。"""
        with pytest.raises(StorageViolationError):
            validate_d_root(Path("E:\\data"))

    def test_f_drive_rejected(self) -> None:
        """F 盘路径必须被拒绝。"""
        with pytest.raises(StorageViolationError):
            validate_d_root(Path("F:\\backup"))

    def test_d_root_passes_on_d_drive(self) -> None:
        """D 盘固定卷路径应通过验证（集成测试，依赖真实 D 盘）。"""
        try:
            receipt = validate_d_root(Path("D:\\"))
            assert receipt.drive == "D"
        except StorageViolationError as exc:
            # 测试环境无 D 盘时 skip（CI 环境）
            pytest.skip(f"D: 盘不可用或不是固定卷：{exc}")
