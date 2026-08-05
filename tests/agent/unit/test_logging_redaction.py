"""tests.agent.unit.test_logging_redaction — 测试日志脱敏行为。"""

from __future__ import annotations

import json
import logging
from io import StringIO

import pytest
from factory_agent.observability.logging import (
    _MASK_PREFIX,
    _REDACTED,
    FactoryLogger,
    _redact_dict,
    _redact_value,
    _scan_str,
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


class TestScanStrValuePattern:
    """测试 ``_scan_str`` 值模式扫描：命中 token 即整段掩码。

    不依赖 key 名——即便字段名是 ``url`` / ``remark`` 这样的中性词，
    只要值里塞了 ``sk-…`` / ``AKIA…`` / ``Bearer …`` / PEM 头 / JWT，
    该片段也必须被掩码。
    """

    @pytest.mark.parametrize(("raw_value", "expected_substring"), [
        # Stripe / OpenAI 风格前缀
        ("prefix sk-live-ABCDEFGHIJ1234567890 suffix", "sk-live-ABCDE"),
        # GitHub PAT
        ("CI token: ghp_abcDEF1234567890xyz leakage", "ghp_"),
        # GitHub OAuth
        ("oauth token gho_zzzz9999abcdefghijkl", "gho_"),
        # GitHub server-to-server
        ("ghs_AA11BB22CC33DD44EE55FF66", "ghs_"),
        # GitHub user token
        ("user token ghu_aaaabbbbccccdddd", "ghu_"),
        # GitHub fine-grained PAT
        ("new PAT github_pat_11AAAA0_aBcDeFgHiJkLmNoPqRsTuVwX", "github_pat_"),
        # AWS access key (16 位大写字母数字)
        ("AWS key AKIAIOSFODNN7EXAMPLE rest", "AKIA"),
        # Slack tokens
        ("slack bot xoxb-1234567890-FAKE-CAFE", "xoxb-"),
        # Slack user token
        ("slack user xoxp-9999-8888-aaa-bbb", "xoxp-"),
        # JWT (三段 base64)
        ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signatureXYZ", "eyJ"),
    ])
    def test_token_pattern_is_masked(
        self, raw_value: str, expected_substring: str
    ) -> None:
        """命中任一凭据值模式，整段必须被掩码且保留片段可读。"""
        result = _scan_str(raw_value)
        assert result != raw_value, "命中值模式必须改变原字符串"
        assert expected_substring in result, (
            f"掩码后应保留前缀 '{expected_substring}' 用于可读性"
        )
        assert _MASK_PREFIX in result, "掩码后必须含 '****' 区段"

    def test_bearer_header_keeps_prefix(self) -> None:
        """``Bearer <token>`` 头应保留 ``Bearer`` 前缀并掩码 token。"""
        raw = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz1234"
        result = _scan_str(raw)
        assert "Bearer" in result
        assert "abcdefghijklmnopqrstuvwxyz1234" not in result
        assert _MASK_PREFIX in result

    def test_pem_private_key_block_redacted(self) -> None:
        """PEM 私钥头必须整段 ``[REDACTED]``（结构敏感，不暴露类型）。"""
        raw = "key block -----BEGIN RSA PRIVATE KEY-----ABCDEF"
        result = _scan_str(raw)
        assert "-----BEGIN RSA PRIVATE KEY-----" not in result
        assert _REDACTED in result

    def test_pem_ec_private_key_block_redacted(self) -> None:
        """EC 私钥头（``BEGIN EC PRIVATE KEY``）同样必须整段 ``[REDACTED]``。"""
        raw = "-----BEGIN EC PRIVATE KEY-----ABCDEF-----END"
        result = _scan_str(raw)
        assert "-----BEGIN EC PRIVATE KEY-----" not in result

    def test_safe_string_passthrough(self) -> None:
        """普通字符串（含特殊字符 URL、空格）不应被改写。"""
        original = "https://example.com/path?query=value&other=42"
        assert _scan_str(original) == original

    def test_empty_string_passthrough(self) -> None:
        """空字符串应原样返回。"""
        assert _scan_str("") == ""

    def test_multiple_tokens_in_one_string(self) -> None:
        """同一字符串里的多个 token 应都独立被掩码。"""
        raw = "first sk-live-AAAAAAAAAA end mid ghp_xxxxxxxxxxxx end"
        result = _scan_str(raw)
        assert "sk-live-AAAAAAAAAA" not in result
        assert "ghp_xxxxxxxxxxxx" not in result


class TestRedactValueContentScan:
    """测试 ``_redact_value``：key 不敏感时也按值扫描。"""

    @pytest.mark.parametrize(("key", "value"), [
        ("url", "https://x.com/?t=sk-live-ABCDEFGHIJKLM1234"),
        ("remark", "rate limit AKIAIOSFODNN7EXAMPLE ignored"),
        ("note", "bearer sentence Bearer abcdEFGH1234567890wxyz"),
        ("payload", "-----BEGIN PRIVATE KEY-----XXXXXXXX"),
    ])
    def test_token_in_safe_key_value_is_masked(
        self, key: str, value: str
    ) -> None:
        """值里塞了 token，即便 key 名不敏感也必须被掩码。"""
        result = _redact_value(key, value)
        assert result != value
        assert _REDACTED in str(result) or _MASK_PREFIX in str(result)

    def test_safe_key_safe_value_passthrough(self) -> None:
        """安全 key + 安全值 → 原样返回。"""
        original = "all clear here"
        assert _redact_value("operation", original) == original

    def test_list_value_scanned_recursively(self) -> None:
        """list 字段值里嵌入的 token 必须被扫描。"""
        data = [
            "normal string",
            "sk-live-ABCDEFGHIJKLM",
            "another line",
        ]
        result = _redact_value("remarks", data)
        assert isinstance(result, list)
        assert result[0] == "normal string"
        assert result[1] != "sk-live-ABCDEFGHIJKLM"
        assert _MASK_PREFIX in str(result[1])
        assert result[2] == "another line"

    def test_tuple_value_returns_tuple(self) -> None:
        """tuple 字段值应保留类型，逐元素递归。"""
        data = ("x", "sk-live-ABCDEFGHIJKLM", "y")
        result = _redact_value("remarks", data)
        assert isinstance(result, tuple)
        assert result[0] == "x"
        assert result[1] != "sk-live-ABCDEFGHIJKLM"
        assert result[2] == "y"

    def test_list_of_dicts_recursed(self) -> None:
        """list 嵌套 dict 应递归到 ``_redact_dict``（key 名也脱敏）。"""
        data = [{"token": "Bearer xyz1234567890"}]
        result = _redact_value("records", data)
        assert isinstance(result, list)
        assert result[0]["token"] == _REDACTED


class TestRedactDictListTupleRecursion:
    """测试 ``_redact_dict`` 对 list/tuple 值的递归与嵌套处理。"""

    def test_top_level_list_value(self) -> None:
        """顶层 list 值（key 非敏感）应逐元素扫描。"""
        data = {"headers": ["Authorization: Bearer abcdef1234567890wxyz", "ok"]}
        result = _redact_dict(data)
        assert isinstance(result["headers"], list)
        assert "abcdef1234567890wxyz" not in result["headers"][0]
        assert _MASK_PREFIX in result["headers"][0]
        assert result["headers"][1] == "ok"

    def test_top_level_tuple_value(self) -> None:
        """顶层 tuple 值应保留类型。"""
        data = {"channels": ("sk-live-ABCDEFGHIJKLM", "noop")}
        result = _redact_dict(data)
        assert isinstance(result["channels"], tuple)
        assert result["channels"][0] != "sk-live-ABCDEFGHIJKLM"
        assert result["channels"][1] == "noop"

    def test_nested_list_inside_dict(self) -> None:
        """dict → list → str 嵌套必须递归扫描。"""
        data = {
            "outer": {
                "inner_list": ["plain", "ghp_AAAABBBBCCCCDDDD"],
            }
        }
        result = _redact_dict(data)
        assert "ghp_AAAABBBBCCCCDDDD" not in result["outer"]["inner_list"][1]
        assert _MASK_PREFIX in result["outer"]["inner_list"][1]

    def test_unsafe_key_overrides_value_scan(self) -> None:
        """敏感 key 命中时整值 ``[REDACTED]``，不再做值扫描。"""
        data = {"token": "AKIAIOSFODNN7EXAMPLE"}
        result = _redact_dict(data)
        assert result["token"] == _REDACTED


class TestFactoryLoggerMessageScan:
    """测试 ``FactoryLogger.format()`` 末尾对 ``message`` 字段的扫描。"""

    def _capture(self, message: str, **kwargs: object) -> dict[str, object]:
        """辅助：捕获 FactoryLogger 输出并解析 JSON。"""
        buf: StringIO = StringIO()
        logger = FactoryLogger("test.message_scan")
        logger._logger.handlers.clear()
        handler = logging.StreamHandler(buf)
        from factory_agent.observability.logging import _JsonFormatter
        handler.setFormatter(_JsonFormatter())
        logger._logger.addHandler(handler)
        logger.info(message, **kwargs)
        return json.loads(buf.getvalue().strip())  # type: ignore[no-any-return]

    def test_token_in_message_is_masked(self) -> None:
        """消息体里嵌入的 token 必须被掩码（不依赖 key 名）。"""
        record = self._capture("dispatched with sk-live-ABCDEFGHIJKLM ok")
        msg = str(record["message"])
        assert "sk-live-ABCDEFGHIJKLM" not in msg
        assert _MASK_PREFIX in msg

    def test_bearer_in_message_is_masked(self) -> None:
        """消息体里嵌入的 Bearer 头必须被掩码。"""
        record = self._capture(
            "auth attempt failed for Bearer abcdef1234567890xy"
        )
        msg = str(record["message"])
        assert "abcdef1234567890xy" not in msg
        assert _MASK_PREFIX in msg

    def test_safe_message_passthrough(self) -> None:
        """安全消息应原样保留。"""
        record = self._capture("hello normal event")
        assert record["message"] == "hello normal event"

    def test_extra_field_token_in_safe_key_masked(self) -> None:
        """extra 字段 key 不敏感但值含 token 也必须被掩码。"""
        record = self._capture("event", note="sk-live-ABCDEFGHIJKLM")
        note = str(record["note"])
        assert "sk-live-ABCDEFGHIJKLM" not in note
        assert _MASK_PREFIX in note


class TestFactoryLoggerListExtraScan:
    """FactoryLogger 输出层面：list/tuple extra 字段应被扫描。"""

    def _capture(self, message: str, **kwargs: object) -> dict[str, object]:
        buf: StringIO = StringIO()
        logger = FactoryLogger("test.list_scan")
        logger._logger.handlers.clear()
        handler = logging.StreamHandler(buf)
        from factory_agent.observability.logging import _JsonFormatter
        handler.setFormatter(_JsonFormatter())
        logger._logger.addHandler(handler)
        logger.info(message, **kwargs)
        return json.loads(buf.getvalue().strip())  # type: ignore[no-any-return]

    def test_list_extra_field_scanned(self) -> None:
        """extra 字段值是 list 时，里面 token 必须被掩码。"""
        record = self._capture(
            "audit",
            records=["first", "sk-live-ABCDEFGHIJKLM", "last"],
        )
        records = record["records"]
        assert isinstance(records, list)
        assert records[0] == "first"
        assert "sk-live-ABCDEFGHIJKLM" not in records[1]
        assert records[2] == "last"
