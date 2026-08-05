"""factory_agent.policy.canonical_json — RFC 8785 (JCS) + Unicode NFC 规范化。

本模块产生的字节序列必须与 TypeScript、Rust 实现**完全一致**，
是 semanticPlanHash / planRevisionDigest / barrierId / eventDigest 的共同底座。

规范化规则（算法版本 canonical-json-v1）：
  1. 所有字符串（对象键与字符串值）先递归做 Unicode NFC 归一化。
  2. 对象键按 UTF-16 code unit 序列升序排列（RFC 8785 §3.2.3）。
  3. 输出无任何多余空白；字符串按 RFC 8785 §3.2.2.2 最小化转义。
  4. 数字按 RFC 8785 §3.2.2.3 用 ECMAScript Number::toString 序列化：
     取「最短且可精确往返」的十进制表示。1 与 1.0 → "1"；-0 → "0"；
     1e2 → "100"；1.5 → "1.5"；1e-7 → "1e-7"；1e-6 → "0.000001"。
     仅拒绝 NaN、Infinity 与超出 I-JSON 可互操作范围
     [-(2^53-1), 2^53-1] 的数字（含大整数与大幅值浮点）；范围内的合法小数
     与指数均接受。Python/Rust 复刻 ES6 算法以与 TypeScript 的 String() 字节一致。
  5. 解析 JSON 文本时拒绝重复对象键，并按 RFC 8259 严格数字文法
     （拒绝前导零、尾随小数点、缺数字指数等），与 Rust/TS 一致。

失败错误码（不记录完整计划正文，只暴露稳定 error_code）：
  canonical-json-error
"""

from __future__ import annotations

import json
import unicodedata
from decimal import Decimal
from typing import Any

from factory_agent.errors import FactoryError

# 算法版本号：任何影响输出字节的改动都必须同步升级此常量与 golden vectors。
CANONICAL_JSON_VERSION = "canonical-json-v1"

# I-JSON / RFC 8785 可互操作整数范围：[-(2^53-1), 2^53-1]。
_MAX_SAFE_INTEGER = 2**53 - 1
_MIN_SAFE_INTEGER = -(2**53 - 1)

# RFC 8785 §3.2.2.2 规定的短转义序列（其余控制字符用 \u00xx）。
_SHORT_ESCAPES: dict[int, str] = {
    0x08: "\\b",
    0x09: "\\t",
    0x0A: "\\n",
    0x0C: "\\f",
    0x0D: "\\r",
    0x22: '\\"',
    0x5C: "\\\\",
}


class CanonicalJsonError(FactoryError):
    """规范化失败时抛出（非法数字、重复键、非法类型等）。"""

    error_code = "canonical-json-error"


def _nfc(text: str) -> str:
    """对字符串做 Unicode NFC 归一化。

    Args:
        text: 原始字符串。

    Returns:
        NFC 归一化后的字符串。
    """
    return unicodedata.normalize("NFC", text)


def _encode_string(text: str) -> str:
    """按 RFC 8785 §3.2.2.2 将字符串编码为带引号的 JSON 字面量。

    先做 NFC，再逐字符最小化转义：仅转义双引号、反斜杠和控制字符
    （U+0000..U+001F），其余字符（含非 ASCII）原样输出为 UTF-8。

    Args:
        text: 原始字符串值或键。

    Returns:
        含首尾双引号的 JSON 字符串字面量。
    """
    normalized = _nfc(text)
    parts: list[str] = ['"']
    for ch in normalized:
        code = ord(ch)
        short = _SHORT_ESCAPES.get(code)
        if short is not None:
            parts.append(short)
        elif code < 0x20:
            # 其余控制字符：\u00xx（小写十六进制）
            parts.append(f"\\u{code:04x}")
        else:
            parts.append(ch)
    parts.append('"')
    return "".join(parts)


def _es6_format(digit_str: str, n: int) -> str:
    """按 ECMAScript Number::toString（RFC 8785 §3.2.2.3）格式化非负数字。

    输入为「最短且末位非零」的有效数字串 digit_str（长度 k≥1）与整数 n，
    满足 value = int(digit_str) × 10^(n-k)。据 ECMA-262 分四种情形输出：
      - k ≤ n ≤ 21：digit_str 后补 (n-k) 个 0（纯整数）。
      - 0 < n ≤ 21：在第 n 位后插入小数点。
      - -6 < n ≤ 0："0." + (-n) 个 0 + digit_str。
      - 其余（n>21 或 n≤-6）：指数形式 d1[.d2..dk]e±(n-1)。

    Args:
        digit_str: 最短有效数字串（无前导/尾随零，除单个 "0"）。
        n:         使 value = int(digit_str) × 10^(n-len(digit_str)) 成立的整数。

    Returns:
        ES6 规范数字字面量（不含符号）。
    """
    k = len(digit_str)
    if k <= n <= 21:
        return digit_str + "0" * (n - k)
    if 0 < n <= 21:
        return digit_str[:n] + "." + digit_str[n:]
    if -6 < n <= 0:
        return "0." + "0" * (-n) + digit_str
    # 指数形式：尾数首位后接小数点与其余位，指数为 n-1。
    mantissa = digit_str[0] if k == 1 else f"{digit_str[0]}.{digit_str[1:]}"
    e = n - 1
    return f"{mantissa}e{'+' if e >= 0 else '-'}{abs(e)}"


