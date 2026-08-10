"""factory_agent.security.storage_contract — D 盘存储位置安全合同。

Phase 0 职责（只读验证，不创建目录、不写入文件）：
  - 验证路径必须位于 D 盘固定卷
  - 验证路径必须位于 D 盘项目根 (``D:\\codex项目``) 严格内部，
    拒绝 ``D:\\codex项目-evil`` 等前缀混淆（按 path segments normcase 分段比较）
  - 拒绝 C/E/F 及其他非 D 盘 fallback 路径
  - 拒绝 junction / symlink 伪装（检查路径自身与全部祖先）
  - 拒绝 SUBST 虚拟盘（QueryDosDeviceW 返回 \\??\\ 前缀）
  - 拒绝网络卷 (DRIVE_REMOTE) 与可移动卷 (DRIVE_REMOVABLE)
  - 拒绝未知卷类型 (DRIVE_UNKNOWN / DRIVE_NO_ROOT_DIR / CDROM / RAMDISK)
  - 拒绝 reparse 链跨卷逃逸（解析后落到非 D 盘、或落到 D 盘项目根之外）
  - 拒绝 reparse 解析后落到 D 盘项目根前缀之外的路径（重定向逃逸）

设计要点：绝不能对入参先做 ``Path.resolve()`` 再检测 reparse point。
Windows 上 ``resolve()`` 会跟随 junction/symlink，检测目标而非链接本身，
使伪装路径全部误判为合规。因此本模块先用 ``os.path.abspath``
做纯词法归一化（不触碰文件系统），再逐级检查 reparse 属性。

存储根前缀校验同样必须按 segments + normcase 比较，单靠 ``startswith``
会被 ``D:\\codex项目-evil`` 绕过。规则 7 的 realpath 结果也必须经同一
根前缀校验（防止 reparse 链把项目根内的链接解析到项目根外）。

Phase 1+ 才添加实际写入、卷 identity 持久化与 WSL/Docker 迁移逻辑。
"""

from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from factory_agent.errors import StorageViolationError

# ── Windows 卷类型常量（GetDriveTypeW 返回值）────────────────────────────────
_DRIVE_UNKNOWN = 0      # 未知卷类型
_DRIVE_NO_ROOT_DIR = 1  # 根目录不存在
_DRIVE_REMOVABLE = 2    # 可移动介质（U 盘等）
_DRIVE_FIXED = 3        # 固定磁盘
_DRIVE_REMOTE = 4       # 网络驱动器
_DRIVE_CDROM = 5        # CD-ROM
_DRIVE_RAMDISK = 6      # RAM 盘

# 卷类型名称表（仅用于错误消息，不含敏感信息）
_DRIVE_TYPE_NAMES: dict[int, str] = {
    _DRIVE_UNKNOWN: "UNKNOWN",
    _DRIVE_NO_ROOT_DIR: "NO_ROOT_DIR",
    _DRIVE_REMOVABLE: "REMOVABLE",
    _DRIVE_FIXED: "FIXED",
    _DRIVE_REMOTE: "REMOTE",
    _DRIVE_CDROM: "CDROM",
    _DRIVE_RAMDISK: "RAMDISK",
}

# 接受的卷类型仅限固定磁盘
_ALLOWED_DRIVE_TYPES: frozenset[int] = frozenset({_DRIVE_FIXED})

# 必须驻留在 D 盘
_REQUIRED_DRIVE = "D"

# 项目工作根（Phase 0 强约束）：所有写入路径必须严格位于此目录或其一
# 级后代之内。该目录位于 D 盘固定卷上；将根校验下沉到模块常量是为了让
# ``config.get_d_root()`` 与 ``validate_storage_path`` 共用同一基线，
# 避免环境变量或参数覆盖把工作根挪到 ``D:\\other`` 这类合规但不在项目内
# 的位置（合规盘符 ≠ 合规工作根）。
_REQUIRED_ROOT: Path = Path("D:/codex项目")

