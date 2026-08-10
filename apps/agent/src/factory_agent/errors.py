"""factory_agent.errors — 全局错误层级。

所有 Factory Agent 异常均继承 FactoryError，以便调用方统一捕获。
每个错误携带稳定的 error_code（kebab-case），供日志和监控使用。
"""

from __future__ import annotations


class FactoryError(Exception):
    """所有 Factory Agent 异常的根类。

    Attributes:
        error_code: 稳定的错误码，kebab-case，不含敏感信息。
        detail:     人类可读的附加说明（已脱敏）。
    """

    error_code: str = "factory-error"

    def __init__(self, detail: str = "") -> None:
        """初始化 FactoryError。

        Args:
            detail: 已脱敏的附加说明，可为空字符串。
        """
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return f"[{self.error_code}] {self.detail}" if self.detail else f"[{self.error_code}]"


class StorageViolationError(FactoryError):
    """存储路径违反安全合同时抛出。

    Phase 0 拒绝条件：C/E/F 盘 fallback、junction、symlink、
    SUBST 盘、网络/可移动卷、未知卷 identity。
    """

    error_code = "storage-violation"


class LogRedactionError(FactoryError):
    """日志条目含有禁止输出的敏感字段时抛出。"""

    error_code = "log-redaction-violation"


class ConfigurationError(FactoryError):
    """配置缺失或格式错误时抛出。"""

    error_code = "configuration-error"


class IdentityFormatError(FactoryError):
    """关联 ID（run/task/step/attempt/trace）格式不合规时抛出。"""

    error_code = "identity-format-error"
