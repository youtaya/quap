"""Tokenless A-share market data: pytdx → Eastmoney → Sina → Tencent degradation.

One source serves a whole capability request. A source that times out, returns an empty body, or
returns fields that break the platform contract is abandoned for the next source in the go-stock
A-share order, and the source that actually answered is recorded in dataset metadata.

Units are normalized at this boundary: volume is shares, turnover is CNY. Each source's conversion
is locked by fixture tests. Missing values stay ``None``; they are never zero, and a missing bar is
never interpreted as a suspension.
"""

import re
import time
from datetime import datetime, timedelta, timezone
from functools import partial

from quant_platform.domain import CN, board, digest, now, number, symbol
from . import SOURCES, Deferred, ProviderError, prices
from .sources import (
    EastMoney,
    Sina,
    Tdx,
    Tencent,
    Transport,
    factor_ratio,
    symbol_or_index,
)

# Which sources can serve each capability, in degradation order. ``constraints`` is derived from
# collected prices and ``security_history`` is observed from the security master, not announced.
# 主表额外保留新浪兜底：东方财富与通达信都不可达时，主表仍须可用。
CAPABILITY_SOURCES = {
    "securities": ("eastmoney", "pytdx", "sina"),
    "calendar": ("pytdx", "eastmoney", "sina", "tencent"),
    "history": ("pytdx", "eastmoney", "sina", "tencent"),
    "factors": ("eastmoney", "tencent"),
    "benchmark": ("pytdx", "eastmoney", "sina", "tencent"),
    "quotes": ("pytdx", "eastmoney", "sina", "tencent"),
    "minutes": ("pytdx", "eastmoney", "sina", "tencent"),
    # 纯推导：没有可探测的源，也没有源响应可以校验。``constraints`` 由前收盘推限价，
    # ``security_history`` 由证券主表的名称推当前风险状态（公开源不发布有日期的更名史），所以两者
    # 的「契约已验证」就是推导代码本身。把它们写成源能力会让 readiness 门禁永远不成立。
    "constraints": ("derived",),
    "security_history": ("derived",),
}
MINUTE_ENDPOINTS = {"stk_mins", "rt_min", "rt_min_daily"}
# 主板 ±10%，科创板/创业板 ±20%，风险警示 ±5%。公开源不发布官方涨跌停价，只能按前收盘推导。
LIMIT_RATES = {"STAR Market": 0.2, "ChiNext": 0.2, "Main Board": 0.1}
RISK_LIMIT_RATE = 0.05
# 风险警示标记只出现在名称开头，形如 `ST`/`*ST`/`SST`/`S*ST`/`S ST`；`*` 与空格只属于标记本身。
# 去掉这些标记字符后判断开头，就不会把 `TEST` 这类名称误判成风险警示股。
RISK_WARNING_PREFIX = re.compile(r"^(?:SST|ST)")
RISK_MARKER_CHARS = "* \t\u3000"
# 一个源刚以传输错误失败后，在冷却窗口内直接跳过它。没有这一步，降级链会为每一次请求把不可达的
# 源重新超时一遍：通达信 10 秒、东方财富 4.5 秒，298 只证券的历史回填因此要一个多小时才走完。
# 窗口刻意开得短，让源不必等运维动作就能自行恢复。冷却只活在进程内：一个采集进程会在同一个
# `Market` 实例上跑完一整个任务的全部证券，那正是浪费发生的地方。
SOURCE_COOLDOWN_SECONDS = 120


def risk_warning(name):
    """Whether an exchange-published security name carries a risk-warning marker.

    The marker only ever appears at the start of the name (``ST``/``*ST``/``SST``/``S*ST``), and
    delisting names carry ``退``. ``*`` and spaces belong to the marker itself, so they are removed
    before the prefix is read — a plain substring test would read ``TEST`` as a risk warning.
    """
    compact = "".join(ch for ch in str(name or "").upper() if ch not in RISK_MARKER_CHARS)
    return bool(RISK_WARNING_PREFIX.match(compact)) or "退" in compact