# Windows FILE_ATTRIBUTE_REPARSE_POINT（junction / symlink 均置此位）
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400

# SUBST 虚拟盘的 NT 设备路径前缀；真实卷为 \Device\HarddiskVolumeN
_SUBST_DEVICE_PREFIX = "\\??\\"

# QueryDosDeviceW 输出缓冲区大小（NT 路径远小于此值）
_DOS_DEVICE_BUFFER_CHARS = 1024


@dataclass(frozen=True)
class ValidationReceipt:
    """路径验证回执，记录验证通过时的可观察事实。

    Attributes:
        path:          词法归一化后的绝对路径（未跟随 reparse point）。
        drive:         盘符（大写，如 "D"）。
        drive_type:    GetDriveTypeW 返回值。
        dos_device:    QueryDosDeviceW 返回的 NT 设备路径。
        checked_levels: 已检查 reparse 属性的路径层级数（自身 + 祖先）。
    """

    path: Path
    drive: str
    drive_type: int
    dos_device: str
    checked_levels: int


def _get_drive_type(drive_root: str) -> int:
    """调用 Win32 GetDriveTypeW 获取卷类型。

    Args:
        drive_root: 格式为 ``"D:\\"`` 的驱动器根路径。

    Returns:
        GetDriveTypeW 的返回值（整型），见 _DRIVE_* 常量。
    """
    # windll 仅在 Windows 上存在；unused-ignore 保证在 Windows 本地 mypy 下
    # 不会因该 ignore 多余而报错，同时保留 Linux CI 上的 attr-defined 抑制。
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined,unused-ignore]
    return int(kernel32.GetDriveTypeW(drive_root))


def _query_dos_device(drive_letter: str) -> str:
    """查询盘符对应的 NT 设备路径，用于识别 SUBST 虚拟盘。

    真实固定卷返回形如 ``\\Device\\HarddiskVolume3``；
    ``subst X: D:\\foo`` 创建的虚拟盘返回形如 ``\\??\\D:\\foo``。
    GetDriveTypeW 对 SUBST 盘同样返回 FIXED，故必须额外用本函数区分。

    Args:
        drive_letter: 单个盘符字母（如 "D"），不含冒号。

    Returns:
        NT 设备路径字符串；查询失败时返回空字符串（由调用方判定为不可信）。
    """
    # windll 仅在 Windows 上存在；unused-ignore 保证在 Windows 本地 mypy 下
    # 不会因该 ignore 多余而报错，同时保留 Linux CI 上的 attr-defined 抑制。
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined,unused-ignore]
    buffer = ctypes.create_unicode_buffer(_DOS_DEVICE_BUFFER_CHARS)
    written = kernel32.QueryDosDeviceW(
        f"{drive_letter}:", buffer, _DOS_DEVICE_BUFFER_CHARS
    )
    if not written:
        return ""
    return buffer.value


def _is_reparse_point(path: Path) -> bool:
    """检测单个路径是否为 reparse point（junction 或 symlink）。

    使用 ``os.lstat`` 而非 ``os.stat``，以读取链接自身而非其目标的属性。

    Args:
        path: 待检查的路径（允许不存在）。

    Returns:
        True 表示是 reparse point；False 表示普通路径或路径不存在。
    """
    try:
        stat_result = os.lstat(path)
    except OSError:
        # 路径不存在或无权访问：交由后续 require_exists 规则处理
        return False
    attrs = getattr(stat_result, "st_file_attributes", 0)
    return bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)


def _iter_self_and_ancestors(path: Path) -> list[Path]:
    """生成路径自身及其全部祖先目录（自深至浅）。

    用于逐级检查 reparse 属性：只要链上任何一级是 junction/symlink，
    整条路径即视为伪装路径。

    Args:
        path: 已词法归一化的绝对路径。

    Returns:
        含路径自身与各级祖先的列表，顺序为由深到浅。
    """
    return [path, *path.parents]


