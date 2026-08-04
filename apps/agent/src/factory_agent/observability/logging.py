"""factory_agent.observability.logging — 结构化日志与敏感字段脱敏。

强制规则：
  1. 每条日志必须携带以下关联字段（缺失字段用 None 填充，不拒绝）：
       request_id, task_id, run_id, step_id, attempt_id, trace_id,
       module, operation, duration_ms
  2. 日志值中禁止出现以下模式（大小写不敏感），发现则替换为 [REDACTED]：
       token / password / private_key / secret / api_key / authorization
  3. 输出格式为 JSON，输出到 stderr（不污染 stdout 业务输出）。

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


def _redact_value(key: str, value: object) -> object:
    """对单个键值对执行脱敏：若 key 匹配敏感模式则替换 value。

    Args:
        key:   字段名。
        value: 原始字段值。

    Returns:
        脱敏后的值（敏感 key 返回 _REDACTED，其余原值返回）。
    """
    if _SENSITIVE_KEY_PATTERN.search(key):
        return _REDACTED
    return value


def _redact_dict(data: dict[str, object]) -> dict[str, object]:
    """递归脱敏字典中所有层级的敏感字段。

    Args:
        data: 原始字典。

    Returns:
        脱敏后的新字典（不修改原始对象）。
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
