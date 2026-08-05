"""factory_agent.observability.logging — 结构化日志与敏感字段脱敏。

强制规则：
  1. 每条日志必须携带以下关联字段（缺失字段用 None 填充，不拒绝）：
       request_id, task_id, run_id, step_id, attempt_id, trace_id,
       module, operation, duration_ms
  2. 日志字段名（key）匹配敏感模式（大小写不敏感）时整值替换为
     ``[REDACTED]``：token / password / private_key / secret / api_key /
     authorization。
  3. 字段值（value）若为字符串，扫描凭据值模式（sk-/ghp_/gho_/ghs_/
     ghu_/github_pat_/Bearer/AKIA/xox[baprs]-/-----BEGIN …PRIVATE KEY-----/
     eyJ JWT），命中片段生成前缀末 4 掩码 ``abcd****wxyz``（PEM 头整段
     ``[REDACTED]``）。
  4. list/tuple 字段值逐元素递归扫描；嵌套 dict 递归到 ``_redact_dict``。
  5. ``entry['message']`` 在 ``format()`` 末尾再显式过 ``_scan_str``，
     不依赖 key 名脱敏（防止调用方把 token 拼进消息体投递）。
  6. 输出格式为 JSON，输出到 stderr（不污染 stdout 业务输出）。

用法示例：
    logger = get_logger("factory_agent.domain.events")
    logger.info(
        "event_dispatched",
        run_id="run_abc1234567890abc",
        operation="dispatch",
        duration_ms=12,
    )
"""

from __future__ import annotations

import json
import logging
import re
import sys

# ── 敏感字段检测正则（Key 名匹配，大小写不敏感）────────────────────────────
_SENSITIVE_KEY_PATTERN: re.Pattern[str] = re.compile(
    r"(token|password|private_key|secret|api_key|authorization)",
    re.IGNORECASE,
)

# ── 敏感值模式（按值内容匹配，不依赖字段名）─────────────────────────────────
# 涵盖云厂商 / SaaS 服务常见的凭据格式。任一子模式命中即对 *该命中片段*
# 整体脱敏（保留首尾若干字符用于人工可读），未被命中的字符串部分原样
# 保留。注释同时列出每个子模式对应的真实凭据样例，便于审计：
#   sk-             → Stripe / OpenAI 等 "sk-live-…/sk-test-…" 前缀
#   ghp_            → GitHub personal access token (classic)
#   gho_            → GitHub OAuth token
#   ghs_            → GitHub server-to-server token
#   ghu_            → GitHub user token
#   github_pat_     → GitHub fine-grained PAT (新格式)
#   Bearer          → RFC 6750 Authorization Bearer <token> 头
#   AKIA[0-9A-Z]{16}→ AWS Access Key ID（固定 16 位大写字母数字）
#   xox[baprs]-     → Slack token（bot/app/user/etc.）
#   -----BEGIN …PRIVATE KEY----- → PEM 私钥头
#   eyJ             → JWT（base64 编码的 header 必以 eyJ 开头）
# 使用 ``re.IGNORECASE`` 兼顾大小写变体（如 ``AKIA`` / ``akia``）。
_SENSITIVE_VALUE_PATTERN: re.Pattern[str] = re.compile(
    r"(sk-[A-Za-z0-9_\-]+)"
    r"|(gh[opsu]_[A-Za-z0-9]+)"
    r"|(github_pat_[A-Za-z0-9_]+)"
    r"|(Bearer\s+[A-Za-z0-9._\-]+)"
    r"|(AKIA[0-9A-Z]{16})"
    r"|(xox[baprs]-[A-Za-z0-9\-]+)"
    r"|(-----BEGIN [A-Z ]*PRIVATE KEY-----[^-]*)"
    r"|(eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)",
    re.IGNORECASE,
)

# 关联 ID 字段名集合（强制要求出现在结构化记录中）
_CORRELATION_FIELDS: frozenset[str] = frozenset({
    "request_id",
    "task_id",
    "run_id",
    "step_id",
    "attempt_id",
    "trace_id",
    "module",
    "operation",
    "duration_ms",
})

_REDACTED = "[REDACTED]"
# 显式 token 脱敏后的占位前缀，方便人眼区分「已脱敏」与「原文保留」。
_MASK_PREFIX = "****"


