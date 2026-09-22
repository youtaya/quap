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
    tushare_token: SecretStr = SecretStr("")
    tushare_token_file: Path | None = None
    quote_seconds: int = Field(30, ge=15, le=3600)
    realtime_rpm: int = Field(40, ge=1, le=50)
    ordinary_rpm: int = Field(20, ge=1, le=500)
    daily_quota: int = Field(8000, ge=1)
    history_sessions: int = Field(500, ge=120, le=2000)
    artifact_root: Path = Path(".runtime/artifacts")
    backup_root: Path = Path(".runtime/backups")
    qlib_enabled: bool = False
    provider: str = "tushare"
    environment: str = "production"
    notify_email: str = "jxiaoping@gmail.com"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = Field(587, ge=1, le=65535)
    smtp_user: str = "jxiaoping@gmail.com"
    smtp_password: SecretStr = SecretStr("")
    smtp_password_file: Path | None = None
    smtp_from: str = ""
    smtp_starttls: bool = True

    @model_validator(mode="after")
    def validate_secrets(self):
        for name in ("database_url", "api_token", "tushare_token", "smtp_password"):
            path = getattr(self, name + "_file")
            if path:
                setattr(self, name, SecretStr(path.read_text(encoding="utf-8").strip()))
        if self.provider != "tushare" or self.environment not in {"production", "test"}:
            raise ValueError("Only entitled Tushare data is supported; no public/demo fallback.")
        if not self.database_url.get_secret_value().startswith(("postgresql://", "postgresql+psycopg://")):
            raise ValueError("PostgreSQL is required.")
        if self.api_token.get_secret_value() and len(self.api_token.get_secret_value()) < 32:
            raise ValueError("The operator token must contain at least 32 characters.")
        return self

    @property
    def dsn(self):
        return self.database_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://", 1)
