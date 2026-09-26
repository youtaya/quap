"""Bounded HTTPS Tushare transport with shared account budgets and explicit units."""

import json
import random
import time
from datetime import datetime, timedelta, timezone

import httpx

from quant_platform.domain import CN, board, digest, fresh, now, number, provider_code, source_time, symbol
from . import Deferred, PermissionDenied, ProviderError

FIELDS = {
    "stock_basic": "ts_code,symbol,name,exchange,list_status,list_date,delist_date",
    "trade_cal": "exchange,cal_date,is_open,pretrade_date",
    "daily": "ts_code,trade_date,open,high,low,close,pre_close,vol,amount",
    "index_daily": "ts_code,trade_date,open,high,low,close,pre_close,vol,amount",
    "adj_factor": "ts_code,trade_date,adj_factor",
    "suspend_d": "ts_code,trade_date,suspend_type",
    "rt_k": "ts_code,name,pre_close,open,high,low,close,vol,amount,trade_time",
    "stk_mins": "ts_code,trade_time,open,high,low,close,vol,amount",
    "rt_min": "ts_code,time,open,high,low,close,vol,amount",
    "rt_min_daily": "ts_code,time,open,high,low,close,vol,amount",
    "namechange": "ts_code,name,start_date,end_date,ann_date,change_reason",
    "stk_limit": "ts_code,trade_date,up_limit,down_limit",
}
MINUTE_ENDPOINTS = {"stk_mins", "rt_min", "rt_min_daily"}
ROW_LIMITS = {"stk_mins": 8000, "rt_min": 1000, "rt_min_daily": 1000, "namechange": 6000, "stk_limit": 6000}


class Budget:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        self.key = digest(settings.tushare_token.get_secret_value())

    def acquire(self, endpoint):
        at = now()
        minute, day = at.replace(second=0, microsecond=0), at.astimezone(CN).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        group = "minute" if endpoint in MINUTE_ENDPOINTS else "real-time" if endpoint == "rt_k" else "ordinary"
        limit = (
            self.settings.minute_rpm
            if group == "minute"
            else self.settings.realtime_rpm if group == "real-time" else self.settings.ordinary_rpm
        )
        daily_limit = self.settings.minute_daily_quota if group == "minute" else self.settings.daily_quota
        with self.db.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (self.key,))
            for window, name, maximum in ((minute, group, limit), (day, endpoint + ":day", daily_limit)):
                row = conn.execute(
                    "SELECT used FROM quota WHERE credential=%s AND endpoint=%s AND window_start=%s",
                    (self.key, name, window),
                ).fetchone()
                if row and row["used"] >= maximum:
                    seconds = 60 if name == group else int((day + timedelta(days=1) - at).total_seconds())
                    raise Deferred("Provider quota exhausted; request deferred.", seconds)
            for window, name in ((minute, group), (day, endpoint + ":day")):
                conn.execute(
                    "INSERT INTO quota VALUES(%s,%s,%s,1) ON CONFLICT(credential,endpoint,window_start) "
                    "DO UPDATE SET used=quota.used+1",
                    (self.key, name, window),
                )