def limit_rate(code, name=None):
    """Price-limit band for one security: 20% on STAR/ChiNext, 5% for main-board risk warning, else 10%.

    Public sources publish no official limit prices, so the band is derived from the board. The 5%
    risk-warning band only narrows the main board: STAR Market and ChiNext keep their 20% band even
    for risk-warning names, so the marker must not override the board band.
    """
    band = LIMIT_RATES.get(board(code), 0.1)
    return RISK_LIMIT_RATE if (risk_warning(name) and band == LIMIT_RATES["Main Board"]) else band


def paged(fetch, add, limit):
    """Walk one paged listing until the source announces everything it has.

    Four listings page identically — Eastmoney's security master, Sina's two exchange nodes, and CSI
    300 membership on both sources — and share the same two stop conditions: a page that comes back
    empty, or as many rows as the source announced. Only the stop condition lives here, so it cannot
    drift apart between sources.

    ``fetch(page)`` returns ``(rows, announced_total)``. Some sources ride the total along with the
    first page and some need a separate request, so the caller resolves it and returns ``None`` on
    the pages where it is not known; the total is then read from the first page only, which keeps the
    extra request to at most one per listing.

    ``add(rows)`` folds a page into the caller's accumulator and returns how far that page brought
    the listing towards the announced total: distinct rows where the source lists only securities the
    caller keeps, raw rows where it interleaves others that are dropped. Returns ``(covered, total)``.
    """
    total, covered = None, 0
    for page in range(1, limit + 1):
        rows, announced = fetch(page)
        if total is None:
            total = announced
        covered = add(rows)
        if not rows or (total is not None and covered >= total):
            break
    return covered, total


class SourceBudget:
    """Per-source request windows; a source over its share defers instead of being silently skipped."""

    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        self.key = "public-market"

    def acquire(self, source, realtime=False, minute=False):
        at = now()
        minute_window = at.replace(second=0, microsecond=0)
        day_window = at.astimezone(CN).replace(hour=0, minute=0, second=0, microsecond=0)
        if minute:
            limit, daily_limit = self.settings.minute_rpm, self.settings.minute_daily_quota
        elif realtime:
            limit, daily_limit = self.settings.realtime_rpm, self.settings.daily_quota
        else:
            limit, daily_limit = self.settings.ordinary_rpm, self.settings.daily_quota
        with self.db.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (self.key + ":" + source,))
            for window, name, maximum in ((minute_window, source, limit), (day_window, source + ":day", daily_limit)):
                row = conn.execute(
                    "SELECT used FROM quota WHERE credential=%s AND endpoint=%s AND window_start=%s",
                    (self.key, name, window),
                ).fetchone()
                if row and row["used"] >= maximum:
                    seconds = 60 if name == source else int((day_window + timedelta(days=1) - at).total_seconds())
                    raise Deferred("Source quota exhausted; request deferred.", seconds)
            for window, name in ((minute_window, source), (day_window, source + ":day")):
                conn.execute(
                    "INSERT INTO quota VALUES(%s,%s,%s,1) ON CONFLICT(credential,endpoint,window_start) "
                    "DO UPDATE SET used=quota.used+1",
                    (self.key, name, window),
                )


