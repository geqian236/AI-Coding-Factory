"""tests.agent.unit.test_logging_redaction — 测试日志脱敏行为。"""

from __future__ import annotations

import json
import logging
from io import StringIO

import pytest
from factory_agent.observability.logging import (
    _REDACTED,
    FactoryLogger,
    _redact_dict,
    _redact_value,
)


class TestRedactValue:
    """测试单字段脱敏逻辑。"""

    @pytest.mark.parametrize("key", [
        "token", "Token", "TOKEN",
        "password", "Password", "PASSWORD",
        "private_key", "PRIVATE_KEY",
        "secret", "Secret", "SECRET",
        "api_key", "API_KEY",
        "authorization", "Authorization", "AUTHORIZATION",
    ])
    def test_sensitive_keys_are_redacted(self, key: str) -> None:
        """所有敏感 key 的值必须被替换为 [REDACTED]。"""
        result = _redact_value(key, "super_secret_value_12345")
        assert result == _REDACTED

    @pytest.mark.parametrize("key", [
        "run_id", "task_id", "operation", "module",
        "duration_ms", "message", "level", "status",
    ])
    def test_safe_keys_are_not_redacted(self, key: str) -> None:
        """非敏感 key 的值不应被修改。"""
        original = "safe_value_xyz"
        result = _redact_value(key, original)
        assert result == original

    def test_none_value_passes_through(self) -> None:
        """None 值在非敏感 key 下原样返回。"""
        assert _redact_value("run_id", None) is None


class TestRedactDict:
    """测试字典脱敏。"""

    def test_nested_sensitive_key(self) -> None:
        """嵌套字典中的敏感字段也必须脱敏。"""
        data = {
            "run_id": "run_abc123",
            "auth": {
                "token": "Bearer xyz789",
                "user": "alice",
            },
        }
        result = _redact_dict(data)
        assert result["run_id"] == "run_abc123"
        assert result["auth"]["token"] == _REDACTED
        assert result["auth"]["user"] == "alice"

    def test_top_level_password(self) -> None:
        """顶层 password 字段必须脱敏。"""
        data = {"password": "hunter2", "operation": "login"}
        result = _redact_dict(data)
        assert result["password"] == _REDACTED
        assert result["operation"] == "login"


class TestFactoryLogger:
    """测试 FactoryLogger 的日志输出格式与脱敏行为。"""

    def _capture_log(self, level: str, message: str, **kwargs: object) -> dict[str, object]:
        """辅助：捕获 FactoryLogger 输出并解析 JSON。"""
        buf = StringIO()
        logger = FactoryLogger("test.redaction")
        # 替换 handler 为内存 buf
        logger._logger.handlers.clear()
        handler = logging.StreamHandler(buf)
        from factory_agent.observability.logging import _JsonFormatter
        handler.setFormatter(_JsonFormatter())
        logger._logger.addHandler(handler)

        getattr(logger, level)(message, **kwargs)
        output = buf.getvalue().strip()
        return json.loads(output)  # type: ignore[no-any-return]

    def test_token_in_extra_is_redacted(self) -> None:
        """extra 参数中 token 字段的值必须被脱敏。"""
        record = self._capture_log("info", "auth_event", token="Bearer abc123xyz")
        assert record.get("token") == _REDACTED

    def test_password_in_extra_is_redacted(self) -> None:
        """extra 参数中 password 字段必须被脱敏。"""
        record = self._capture_log("warning", "login_attempt", password="s3cr3t!")
        assert record.get("password") == _REDACTED

    def test_private_key_redacted(self) -> None:
        """private_key 字段必须被脱敏。"""
        record = self._capture_log("error", "key_event", private_key="-----BEGIN RSA-----")
        assert record.get("private_key") == _REDACTED

    def test_secret_redacted(self) -> None:
        """secret 字段必须被脱敏。"""
        record = self._capture_log("info", "secret_event", secret="topsecret")
        assert record.get("secret") == _REDACTED

    def test_safe_fields_preserved(self) -> None:
        """非敏感字段（run_id、operation 等）必须原样保留。"""
        record = self._capture_log(
            "info",
            "dispatch_event",
            run_id="run_aabbccdd11223344",
            operation="dispatch",
            duration_ms=42,
        )
        assert record["run_id"] == "run_aabbccdd11223344"
        assert record["operation"] == "dispatch"
        assert record["duration_ms"] == 42

    def test_message_preserved(self) -> None:
        """message 字段必须原样出现在输出中。"""
        record = self._capture_log("info", "hello_world_event")
        assert record["message"] == "hello_world_event"

    def test_level_field_present(self) -> None:
        """输出必须包含 level 字段。"""
        record = self._capture_log("warning", "some_warning")
        assert record["level"] == "WARNING"

    def test_output_is_valid_json(self) -> None:
        """每条日志输出必须是合法 JSON（不含多余换行）。"""
        buf = StringIO()
        logger = FactoryLogger("test.json_format")
        logger._logger.handlers.clear()
        handler = logging.StreamHandler(buf)
        from factory_agent.observability.logging import _JsonFormatter
        handler.setFormatter(_JsonFormatter())
        logger._logger.addHandler(handler)
        logger.info("test_json")
        raw = buf.getvalue().strip()
        # 应只有一行（单条 JSON）
        lines = [line for line in raw.splitlines() if line.strip()]
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert isinstance(parsed, dict)


class TestSecretScanZeroHits:
    """批量验证日志输出无敏感值泄漏（secret scan = 0 hits）。"""

    _SENSITIVE_PATTERNS = [
        "Bearer_token_xyz123",
        "hunter2_password",
        "PRIVATE_KEY_BEGIN_END",
        "top_secret_value",
    ]

    def test_no_sensitive_values_in_output(self) -> None:
        """包含多个敏感字段的日志记录，输出不得含原始敏感值。"""
        buf = StringIO()
        logger = FactoryLogger("test.secret_scan")
        logger._logger.handlers.clear()
        handler = logging.StreamHandler(buf)
        from factory_agent.observability.logging import _JsonFormatter
        handler.setFormatter(_JsonFormatter())
        logger._logger.addHandler(handler)

        logger.info(
            "audit_event",
            token="Bearer_token_xyz123",
            password="hunter2_password",
            private_key="PRIVATE_KEY_BEGIN_END",
            secret="top_secret_value",
            run_id="run_0011223344556677",
        )

        output = buf.getvalue()
        for sensitive in self._SENSITIVE_PATTERNS:
            assert sensitive not in output, (
                f"敏感值 '{sensitive[:20]}...' 泄漏到日志输出中"
            )