class Tushare:
    url = "https://api.tushare.pro"

    def __init__(self, settings, db=None, client=None, sleep=time.sleep):
        self.token = settings.tushare_token.get_secret_value()
        self.db = db
        self.budget = Budget(db, settings) if db else None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(15, connect=5), follow_redirects=False, trust_env=False
        )
        self.sleep = sleep

    def close(self):
        self.client.close()

    def request(self, endpoint, params, fields=None, probe=False):
        if endpoint not in FIELDS:
            raise ProviderError("Unsupported provider capability.")
        if not self.token:
            self._health(endpoint, "blocked", {"configured": False, "entitled": False})
            raise PermissionDenied("Tushare credentials are not configured.")
        if self.db and not probe:
            states = self.db.rows("SELECT * FROM capabilities WHERE endpoint=%s", (endpoint,))
            if states:
                state = states[0]
                if state["status"] == "blocked":
                    raise PermissionDenied("Capability blocked; run doctor to revalidate permissions.")
                if state["status"] == "circuit_open" and now() - state["updated_at"] < timedelta(minutes=5):
                    raise Deferred("Provider circuit is open.", 300)
        fields = fields or FIELDS[endpoint]
        requested = fields.split(",")
        started = time.monotonic()
        for attempt in range(3):
            if self.budget:
                self.budget.acquire(endpoint)
            try:
                with self.client.stream(
                    "POST",
                    self.url,
                    json={"api_name": endpoint, "token": self.token, "params": params, "fields": fields},
                ) as res:
                    if 300 <= res.status_code < 400:
                        raise ProviderError("Provider redirects are prohibited.")
                    if res.status_code in {401, 403}:
                        raise PermissionDenied("Provider rejected credentials or entitlement.")
                    if res.status_code == 429:
                        raise Deferred("Provider rate limit; retry later.")
                    res.raise_for_status()
                    content = bytearray()
                    for chunk in res.iter_bytes():
                        content.extend(chunk)
                        if len(content) > 8 * 1024 * 1024 or time.monotonic() - started > 60:
                            raise ProviderError("Provider response exceeded byte/time budget.")
                    payload = json.loads(content)
                if not isinstance(payload, dict):
                    raise ProviderError("Malformed provider envelope.")
                if payload.get("code") != 0:
                    message = str(payload.get("msg", ""))
                    if any(word in message for word in ("频率", "每分钟", "每小时", "每天", "访问次数")):
                        raise Deferred("Provider quota/rate limit; retry later.", 300)
                    if payload.get("code") == 2002 or any(word in message for word in ("权限", "积分", "token")):
                        raise PermissionDenied("Required provider permission is unavailable.")
                    raise ProviderError("Provider returned a non-success application code.")
                data = payload.get("data")
                if not isinstance(data, dict):
                    raise ProviderError("Provider data envelope is absent.")
                columns, items = data.get("fields"), data.get("items")
                if (
                    not isinstance(columns, list)
                    or len(set(columns)) != len(columns)
                    or not set(requested).issubset(columns)
                    or not isinstance(items, list)
                    or len(items) > ROW_LIMITS.get(endpoint, 6000)
                ):
                    raise ProviderError("Provider schema or row limit changed.")
                if any(not isinstance(row, list) or len(row) != len(columns) for row in items):
                    raise ProviderError("Provider row shape changed.")
                rows = [dict(zip(columns, row)) for row in items]
                times = [source_time(row.get("trade_time")) for row in rows] if endpoint == "rt_k" else []
                self._health(
                    endpoint,
                    "reachable",
                    {
                        "configured": True,
                        "entitled": True,
                        "schema_verified": True,
                        "rows": len(rows),
                        "latency_seconds": round(time.monotonic() - started, 3),
                        "timestamp_verified": bool(times) and all(times),
                        "fresh": bool(times) and all(fresh(t) for t in times),
                    },
                )
                return rows
            except PermissionDenied:
                self._health(endpoint, "blocked", {"configured": True, "entitled": False})
                raise
            except Deferred:
                raise
            except (httpx.HTTPError, ValueError, ProviderError) as exc:
                if attempt == 2:
                    self._health(endpoint, "circuit_open", {"configured": True, "error_type": type(exc).__name__})
                    raise ProviderError("Provider transport/schema failed after three attempts.") from None
                self.sleep(2**attempt + random.random())
        raise ProviderError("Unreachable request state.")

    def _health(self, endpoint, status, data):
        if self.db:
            self.db.capability(endpoint, status, data)

    def securities(self):
        result = {}
        for exchange in ("SSE", "SZSE"):
            for status in ("L", "D", "P"):
                rows = self.request("stock_basic", {"exchange": exchange, "list_status": status})
                if len(rows) >= 6000 or (status == "L" and not rows):
                    raise ProviderError("Security master is empty or potentially truncated.")
                for row in rows:
                    try:
                        code = symbol(row["ts_code"])
                    except ValueError:
                        continue
                    if row["exchange"] != exchange or row["list_status"] != status or code in result:
                        raise ProviderError("Security master contains conflicting identities.")
                    if row["symbol"] != code[2:]:
                        raise ProviderError("Security master code mismatch.")
                    row = {
                        **row,
                        "symbol": code,
                        "board": board(code),
                        "list_date": parse_date(row["list_date"]),
                        "delist_date": parse_date(row["delist_date"]) if row["delist_date"] else None,
                    }
                    result[code] = row
        return result

    def quotes(self, codes):
        codes = sorted(set(codes))
        result = []
        for index in range(0, len(codes), 200):
            batch = codes[index : index + 200]
            rows = self.request("rt_k", {"ts_code": ",".join(provider_code(code) for code in batch)})
            parsed = [parse_quote(row) for row in rows]
            if len(parsed) != len(batch) or {r["symbol"] for r in parsed} != set(batch):
                raise ProviderError("Real-time batch missing, duplicated, or unexpected identities.")
            result.extend(parsed)
        return result

    def minutes(self, endpoint, codes, start=None, end=None, at=None):
        if endpoint not in MINUTE_ENDPOINTS:
            raise ProviderError("Unsupported minute endpoint.")
        codes = sorted(set(symbol(code) for code in codes))
        at = at or now()
        output = []
        batches = (
            [codes[i : i + 100] for i in range(0, len(codes), 100)] if endpoint == "rt_min" else [[c] for c in codes]
        )
        for batch in batches:
            params = {
                "ts_code": ",".join(provider_code(c) for c in batch),
                "freq": "5min" if endpoint == "stk_mins" else "5MIN",
            }
            if endpoint == "stk_mins":
                if start is None or end is None:
                    raise ProviderError("Historical minute requests need bounded dates.")
                params.update(
                    start_date=start.astimezone(CN).strftime("%Y-%m-%d %H:%M:%S"),
                    end_date=end.astimezone(CN).strftime("%Y-%m-%d %H:%M:%S"),
                )
            rows = self.request(endpoint, params)
            if len(rows) >= ROW_LIMITS[endpoint]:
                raise ProviderError("Minute response may be truncated; shorten the request window.")
            seen = set()
            for raw in rows:
                bar = parse_minute(raw, endpoint, at)
                if bar is None:
                    continue
                identity = (bar["symbol"], bar["bar_end"])
                if bar["symbol"] not in batch or identity in seen:
                    raise ProviderError("Minute response has unexpected or duplicate identities.")
                if start and bar["bar_end"] < start or end and bar["bar_end"] > end:
                    raise ProviderError("Minute response is outside the requested interval.")
                seen.add(identity)
                output.append(bar)
        return sorted(output, key=lambda row: (row["bar_end"], row["symbol"]))

    def daily_partition(self, endpoint, day, codes, job=None):
        params = {"trade_date": day.strftime("%Y%m%d")}
        saved = (
            {
                r["symbol"]: r["data"]
                for r in self.db.rows(
                    "SELECT symbol,data FROM collection_checkpoints WHERE job_id=%s AND endpoint=%s",
                    (job["id"], endpoint),
                )
            }
            if self.db and job
            else {}
        )
        rows = [] if saved else self.request(endpoint, params)
        if saved or len(rows) >= 6000:
            rows = []
            # Both daily and adj_factor document single-security requests; do not assume pagination support.
            for code in sorted(codes):
                part = saved.get(code)
                if part is None:
                    part = self.request(endpoint, {**params, "ts_code": provider_code(code)})
                    if len(part) > 1 or any(
                        r.get("ts_code") != provider_code(code) or parse_date(r.get("trade_date")) != day for r in part
                    ):
                        raise ProviderError("Single-date, single-security response has conflicting identities.")
                    if self.db and job:
                        self.db.checkpoint(job, endpoint, code, part)
                rows.extend(part)
        seen, result = set(), []
        for row in rows:
            try:
                code = symbol(row["ts_code"])
            except ValueError:
                continue
            if code not in codes:
                continue
            if code in seen or parse_date(row["trade_date"]) != day:
                raise ProviderError("Duplicate or misdated historical row.")
            seen.add(code)
            if endpoint == "adj_factor":
                factor = number(row["adj_factor"])
                if factor is None or factor <= 0:
                    raise ProviderError("Invalid adjustment factor.")
                result.append({"symbol": code, "day": day, "factor": factor})
            else:
                result.append(parse_bar(row))
        return result


