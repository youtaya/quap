"""Entitled data-provider boundary; synthetic/public fallbacks are intentionally absent."""

from typing import Protocol


class ProviderError(RuntimeError):
    pass


class PermissionDenied(ProviderError):
    pass


class Deferred(ProviderError):
    def __init__(self, message, seconds=60):
        super().__init__(message)
        self.seconds = seconds


class MarketProvider(Protocol):
    def request(self, endpoint: str, params: dict, fields: str) -> list[dict]: ...