def _mask_token(token: str) -> str:
    """对单个命中的凭据片段生成可读的掩码形式。

    规则：
      - 长度 <= 8 的 token 直接返回 ``[REDACTED]``，避免前缀末位重叠。
      - PEM 私钥头 ``-----BEGIN …PRIVATE KEY-----`` 整段替换为
        ``[REDACTED]``（结构敏感，不暴露类型即可）。
      - 带命名前缀的 token（``sk-`` / ``ghp_`` / ``gho_`` / ``ghs_`` /
        ``ghu_`` / ``github_pat_`` / ``xox[baprs]-``）：保留完整命名前缀
        （含分隔符 ``-`` 或 ``_``），主体保留末 4 字符，中间用 ``****``
        拼接。这样既能人眼识别凭据类型又不暴露完整秘密。
      - AKIA AWS Access Key：保留 ``AKIA`` + 末 4 字符。
      - Bearer 头：保留 ``Bearer `` 前缀 + token 主体末 4 字符。
      - 其他 token（无前缀）：保留前 4 + 末 4 字符。

    Args:
        token: 已从原字符串中匹配到的整段子串（不含前后非匹配字符）。

    Returns:
        掩码后的字符串，长度始终大于 0。
    """
    if token.startswith("-----BEGIN"):
        return _REDACTED
    # 命名前缀（连字符或下划线分隔符），按长度从长到短排序优先匹配
    # 例如 github_pat_ 必须先于 ghu_ 匹配
    named_prefixes = (
        "github_pat_",
        "sk-live-", "sk-test-", "sk-",
        "ghp_", "gho_", "ghs_", "ghu_",
        "xoxb-", "xoxp-", "xoxa-", "xoxr-", "xoxs-",
    )
    lower = token.lower()
    for p in named_prefixes:
        if lower.startswith(p):
            prefix = token[: len(p)]  # 保留原始大小写
            body = token[len(p):]
            if len(body) <= 9:
                # body 太短，整段掩码为 [REDACTED]（保留前缀）
                return f"{prefix}{_REDACTED}"
            # 标准规则：完整命名前缀 + body 前 5 字符 + **** + body 末 4 字符
            # 前 5 让运维可肉眼判断"secret 起点是否一致"，末 4 暴露最少后缀。
            return f"{prefix}{body[:5]}{_MASK_PREFIX}{body[-4:]}"
    # AKIA AWS Access Key
    if token.upper().startswith("AKIA") and len(token) >= 8:
        return f"AKIA{_MASK_PREFIX}{token[-4:]}"
    # Bearer 头
    if lower.startswith("bearer"):
        prefix = token[: len("Bearer")]
        rest = token[len("Bearer"):].lstrip()
        if len(rest) <= 8:
            return f"{prefix} {_REDACTED}"
        return f"{prefix} {rest[:2]}{_MASK_PREFIX}{rest[-4:]}"
    # 无前缀兜底：保留前 4 + 末 4
    if len(token) <= 8:
        return _REDACTED
    return f"{token[:4]}{_MASK_PREFIX}{token[-4:]}"


def _scan_str(value: str) -> str:
    """扫描字符串中的凭据模式并替换为掩码，命中即整段掩码。

    关键不变量：返回类型恒为 ``str``，长度可能缩短；非命中字符串原样返回。
    空串、非字符串输入由调用方负责规避（类型签名仅承诺 ``str``）。

    Args:
        value: 待扫描的字符串。

    Returns:
        脱敏后的字符串（未被命中则等于原串）。
    """
    if not value:
        return value
    return _SENSITIVE_VALUE_PATTERN.sub(lambda m: _mask_token(m.group(0)), value)


