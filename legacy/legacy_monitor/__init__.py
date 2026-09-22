# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Public-feed A-share monitoring with optional Qlib historical context.

This package does not register or replace Qlib's historical data providers and
never submits trades. Streamlit is needed only for the example dashboard.
"""

from .alerts import AlertStore
from .engine import MonitorEngine
from .history import QlibHistory
from .market import TradingCalendar, parse_baskets
from .models import AlertSettings, Basket, Quote, Snapshot
from .providers import DemoProvider, EastmoneyProvider, FeedError, QuoteProvider

__all__ = [
    "AlertStore",
    "MonitorEngine",
    "QlibHistory",
    "TradingCalendar",
    "parse_baskets",
    "AlertSettings",
    "Basket",
    "Quote",
    "Snapshot",
    "DemoProvider",
    "EastmoneyProvider",
    "FeedError",
    "QuoteProvider",
]
