"""tests.security.test_storage_contract — StorageLocationContract fail-closed 测试。

覆盖 Phase 0 要求的全部拒绝场景：
  - C / E / F 盘 fallback
  - junction / symlink 伪装（路径自身与祖先两种位置）
  - SUBST 虚拟盘
  - 网络卷 / 可移动卷 / CDROM / RAMDISK / 未知卷类型
  - 卷 identity 不可查询
  - reparse 链跨卷逃逸

真实 junction/SUBST 需要管理员权限或改动主机状态，因此用 monkeypatch
替换 Win32 调用与 os.lstat，只验证合同判定逻辑本身。
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
from factory_agent.errors import StorageViolationError
from factory_agent.security import storage_contract as sc

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="StorageLocationContract 仅支持 Windows 平台",
)

# 一条位于 D 盘、结构合法的基准路径
_GOOD_PATH = "D:\\codex项目\\AI-Coding-Factory-Data\\dev"

# 真实固定卷的 NT 设备路径样例
_REAL_DEVICE = "\\Device\\HarddiskVolume3"


@pytest.fixture
def allow_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """将全部 Win32 探测桩为「合规固定卷、无 reparse」的基线。

    个别测试在此基线上只覆盖自己关心的那一个探测，
    确保断言失败必然来自被测规则而非环境噪声。
    """
    monkeypatch.setattr(sc, "_get_drive_type", lambda root: sc._DRIVE_FIXED)
    monkeypatch.setattr(sc, "_query_dos_device", lambda letter: _REAL_DEVICE)
    monkeypatch.setattr(sc, "_is_reparse_point", lambda path: False)
    # 必须接受 strict 关键字：pathlib.Path.resolve() 会以
    # os.path.realpath(self, strict=...) 形式调用，签名不兼容会波及 pytest 内部。
    monkeypatch.setattr(os.path, "realpath", lambda p, **_kwargs: str(p))


class TestDriveLetterRules:
    """规则 3：盘符必须为 D，拒绝其他盘 fallback。"""

    @pytest.mark.parametrize("bad_path", [
        "C:\\Windows\\Temp",
        "C:\\Users\\23631\\AppData\\Local\\Temp",
        "E:\\data",
        "F:\\backup",
        "G:\\somewhere",
        "Z:\\mapped",
    ])
    def test_non_d_drive_rejected(self, allow_all: None, bad_path: str) -> None:
        """C/E/F 及任何非 D 盘路径必须 fail closed。"""
        with pytest.raises(StorageViolationError) as exc_info:
            sc.validate_storage_path(bad_path)
        assert "必须位于 D: 盘" in str(exc_info.value)

    def test_d_drive_accepted(self, allow_all: None) -> None:
        """合规 D 盘路径应返回回执。"""
        receipt = sc.validate_storage_path(_GOOD_PATH)
        assert receipt.drive == "D"
        assert receipt.drive_type == sc._DRIVE_FIXED
        assert receipt.dos_device == _REAL_DEVICE

    def test_lowercase_drive_normalized(self, allow_all: None) -> None:
        """小写盘符应归一化为大写后通过。"""
        receipt = sc.validate_storage_path("d:\\codex项目")
        assert receipt.drive == "D"

    @pytest.mark.parametrize("bad_path", [
        "\\\\server\\share\\file",       # UNC 路径无盘符
        "\\\\?\\D:\\codex项目",          # 设备命名空间前缀
        "\\\\.\\PhysicalDrive0",         # 物理设备路径
    ])
    def test_non_drive_paths_rejected(self, allow_all: None, bad_path: str) -> None:
        """UNC / 设备命名空间路径无有效盘符，必须拒绝。"""
        with pytest.raises(StorageViolationError):
            sc.validate_storage_path(bad_path)


class TestDriveTypeRules:
    """规则 4：卷类型必须为 FIXED。"""

    @pytest.mark.parametrize(("drive_type", "expected_name"), [
        (sc._DRIVE_UNKNOWN, "UNKNOWN"),
        (sc._DRIVE_NO_ROOT_DIR, "NO_ROOT_DIR"),
        (sc._DRIVE_REMOVABLE, "REMOVABLE"),
        (sc._DRIVE_REMOTE, "REMOTE"),
        (sc._DRIVE_CDROM, "CDROM"),
        (sc._DRIVE_RAMDISK, "RAMDISK"),
    ])
    def test_non_fixed_volume_rejected(
        self,
        allow_all: None,
        monkeypatch: pytest.MonkeyPatch,
        drive_type: int,
        expected_name: str,
    ) -> None:
        """网络卷、可移动卷、CDROM、RAMDISK、未知卷全部必须拒绝。"""
        monkeypatch.setattr(sc, "_get_drive_type", lambda root: drive_type)
        with pytest.raises(StorageViolationError) as exc_info:
            sc.validate_storage_path(_GOOD_PATH)
        message = str(exc_info.value)
        assert expected_name in message
        assert "仅允许 FIXED" in message


class TestSubstRules:
    """规则 5：拒绝 SUBST 虚拟盘（GetDriveTypeW 对其同样返回 FIXED）。"""

    def test_subst_drive_rejected(
        self, allow_all: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NT 设备路径带 \\??\\ 前缀说明是 SUBST，必须拒绝。"""
        monkeypatch.setattr(
            sc, "_query_dos_device", lambda letter: "\\??\\C:\\fake_d_root"
        )
        with pytest.raises(StorageViolationError) as exc_info:
            sc.validate_storage_path(_GOOD_PATH)
        assert "SUBST" in str(exc_info.value)

    def test_unqueryable_volume_rejected(
        self, allow_all: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """卷 identity 查询失败时不得放行（fail closed）。"""
        monkeypatch.setattr(sc, "_query_dos_device", lambda letter: "")
        with pytest.raises(StorageViolationError) as exc_info:
            sc.validate_storage_path(_GOOD_PATH)
        assert "卷 identity 不可信" in str(exc_info.value)


class TestReparsePointRules:
    """规则 6：路径自身与全部祖先均不得为 junction / symlink。"""

    def test_reparse_on_leaf_rejected(
        self, allow_all: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """路径末级本身是 reparse point 时必须拒绝。"""
        leaf = Path(os.path.abspath(_GOOD_PATH))
        monkeypatch.setattr(sc, "_is_reparse_point", lambda path: path == leaf)
        with pytest.raises(StorageViolationError) as exc_info:
            sc.validate_storage_path(_GOOD_PATH)
        assert "reparse point" in str(exc_info.value)
        assert "第 1 级" in str(exc_info.value)

    def test_reparse_on_ancestor_rejected(
        self, allow_all: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """祖先目录是 junction 时同样必须拒绝（防中间层伪装）。"""
        ancestor = Path(os.path.abspath(_GOOD_PATH)).parent
        monkeypatch.setattr(sc, "_is_reparse_point", lambda path: path == ancestor)
        with pytest.raises(StorageViolationError) as exc_info:
            sc.validate_storage_path(_GOOD_PATH)
        assert "reparse point" in str(exc_info.value)
        assert "第 2 级" in str(exc_info.value)

    def test_receipt_records_checked_levels(self, allow_all: None) -> None:
        """回执必须记录实际检查过的层级数，供审计复算。"""
        receipt = sc.validate_storage_path(_GOOD_PATH)
        expected = len([Path(os.path.abspath(_GOOD_PATH)), *Path(os.path.abspath(_GOOD_PATH)).parents])
        assert receipt.checked_levels == expected
        assert receipt.checked_levels >= 2


class TestReparseDetectionUsesLstat:
    """回归测试：reparse 检测必须基于未解析路径，不能先 resolve()。

    这是本模块最容易回归的缺陷——Windows 上 Path.resolve() 会跟随
    junction/symlink，先 resolve 再检测等于检测目标而非链接，
    使所有伪装路径误判为合规。
    """

    def test_is_reparse_point_reads_link_attribute(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_is_reparse_point 必须读取 lstat 的 FILE_ATTRIBUTE_REPARSE_POINT 位。"""

        class _FakeStat:
            st_file_attributes = sc._FILE_ATTRIBUTE_REPARSE_POINT

        monkeypatch.setattr(os, "lstat", lambda p: _FakeStat())
        assert sc._is_reparse_point(Path("D:\\anything")) is True

    def test_is_reparse_point_false_for_plain_dir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """普通目录不带 reparse 属性位，应返回 False。"""

        class _FakeStat:
            st_file_attributes = stat.FILE_ATTRIBUTE_DIRECTORY

        monkeypatch.setattr(os, "lstat", lambda p: _FakeStat())
        assert sc._is_reparse_point(Path("D:\\anything")) is False

    def test_is_reparse_point_false_when_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """路径不存在时不抛异常，返回 False 交由 require_exists 规则处理。"""

        def _raise(_path: object) -> None:
            raise OSError(2, "not found")

        monkeypatch.setattr(os, "lstat", _raise)
        assert sc._is_reparse_point(Path("D:\\nonexistent_xyz")) is False


class TestCrossVolumeEscape:
    """规则 7：reparse 链解析结果不得跨卷逃逸到非 D 盘。"""

    def test_escape_to_c_drive_rejected(
        self, allow_all: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """归一化路径在 D 盘但实际解析到 C 盘时必须拒绝。"""
        monkeypatch.setattr(
            os.path,
            "realpath",
            lambda p, **_kwargs: "C:\\Users\\23631\\AppData\\Local\\Temp",
        )
        with pytest.raises(StorageViolationError) as exc_info:
            sc.validate_storage_path(_GOOD_PATH)
        assert "跨卷逃逸" in str(exc_info.value)

    def test_stay_on_d_drive_accepted(
        self, allow_all: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """解析后仍在 D 盘（如经过合规目录层）应通过。"""
        monkeypatch.setattr(
            os.path, "realpath", lambda p, **_kwargs: "D:\\codex项目\\real_target"
        )
        receipt = sc.validate_storage_path(_GOOD_PATH)
        assert receipt.drive == "D"


class TestRequireExists:
    """规则 8：require_exists=True 时路径必须存在。"""

    def test_missing_path_rejected_when_required(self, allow_all: None) -> None:
        """要求存在但路径不存在时必须拒绝。"""
        with pytest.raises(StorageViolationError) as exc_info:
            sc.validate_storage_path(
                "D:\\codex项目\\__definitely_missing_dir_9f3a__",
                require_exists=True,
            )
        assert "路径不存在" in str(exc_info.value)

    def test_missing_path_allowed_by_default(self, allow_all: None) -> None:
        """默认 require_exists=False，不存在的路径只做结构校验。"""
        receipt = sc.validate_storage_path("D:\\codex项目\\__missing_but_ok__")
        assert receipt.drive == "D"


class TestReadOnlyGuarantee:
    """Phase 0 合同承诺：验证过程不得创建任何文件或目录。"""

    def test_validation_creates_nothing(self, allow_all: None) -> None:
        """对不存在的深层路径验证后，磁盘上不得出现新条目。"""
        target = "D:\\codex项目\\__probe_no_create__\\nested\\deep"
        sc.validate_storage_path(target)
        assert not Path(target).exists()
        assert not Path("D:\\codex项目\\__probe_no_create__").exists()