def _redact_value(key: str, value: object) -> object:
    """对单个键值对执行脱敏：先看 key 名再扫 value 内容。

    优先级：
      1. key 匹配敏感模式 → 整值替换为 ``[REDACTED]``（无须看 value）
      2. value 是 ``str`` → 走 ``_scan_str`` 内容扫描
      3. value 是 ``list``/``tuple`` → 逐元素递归（元素若是 dict 走
         ``_redact_dict``，若是 str 走 ``_scan_str``，若是 list/tuple
         递归调用本函数）
      4. 其它原始类型 → 原样保留

    Args:
        key:   字段名。
        value: 原始字段值。

    Returns:
        脱敏后的值，类型与输入保持一致（除 list/tuple 被复制）。
    """
    if _SENSITIVE_KEY_PATTERN.search(key):
        return _REDACTED
    if isinstance(value, str):
        return _scan_str(value)
    if isinstance(value, list):
        return [_redact_value_in_container(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value_in_container(item) for item in value)
    return value


def _redact_value_in_container(item: object) -> object:
    """容器内部元素脱敏：分支 dict / str / list / tuple / 其它。

    容器（list/tuple）里的元素 *不再* 携带字段名，因此不能复用
    ``_redact_value(key, item)``（key 会传错，可能误判为敏感 key）。
    这里拆三个分支：
      - dict → 直接走 ``_redact_dict``（按各自 key 判定）
      - str  → 走 ``_scan_str``（值模式）
      - list/tuple → 递归调用本函数（维持容器类型）
      - 其它 → 原样保留

    Args:
        item: 容器内的一个元素。

    Returns:
        脱敏后的元素。
    """
    if isinstance(item, dict):
        return _redact_dict(item)
    if isinstance(item, str):
        return _scan_str(item)
    if isinstance(item, list):
        return [_redact_value_in_container(sub) for sub in item]
    if isinstance(item, tuple):
        return tuple(_redact_value_in_container(sub) for sub in item)
    return item


def _redact_dict(data: dict[str, object]) -> dict[str, object]:
    """递归脱敏字典中所有层级的敏感字段。

    行为：
      - 字典：递归到子字典（必须显式递归，因为 ``_redact_value`` 仅
        处理扁平原子值，不负责解构 dict）；
      - list/tuple：保留容器类型，逐元素递归（元素若是 dict 走本函数，
        若是 str 走 ``_scan_str``，若 list/tuple 走自身递归）；
      - str：经 ``_scan_str`` 做值扫描；
      - 其它原始类型：原样保留。

    Args:
        data: 原始字典（不修改）。

    Returns:
        脱敏后的新字典。
    """
    result: dict[str, object] = {}
    for k, v in data.items():
        if isinstance(v, dict):
            result[k] = _redact_dict(v)
        else:
            result[k] = _redact_value(k, v)
    return result


class _JsonFormatter(logging.Formatter):
    """将 LogRecord 序列化为单行 JSON 的 Formatter。

    输出格式（字段顺序稳定）：
      { "ts": <ISO-8601>, "level": "INFO", "message": "...",
        <correlation_fields>, <extra_fields> }
    """

    def format(self, record: logging.LogRecord) -> str:
        """格式化日志记录为 JSON 字符串。

        Args:
            record: Python logging.LogRecord 对象。

        Returns:
            单行 JSON 字符串（已脱敏）。
        """
        # 基础字段
        entry: dict[str, object] = {
            "ts":        self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level":     record.levelname,
            "message":   record.getMessage(),
        }

        # 关联字段：从 record.__dict__ 提取，缺失则为 None
        for field in sorted(_CORRELATION_FIELDS):
            entry[field] = getattr(record, field, None)

        # extra 字段（非内置 LogRecord 属性）
        _builtin = frozenset(logging.LogRecord(
            "", 0, "", 0, "", (), None
        ).__dict__.keys()) | {"message", "asctime"}
        for k, v in record.__dict__.items():
            if k not in _builtin and k not in _CORRELATION_FIELDS:
                entry[k] = v

        # 整体脱敏
        entry = _redact_dict(entry)

        # 关键不变量：message 字段即便 key 名不命中（默认就是 "message"）
        # 也必须再过一次 ``_scan_str``，以防调用方把 token 拼接进消息体
        # 投递。``_redact_dict`` 只在 key 命中 ``_SENSITIVE_KEY_PATTERN``
        # 时才走 REDACTED 路径；消息体里的内嵌凭据只能靠值模式捕获。
        # 类型断言：record.getMessage() 始终返回 ``str``，扫描后仍是 ``str``。
        entry["message"] = _scan_str(str(entry["message"]))

        return json.dumps(entry, ensure_ascii=False, default=str)


def get_logger(name: str) -> FactoryLogger:
    """获取命名的 FactoryLogger 实例。

    Args:
        name: logger 名称，通常使用模块全名（如 "factory_agent.domain.events"）。

    Returns:
        FactoryLogger 实例，已配置 JSON 格式输出到 stderr。
    """
    return FactoryLogger(name)


class FactoryLogger:
    """Factory Agent 结构化日志器。

    封装 Python stdlib logging，强制 JSON 格式和关联 ID 字段，
    并在输出前执行敏感字段脱敏。

    Attributes:
        name: logger 名称（模块路径）。
    """

    def __init__(self, name: str) -> None:
        """初始化 FactoryLogger。

        Args:
            name: logger 名称。
        """
        self.name = name
        self._logger = logging.getLogger(name)
        if not self._logger.handlers:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(_JsonFormatter())
            self._logger.addHandler(handler)
        self._logger.setLevel(logging.DEBUG)
        self._logger.propagate = False

    def _log(self, level: int, message: str, **kwargs: object) -> None:
        """发出一条结构化日志。

        Args:
            level:   logging 级别常量（如 logging.INFO）。
            message: 事件描述（不含敏感数据）。
            **kwargs: 任意结构化字段，敏感 key 自动脱敏。
        """
        self._logger.log(level, message, extra=kwargs)

    def debug(self, message: str, **kwargs: object) -> None:
        """发出 DEBUG 级别日志。"""
        self._log(logging.DEBUG, message, **kwargs)

    def info(self, message: str, **kwargs: object) -> None:
        """发出 INFO 级别日志。"""
        self._log(logging.INFO, message, **kwargs)

    def warning(self, message: str, **kwargs: object) -> None:
        """发出 WARNING 级别日志。"""
        self._log(logging.WARNING, message, **kwargs)

    def error(self, message: str, **kwargs: object) -> None:
        """发出 ERROR 级别日志。"""
        self._log(logging.ERROR, message, **kwargs)

    def critical(self, message: str, **kwargs: object) -> None:
        """发出 CRITICAL 级别日志。"""
        self._log(logging.CRITICAL, message, **kwargs)