def parse_date(value):
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except (ValueError, TypeError):
        raise ProviderError("Invalid provider trading date.") from None


def prices(row, strict=True):
    values = {key: number(row.get(key)) for key in ("open", "high", "low", "close", "pre_close")}
    valid = all(values[k] is not None and values[k] > 0 for k in ("open", "high", "low", "close"))
    valid = (
        valid
        and values["low"]
        <= min(values["open"], values["close"])
        <= max(values["open"], values["close"])
        <= values["high"]
    )
    if strict and not valid:
        raise ProviderError("Invalid historical OHLC.")
    if not valid:
        values = {key: None for key in values}
    return values


def positive(value, multiplier=1):
    value = number(value)
    return value * multiplier if value is not None and value >= 0 else None


def parse_bar(row):
    return {
        "symbol": symbol(row["ts_code"]),
        "day": parse_date(row["trade_date"]),
        **prices(row),
        "volume": positive(row.get("vol"), 100),
        "turnover": positive(row.get("amount"), 1000),
    }


def parse_minute(row, endpoint, at=None):
    at = at or now()
    stamp = source_time(row.get("trade_time" if endpoint == "stk_mins" else "time"))
    if stamp is None:
        raise ProviderError("Minute bars require a complete source timestamp.")
    minute = stamp.hour * 60 + stamp.minute
    # Opening auction rows have no five-minute interval and are not synthesized into one.
    if minute in {570, 780}:
        return None
    if stamp.second or stamp.minute % 5 or not (575 <= minute <= 690 or 785 <= minute <= 900):
        raise ProviderError("Minute bar is not on a canonical five-minute session boundary.")
    volume, amount = positive(row.get("vol")), positive(row.get("amount"))
    if volume is None or amount is None:
        raise ProviderError("Minute volume/amount must be finite and nonnegative.")
    return {
        "symbol": symbol(row["ts_code"]),
        "bar_end": stamp.astimezone(timezone.utc),
        "bar_start": (stamp - timedelta(minutes=5)).astimezone(timezone.utc),
        "available_at": at.astimezone(timezone.utc),
        "finalized": stamp < at,
        **{k: v for k, v in prices(row).items() if k != "pre_close"},
        "volume": volume,
        "amount": amount,
    }


def parse_quote(row):
    stamp = source_time(row.get("trade_time"))
    return {
        "symbol": symbol(row["ts_code"]),
        "name": str(row.get("name") or ""),
        **prices(row, False),
        "volume": positive(row.get("vol")),
        "turnover": positive(row.get("amount")),
        "source_time": stamp.isoformat() if stamp else None,
        "received_at": now().isoformat(),
        "reference_verified": False,
        "quality": "dated" if stamp else "timestamp_unverified",
    }