def _encode_number(value: int | float) -> str:
    """按 RFC 8785 §3.2.2.3（ES6 Number::toString）序列化数字。

    Args:
        value: int 或 float（bool 已在上层排除）。

    Returns:
        最短可往返的十进制字面量字符串。

    Raises:
        CanonicalJsonError: 数字为 NaN/Infinity 或超出 I-JSON 可互操作范围。
    """
    if isinstance(value, float):
        if value != value:  # NaN 自不相等  # noqa: PLR0124
            raise CanonicalJsonError("拒绝 NaN")
        if value in (float("inf"), float("-inf")):
            raise CanonicalJsonError("拒绝 Infinity")

    # I-JSON 可互操作范围：|value| ≤ 2^53-1（同时约束大整数与大幅值浮点）。
    if value < _MIN_SAFE_INTEGER or value > _MAX_SAFE_INTEGER:
        raise CanonicalJsonError(
            "数字超出 I-JSON 可互操作范围 [-(2^53-1), 2^53-1]"
        )

    # 整数直接输出十进制（范围内整数的 ES6 形式即其十进制，无指数）。
    if isinstance(value, int):
        return str(value)

    # 浮点：-0.0 与 0.0 统一为 "0"。
    if value == 0.0:
        return "0"

    sign = "-" if value < 0 else ""
    # repr(float) 给出最短可往返表示；Decimal 提供精确有效数字与指数。
    dec = Decimal(repr(abs(value))).as_tuple()
    digits = list(dec.digits)
    exp = dec.exponent
    # 去尾随零（令有效数字串最短），同步调整指数。
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exp += 1  # type: ignore[operator]
    # 去前导零。
    while len(digits) > 1 and digits[0] == 0:
        digits.pop(0)
    digit_str = "".join(str(d) for d in digits)
    # value = int(digit_str) × 10^exp，故 n = exp + k。
    n = exp + len(digit_str)  # type: ignore[operator]
    return sign + _es6_format(digit_str, n)


def _encode(value: object) -> str:  # noqa: ANN401
    """递归将已解析的 Python 值编码为规范 JSON 字符串片段。

    Args:
        value: None / bool / int / float / str / list / dict。

    Returns:
        规范化 JSON 片段（无多余空白）。

    Raises:
        CanonicalJsonError: 遇到不支持的类型或非法数字。
    """
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    # bool 是 int 子类，必须在 int 判断之前处理，上方已覆盖。
    if isinstance(value, (int, float)):
        return _encode_number(value)
    if isinstance(value, str):
        return _encode_string(value)
    if isinstance(value, list):
        return "[" + ",".join(_encode(item) for item in value) + "]"
    if isinstance(value, dict):
        return _encode_object(value)
    raise CanonicalJsonError(f"不支持的类型：{type(value).__name__}")


def _encode_object(obj: dict[Any, Any]) -> str:
    """编码对象：键做 NFC，按 UTF-16 code unit 序排序后拼接。

    Args:
        obj: 待编码字典（键必须为字符串）。

    Returns:
        规范化 JSON 对象片段。

    Raises:
        CanonicalJsonError: 键非字符串，或 NFC 归一化后出现重复键。
    """
    normalized_items: list[tuple[str, Any]] = []
    seen: set[str] = set()
    for raw_key, val in obj.items():
        if not isinstance(raw_key, str):
            raise CanonicalJsonError("对象键必须为字符串")
        key = _nfc(raw_key)
        if key in seen:
            # NFC 归一化后可能产生重复键，同样拒绝。
            raise CanonicalJsonError("NFC 归一化后出现重复对象键")
        seen.add(key)
        normalized_items.append((key, val))

    # RFC 8785：按 UTF-16 code unit 序列排序。
    # 用 UTF-16-BE 编码后的字节序列比较，等价于 UTF-16 code unit 比较。
    normalized_items.sort(key=lambda kv: kv[0].encode("utf-16-be"))

    parts = [f"{_encode_string(k)}:{_encode(v)}" for k, v in normalized_items]
    return "{" + ",".join(parts) + "}"


def canonicalize(value: object) -> bytes:  # noqa: ANN401
    """将已解析的 Python 值规范化为 RFC 8785 UTF-8 字节序列。

    Args:
        value: 由 JSON 解析得到的 Python 对象（dict/list/str/int/float/bool/None）。

    Returns:
        规范化后的 UTF-8 字节序列。

    Raises:
        CanonicalJsonError: 值含非法数字、非法类型或重复键。
    """
    return _encode(value).encode("utf-8")


def canonicalize_json_text(text: str) -> bytes:
    """解析 JSON 文本（拒绝重复键、NaN、Infinity）后规范化为字节序列。

    Args:
        text: 原始 JSON 文本。

    Returns:
        规范化 UTF-8 字节序列。

    Raises:
        CanonicalJsonError: JSON 非法、含重复键或非法数字。
    """

    def _reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        """object_pairs_hook：在解析阶段即拒绝重复键。"""
        result: dict[str, Any] = {}
        for k, v in pairs:
            if k in result:
                raise CanonicalJsonError(f"JSON 文本含重复对象键：'{k}'")
            result[k] = v
        return result

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate,
            parse_constant=_reject_constant,
        )
    except CanonicalJsonError:
        raise
    except json.JSONDecodeError as exc:
        raise CanonicalJsonError(f"JSON 解析失败：{exc.msg}") from exc
    return canonicalize(parsed)


def _reject_constant(name: str) -> object:  # noqa: ANN401
    """parse_constant 回调：拒绝 NaN / Infinity / -Infinity 字面量。

    Args:
        name: json 模块识别到的常量名。

    Raises:
        CanonicalJsonError: 始终抛出，因为这些值不可互操作。
    """
    raise CanonicalJsonError(f"拒绝非法 JSON 数字常量：{name}")
