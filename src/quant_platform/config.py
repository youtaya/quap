"""Secret-file configuration; credentials are never returned by the API."""

from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QUANT_", extra="ignore")
    database_url: SecretStr = SecretStr("postgresql://quant@localhost:5432/quant")
    database_url_file: Path | None = None
    api_token: SecretStr = SecretStr("")
    api_token_file: Path | None = None
    quote_seconds: int = Field(30, ge=15, le=3600)
    realtime_rpm: int = Field(40, ge=1, le=50)
    ordinary_rpm: int = Field(20, ge=1, le=500)
    daily_quota: int = Field(8000, ge=1)
    history_sessions: int = Field(1500, ge=120, le=2000)
    # 原始日 K 窗口与「已复权窗口」不是一回事：公开源的前复权序列深度远小于日 K。腾讯按 count=800
    # 才给出最深的一档前复权（超过反而被服务端截短），东方财富不可达时平台就只能拿到约 800 个会话。
    # 要求整窗都有因子会让准入判定永远不成立，平台永远不产出代次。这里显式声明平台能接受的最小
    # 连续已复权会话数；真实窗口取「源实际给出的深度」与 `history_sessions` 的较小值。
    factor_sessions: int = Field(500, ge=60, le=2000)
    minute_history_sessions: int = Field(252, ge=20, le=1000)
    intraday_pool_capacity: int = Field(300, ge=1, le=300)
    minute_rpm: int = Field(40, ge=1, le=500)
    minute_daily_quota: int = Field(8000, ge=1, le=300000)
    training_deadline_seconds: int = Field(21600, ge=1800, le=86400)
    data_deadline_seconds: int = Field(7200, ge=120, le=86400)
    inference_deadline_seconds: int = Field(120, ge=30, le=120)
    artifact_root: Path = Path(".runtime/artifacts")
    backup_root: Path = Path(".runtime/backups")
    backup_replica_root: Path | None = None
    observation_root: Path = Path(".runtime/observations")
    notify_webhook: str = ""
    qlib_enabled: bool = True
    research_data_enabled: bool = False
    research_rpm: int = Field(10, ge=1, le=10)
    research_attempts: int = Field(3, ge=1, le=3)
    research_request_deadline_seconds: int = Field(60, ge=1, le=60)
    research_response_bytes: int = Field(8 * 1024 * 1024, ge=1024, le=8 * 1024 * 1024)
    provider: str = "market"
    # ``index`` 只拉沪深 300 成分、观察篮子和基准；``all`` 才拉全部上市 A 股。
    history_scope: str = "index"
    environment: str = "production"

    @model_validator(mode="after")
    def validate_secrets(self):
        for name in ("database_url", "api_token"):
            path = getattr(self, name + "_file")
            if path:
                setattr(self, name, SecretStr(path.read_text(encoding="utf-8").strip()))
        if not self.qlib_enabled:
            raise ValueError("Qlib is required; native recommendation fallback is not supported.")
        if self.provider != "market" or self.environment not in {"production", "test"}:
            raise ValueError("Only the public multi-source market provider is supported; no vendor token.")
        if self.history_scope not in {"index", "all"}:
            raise ValueError("History scope must be 'index' or 'all'.")
        if not self.database_url.get_secret_value().startswith(("postgresql://", "postgresql+psycopg://")):
            raise ValueError("PostgreSQL is required.")
        if self.api_token.get_secret_value() and len(self.api_token.get_secret_value()) < 32:
            raise ValueError("The operator token must contain at least 32 characters.")
        return self

    @property
    def dsn(self):
        return self.database_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://", 1)
