"""Explicit research-only inputs; never production provider configuration."""

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from quant_platform.domain import StrictModel, symbol

ADATA_VERSION = "2.9.5"
SOURCE_IDS = Literal["adata-eastmoney-day", "adata-eastmoney-intraday"]
SOURCE_CATALOG = {
    key: {
        "source_id": key,
        "provider": "adata",
        "provider_version": ADATA_VERSION,
        "upstream": "eastmoney",
        "endpoint": endpoint,
        "frequency": frequency,
        "market": "Shanghai/Shenzhen A shares",
        "price_unit": "CNY",
        "volume_unit": "shares",
        "amount_unit": "CNY",
        "adjustment": "raw",
        "history_limit": history,
        "historical_availability": "unknown",
        "permitted_purpose": ["research", "display", "cross_check"],
        "production_eligible": False,
        "transport_note": note,
    }
    for key, endpoint, frequency, history, note in (
        (
            "adata-eastmoney-day",
            "get_market",
            "day",
            "upstream-dependent; unverified",
            "Pinned adapter requests HTTP; unavailable under the HTTPS-only contract.",
        ),
        (
            "adata-eastmoney-intraday",
            "get_market_min",
            "1min",
            "same Shanghai session only",
            "HTTPS-only, no redirects, no proxy, bounded response and deadline.",
        ),
    )
}


class ResearchMutation(StrictModel):
    request_key: str = Field(min_length=1, max_length=120)
    expected_revision: int = Field(ge=0)


class SourceChange(ResearchMutation):
    enabled: bool
    terms_acknowledged: bool = False

    @model_validator(mode="after")
    def explicit_terms(self):
        if self.enabled and not self.terms_acknowledged:
            raise ValueError("Enabling a source requires acknowledging its upstream data terms.")
        return self


class CollectionInput(ResearchMutation):
    source_id: SOURCE_IDS
    symbols: list[str] = Field(min_length=1, max_length=50)
    start: date
    end: date

    @field_validator("symbols")
    @classmethod
    def identities(cls, values):
        codes = [symbol(value) for value in values]
        if len(set(codes)) != len(codes):
            raise ValueError("Duplicate stock identities are not allowed.")
        return sorted(codes)

    @model_validator(mode="after")
    def bounded_range(self):
        if self.end < self.start or (self.end - self.start).days > 3660:
            raise ValueError("Research date range must be ordered and at most ten years.")
        if self.source_id.endswith("intraday") and self.start != self.end:
            raise ValueError("Intraday research is limited to one current session.")
        return self


class FactorInput(ResearchMutation):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    expression: str = Field(min_length=1, max_length=2048)
    author: str = Field(min_length=1, max_length=120)
    origin: Literal["operator", "imported_research"] = "operator"

    @field_validator("expression")
    @classmethod
    def safe_expression(cls, value):
        from quant_platform.research.factors import compile_factor

        compile_factor(value)
        return value


class FactorSetInput(ResearchMutation):
    name: str = Field(min_length=1, max_length=120)
    factors: list[UUID] = Field(min_length=1, max_length=64)

    @field_validator("factors")
    @classmethod
    def unique_factors(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("Factor IDs must be unique; order is part of the contract.")
        return values


class TrainingConfigurationInput(ResearchMutation):
    name: str = Field(min_length=1, max_length=120)
    frequency: Literal["day", "5min"] = "day"
    factor_set_id: UUID | None = None
    learning_rate: float = Field(0.05, ge=0.001, le=0.3)
    num_leaves: int = Field(31, ge=2, le=127)
    rounds: int = Field(1000, ge=10, le=2000)
    seed: int = Field(42, ge=0, le=2147483647)
    train_sessions: int = Field(756, ge=10, le=1500)
    validation_sessions: int = Field(252, ge=5, le=500)
    test_sessions: int = Field(252, ge=5, le=500)
    fixture_windows: bool = False

    @model_validator(mode="after")
    def feature_scope(self):
        if self.frequency == "5min" and self.factor_set_id:
            raise ValueError("Custom features are daily-only; the five-minute feature contract is unchanged.")
        if not self.fixture_windows:
            expected = (756, 252, 252) if self.frequency == "day" else (120, 40, 60)
            if (self.train_sessions, self.validation_sessions, self.test_sessions) != expected:
                raise ValueError("Reduced or altered windows require explicit test-only fixture configuration.")
        return self


class ExperimentInput(ResearchMutation):
    generation_id: UUID
    baseline_configuration_id: UUID
    candidate_configuration_id: UUID


class FreezeInput(ResearchMutation):
    configuration_id: UUID