def _is_within_required_root(path: Path) -> bool:
    """判定路径是否严格位于项目根 ``_REQUIRED_ROOT`` 内部。

    关键不变量：必须按 ``Path.parts`` 各段配对 + ``os.path.normcase`` 做
    Windows 大小写不敏感比较，不得使用 ``str.startswith`` 或裸 ``os.path.commonpath``。
    原因：``D:\\codex项目-evil`` 是 ``D:\\codex项目`` 的字符串前缀的延伸，
    但它的 *第二段* 是 ``codex项目-evil`` 而不是 ``codex项目``，按段比较
    会被正确拒绝；startswith 会被错误放行。

    路径自身可等于根（合规根本身），或者其 segments 序列以根 segments
    序列作为前缀（合规后代）。注意：本函数只负责结构层校验，不负责
    路径是否存在；存在性校验由 ``require_exists`` 规则处理。

    Args:
        path: 已词法归一化的绝对路径（必须包含完整盘符部分）。

    Returns:
        True 表示路径等于根或严格祖先后代；False 表示其它路径。
    """
    root_parts = tuple(os.path.normcase(part) for part in _REQUIRED_ROOT.parts)
    target_parts = tuple(os.path.normcase(part) for part in path.parts)
    if len(target_parts) < len(root_parts):
        return False
    return target_parts[: len(root_parts)] == root_parts