class Market:
    """A-share market data behind one degradation order and one unit contract."""

    name = "public-market"

    def __init__(self, settings, db=None, client=None, sleep=time.sleep, tdx=None):
        self.settings = settings
        self.db = db
        self.sleep = sleep
        self.transport = Transport(client, sleep)
        self.tdx = tdx if tdx is not None else Tdx()
        self.eastmoney = EastMoney(self.transport)
        self.sina = Sina(self.transport)
        self.tencent = Tencent(self.transport)
        self.budget = SourceBudget(db, settings) if db else None
        self.last_sources = {}
        self.listing_dates_filled = 0
        self._recorded = set()
        self._cooldown = {}

    def close(self):
        self.transport.close()
        self.tdx.close()

    # ---------------------------------------------------------------- degradation
    def _acquire(self, source, realtime=False, minute=False):
        if self.budget:
            self.budget.acquire(source, realtime, minute)

    def _is_cooling(self, source):
        """Whether ``source`` is still inside its post-failure cooldown window."""
        until = self._cooldown.get(source)
        if until is None:
            return False
        if time.monotonic() < until:
            return True
        del self._cooldown[source]  # 冷却结束，再给它一次机会，无需运维介入
        return False

    def _degrade(self, capability, attempts, accept=bool):
        """Try each (source, callable) in order; the first contract-valid result wins."""
        failures, deferred = [], False
        for source, call in attempts:
            if self._is_cooling(source):
                failures.append(f"{source}:cooling")
                continue
            self._acquire(source, realtime=capability == "quotes", minute=capability == "minutes")
            try:
                rows = call()
            except Deferred as exc:
                deferred = True
                failures.append(f"{source}:{type(exc).__name__}")
                continue
            except ProviderError as exc:
                # 传输或解析失败是这个源此刻的状态，冷却它。契约性的空响应（accept 为假）只对本次
                # 请求成立——停牌证券在哪个源上都是空的——所以不冷却。
                self._cooldown[source] = time.monotonic() + SOURCE_COOLDOWN_SECONDS
                failures.append(f"{source}:{type(exc).__name__}")
                continue
            if not accept(rows):
                failures.append(f"{source}:rejected")
                continue
            self.last_sources[capability] = source
            self._qualify(capability, source, rows)
            return source, rows
        self._qualify(capability, None, None, failures, deferred)
        raise ProviderError("No public source served %s (%s)." % (capability, ",".join(failures) or "none"))

    def _qualify(self, capability, source, rows, failures=None, deferred=False):
        """Publish this capability's qualification state; a reachability probe cannot prove it.

        ``readiness``, ``qualification`` and the dashboard all gate on ``schema_verified``, which
        means "the declared contract actually parsed out of a live source". Only a real collection
        can show that, and here the rows have already passed ``prices``/``symbol``/``number``, so the
        evidence is written at this boundary rather than from :func:`operations.doctor`. Swapping the
        vendor provider for this one dropped that write, which left every capability permanently
        unverified and every readiness check permanently blocked.

        A transient failure (any source deferred on quota or rate limit) records nothing, exactly
        like the deferred path of the vendor provider: it says nothing about the contract. The state
        is written at most once per capability per provider instance because one ``history`` job
        issues one request per security and would otherwise repeat the same row hundreds of times.
        """
        if self.db is None or capability in self._recorded:
            return
        self._recorded.add(capability)
        sources = list(CAPABILITY_SOURCES.get(capability, (source,) if source else ()))
        if source is not None:
            self.db.capability(
                capability,
                "reachable",
                {
                    "sources": sources,
                    "source": source,
                    "schema_verified": True,
                    "rows": len(rows),
                    "verified_at": now().isoformat(),
                },
            )
        elif not deferred:
            self.db.capability(
                capability,
                "circuit_open",
                {"sources": sources, "schema_verified": False, "failures": failures},
            )

    def _qualify_derived(self, capability, source, rows):
        """Record a capability whose rows are read out of another capability's live response.

        ``calendar`` and ``benchmark`` are read out of the same requests as ``history``, and
        ``factors`` out of the paired request ``_factors`` makes, so none of the three ever reaches
        ``_degrade`` under its own name. ``_qualify`` is keyed on the capability that issued the
        request, which left them permanently unverified — and the readiness gate requires every
        entry of ``DAILY_CAPABILITIES`` to carry ``schema_verified``, so it could never be
        satisfied: the platform reported itself blocked while it was in fact collecting.

        The evidence is unchanged from any other qualification — rows that parsed out of a source
        this process just talked to. ``source`` is ``None`` when no source served the request at
        all (an empty factor map), which is no evidence and records nothing.
        """
        if source is None:
            return
        self.last_sources[capability] = source
        self._qualify(capability, source, rows)

    # ---------------------------------------------------------------- capabilities
    def securities(self):
        """Security master: Eastmoney paginated list, then 通达信 list, then Sina's exchange nodes.

        Listing dates are a separate question from identity, so they are filled afterwards rather
        than being a fourth degradation branch: only 东方财富 publishes them at all, and the host
        that serves them is not the host that serves the master. See ``_attach_listing_dates``.
        """
        result = self._degrade(
            "securities",
            (
                ("eastmoney", self._eastmoney_directory),
                ("pytdx", self._tdx_directory),
                ("sina", self._sina_directory),
            ),
        )[1]
        self._attach_listing_dates(result)
        return result

    def _attach_listing_dates(self, result):
        """Fill missing listing dates; never let the enrichment fail the security master.

        通达信 and 新浪 publish no listing date, so whenever the master comes from either of them
        every ``list_date`` stays NULL and every ``list_date <= day`` filter degrades to "unknown
        means listed" — a look-ahead bias in the point-in-time universe. 东方财富's data-centre host
        is a different host from its paginated master and keeps answering when that one is refused,
        so the dates are read from there.

        Best effort by design: this is an enrichment of a capability that already succeeded, so a
        transport failure, a rejected envelope, or an exhausted quota leaves the master exactly as
        it was. The gap stays visible as ``list_dates_verified=false`` in ``settings.directory``.

        Whatever was read is kept. The first version returned early on an interruption, so an
        exhausted quota threw away every date already in hand — and it always ran out, because the
        500-row page size needed 50 requests against a 20-per-minute budget. The page size is now
        large enough that the whole master is a handful of requests, and a partial read still fills
        a partial answer.
        """
        if not result or all(row["list_date"] for row in result.values()):
            return
        known = {}

        def page(number):
            self._acquire("eastmoney")
            return self.eastmoney.listing_dates(number)

        def add(rows):
            known.update(rows)
            return len(known)

        try:
            paged(page, add, 12)
        except (ProviderError, Deferred):
            pass  # 已取到的那部分照样补上，剩下的留给下一次主表刷新
        filled = 0
        for code, row in result.items():
            if row["list_date"] is None and (listed := known.get(code[2:])):
                row["list_date"] = listed
                filled += 1
        self.listing_dates_filled = filled

    def _eastmoney_directory(self):
        result = {}

        def add(rows):
            for row in rows:
                self._add_security(result, row)
            return len(result)

        covered, announced = paged(lambda page: self.eastmoney.directory_page(page), add, 100)
        if announced and covered < announced * 0.9:
            raise ProviderError("Eastmoney security master looks truncated.")
        return result

    def _tdx_directory(self):
        result = {}
        params = self.tdx.params()
        for market in (params.MARKET_SH, params.MARKET_SZ):
            for row in self.tdx.directory(market):
                self._add_security(result, row)
        return result

    def _sina_directory(self):
        """Exchange-scoped pages; the combined ``hs_a`` node would bury 北交所 rows on page one."""
        result = {}
        for node in ("sh_a", "sz_a"):
            self._collect_sina_node(node, result)
        if not result:
            raise ProviderError("Sina security master returned no rows.")
        return result

    def _collect_sina_node(self, node, result):
        # 新浪的分页按「已取行数」而不是「已留行数」推进：节点里夹带的北交所/基金行会被丢掉，
        # 改用 distinct 计数会让循环一路翻到页数上限，白花几十个请求配额。
        seen = 0

        def add(rows):
            nonlocal seen
            seen += len(rows)
            for row in rows:
                self._add_security(result, row)
            return seen

        paged(partial(self._sina_page, node), add, 100)

    def _sina_page(self, node, page):
        """One Sina directory page; the node total rides along only on some nodes."""
        rows, count = self.sina.directory_page(page, node=node)
        if count is None and page == 1:
            count = self.sina.directory_count(node)
        return rows, count

    @staticmethod
    def _add_security(result, row):
        try:
            code = symbol(row["code"])
        except (ValueError, KeyError):
            return  # 北交所、基金和指数以外的代码一律丢弃
        name = str(row.get("name") or "").strip()
        if code in result and result[code]["name"] != name:
            raise ProviderError("Security master contains conflicting identities.")
        result[code] = {
            "symbol": code,
            "name": name,
            "board": board(code),
            "exchange": "SSE" if code.startswith("SH") else "SZSE",
            "list_status": row.get("list_status") or "L",
            "list_date": row.get("list_date"),
            "delist_date": row.get("delist_date"),
        }

    def constituents(self):
        """Current CSI 300 membership used to scope default history collection.

        The default ``history_scope`` is ``index``, so an empty membership leaves every daily
        collection with nothing to fetch. Eastmoney's board page is therefore degraded to Sina's
        ``hs300`` node exactly like the security master, instead of silently returning an empty set.
        """
        return self._degrade(
            "constituents",
            (
                ("eastmoney", self._eastmoney_constituents),
                ("sina", self._sina_constituents),
            ),
        )[1]

    def _eastmoney_constituents(self):
        members = set()

        def add(rows):
            members.update(rows)
            return len(members)

        paged(lambda page: self.eastmoney.constituents(page), add, 20)
        return members

    def _sina_constituents(self):
        members = set()

        def add(rows):
            members.update(row["code"] for row in rows)
            return len(members)

        paged(partial(self._sina_page, "hs300"), add, 10)
        return members

    def calendar(self, start, end):
        """Open sessions from the two composite indexes; every other day in range is closed.

        SSE 与 SZSE 分开推导，同一区间各自落库。索引 K 线里没有的日期即该交易所当天休市。
        """
        calendars = {"SSE": {}, "SZSE": {}}
        records = []
        for exchange, code in (("SSE", "SH000001"), ("SZSE", "SZ399001")):
            opened = {row["day"] for row in self.history(code, start, end)["bars"] if row["close"] is not None}
            cursor = start
            while cursor <= end:
                calendars[exchange][cursor] = cursor in opened
                records.append((exchange, cursor, cursor in opened))
                cursor += timedelta(days=1)
        # 两根指数 K 线走的是 ``history`` 的请求，所以日历的契约验证必须在这里显式记一次，见
        # ``_qualify_derived``。
        self._qualify_derived("calendar", self.last_sources.get("history"), records)
        return calendars

    def history(self, code, start, end, limit=4000):
        """One security's raw daily bars plus its qfq/raw adjustment factor, from one source."""
        code = symbol_or_index(code)

        def accept(payload):
            return bool(payload[0])

        source, payload = self._degrade(
            "history", [(name, self._history_call(name, code, start, end, limit)) for name in SOURCES], accept
        )
        bars, qfq = payload
        factors, factor_source = self._factors(code, start, end, bars, qfq, source, limit)
        return {
            "symbol": code,
            "source": source,
            "factor_source": factor_source,
            "bars": bars,
            "factors": factors,
        }

    def _history_call(self, source, code, start, end, limit):
        def call():
            if source == "pytdx":
                return self.tdx.daily(code, start, end, limit=min(limit, 800)), None
            if source == "eastmoney":
                return self.eastmoney.daily_pair(code, start, end, limit=limit)
            if source == "sina":
                return self.sina.daily(code, start, end, limit=min(limit, 1970)), None
            return self.tencent.daily_pair(code, start, end, limit=min(limit, 2000))

        return call

    def _factors(self, code, start, end, bars, qfq, source, limit):
        """qfq/raw from the source that already answered, else a paired request to one that can.

        This is the same degradation question as ``_degrade``, so it honours the same cooldown: a
        factor source that just failed must not be re-timed-out once per security.
        """
        raw = {row["day"]: row["close"] for row in bars}
        if qfq is not None:
            factors = factor_ratio(raw, qfq)
            self._qualify_derived("factors", source, factors)
            return factors, source
        for other in ("eastmoney", "tencent"):
            if other == source or self._is_cooling(other):
                continue
            self._acquire(other)
            try:
                pair = (
                    self.eastmoney.daily_pair(code, start, end, limit=limit)
                    if other == "eastmoney"
                    else self.tencent.daily_pair(code, start, end, limit=min(limit, 2000))
                )
            except ProviderError:
                self._cooldown[other] = time.monotonic() + SOURCE_COOLDOWN_SECONDS
                continue
            other_bars, other_qfq = pair
            factors = factor_ratio({row["day"]: row["close"] for row in other_bars}, other_qfq)
            if factors:
                # 因子来自真实成对价格，但走的是这里而不是 ``_degrade``，见 ``_qualify_derived``。
                self._qualify_derived("factors", other, factors)
                return factors, other
        # 拿不到成对价格时，该标的当天的因子任务记为部分缺失；缺失 K 线不解释成停牌。
        return {}, None

    def benchmark(self, start, end):
        """CSI 300 history, recorded under the platform's canonical index identity."""
        result = self.history("SH000300", start, end)
        # SH000300 不是上市证券，不在 ``scoped_codes`` 里，所以它自己的请求就是基准能力的契约验证，
        # 见 ``_qualify_derived``。
        self._qualify_derived("benchmark", result["source"], result["bars"])
        return {**result, "symbol": "SH000300"}

    def quotes(self, codes):
        """Realtime rows with a source-supplied full timestamp; undated rows degrade to the next source."""
        codes = sorted(set(symbol(code) for code in codes))
        return self._degrade("quotes", [(name, self._quotes_call(name, codes)) for name in SOURCES])[1]

    def _quotes_call(self, source, codes):
        def call():
            if source == "pytdx":
                rows = self.tdx.quotes(codes)
            elif source == "eastmoney":
                rows = self.eastmoney.quotes(codes)
            elif source == "sina":
                rows = self.sina.quotes(codes)
            else:
                rows = self.tencent.quotes(codes)
            if {row["code"] for row in rows} != set(codes):
                raise ProviderError("Quote batch missing or duplicated identities.")
            if any(not row.get("trade_time") for row in rows):
                # 无法给出自己时间戳的源不满足新鲜度契约，按字段不合法降级。
                raise ProviderError("Quote rows carry no source timestamp.")
            return [
                {
                    "symbol": row["code"],
                    "name": str(row.get("name") or ""),
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                    "pre_close": row.get("pre_close"),
                    "volume": row.get("volume"),
                    "turnover": row.get("turnover"),
                    "source_time": row.get("trade_time"),
                    "received_at": now().isoformat(),
                    "reference_verified": False,
                    "quality": "dated",
                }
                for row in rows
            ]

        return call

    def minutes(self, endpoint, codes, start, end, at=None):
        if endpoint not in MINUTE_ENDPOINTS:
            raise ProviderError("Unsupported minute capability.")
        at = at or now()
        output = []
        for code in sorted(set(symbol(code) for code in codes)):
            _, rows = self._degrade(
                "minutes", [(name, self._minutes_call(name, code, start, end)) for name in SOURCES]
            )
            seen = set()
            for row in rows:
                bar = self._minute_bar(code, row, at)
                if bar is None:
                    continue
                if bar["bar_end"] in seen:
                    raise ProviderError("Minute response has duplicate identities.")
                if start and bar["bar_end"] < start or end and bar["bar_end"] > end:
                    raise ProviderError("Minute response is outside the requested interval.")
                seen.add(bar["bar_end"])
                output.append(bar)
        return sorted(output, key=lambda row: (row["bar_end"], row["symbol"]))

    def _minutes_call(self, source, code, start, end):
        def call():
            if source == "pytdx":
                return self.tdx.minutes(code, limit=800)
            if source == "eastmoney":
                return self.eastmoney.minutes(code, start, end, limit=4000)
            if source == "sina":
                return self.sina.minutes(code, limit=1970)
            return self.tencent.minutes(code, limit=320)

        return call

    @staticmethod
    def _minute_bar(code, row, at):
        stamp = row.get("time")
        if not stamp:
            raise ProviderError("Minute bars require a complete source timestamp.")
        try:
            moment = datetime.strptime(str(stamp)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN)
        except ValueError:
            raise ProviderError("Minute timestamp is invalid.") from None
        minute = moment.hour * 60 + moment.minute
        if minute in {570, 780}:  # 集合竞价没有五分钟区间，不合成
            return None
        if moment.second or moment.minute % 5 or not (575 <= minute <= 690 or 785 <= minute <= 900):
            raise ProviderError("Minute bar is not on a canonical five-minute session boundary.")
        volume, amount = row.get("volume"), row.get("turnover")
        if volume is None or amount is None:
            raise ProviderError("Minute volume/turnover must be finite and nonnegative.")
        return {
            "symbol": code,
            "bar_end": moment.astimezone(timezone.utc),
            "bar_start": (moment - timedelta(minutes=5)).astimezone(timezone.utc),
            "available_at": at.astimezone(timezone.utc),
            "finalized": moment < at,
            **{k: v for k, v in prices(row).items() if k != "pre_close"},
            "volume": volume,
            "amount": amount,
        }

    def constraints(self, day, closes, names=None):
        """Derived price limits from the previous close; public sources publish no official limits.

        ``CAPABILITY_SOURCES`` already declares ``constraints`` as ``derived``, and this method takes
        the previous closes the platform has already stored. Re-fetching one history window per
        security per session would need ``sessions x securities`` provider calls — 1500 x 298 in a
        default deployment — which no tokenless quota can serve, so the derivation never converges.

        A missing close is skipped rather than read as a suspension. Suspension itself has **no**
        tokenless source at all, so it is reported as unknown rather than as ``False``: Qlib's own
        convention is that a suspended session is NaN in the data, and a boolean that is always
        ``False`` is not a fact but a claim nobody can support. Tradability is judged downstream
        from the bar the platform actually holds.
        """
        names = names or {}
        rows = []
        for raw, value in sorted(closes.items()):
            try:
                code = symbol(raw)
            except ValueError:
                continue
            close = number(value)
            if not close or close <= 0:
                continue
            rate = limit_rate(code, names.get(code))
            rows.append(
                {
                    "symbol": code,
                    "day": day,
                    "limit_up": round(close * (1 + rate), 2),
                    "limit_down": round(close * (1 - rate), 2),
                    # 停牌没有免令牌来源。写 ``False`` 会让「不知道」冒充「已确认可交易」，所以这里
                    # 报未知；Qlib 的约定同样是停牌日在数据里是 NaN，而不是一个布尔列。
                    "suspended": None,
                    "suspended_source": "unavailable",
                    "derived": True,
                    "limit_source": "previous_close_rule",
                }
            )
        return rows

    def security_history(self, code, name, at=None):
        """Observed current name as one point-in-time state.

        Public sources publish no dated name-change history, so the state starts the day it was
        observed and carries ``observed=True``. The risk flag comes from the exchange-published
        security name itself (``ST``/``退``), not from a filing.

        ``at`` is the observation instant and is part of the contract, not a convenience: a state is
        only usable from its own ``effective_day`` forward, so the scheduler stamps the analysis
        target's close. That backfill is bounded and declared — ``observed_lag_days`` says how many
        calendar days behind the observation is, and ``backfilled`` flags anything that is not a
        same-day observation, so a filled-in state can never be mistaken for a contemporaneous one.
        """
        code = symbol(code)
        at = at or now()
        local = at.astimezone(CN)
        observed = local.date()
        lag = (now().astimezone(CN).date() - observed).days
        return [
            {
                "symbol": code,
                "name": name or "",
                "start": str(observed),
                "end": None,
                "risk_warning": risk_warning(name),
                "observed": True,
                "announcement_verified": True,
                "observed_at": local.replace(microsecond=0).isoformat(),
                "observed_lag_days": lag,
                "backfilled": lag > 0,
                "available_at": local.replace(hour=15, minute=0, second=0, microsecond=0),
            }
        ]

    # ---------------------------------------------------------------- diagnostics
    def probe(self):
        """Reachability of the four sources; there is no credential or entitlement to check."""
        end = now().astimezone(CN).date()
        start = end - timedelta(days=10)
        checks = {
            "pytdx": lambda: self.tdx.daily("SH600895", start, end, limit=5),
            "eastmoney": lambda: self.eastmoney.daily("SH600895", start, end, limit=5),
            "sina": lambda: self.sina.daily("SH600895", start, end, limit=5),
            "tencent": lambda: self.tencent.daily("SH600895", start, end, limit=5),
        }
        result = {}
        for source, check in checks.items():
            started = time.monotonic()
            try:
                rows = check()
                result[source] = {
                    "reachable": bool(rows),
                    "rows": len(rows),
                    "latency_seconds": round(time.monotonic() - started, 3),
                }
            except Exception as exc:  # noqa: BLE001 - a probe reports, it never raises
                result[source] = {
                    "reachable": False,
                    "error_type": type(exc).__name__,
                    "latency_seconds": round(time.monotonic() - started, 3),
                }
        return result

    def fingerprint(self, capability, rows):
        return digest({"capability": capability, "rows": rows})
