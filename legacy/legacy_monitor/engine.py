# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Single-session polling orchestration with bounded state and no trading side effects."""

from __future__ import annotations

import sqlite3
import time
from collections import deque
from dataclasses import asdict
from threading import Lock

from .market import TradingCalendar, fingerprint, market_members, quote_observation, summarize
from .models import AlertSettings, PollResult, SHANGHAI, number, shanghai_now
from .providers import FeedError


class MonitorEngine:
    """Fetch complete snapshots, calculate proxies, and evaluate fresh observations.

    Parameters
    ----------
    provider : QuoteProvider
        Live or explicitly simulated provider.
    store : AlertStore or None
        Persistent local event/state store; None disables alert evaluation visibly.
    history : QlibHistory, optional
        Historical context, trading calendar, and dated basket membership.
    notifier : callable, optional
        Receives persisted events for local/log callbacks. Failures are visible
        and never discard quotes or already-saved events.
    """

    def __init__(
        self,
        provider,
        store,
        baskets=(),
        settings=None,
        history=None,
        interval=30,
        clock=shanghai_now,
        monotonic=time.monotonic,
        notifier=None,
    ):
        if number(interval) is None or interval < 15:
            raise ValueError("轮询间隔至少为 15 秒。")
        if any(basket.universe for basket in baskets) and history is None:
            raise ValueError("基于 Qlib 宇宙的篮子需要可用的本地 Qlib 数据集。")
        self.provider, self.store, self.baskets, self.history = provider, store, baskets, history
        self.notifier = notifier
        self.settings = settings or AlertSettings()
        self.interval, self.clock, self.monotonic = interval, clock, monotonic
        self.calendar = history.calendar if history else TradingCalendar()
        self.result = PollResult()
        self.samples = deque(maxlen=600)
        self.members = {}
        self.next_poll = 0.0
        self.last_attempt = None
        self.effective_interval = None
        self.failures = 0
        self._lock = Lock()
        self._scope = None
        self._last_observation = None

    def observation_time(self):
        if self.provider.mode == "demo" and self.result.snapshot:
            return self.result.snapshot.fetched_at
        return self.clock()

    def _session(self, now):
        calendar = TradingCalendar() if self.provider.mode == "demo" else self.calendar
        return calendar.session(now)

    def view(self):
        """Recompute freshness for display without re-evaluating any alert."""
        now = self.observation_time()
        session, status, active = self._session(now)
        self.result.session = session
        self.result.calendar_status = "simulated" if self.provider.mode == "demo" else status
        if self.result.snapshot:
            quotes = {q.symbol: q for q in self.result.snapshot.quotes}
            if not active:
                dated = [
                    q.timestamp.astimezone(SHANGHAI).date()
                    for q in quotes.values()
                    if q.timestamp is not None and q.timestamp.tzinfo is not None and q.timestamp <= now
                ]
                reference = max(dated) if dated else None
                quotes = {
                    s: q
                    for s, q in quotes.items()
                    if q.timestamp is not None
                    and q.timestamp.tzinfo is not None
                    and q.timestamp.astimezone(SHANGHAI).date() == reference
                    and q.timestamp <= now
                }
            self.result.summaries = {
                name: summarize(name, members, quotes, now, require_fresh=active)
                for name, members in self.members.items()
            }
        return self.result

    def poll(self, force=False):
        """Poll only when due; manual live refresh still respects throttling/backoff."""
        acquired = self._lock.acquire(blocking=False)  # pylint: disable=R1732
        if not acquired:
            return self.view()
        try:
            return self._poll_locked(force)
        finally:
            self._lock.release()

    def _poll_locked(self, force):
        tick = self.monotonic()
        if tick < self.next_poll and not (force and self.provider.mode == "demo"):
            return self.view()
        self.result.events = []
        self.result.storage_error = None
        self.result.notify_error = None
        if self.last_attempt is not None:
            self.effective_interval = tick - self.last_attempt
        self.last_attempt = tick
        try:
            snapshot = self.provider.fetch()
            if snapshot.mode != self.provider.mode or not snapshot.quotes:
                raise FeedError("行情源返回了空快照，或模式标记不正确。")
            now = snapshot.fetched_at if snapshot.mode == "demo" else self.clock()
            baskets = self.history.resolve(self.baskets, now.date()) if self.history else self.baskets
            quotes = {q.symbol: q for q in snapshot.quotes}
            if len(quotes) != len(snapshot.quotes):
                raise FeedError("行情源返回了重复代码。")
            members = market_members(quotes, baskets)
            scope = fingerprint((now.date(), members, asdict(self.settings)))
            if scope != self._scope:
                self.samples.clear()
                self._last_observation = None
                self._scope = scope
            self.members = members
            self.result.snapshot = snapshot
            self.result.error = None
            self.failures = 0
            self.view()
            _, _, active = self._session(now)
            fresh_summaries = {name: summarize(name, group, quotes, now) for name, group in members.items()}
            identity = fingerprint(sorted((q.symbol, quote_observation(q)) for q in snapshot.quotes))
            if active and identity != self._last_observation:
                if any(summary.eligible for summary in fresh_summaries.values()):
                    self.samples.append({"time": now, **{n: s.proxy for n, s in fresh_summaries.items()}})
                self._last_observation = identity
            self._evaluate_alerts(quotes, fresh_summaries, snapshot, scope, now, active)
        except (FeedError, ValueError, OSError, RuntimeError) as exc:
            self.failures += 1
            self.result.error = f"刷新失败，界面保留的不是实时数据：{exc}"
        finally:
            self.result.duration = self.monotonic() - tick
            delay = self.interval * min(2 ** min(self.failures, 4), 16)
            self.next_poll = self.monotonic() + delay
        return self.view()

    def _evaluate_alerts(self, quotes, summaries, snapshot, scope, now, active):
        if self.store is None:
            self.result.storage_error = "告警库不可用，已停止评估告警。"
            return
        if not active:
            return
        try:
            self.result.events = self.store.evaluate(
                self._observations(quotes, summaries, now),
                snapshot.source,
                snapshot.mode,
                scope,
                now,
                self.settings.cooldown,
            )
        except (sqlite3.Error, OSError) as exc:
            self.result.storage_error = f"告警写入失败，事件未保存：{exc}"
            return
        if not self.result.events or self.notifier is None:
            return
        try:
            self.notifier(self.result.events)
        except Exception as exc:
            self.result.notify_error = f"告警通知失败：{exc}"

    def _observations(self, quotes, summaries, now):
        observations = []
        settings = self.settings
        for quote in quotes.values():
            if not quote.fresh(now) or quote.daily_return is None:
                continue
            identity = quote_observation(quote)
            observations.append(
                (quote.symbol, "absolute_daily_return", abs(quote.daily_return), settings.stock_change, identity)
            )
            if settings.turnover is not None and number(quote.turnover) is not None and quote.turnover >= 0:
                observations.append(
                    (quote.symbol, "cumulative_turnover_cny", quote.turnover, settings.turnover, identity)
                )
        if not getattr(self.provider, "directory_fresh", True):
            return observations
        for name, summary in summaries.items():
            if summary.daily_return is None or summary.coverage < settings.minimum_coverage:
                continue
            observations.append(
                (name, "absolute_daily_return", abs(summary.daily_return), settings.basket_change, summary.observation)
            )
            if settings.turnover is not None and summary.turnover_coverage >= settings.minimum_coverage:
                observations.append(
                    (name, "cumulative_turnover_cny", summary.turnover, settings.turnover, summary.observation)
                )
        return observations