def validate_storage_path(
    path: os.PathLike[str] | str,
    *,
    require_exists: bool = False,
) -> ValidationReceipt:
    """验证路径是否满足 Phase 0 D 盘存储位置合同。

    仅执行只读验证：不创建目录、不写入文件、不修改任何文件系统状态。
    任一规则不通过即 fail closed，抛出携带稳定 error_code 的异常。

    验证顺序（前置规则失败则不继续，避免对不可信路径做多余系统调用）：
      1. 平台必须为 Windows
      2. 路径必须能解析出有效盘符
      3. 盘符必须为 D（拒绝 C/E/F 及其他 fallback）
      3.5. 路径必须严格位于项目根 ``_REQUIRED_ROOT`` 内（按 segments normcase）
      4. 卷类型必须为 FIXED（拒绝网络/可移动/CDROM/RAMDISK/未知）
      5. NT 设备路径不得为 SUBST 虚拟盘
      6. 路径自身与全部祖先均不得为 reparse point
      7. reparse 解析结果不得跨卷逃逸到非 D 盘，且必须仍位于项目根内
      8. require_exists=True 时路径必须存在

    Args:
        path:           待验证的路径（字符串或 PathLike）。
        require_exists: 若为 True，路径必须已存在；默认 False。

    Returns:
        ValidationReceipt：验证通过时的可观察事实回执。

    Raises:
        StorageViolationError: 路径违反安全合同（消息已脱敏，仅含路径结构信息）。
        NotImplementedError:   在非 Windows 平台调用时抛出。
    """
    if sys.platform != "win32":
        raise NotImplementedError("StorageLocationContract 仅支持 Windows 平台")

    # 词法归一化：os.path.abspath 只做字符串级处理，不跟随 reparse point，
    # 因此保留了 junction/symlink 链接本身，供后续逐级检测。
    absolute = Path(os.path.abspath(os.fspath(path)))

    # 规则 2：必须能解析出有效盘符（形如 "D:"）
    drive_part = absolute.drive
    if len(drive_part) != 2 or drive_part[1] != ":" or not drive_part[0].isalpha():
        raise StorageViolationError(
            f"路径无法解析为有效盘符路径（drive='{drive_part}'），"
            "已拒绝 UNC / 相对 / 设备命名空间路径"
        )

    drive_letter = drive_part[0].upper()

    # 规则 3：必须在 D 盘
    if drive_letter != _REQUIRED_DRIVE:
        raise StorageViolationError(
            f"存储路径必须位于 {_REQUIRED_DRIVE}: 盘，实际盘符为 "
            f"'{drive_letter}:'（已拒绝 C/E/F 及其他盘 fallback）"
        )

    # 规则 3.5（新增）：必须严格位于项目根 ``_REQUIRED_ROOT`` 内部。
    # 不允许 ``D:\codex项目-evil`` 之类字符串前缀混淆；按 path segments
    # normcase 逐段比较，避免裸 startswith 绕过。
    if not _is_within_required_root(absolute):
        raise StorageViolationError(
            f"存储路径必须位于项目根 '{_REQUIRED_ROOT}' 严格内部，"
            f"实际路径 '{absolute}' 不在该前缀之内"
            "（已按 path segments normcase 校验，"
            "防 D:\\codex项目-evil 等前缀混淆）"
        )

    # 规则 4：卷类型必须为固定磁盘
    drive_type = _get_drive_type(f"{drive_part}\\")
    if drive_type not in _ALLOWED_DRIVE_TYPES:
        type_name = _DRIVE_TYPE_NAMES.get(drive_type, f"TYPE_{drive_type}")
        raise StorageViolationError(
            f"{drive_part} 卷类型不合规：得到 {type_name}（{drive_type}），"
            f"仅允许 FIXED（{_DRIVE_FIXED}）固定磁盘"
        )

    # 规则 5：拒绝 SUBST 虚拟盘（GetDriveTypeW 对 SUBST 同样返回 FIXED）
    dos_device = _query_dos_device(drive_letter)
    if not dos_device:
        raise StorageViolationError(
            f"无法查询 {drive_part} 的 NT 设备路径，卷 identity 不可信，已拒绝"
        )
    if dos_device.startswith(_SUBST_DEVICE_PREFIX):
        raise StorageViolationError(
            f"{drive_part} 是 SUBST 虚拟盘（NT 设备路径以 "
            f"'{_SUBST_DEVICE_PREFIX}' 开头），已拒绝"
        )

    # 规则 6：路径自身与全部祖先均不得为 reparse point
    levels = _iter_self_and_ancestors(absolute)
    for index, level in enumerate(levels, start=1):
        if _is_reparse_point(level):
            raise StorageViolationError(
                f"路径链第 {index} 级是 reparse point"
                "（junction 或 symlink），已拒绝伪装路径"
            )

    # 规则 7：reparse 解析结果不得跨卷逃逸（防御 lstat 漏检的边界情形）。
    # 同时 resolved 路径也必须通过项目根前缀校验——若链接位于
    # ``D:\codex项目\link`` 但指向 ``D:\other\elsewhere``，仅靠盘符检测
    # 会放行跨根重定向；这里使用与规则 3.5 同一套按段 normcase 比较。
    try:
        resolved = Path(os.path.realpath(absolute))
    except OSError as exc:
        raise StorageViolationError(f"路径 reparse 链解析失败：{exc.strerror}") from exc
    resolved_drive = resolved.drive
    if resolved_drive[:1].upper() != _REQUIRED_DRIVE:
        raise StorageViolationError(
            f"路径 reparse 链跨卷逃逸：归一化路径在 {drive_part}，"
            f"实际解析到 '{resolved_drive}'，已拒绝"
        )
    if not _is_within_required_root(resolved):
        raise StorageViolationError(
            f"reparse 解析结果不在项目根 '{_REQUIRED_ROOT}' 之内："
            f"resolved='{resolved}'（已按 segments normcase 校验）"
        )

    # 规则 8：按需检查存在性
    if require_exists and not absolute.exists():
        raise StorageViolationError("require_exists=True 但路径不存在")

    return ValidationReceipt(
        path=absolute,
        drive=drive_letter,
        drive_type=drive_type,
        dos_device=dos_device,
        checked_levels=len(levels),
    )
