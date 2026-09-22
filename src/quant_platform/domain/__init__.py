"""Provider-independent identities, sessions and immutable research inputs."""

import hashlib
import json
import math
import re
from datetime import date, datetime, time, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CN = ZoneInfo("Asia/Shanghai")


def now():
    return datetime.now(timezone.utc)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, allow_nan=False).encode()).hexdigest()


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def board(symbol):
    exchange, prefix = symbol[:2], symbol[2:5]
    if exchange == "SH" and prefix in {"688", "689"}:
        return "STAR Market"
    if exchange == "SZ" and prefix in {"300", "301"}:
        return "ChiNext"
    if (exchange == "SH" and prefix in {"600", "601", "603", "605"}) or (
        exchange == "SZ" and prefix in {"000", "001", "002", "003"}
    ):
        return "Main Board"
    raise ValueError("Unsupported Shanghai/Shenzhen A-share identity.")


def symbol(value):
    if not isinstance(value, str):
        raise ValueError("Symbols must be strings.")
    value = value.strip().upper()
    if re.fullmatch(r"[0-9]{6}\.(SH|SZ)", value):
        value = value[-2:] + value[:6]
    elif re.fullmatch(r"[0-9]{6}", value):
        value = ("SH" if value.startswith("6") else "SZ") + value
    if not re.fullmatch(r"(SH|SZ)[0-9]{6}", value):
        raise ValueError("Invalid exchange-qualified symbol.")
    board(value)
    return value


def provider_code(value):
    value = symbol(value)
    return value[2:] + "." + value[:2]


def session(at, is_open):
    """None is unknown calendar coverage, never a guessed weekday session."""
    local = at.astimezone(CN)
    if is_open is None:
        return "unverified", False
    if not is_open:
        return "closed", False
    t = local.time()
    if time(9, 30) <= t <= time(11, 30):
        return "morning", True
    if time(13) <= t <= time(15):
        return "afternoon", True
    return ("pre-open" if t < time(9, 30) else "lunch" if t < time(13) else "closed"), False


def source_time(value):
    """Accept only a source-supplied full date and time; never infer today's date."""
    if not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y%m%d%H%M%S", "%Y%m%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=CN)
        except ValueError:
            pass
    return None


def fresh(stamp, at=None):
    at = at or now()
    if isinstance(stamp, str):
        stamp = datetime.fromisoformat(stamp)
    return bool(
        stamp
        and stamp.tzinfo
        and stamp.astimezone(CN).date() == at.astimezone(CN).date()
        and -5 <= (at - stamp).total_seconds() <= 120
    )


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class AlertPolicy(StrictModel):
    stock_change: float = Field(0.03, gt=0)
    basket_change: float = Field(0.01, gt=0)
    minimum_coverage: float = Field(0.95, gt=0, le=1)
    cooldown: int = Field(300, ge=1)


class BasketInput(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    members: dict[str, float] = Field(min_length=1, max_length=1000)
    expected_revision: int = Field(0, ge=0)
    alerts: AlertPolicy = Field(default_factory=AlertPolicy)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value):
        if not value.strip():
            raise ValueError("Basket name must not be blank.")
        return value.strip()

    @field_validator("members")
    @classmethod
    def validate_members(cls, value):
        result = {}
        for code, weight in value.items():
            key = symbol(code)
            if key in result or number(weight) is None or weight <= 0:
                raise ValueError("Duplicate identities or invalid positive weights.")
            result[key] = float(weight)
        total = sum(result.values())
        if not math.isfinite(total):
            raise ValueError("Invalid total weight.")
        return {key: val / total for key, val in sorted(result.items())}


class ScreenRule(StrictModel):
    minimum_bars: int = Field(120, ge=61, le=2000)
    minimum_turnover: float = Field(20_000_000, ge=0)
    top_n: int = Field(20, ge=1, le=200)
    exclude_risk_names: bool = True
    pe_weight: float = Field(0.4, ge=0, le=1)
    pb_weight: float = Field(0.3, ge=0, le=1)
    pe_history_weight: float = Field(0.2, ge=0, le=1)
    pb_history_weight: float = Field(0.1, ge=0, le=1)
    max_pe_ttm: float = Field(80, gt=0)
    max_pb: float = Field(10, gt=0)
    min_industry_peers: int = Field(8, ge=1, le=500)
    history_sessions: int = Field(252, ge=2, le=2000)

    @model_validator(mode="before")
    @classmethod
    def drop_unknown_fields(cls, value):
        if isinstance(value, dict):
            return {key: item for key, item in value.items() if key in cls.model_fields}
        return value


class HoldingPolicy(StrictModel):
    exit_loss: float = Field(0.15, gt=0, le=1)
    reduce_gain: float = Field(0.20, gt=0)
    add_max_pnl: float = Field(0.02)
    reduce_if_not_undervalued: bool = True
    alert_change: float = Field(0.05, gt=0)
    cooldown: int = Field(300, ge=1)


class HoldingInput(StrictModel):
    symbol: str
    cost_price: float = Field(gt=0)
    quantity: float | None = Field(None, gt=0)
    note: str = Field("", max_length=500)
    expected_revision: int = Field(0, ge=0)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value):
        return symbol(value)

    @field_validator("note")
    @classmethod
    def strip_note(cls, value):
        return value.strip()


class HistoryRepository(Protocol):
    def history(self, code: str, as_of: date, limit: int = 500) -> list[dict]: ...


class AnalysisBackend(Protocol):
    def indicators(self, rows: list[dict]) -> dict: ...
