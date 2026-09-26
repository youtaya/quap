"""Contracts shared by the control plane and the mandatory Qlib engine."""

import math
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from quant_platform.domain import CN, StrictModel, symbol

ENGINE_VERSION = "qlib-v1"
DATA_CONTRACT = "qlib-adjusted-v1"
Frequency = Literal["day", "5min"]


class PipelineBlocked(RuntimeError):
    """A missing prerequisite, never permission to use a substitute model."""


class ScanPolicy(StrictModel):
    price_ceiling: float = Field(10, gt=0)
    minimum_sessions: int = Field(120, ge=60, le=2000)
    minimum_turnover: float = Field(20_000_000, ge=0)
    top_n: int = Field(20, ge=1, le=20)
    max_weight: float = Field(0.05, gt=0, le=0.05)
    minimum_cash: float = Field(0.05, ge=0.05, le=1)
    change_band: float = Field(0.005, ge=0, le=0.05)
    minimum_coverage: float = Field(0.95, ge=0.95, le=1)
    exclude_risk_names: Literal[True] = True


class PolicyChange(StrictModel):
    expected_revision: int = Field(ge=0)
    policy: ScanPolicy


class PortfolioInput(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    weights: dict[str, float] = Field(default_factory=dict, max_length=300)
    cash_weight: float = Field(1, ge=0, le=1)
    expected_revision: int = Field(0, ge=0)

    @field_validator("name")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("A portfolio name is required.")
        return value.strip()

    @field_validator("weights")
    @classmethod
    def identities(cls, value):
        result = {}
        for code, weight in value.items():
            code = symbol(code)
            if code in result or not math.isfinite(weight) or not 0 < weight <= 1:
                raise ValueError("Weights must be unique, finite, positive fractions.")
            result[code] = weight
        return dict(sorted(result.items()))

    @model_validator(mode="after")
    def total(self):
        if not math.isclose(sum(self.weights.values()) + self.cash_weight, 1, abs_tol=1e-8):
            raise ValueError("Stock weights plus cash must equal one; weights are never normalized silently.")
        return self


class WatchlistInput(StrictModel):
    symbols: list[str] = Field(default_factory=list, max_length=300)
    expected_revision: int = Field(0, ge=0)

    @field_validator("symbols")
    @classmethod
    def identities(cls, value):
        parsed = [symbol(item) for item in value]
        if len(set(parsed)) != len(parsed):
            raise ValueError("Duplicate watchlist identities.")
        return sorted(parsed)


class RunInput(StrictModel):
    purpose: Literal["inference", "training", "shadow"] = "inference"
    frequency: Frequency = "day"
    model_id: UUID | None = None
    training_configuration_id: UUID | None = None
    as_of: datetime | None = None
    portfolio_id: UUID | None = None
    request_key: str = Field(min_length=1, max_length=120)

    @field_validator("as_of")
    @classmethod
    def aware(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("An explicit time zone is required.")
        return value

    @model_validator(mode="after")
    def shadow_model(self):
        if (self.purpose == "shadow") != (self.model_id is not None):
            raise ValueError("Only shadow requests require an explicit challenger model ID.")
        if self.training_configuration_id is not None and self.purpose != "training":
            raise ValueError("Training configurations are accepted only for explicit training requests.")
        return self


class Acceptance(StrictModel):
    portfolio_id: UUID | None = None
    selected_symbols: list[str] | None = Field(None, min_length=1, max_length=300)

    @field_validator("selected_symbols")
    @classmethod
    def selections(cls, value):
        return WatchlistInput.identities(value) if value is not None else None

    name: str = Field(min_length=1, max_length=120)
    expected_revision: int = Field(ge=0)
    expected_policy_revision: int = Field(ge=0)
    request_key: str = Field(min_length=1, max_length=120)


class ReleaseAction(StrictModel):
    expected_active_id: UUID | None = None


def five_minute_end(at):
    local = at.astimezone(CN)
    minute = local.hour * 60 + local.minute
    if not (575 <= minute <= 690 or 785 <= minute <= 900):
        return None
    return local.replace(minute=local.minute // 5 * 5, second=0, microsecond=0)


def intraday_window(cutoff, available):
    cutoff, available = cutoff.astimezone(CN), available.astimezone(CN)
    if available < cutoff or available > cutoff + timedelta(seconds=120):
        raise PipelineBlocked("Intraday publication deadline missed.")
    effective = cutoff + timedelta(minutes=5)
    session_end = cutoff.replace(hour=11 if cutoff.hour < 12 else 15, minute=30 if cutoff.hour < 12 else 0)
    if effective >= session_end:
        raise PipelineBlocked("No future execution-reference interval in this session.")
    return effective, min(cutoff + timedelta(minutes=7), session_end)


def action_for(previous, target, band=0.005):
    if target is None:
        return "unavailable"
    if previous == 0 and target == 0:
        return "watch"
    if previous > 0 and target == 0:
        return "exit"
    if abs(target - previous) < band:
        return "hold"
    return "add" if previous == 0 else "increase" if target > previous else "reduce"
