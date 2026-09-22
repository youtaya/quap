"""Bounded HTTPS Tushare transport with shared account budgets and explicit units."""

import json
import random
import time
from datetime import datetime, timedelta

import httpx

from quant_platform.domain import CN, board, digest, fresh, now, number, provider_code, source_time, symbol
from quant_platform.storage import jsonb
from . import Deferred, PermissionDenied, ProviderError

FIELDS = {
    "stock_basic": "ts_code,symbol,name,exchange,industry,list_status,list_date,delist_date",
    "trade_cal": "exchange,cal_date,is_open,pretrade_date",
    "daily": "ts_code,trade_date,open,high,low,close,pre_close,vol,amount",
    "daily_basic": "ts_code,trade_date,pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,total_mv,circ_mv,turnover_rate",
    "index_daily": "ts_code,trade_date,open,high,low,close,pre_close,vol,amount",
    "adj_factor": "ts_code,trade_date,adj_factor",
    "suspend_d": "ts_code,trade_date,suspend_type",
    "rt_k": "ts_code,name,pre_close,open,high,low,close,vol,amount,trade_time",
}


class Budget:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        self.key = digest(settings.tushare_token.get_secret_value())

    def acquire(self, endpoint):
        at = now()
        minute, day = at.replace(second=0, microsecond=0), at.astimezone(CN).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        group = "real-time" if endpoint == "rt_k" else "ordinary"
        limit = self.settings.realtime_rpm if endpoint == "rt_k" else self.settings.ordinary_rpm
        with self.db.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (self.key,))
            for window, name, maximum in ((minute, group, limit), (day, endpoint + ":day", self.settings.daily_quota)):
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
                    or len(items) > 6000
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
            elif endpoint == "daily_basic":
                result.append(parse_basic(row))
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


def parse_basic(row):
    """Keep missing valuation fields as None; never coerce blanks to zero."""
    return {
        "symbol": symbol(row["ts_code"]),
        "day": parse_date(row["trade_date"]),
        "pe": number(row.get("pe")),
        "pe_ttm": number(row.get("pe_ttm")),
        "pb": number(row.get("pb")),
        "ps": number(row.get("ps")),
        "ps_ttm": number(row.get("ps_ttm")),
        "dv_ratio": number(row.get("dv_ratio")),
        "total_mv": number(row.get("total_mv")),
        "circ_mv": number(row.get("circ_mv")),
        "turnover_rate": number(row.get("turnover_rate")),
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
