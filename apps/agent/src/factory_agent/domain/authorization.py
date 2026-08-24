"""Intent/Execution Authorization 外层 wire 合同的纯领域解析器。

validator schema 仅来自 codegen 生成常量并在使用前校验内容摘要；本模块不读磁盘、
不签发或消费授权，也不提前实现 Task 4/5 的 policy 求交、TTL、fencing 或 CAS。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

# jsonschema 暂无完整 PEP 561 stubs；仅在第三方导入点收窄豁免。
import jsonschema  # type: ignore[import-untyped]

from factory_agent.contracts.generated.models import (
    EXECUTION_AUTHORIZATION_SCHEMA_ID,
    EXECUTION_AUTHORIZATION_SCHEMA_JSON,
    EXECUTION_AUTHORIZATION_SCHEMA_JSON_SHA256,
    EXECUTION_AUTHORIZATION_SCHEMA_VERSION,
    INTENT_AUTHORIZATION_SCHEMA_ID,
    INTENT_AUTHORIZATION_SCHEMA_JSON,
    INTENT_AUTHORIZATION_SCHEMA_JSON_SHA256,
    INTENT_AUTHORIZATION_SCHEMA_VERSION,
)
from factory_agent.domain.plans import _deep_freeze_json
from factory_agent.errors import FactoryError
from factory_agent.policy.canonical_json import canonicalize
from factory_agent.policy.plan_hash import factory_format_checker

_SHA256_PREFIX = "sha256:"
_DRAFT7_SCHEMA_URI = "http://json-schema.org/draft-07/schema#"

# runtime 不能把 generated 常量本身当作信任根；这里冻结预期 ID/version 与文档身份。
_AUTHORIZATION_SCHEMA_IDENTITIES: Mapping[str, tuple[str, int, str, str]] = MappingProxyType(
    {
        "intent": (
            "intent-authorization.v1",
            1,
            "https://factory.local/contracts/schemas/intent-authorization.v1.schema.json",
            "IntentAuthorization",
        ),
        "execution": (
            "execution-authorization.v1",
            1,
            "https://factory.local/contracts/schemas/execution-authorization.v1.schema.json",
            "ExecutionAuthorization",
        ),
    }
)


class AuthorizationContractError(FactoryError):
    """生成 schema、wire 合同或 JCS 输入不可信时抛出。"""

    error_code = "INVALID_AUTHORIZATION_CONTRACT"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝生成 schema 中的重复键，防止 parser 静默采用最后一个值。"""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate generated schema key")
        result[key] = value
    return result


def _build_validator(
    schema_json: str,
    expected_digest: str,
    schema_id: str,
    schema_version: int,
    expected_identity: tuple[str, int, str, str],
) -> jsonschema.Draft7Validator:
    """同时绑定 generated ID/version、文档 metadata 与摘要，再编译 Draft7 validator。"""
    try:
        expected_schema_id, expected_version, expected_document_id, expected_title = expected_identity
        if schema_id != expected_schema_id or type(schema_version) is not int or schema_version != expected_version:
            raise ValueError("generated schema identity mismatch")
        actual_digest = _SHA256_PREFIX + hashlib.sha256(schema_json.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(actual_digest, expected_digest):
            raise ValueError("generated schema digest mismatch")
        schema = json.loads(schema_json, object_pairs_hook=_reject_duplicate_keys)
        if not isinstance(schema, dict):
            raise TypeError("generated schema must be an object")
        if (
            schema.get("$schema") != _DRAFT7_SCHEMA_URI
            or schema.get("$id") != expected_document_id
            or schema.get("title") != expected_title
        ):
            raise ValueError("generated schema document identity mismatch")
        jsonschema.Draft7Validator.check_schema(schema)
        return jsonschema.Draft7Validator(schema, format_checker=factory_format_checker())
    except Exception:  # noqa: BLE001 - 对外不链接可能携带 schema 正文的第三方异常。
        raise AuthorizationContractError("authorization validator initialization failed") from None


_INTENT_VALIDATOR = _build_validator(
    INTENT_AUTHORIZATION_SCHEMA_JSON,
    INTENT_AUTHORIZATION_SCHEMA_JSON_SHA256,
    INTENT_AUTHORIZATION_SCHEMA_ID,
    INTENT_AUTHORIZATION_SCHEMA_VERSION,
    _AUTHORIZATION_SCHEMA_IDENTITIES["intent"],
)
_EXECUTION_VALIDATOR = _build_validator(
    EXECUTION_AUTHORIZATION_SCHEMA_JSON,
    EXECUTION_AUTHORIZATION_SCHEMA_JSON_SHA256,
    EXECUTION_AUTHORIZATION_SCHEMA_ID,
    EXECUTION_AUTHORIZATION_SCHEMA_VERSION,
    _AUTHORIZATION_SCHEMA_IDENTITIES["execution"],
)


@dataclass(frozen=True, slots=True)
class ParsedAuthorization:
    """经权威 schema/JCS 校验且与调用方完全解除别名的授权合同。"""

    schema_id: str
    schema_version: int
    canonical_bytes: bytes
    contract_digest: str
    contract: Mapping[str, Any]


def _parse_authorization(
    contract: Mapping[str, Any],
    *,
    validator: jsonschema.Draft7Validator,
    schema_id: str,
    schema_version: int,
) -> ParsedAuthorization:
    """执行外层 schema、JCS 安全数字与深不可变边界；不解释授权业务语义。"""
    if not isinstance(contract, dict):
        raise AuthorizationContractError("authorization contract must be an object")
    owned_contract = deepcopy(contract)
    try:
        if next(validator.iter_errors(owned_contract), None) is not None:
            raise ValueError("authorization schema validation failed")
        canonical_bytes = canonicalize(owned_contract)
        frozen_contract = _deep_freeze_json(owned_contract)
        if not isinstance(frozen_contract, Mapping):
            raise TypeError("frozen authorization contract must be an object")
    except Exception as exc:  # noqa: BLE001 - 对外只暴露稳定授权错误分类。
        raise AuthorizationContractError("authorization contract validation failed") from exc
    return ParsedAuthorization(
        schema_id=schema_id,
        schema_version=schema_version,
        canonical_bytes=canonical_bytes,
        contract_digest=_SHA256_PREFIX + hashlib.sha256(canonical_bytes).hexdigest(),
        contract=frozen_contract,
    )


def parse_intent_authorization(contract: Mapping[str, Any]) -> ParsedAuthorization:
    """解析 intent-authorization.v1 外层合同，不执行后续 capability/policy 求交。"""
    return _parse_authorization(
        contract,
        validator=_INTENT_VALIDATOR,
        schema_id=INTENT_AUTHORIZATION_SCHEMA_ID,
        schema_version=INTENT_AUTHORIZATION_SCHEMA_VERSION,
    )


def parse_execution_authorization(contract: Mapping[str, Any]) -> ParsedAuthorization:
    """解析 execution-authorization.v1 外层合同，不执行消费、TTL 或 fencing。"""
    return _parse_authorization(
        contract,
        validator=_EXECUTION_VALIDATOR,
        schema_id=EXECUTION_AUTHORIZATION_SCHEMA_ID,
        schema_version=EXECUTION_AUTHORIZATION_SCHEMA_VERSION,
    )


__all__ = [
    "AuthorizationContractError",
    "ParsedAuthorization",
    "parse_execution_authorization",
    "parse_intent_authorization",
]
