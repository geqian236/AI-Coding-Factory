"""Win32 Global named mutex 适配器。

SQLite 控制面只能有一个写 owner。该模块把 storage identity 映射为稳定的
``Global\\`` kernel mutex 名称，并在 acquire 前强制 current-user-only DACL、
非继承 HANDLE、timeout/abandoned 分支，避免多个进程同时打开数据库推进状态。
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED_0 = 0x00000080
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF

_SYNCHRONIZE = 0x00100000
_MUTEX_MODIFY_STATE = 0x00000001
_MUTEX_REQUIRED_RIGHTS = _SYNCHRONIZE | _MUTEX_MODIFY_STATE
_READ_CONTROL = 0x00020000
_HANDLE_FLAG_INHERIT = 0x00000001
_ERROR_INVALID_HANDLE = 6
_SE_DACL_PROTECTED = 0x1000
MAX_MUTEX_TIMEOUT_MS = 60_000


class _MutexError(Exception):
    """所有 mutex 稳定错误码的基类。"""

    error_code = "SINGLETON_MUTEX_ERROR"

    def __init__(self, message: str | None = None, *, cause: BaseException | None = None) -> None:
        super().__init__(message or self.error_code)
        if cause is not None:
            self.__cause__ = cause


class MutexSecurityError(_MutexError):
    error_code = "SINGLETON_MUTEX_SECURITY_FAILED"


class MutexCreationError(_MutexError):
    error_code = "SINGLETON_MUTEX_CREATE_FAILED"


class MutexTimeoutError(_MutexError):
    error_code = "SINGLETON_MUTEX_TIMEOUT"


class InvalidMutexTimeoutError(_MutexError):
    error_code = "SINGLETON_MUTEX_TIMEOUT_INVALID"


class MutexWaitError(_MutexError):
    error_code = "SINGLETON_MUTEX_WAIT_FAILED"


class MutexOwnershipError(_MutexError):
    error_code = "SINGLETON_MUTEX_WRONG_OWNER"


@dataclass(frozen=True, slots=True)
class MutexSecurityReceipt:
    """记录 acquire 后实际 kernel object 安全边界，供启动验收和测试审计。"""

    protected_dacl: bool
    inheritable: bool
    current_sid: str
    owner_sid: str
    allowed_sids: tuple[str, ...]
    denied_sids: tuple[str, ...]
    allowed_rights: tuple[str, ...]


@dataclass(slots=True)
class _SecurityAttributes:
    """持有 SECURITY_ATTRIBUTES 及其底层 SECURITY_DESCRIPTOR 生命周期。"""

    pointer: ctypes.c_void_p
    attributes: _SecurityAttributesStruct


class _Win32Api(Protocol):
    def build_current_user_security(self) -> _SecurityAttributes: ...
    def create_mutex(self, name: str, security: object) -> int: ...
    def verify_current_user_dacl(self, handle: int) -> bool: ...
    def wait(self, handle: int, timeout_ms: int) -> int: ...
    def release(self, handle: int) -> None: ...
    def close(self, handle: int) -> None: ...
    def get_last_error(self) -> int: ...


class _SecurityAttributesStruct(ctypes.Structure):
    _fields_ = [
        ("nLength", ctypes.c_ulong),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", ctypes.c_bool),
    ]


class _AclSizeInformation(ctypes.Structure):
    _fields_ = [
        ("AceCount", ctypes.c_ulong),
        ("AclBytesInUse", ctypes.c_ulong),
        ("AclBytesFree", ctypes.c_ulong),
    ]


class _AceHeader(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", ctypes.c_ushort),
    ]


class _AccessAllowedAce(ctypes.Structure):
    _fields_ = [
        ("Header", _AceHeader),
        ("Mask", ctypes.c_ulong),
        ("SidStart", ctypes.c_ulong),
    ]


class _AccessDeniedAce(ctypes.Structure):
    _fields_ = [
        ("Header", _AceHeader),
        ("Mask", ctypes.c_ulong),
        ("SidStart", ctypes.c_ulong),
    ]


class _RealWin32Api:
    """真实 Win32 调用封装；生产逻辑只通过窄接口调用，便于测试注入 fake。"""

    def __init__(self) -> None:
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

        self._close_handle = self._kernel32.CloseHandle
        self._close_handle.argtypes = [ctypes.c_void_p]
        self._close_handle.restype = ctypes.c_bool

        self._set_handle_information = self._kernel32.SetHandleInformation
        self._set_handle_information.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
        self._set_handle_information.restype = ctypes.c_bool

        self._get_handle_information = self._kernel32.GetHandleInformation
        self._get_handle_information.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        self._get_handle_information.restype = ctypes.c_bool

        self._wait_for_single_object = self._kernel32.WaitForSingleObject
        self._wait_for_single_object.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self._wait_for_single_object.restype = ctypes.c_ulong

        self._release_mutex = self._kernel32.ReleaseMutex
        self._release_mutex.argtypes = [ctypes.c_void_p]
        self._release_mutex.restype = ctypes.c_bool

        self._create_mutex_ex = self._kernel32.CreateMutexExW
        self._create_mutex_ex.argtypes = [
            ctypes.POINTER(_SecurityAttributesStruct),
            ctypes.c_wchar_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        self._create_mutex_ex.restype = ctypes.c_void_p

        self._current_sid: str | None = None

    def build_current_user_security(self) -> _SecurityAttributes:
        """构造 protected current-SID-only DACL，且 HANDLE 明确不可继承。"""
        sid = self._get_current_user_sid()
        sddl = f"O:{sid}D:P(A;;0x{_MUTEX_REQUIRED_RIGHTS:x};;;{sid})"
        descriptor = ctypes.c_void_p()
        converter = self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        converter.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        converter.restype = ctypes.c_bool
        if not converter(sddl, 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = _SecurityAttributesStruct(
            ctypes.sizeof(_SecurityAttributesStruct),
            descriptor,
            False,
        )
        return _SecurityAttributes(pointer=descriptor, attributes=attributes)

    def free_security(self, security: object) -> None:
        """释放 ConvertStringSecurityDescriptorToSecurityDescriptorW 分配的 descriptor。"""
        if isinstance(security, _SecurityAttributes) and security.pointer:
            self._kernel32.LocalFree(security.pointer)
            security.pointer = ctypes.c_void_p()

    def create_mutex(self, name: str, security: object) -> int:
        """创建或打开 Global mutex；失败时不回退 Local/default ACL。"""
        if not isinstance(security, _SecurityAttributes):
            raise TypeError("security must be _SecurityAttributes")
        handle = self._create_mutex_ex(
            ctypes.byref(security.attributes),
            name,
            0,
            _MUTEX_REQUIRED_RIGHTS | _READ_CONTROL,
        )
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if not self._set_handle_information(ctypes.c_void_p(handle), _HANDLE_FLAG_INHERIT, 0):
            last_error = ctypes.get_last_error()
            self.close(int(handle))
            raise ctypes.WinError(last_error)
        return int(handle)

    def verify_current_user_dacl(self, handle: int) -> bool:
        """核验实际 DACL，拒绝 default ACL、Everyone、Authenticated Users 或额外 ACE。"""
        receipt = self.security_receipt(handle)
        return (
            receipt.protected_dacl
            and not receipt.inheritable
            and receipt.owner_sid == receipt.current_sid
            and receipt.allowed_sids == (receipt.current_sid,)
            and receipt.denied_sids == ()
            and receipt.allowed_rights == ("SYNCHRONIZE", "MUTEX_MODIFY_STATE")
        )

    def security_receipt(self, handle: int) -> MutexSecurityReceipt:
        """从 kernel object 读取 owner/DACL，并转为稳定 receipt。"""
        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        get_security_info = self._advapi32.GetSecurityInfo
        get_security_info.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        get_security_info.restype = ctypes.c_ulong
        result = get_security_info(
            ctypes.c_void_p(handle),
            6,
            0x00000001 | 0x00000004,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if result != 0:
            raise ctypes.WinError(result)
        try:
            owner_sid = self._sid_to_string(owner)
            allowed: list[tuple[str, int]] = []
            denied: list[str] = []
            protected = self._descriptor_has_protected_dacl(descriptor)
            if dacl:
                allowed, denied = self._read_dacl(dacl)
            rights = self._rights_tuple(allowed[0][1]) if len(allowed) == 1 else ()
            return MutexSecurityReceipt(
                protected_dacl=protected,
                inheritable=self._is_handle_inheritable(handle),
                current_sid=self._get_current_user_sid(),
                owner_sid=owner_sid,
                allowed_sids=tuple(sid for sid, _mask in allowed),
                denied_sids=tuple(denied),
                allowed_rights=rights,
            )
        finally:
            if descriptor:
                self._kernel32.LocalFree(descriptor)

    def wait(self, handle: int, timeout_ms: int) -> int:
        return int(self._wait_for_single_object(ctypes.c_void_p(handle), ctypes.c_ulong(timeout_ms)))

    def release(self, handle: int) -> None:
        if not self._release_mutex(ctypes.c_void_p(handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self, handle: int) -> None:
        if not self._close_handle(ctypes.c_void_p(handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def get_last_error(self) -> int:
        return int(ctypes.get_last_error())

    def _get_current_user_sid(self) -> str:
        if self._current_sid is not None:
            return self._current_sid
        token = ctypes.c_void_p()
        open_process_token = self._advapi32.OpenProcessToken
        open_process_token.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)]
        open_process_token.restype = ctypes.c_bool
        if not open_process_token(self._kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            get_token_information = self._advapi32.GetTokenInformation
            get_token_information.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_ulong),
            ]
            get_token_information.restype = ctypes.c_bool
            needed = ctypes.c_ulong()
            get_token_information(token, 1, None, 0, ctypes.byref(needed))
            buffer = ctypes.create_string_buffer(needed.value)
            if not get_token_information(token, 1, buffer, needed, ctypes.byref(needed)):
                raise ctypes.WinError(ctypes.get_last_error())
            sid_pointer = ctypes.c_void_p.from_buffer(buffer).value
            self._current_sid = self._sid_to_string(ctypes.c_void_p(sid_pointer))
            return self._current_sid
        finally:
            self._close_handle(token)

    def _sid_to_string(self, sid: ctypes.c_void_p) -> str:
        out = ctypes.c_wchar_p()
        converter = self._advapi32.ConvertSidToStringSidW
        converter.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
        converter.restype = ctypes.c_bool
        if not converter(sid, ctypes.byref(out)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return str(out.value)
        finally:
            self._kernel32.LocalFree(out)

    def _descriptor_has_protected_dacl(self, descriptor: ctypes.c_void_p) -> bool:
        control = ctypes.c_ushort()
        revision = ctypes.c_ulong()
        getter = self._advapi32.GetSecurityDescriptorControl
        getter.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ushort), ctypes.POINTER(ctypes.c_ulong)]
        getter.restype = ctypes.c_bool
        if not getter(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(control.value & _SE_DACL_PROTECTED)

    def _is_handle_inheritable(self, handle: int) -> bool:
        flags = ctypes.c_ulong()
        if not self._get_handle_information(ctypes.c_void_p(handle), ctypes.byref(flags)):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(flags.value & _HANDLE_FLAG_INHERIT)

    def _read_dacl(self, dacl: ctypes.c_void_p) -> tuple[list[tuple[str, int]], list[str]]:
        info = _AclSizeInformation()
        get_acl_information = self._advapi32.GetAclInformation
        get_acl_information.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int]
        get_acl_information.restype = ctypes.c_bool
        if not get_acl_information(dacl, ctypes.byref(info), ctypes.sizeof(info), 2):
            raise ctypes.WinError(ctypes.get_last_error())
        allowed: list[tuple[str, int]] = []
        denied: list[str] = []
        get_ace = self._advapi32.GetAce
        get_ace.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)]
        get_ace.restype = ctypes.c_bool
        for index in range(info.AceCount):
            ace_ptr = ctypes.c_void_p()
            if not get_ace(dacl, index, ctypes.byref(ace_ptr)):
                raise ctypes.WinError(ctypes.get_last_error())
            ace_address = ace_ptr.value
            if ace_address is None:
                raise ctypes.WinError(_ERROR_INVALID_HANDLE)
            header = ctypes.cast(ace_ptr, ctypes.POINTER(_AceHeader)).contents
            if header.AceType == 0:
                allowed_ace = ctypes.cast(ace_ptr, ctypes.POINTER(_AccessAllowedAce)).contents
                sid_ptr = ctypes.c_void_p(ace_address + _AccessAllowedAce.SidStart.offset)
                allowed.append((self._sid_to_string(sid_ptr), int(allowed_ace.Mask)))
            elif header.AceType == 1:
                sid_ptr = ctypes.c_void_p(ace_address + _AccessDeniedAce.SidStart.offset)
                denied.append(self._sid_to_string(sid_ptr))
            else:
                return [], ["unsupported-ace"]
        return allowed, denied

    def _rights_tuple(self, mask: int) -> tuple[str, ...]:
        if mask == _MUTEX_REQUIRED_RIGHTS:
            return ("SYNCHRONIZE", "MUTEX_MODIFY_STATE")
        return ()


@dataclass(slots=True)
class MutexLease:
    """一次成功 acquire 的拥有权；release 必须在取得 mutex 的同一线程调用。"""

    _api: _Win32Api
    native_handle: int
    abandoned: bool
    security_receipt: MutexSecurityReceipt
    _owner_thread_id: int
    _released: bool = False
    _closed: bool = False

    def release(self) -> None:
        """释放 mutex；错误线程释放会破坏 OS ownership，因此在调用 Win32 前拒绝。"""
        if threading.get_ident() != self._owner_thread_id:
            raise MutexOwnershipError()
        if self._released:
            return
        try:
            self._api.release(self.native_handle)
        except OSError as exc:
            raise MutexOwnershipError(cause=exc) from exc
        self._released = True

    def close(self) -> None:
        """关闭 native HANDLE；close 不暗含 release，调用方需显式决定恢复边界。"""
        if self._closed:
            return
        self._api.close(self.native_handle)
        self._closed = True


@dataclass(frozen=True, slots=True)
class NamedMutex:
    """按 database storage identity 派生的 Global named mutex。"""

    name: str
    timeout_ms: int
    _api: _Win32Api

    @classmethod
    def for_database(
        cls,
        *,
        volume_identity: str,
        state_database_path: Path,
        timeout_ms: int,
        win32_api: _Win32Api | None = None,
    ) -> NamedMutex:
        """从 volume identity 与数据库绝对路径派生稳定 mutex name。"""
        if (
            not isinstance(timeout_ms, int)
            or isinstance(timeout_ms, bool)
            or not 1 <= timeout_ms <= MAX_MUTEX_TIMEOUT_MS
        ):
            raise InvalidMutexTimeoutError()
        canonical_path = str(Path(state_database_path).resolve()).replace("/", "\\").casefold()
        material = json.dumps(
            ["factory-agent-singleton-v1", volume_identity, canonical_path],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        name = "Global\\AI-Coding-Factory.Agent." + hashlib.sha256(material).hexdigest()
        return cls(name=name, timeout_ms=timeout_ms, _api=win32_api or _RealWin32Api())

    def acquire(self) -> MutexLease:
        """创建/打开 mutex、核验安全描述符并等待拥有权。"""
        try:
            security = self._api.build_current_user_security()
        except BaseException as exc:  # noqa: BLE001 - ACL 构造失败必须 fail closed。
            raise MutexSecurityError("SINGLETON_MUTEX_SECURITY_FAILED", cause=exc) from exc
        try:
            handle = self._api.create_mutex(self.name, security)
        except OSError as exc:
            raise MutexCreationError("SINGLETON_MUTEX_CREATE_FAILED", cause=exc) from exc
        except BaseException as exc:  # noqa: BLE001
            raise MutexCreationError("SINGLETON_MUTEX_CREATE_FAILED", cause=exc) from exc
        finally:
            self._free_security(security)
        try:
            if not self._api.verify_current_user_dacl(handle):
                error = MutexSecurityError("SINGLETON_MUTEX_DACL_MISMATCH")
                error.error_code = "SINGLETON_MUTEX_DACL_MISMATCH"
                raise error
            receipt = self._security_receipt(handle)
            wait_result = self._api.wait(handle, self.timeout_ms)
            if wait_result == WAIT_OBJECT_0:
                return MutexLease(self._api, handle, False, receipt, threading.get_ident())
            if wait_result == WAIT_ABANDONED_0:
                return MutexLease(self._api, handle, True, receipt, threading.get_ident())
            if wait_result == WAIT_TIMEOUT:
                raise MutexTimeoutError("SINGLETON_MUTEX_TIMEOUT")
            if wait_result == WAIT_FAILED:
                raise MutexWaitError("SINGLETON_MUTEX_WAIT_FAILED")
            raise MutexWaitError("SINGLETON_MUTEX_WAIT_FAILED")
        except _MutexError:
            self._api.close(handle)
            raise
        except BaseException as exc:  # noqa: BLE001
            self._api.close(handle)
            raise MutexWaitError("SINGLETON_MUTEX_WAIT_FAILED", cause=exc) from exc

    def _security_receipt(self, handle: int) -> MutexSecurityReceipt:
        receipt_method = getattr(self._api, "security_receipt", None)
        if callable(receipt_method):
            receipt = receipt_method(handle)
            if not isinstance(receipt, MutexSecurityReceipt):
                raise MutexSecurityError("SINGLETON_MUTEX_DACL_MISMATCH")
            return receipt
        return MutexSecurityReceipt(
            protected_dacl=True,
            inheritable=False,
            current_sid="fake-current-sid",
            owner_sid="fake-current-sid",
            allowed_sids=("fake-current-sid",),
            denied_sids=(),
            allowed_rights=("SYNCHRONIZE", "MUTEX_MODIFY_STATE"),
        )

    def _free_security(self, security: object) -> None:
        cleanup = getattr(self._api, "free_security", None)
        if callable(cleanup):
            cleanup(security)
