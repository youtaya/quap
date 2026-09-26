"""Lazy source-specific adata binding, used only inside the opt-in research worker."""

import hashlib
from importlib import import_module, metadata
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

import httpx

from quant_platform.domain import symbol
from quant_platform.domain.research import ADATA_VERSION, SOURCE_CATALOG

ALLOWED_ENDPOINTS = {
    ("push2his.eastmoney.com", "/api/qt/stock/kline/get"),
    ("push2.eastmoney.com", "/api/qt/stock/trends2/get"),
}


class TransportUnavailable(RuntimeError):
    pass


class ControlledTransport:
    """No facade, retries, redirects, proxies, or unbounded response bodies."""

    def __init__(self, code, deadline=60, response_cap=8 * 1024 * 1024, transport=None):
        self.code = symbol(code)[2:]
        self.deadline, self.response_cap = deadline, response_cap
        self.transport = transport
        self.error = None
        self.payload_hash = hashlib.sha256(b"").hexdigest()
        self.requests = 0

    def request(self, method="get", url=None, params=None, **kwargs):
        try:
            parts = urlsplit(url or "")
            if (
                method.lower() != "get"
                or parts.scheme != "https"
                or (parts.hostname, parts.path) not in ALLOWED_ENDPOINTS
                or parts.username
                or parts.password
                or parts.port not in {None, 443}
                or parts.query
                or parts.fragment
                or kwargs
                or self.requests
            ):
                raise TransportUnavailable("transport_policy_rejected")
            params = params or {}
            expected_secid = ("1." if self.code.startswith("6") else "0.") + self.code
            if params.get("secid") != expected_secid or ("fqt" in params and params["fqt"] != 0):
                raise TransportUnavailable("source_or_adjustment_mismatch")
            self.requests += 1
            started = time.monotonic()
            with httpx.Client(
                transport=self.transport,
                timeout=self.deadline,
                follow_redirects=False,
                trust_env=False,
                verify=True,
            ) as client:
                with client.stream("GET", url, params=params, headers={"Accept-Encoding": "identity"}) as response:
                    if response.status_code != 200:
                        raise TransportUnavailable("upstream_http_status")
                    if response.headers.get("content-encoding", "identity").lower() not in {"identity", ""}:
                        raise TransportUnavailable("encoded_response_rejected")
                    body = bytearray()
                    for chunk in response.iter_bytes(chunk_size=65536):
                        if time.monotonic() - started >= self.deadline or len(body) + len(chunk) > self.response_cap:
                            raise TransportUnavailable("response_limit_exceeded")
                        body.extend(chunk)
            self.payload_hash = hashlib.sha256(body).hexdigest()
            data = json.loads(body)
            if data.get("data") is not None and str(data["data"].get("code")) != self.code:
                raise TransportUnavailable("upstream_identity_mismatch")
            return httpx.Response(200, content=bytes(body))
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError, TransportUnavailable) as exc:
            self.error = str(exc) if isinstance(exc, TransportUnavailable) else "transport_or_schema_error"
            raise TransportUnavailable(self.error) from None


def collect_one(payload, transport=None):
    source = SOURCE_CATALOG[payload["source_id"]]
    code = symbol(payload["symbol"])
    controlled = ControlledTransport(
        code, payload.get("deadline", 60), payload.get("response_cap", 8 * 1024 * 1024), transport
    )
    try:
        if metadata.version("adata") != ADATA_VERSION:
            raise TransportUnavailable("unsupported_adata_version")
        module = import_module("adata.stock.market.stock_market.stock_market_east")
        original = module.requests
        module.requests = controlled
        try:
            adapter = module.StockMarketEast()
            if source["frequency"] == "day":
                frame = adapter.get_market(
                    stock_code=code[2:],
                    start_date=payload["start"],
                    end_date=payload["end"],
                    k_type=1,
                    adjust_type=0,
                )
            else:
                adapter._MARKET_MIN_COLUMNS = [
                    "stock_code",
                    "trade_time",
                    "open",
                    "close",
                    "high",
                    "low",
                    "volume",
                    "amount",
                ]
                frame = adapter.get_market_min(stock_code=code[2:])
        finally:
            module.requests = original
        if controlled.error:
            raise TransportUnavailable(controlled.error)
        if frame.empty or not controlled.requests:
            raise TransportUnavailable("empty_or_suppressed_provider_error")
        if len(frame) > 5000:
            raise TransportUnavailable("row_limit_exceeded")
        return {
            "status": "available",
            "records": json.loads(frame.to_json(orient="records")),
            "payload_hash": controlled.payload_hash,
            "source": source,
        }
    except (ImportError, metadata.PackageNotFoundError):
        reason = "optional_adata_runtime_unavailable"
    except TransportUnavailable as exc:
        reason = str(exc)
    except Exception:
        reason = "provider_schema_error"
    return {
        "status": "unavailable",
        "reason": reason,
        "records": [],
        "payload_hash": controlled.payload_hash,
        "source": source,
    }


if __name__ == "__main__":
    result = collect_one(json.loads(sys.stdin.read(32 * 1024 * 1024)))
    Path(sys.argv[1]).write_text(json.dumps(result, allow_nan=False))
