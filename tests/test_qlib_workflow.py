"""Real-Qlib workflow fixtures; never evidence of investment performance."""

import importlib.util
import json
import math
import os
import tempfile
import unittest
from datetime import datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import numpy as np
import pandas as pd

from quant_platform.adapters.qlib.data import build_generation, normalized, verify, write_bins
from quant_platform.domain import CN
from quant_platform.domain.workflow import PipelineBlocked, PortfolioInput, ScanPolicy, intraday_window


class ContractTests(unittest.TestCase):
    def test_adjustment_units_and_zero_volume(self):
        row = {"factor": 2, "data": {"open": 8, "high": 9, "low": 7, "close": 8, "volume": 100, "turnover": 810}}
        result = normalized(row, 4)
        self.assertEqual(result["close"] / result["factor"], 8)
        self.assertEqual(result["volume"] * result["factor"], 100)
        self.assertEqual(result["vwap"] * result["volume"], 810)
        row["data"]["volume"] = 0
        self.assertTrue(math.isnan(normalized(row, 4)["vwap"]))

    def test_no_silent_weight_normalization(self):
        with self.assertRaises(ValueError):
            PortfolioInput(name="Invalid", weights={"SH600000": 0.1}, cash_weight=0)
        self.assertEqual(PortfolioInput(name="Cash").cash_weight, 1)

    def test_intraday_deadline_and_lunch(self):
        cutoff = datetime(2026, 1, 5, 10, 0, tzinfo=CN)
        effective, expires = intraday_window(cutoff, cutoff + timedelta(seconds=60))
        self.assertEqual(effective, cutoff + timedelta(minutes=5))
        self.assertEqual(expires, cutoff + timedelta(minutes=7))
        with self.assertRaises(PipelineBlocked):
            intraday_window(cutoff, cutoff + timedelta(seconds=121))
        with self.assertRaises(PipelineBlocked):
            intraday_window(cutoff.replace(hour=11, minute=25), cutoff.replace(hour=11, minute=26))

    def test_generation_has_full_rolling_history_at_mid_session(self):
        days = list(pd.bdate_range("2025-01-02", periods=70).date)
        code = "SH600000"
        daily = [
            {
                "day": day,
                "factor": 1,
                "dataset_id": 1,
                "data": {"open": 8, "high": 9, "low": 7, "close": 8, "volume": 1000, "turnover": 8000},
            }
            for day in days[:-1]
        ]
        for clock in (time(9, 35), time(10), time(13, 5)):
            with self.subTest(clock=clock), tempfile.TemporaryDirectory() as temporary:
                cutoff = datetime.combine(days[-1], clock, CN)
                minutes = [
                    {
                        "bar_end": datetime.combine(day, time(n // 60, n % 60), CN),
                        "available_at": cutoff,
                        "open": 8,
                        "high": 9,
                        "low": 7,
                        "close": 8,
                        "volume": 1000,
                        "amount": 8000,
                    }
                    for day in days[-21:]
                    for n in list(range(575, 691, 5)) + list(range(785, 901, 5))
                    if datetime.combine(day, time(n // 60, n % 60), CN) <= cutoff
                ]
                db = MagicMock()
                db.history.return_value = daily
                db.transaction.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = {
                    "anchor": 8
                }

                def rows(query, params):
                    if "security_states" in query:
                        return [
                            {
                                "effective_day": days[0],
                                "available_at": datetime.combine(days[0], time(), CN),
                                "data": {"announcement_verified": True, "risk_warning": False},
                            }
                        ]
                    if "market_constraints" in query:
                        return [
                            {"day": day, "data": {"limit_up": 9, "limit_down": 7, "suspended": False}} for day in days
                        ]
                    if "minute_bars" in query:
                        self.assertEqual(params[-1], 1)
                        return minutes
                    if "FROM factors" in query:
                        return [{"factor": 1}]
                    raise AssertionError(query)

                db.rows.side_effect = rows
                snapshot = {
                    "watermark": 1,
                    "history_sessions": 1500,
                    "minute_history_sessions": 252,
                    "calendars": [
                        {"exchange": exchange, "day": str(day), "is_open": True}
                        for day in days
                        for exchange in ("SSE", "SZSE")
                    ],
                    "instruments": {code: {"list_date": str(days[0])}},
                    "pools": [{"day": str(days[-1]), "data": {"symbols": [code]}}],
                    "daily_baseline": {"data": {"weights": {code: 0.05}}},
                    "policy": ScanPolicy().model_dump(),
                    "engine": "fixture",
                }
                run = {"snapshot": snapshot, "as_of": cutoff, "frequency": "5min", "purpose": "inference"}
                with (
                    patch("quant_platform.adapters.qlib.runtime.validate_generation"),
                    patch("quant_platform.adapters.qlib.data.write_bins", wraps=write_bins) as writer,
                ):
                    build_generation(db, SimpleNamespace(artifact_root=Path(temporary)), run, {"id": 1})
                    args = writer.call_args.args
                    self.assertEqual(len(args[2]), 960)
                    self.assertEqual(len(args[1][code]), 960)
                    self.assertEqual(args[2][-1], cutoff.replace(tzinfo=None))
                    self.assertEqual(args[1][code][-1]["features"]["daily_close"], 1)
                minutes.pop(-10)
                with self.assertRaises(PipelineBlocked):
                    build_generation(db, SimpleNamespace(artifact_root=Path(temporary)), run, {"id": 1})

    def test_minute_training_uses_dated_membership_and_full_session_warmup(self):
        days = list(pd.bdate_range("2025-01-02", periods=70).date)
        research_days = days[-40:]
        cutoff = datetime.combine(days[-1], time(15), CN)
        daily = [
            {
                "day": day,
                "factor": 1,
                "dataset_id": 1,
                "data": {"open": 8, "high": 9, "low": 7, "close": 8, "volume": 1000, "turnover": 8000},
            }
            for day in days
        ]
        codes = ["SH600000", "SH600001"]
        minutes = {
            code: [
                {
                    "bar_end": datetime.combine(day, time(n // 60, n % 60), CN),
                    "available_at": cutoff,
                    "open": 8,
                    "high": 9,
                    "low": 7,
                    "close": 8,
                    "volume": 1000,
                    "amount": 8000,
                }
                for day in (research_days[:26] if code == codes[0] else research_days[5:])
                for n in list(range(575, 691, 5)) + list(range(785, 901, 5))
            ]
            for code in codes
        }
        pools = [
            {
                "day": str(day),
                "data": {
                    "out_of_sample": True,
                    "ready": True,
                    "symbols": [codes[index >= 25]],
                    "daily_weights": {codes[index >= 25]: 0.05},
                },
            }
            for index, day in enumerate(research_days)
        ]
        snapshot = {
            "watermark": 1,
            "history_sessions": 1500,
            "minute_history_sessions": 40,
            "calendars": [
                {"exchange": exchange, "day": str(day), "is_open": True} for day in days for exchange in ("SSE", "SZSE")
            ],
            "instruments": {code: {"list_date": str(days[0])} for code in codes},
            "pools": pools,
            "policy": ScanPolicy().model_dump(),
            "engine": "fixture",
        }
        run = {"snapshot": snapshot, "as_of": cutoff, "frequency": "5min", "purpose": "training"}
        for failure in (None, "missing_target_bar", "short_warmup", "missing_limit", "empty_pool"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                db = MagicMock()
                db.history.return_value = daily
                db.transaction.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = {
                    "anchor": 8
                }

                def rows(query, params):
                    if "security_states" in query:
                        return [
                            {
                                "effective_day": days[0],
                                "available_at": datetime.combine(days[0], time(), CN),
                                "data": {"announcement_verified": True, "risk_warning": False},
                            }
                        ]
                    if "market_constraints" in query:
                        return [
                            {
                                "day": day,
                                "data": {
                                    "limit_up": None if failure == "missing_limit" else 9,
                                    "limit_down": 7,
                                    "suspended": False,
                                },
                            }
                            for day in days
                        ]
                    if "minute_bars" in query:
                        result = minutes[params[0]]
                        if failure == "missing_target_bar" and params[0] == codes[0]:
                            return result[:-49] + result[-48:]
                        if failure == "short_warmup" and params[0] == codes[1]:
                            return result[48:]
                        return result
                    raise AssertionError(query)

                db.rows.side_effect = rows
                if failure == "empty_pool":
                    pools[-1]["data"].update(symbols=[], daily_weights={})
                with (
                    patch("quant_platform.adapters.qlib.runtime.validate_generation"),
                    patch("quant_platform.adapters.qlib.data.write_bins", wraps=write_bins) as writer,
                ):
                    if failure not in (None, "empty_pool"):
                        with self.assertRaisesRegex(PipelineBlocked, "Historical daily targets"):
                            build_generation(db, SimpleNamespace(artifact_root=Path(temporary)), run, {"id": 1})
                    else:
                        build_generation(db, SimpleNamespace(artifact_root=Path(temporary)), run, {"id": 1})
                        histories = writer.call_args.args[1]
                        metadata = writer.call_args.args[-1]
                        self.assertEqual(metadata["market_coverage"], 1)
                        self.assertNotIn(codes[0], metadata["omitted"])
                        self.assertTrue(all(value == 1 for value in metadata["historical_coverage"].values()))
                        for code, records in histories.items():
                            for row in records:
                                index = research_days.index(row["day"])
                                if index < 20 or code not in pools[index]["data"]["symbols"]:
                                    self.assertNotIn("label", row["features"])


def fixture_generation(root, frequency, extra_codes=()):
    days = list(pd.bdate_range("2025-01-02", periods=160 if frequency == "day" else 14).date)
    codes = [f"SH60000{i}" for i in range(8)] + list(extra_codes)
    if frequency == "day":
        calendar = days
        codes += ["SH000300"]
    else:
        calendar = [
            datetime.combine(day, time(n // 60, n % 60))
            for day in days
            for n in list(range(575, 691, 5)) + list(range(785, 901, 5))
        ]
    histories = {}
    for k, code in enumerate(codes):
        rows = []
        for i, stamp in enumerate(calendar):
            price = 4 + k * 0.2 + i * (0.0002 + k * 0.00003) + 0.03 * np.sin(i / 7 + k)
            rows.append(
                {
                    "time": stamp,
                    "factor": 1,
                    "data": {
                        "open": price,
                        "high": price * 1.01,
                        "low": price * 0.99,
                        "close": price * 1.001,
                        "volume": 8_000_000 + i * 1000,
                        "turnover": (8_000_000 + i * 1000) * price,
                    },
                    "features": {
                        "risk": 0,
                        "tradable": 1,
                        "listed_sessions": 1500 + i,
                        "limit_up": price * 1.1,
                        "limit_down": price * 0.9,
                        "daily_close": price / 4,
                        "slot": (stamp.hour * 60 + stamp.minute) / 1440 if frequency == "5min" else 0,
                        "label": 0.001 * k + 0.0001 * np.sin(i / 7),
                    },
                }
            )
        histories[code] = rows
    folder = root / f"data-{frequency}"
    manifest = write_bins(
        folder,
        histories,
        calendar,
        frequency,
        {code: 4 for code in codes},
        {
            "id": "fixture",
            "days": list(map(str, days)),
            "policy": ScanPolicy().model_dump(),
            "as_of": datetime.combine(days[-1], time(15), CN).isoformat(),
            "market_coverage": 1,
            "source": "test-fixture-only",
        },
    )
    return folder, manifest


@unittest.skipUnless(importlib.util.find_spec("qlib"), "Requires the isolated Qlib runtime")
class RealQlibTests(unittest.TestCase):
    def test_research_pools_are_purged_dated_and_preserve_strategy_targets(self):
        from quant_platform.adapters.qlib.runtime import reconstruct_pools

        days = list(map(str, pd.bdate_range("2025-01-02", periods=20).date))
        codes = ["SH600000", "SH600001", "SH600002"]
        index = pd.MultiIndex.from_product([pd.to_datetime(days), codes], names=["datetime", "instrument"])
        predictions = pd.Series([1.0, 3.0, 5.0] * len(days), index=index)
        features = {
            code: {
                "price": 8,
                "risk": 0,
                "tradable": 1,
                "limit_up": 9,
                "limit_down": 7,
                "listed_sessions": 500,
                "average_turnover20": 30_000_000,
            }
            for code in codes
        }
        manifest = {
            "id": "fixture",
            "frequency": "day",
            "days": days,
            "watermark": 17,
            "policy": ScanPolicy().model_dump(),
            "instruments": {
                codes[0]: {"list_date": days[0]},
                codes[1]: {"list_date": days[14]},
                codes[2]: {"list_date": days[0], "delist_date": days[11]},
            },
        }
        segments = {"train": (days[0], days[2]), "valid": (days[4], days[5]), "test": (days[12], days[18])}
        with patch("quant_platform.adapters.qlib.strategy.market_features", return_value=features):
            pools = reconstruct_pools(predictions, manifest, segments)
            self.assertEqual(min(pools), days[13])
            self.assertEqual(pools[days[13]]["symbols"], [codes[0]])
            self.assertEqual(pools[days[15]]["symbols"], [codes[1], codes[0]])
            self.assertEqual(pools[days[13]]["daily_weights"], {codes[0]: 0.05})
            self.assertEqual(pools[days[13]]["cash_weight"], 0.95)
            self.assertEqual(pools[days[13]]["signal_day"], days[12])
            self.assertTrue(all(pool["ready"] and pool["out_of_sample"] for pool in pools.values()))
            features[codes[0]]["risk"] = None
            self.assertFalse(reconstruct_pools(predictions, manifest, segments)[days[13]]["ready"])
            with self.assertRaisesRegex(PipelineBlocked, "overlap"):
                reconstruct_pools(predictions, manifest, {**segments, "test": (days[11], days[18])})

    def test_missing_holding_never_becomes_exit(self):
        from quant_platform.adapters.qlib.strategy import proposals

        features = {
            "SH600000": {
                "risk": 0,
                "tradable": 1,
                "price": 12,
                "listed_sessions": 500,
                "average_turnover20": 30_000_000,
                "limit_up": 13.2,
                "limit_down": 10.8,
            }
        }
        result = proposals({"SH600000": 1.0}, features, {}, {"SH600000": 0.03})
        self.assertEqual(result["weights"]["SH600000"], 0.05)
        for field in features["SH600000"]:
            broken = {"SH600000": {**features["SH600000"], field: None}}
            with self.subTest(field=field), self.assertRaises(PipelineBlocked):
                proposals({"SH600000": 1.0}, broken, {}, {"SH600000": 0.03})
        overlay = proposals({"SH600000": 1.0}, features, {}, daily_weights={"SH600000": 0.05}, threshold=2)
        self.assertEqual(overlay["weights"]["SH600000"], 0.025)
        self.assertEqual(overlay["cash_weight"], 0.975)

    def test_simulator_cash_clipping_board_lots_and_t_plus_one(self):
        from qlib.backtest.decision import Order
        from qlib.backtest.position import Position
        from quant_platform.adapters.qlib.runtime import initialize
        from quant_platform.adapters.qlib.strategy import ChinaExchange

        with tempfile.TemporaryDirectory() as temporary:
            path, manifest = fixture_generation(Path(temporary), "5min", ("SH688001", "SZ300001"))
            initialize(path)
            first = pd.Timestamp(manifest["days"][-2]) + pd.Timedelta(hours=10)
            second = pd.Timestamp(manifest["days"][-1]) + pd.Timedelta(hours=10)
            codes = ["SH600000", "SH688001", "SZ300001"]
            for code in codes:
                with self.subTest(code=code):
                    exchange = ChinaExchange(freq="5min", codes=codes, start_time=first, end_time=second)
                    factor = exchange.get_factor(code, first, first)
                    price = exchange.get_deal_price(code, first, first, direction=Order.BUY)
                    limited = Position(cash=price * 150 / factor + 5)
                    buy = Order(
                        stock_id=code, amount=1000 / factor, start_time=first, end_time=first, direction=Order.BUY
                    )
                    exchange.deal_order(buy, position=limited)
                    self.assertAlmostEqual(buy.deal_amount * factor, 0 if code == "SH688001" else 100)
                    self.assertGreaterEqual(limited.get_cash(), 0)
                    exchange = ChinaExchange(freq="5min", codes=codes, start_time=first, end_time=second)
                    position = Position(cash=1_000_000)
                    buy = Order(
                        stock_id=code, amount=250 / factor, start_time=first, end_time=first, direction=Order.BUY
                    )
                    exchange.deal_order(buy, position=position)
                    bought = buy.deal_amount
                    self.assertAlmostEqual(bought * factor, 250 if code == "SH688001" else 200)
                    sell = Order(stock_id=code, amount=bought, start_time=first, end_time=first, direction=Order.SELL)
                    exchange.deal_order(sell, position=position)
                    self.assertEqual(sell.deal_amount, 0)
                    tomorrow = Order(
                        stock_id=code, amount=bought, start_time=second, end_time=second, direction=Order.SELL
                    )
                    exchange.deal_order(tomorrow, position=position)
                    self.assertAlmostEqual(tomorrow.deal_amount, bought)
                    self.assertNotIn(code, position.get_stock_list())
                    self.assertAlmostEqual(exchange.close_cost, 0.0008)
                    odd = Position(cash=100, position_dict={code: {"amount": 50 / factor, "price": price}})
                    exit_order = Order(
                        stock_id=code, amount=50 / factor, start_time=second, end_time=second, direction=Order.SELL
                    )
                    exchange.deal_order(exit_order, position=odd)
                    self.assertAlmostEqual(exit_order.deal_amount * factor, 50)

    def test_intraday_strategy_does_not_cross_lunch_or_overnight(self):
        from quant_platform.adapters.qlib.strategy import TargetWeightStrategy

        strategy = TargetWeightStrategy(frequency="5min", signal=pd.Series(dtype=float))
        calendar = MagicMock()
        strategy.level_infra = {"trade_calendar": calendar}
        calendar.get_trade_step.return_value = 2
        scores = pd.Series({"SH600000": 1.0})
        for signal, trade in (("2026-01-05 11:25", "2026-01-05 13:05"), ("2026-01-05 14:55", "2026-01-06 09:35")):
            calendar.get_step_time.return_value = (pd.Timestamp(signal), pd.Timestamp(signal))
            self.assertIsNone(
                strategy.generate_target_weight_position(scores, MagicMock(), pd.Timestamp(trade), pd.Timestamp(trade))
            )

    @unittest.skipUnless(os.environ.get("QUANT_TEST_DATABASE_URL"), "Requires isolated PostgreSQL")
    def test_canonical_postgres_daily_pools_to_minute_training(self):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict
        from urllib.parse import urlsplit
        from quant_platform.adapters.qlib import runtime
        from quant_platform.cli import migrate
        from quant_platform.config import Settings
        from quant_platform.pipeline import create_run, historical_pools
        from quant_platform.storage import Database, jsonb

        admin_dsn = os.environ["QUANT_TEST_DATABASE_URL"]
        self.assertTrue(conninfo_to_dict(admin_dsn).get("dbname", "").startswith("quant_test"))
        name = "quant_test_chain_" + uuid4().hex
        dsn = urlsplit(admin_dsn)._replace(path="/" + name).geturl()
        self.assertEqual(conninfo_to_dict(dsn).get("dbname"), name)
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            try:
                with tempfile.TemporaryDirectory() as temporary:
                    settings = Settings(
                        database_url=dsn, environment="test", artifact_root=Path(temporary), history_sessions=260
                    )
                    migrate(settings)
                    db = Database(dsn)
                    try:
                        days = list(pd.bdate_range("2025-01-02", periods=260).date)
                        codes = [f"SH60000{i}" for i in range(8)]
                        raw = {}
                        for k, code in enumerate(codes + ["SH000300"]):
                            raw[code] = {}
                            for i, day in enumerate(days):
                                price = (
                                    10
                                    if code == "SH000300"
                                    else (4 + k * 0.1)
                                    * math.exp(i * (0.001 + k * 0.0001) + 0.002 * math.sin(i / 7 + k))
                                )
                                raw[code][day] = {
                                    "open": price,
                                    "high": price * 1.01,
                                    "low": price * 0.99,
                                    "close": price * 1.001,
                                    "volume": 8_000_000 + i * 1000,
                                    "turnover": (8_000_000 + i * 1000) * price,
                                }
                        with db.transaction() as conn, conn.cursor() as cursor:
                            did = db.dataset(
                                conn, "daily", "canonical-fixture", list(map(str, days)), {"fixture": True}
                            )
                            cursor.executemany(
                                "INSERT INTO calendars VALUES(%s,%s,true,now())",
                                [(exchange, day) for day in days for exchange in ("SSE", "SZSE")],
                            )
                            cursor.executemany(
                                "INSERT INTO instruments VALUES(%s,'Fixture','Main Board','SSE',%s,NULL,'L','{}',now())",
                                [(code, days[0]) for code in codes],
                            )
                            cursor.executemany(
                                "INSERT INTO daily_bars VALUES(%s,%s,%s,%s)",
                                [
                                    (code, day, did, jsonb(row))
                                    for code, history in raw.items()
                                    for day, row in history.items()
                                ],
                            )
                            cursor.executemany(
                                "INSERT INTO factors VALUES(%s,%s,%s,1)",
                                [(code, day, did) for code in codes for day in days],
                            )
                            cursor.executemany(
                                "INSERT INTO market_constraints VALUES(%s,%s,%s,%s,%s)",
                                [
                                    (
                                        code,
                                        day,
                                        did,
                                        datetime.combine(day, time(9), CN),
                                        jsonb(
                                            {
                                                "limit_up": row["open"] * 1.1,
                                                "limit_down": row["open"] * 0.9,
                                                "suspended": False,
                                            }
                                        ),
                                    )
                                    for code, history in raw.items()
                                    for day, row in history.items()
                                ],
                            )
                            cursor.executemany(
                                "INSERT INTO security_states VALUES(%s,%s,%s,%s,%s)",
                                [
                                    (
                                        code,
                                        days[index],
                                        did,
                                        datetime.combine(days[index], time(), CN),
                                        jsonb(
                                            {
                                                "announcement_verified": True,
                                                "risk_warning": risk,
                                            }
                                        ),
                                    )
                                    for code in codes
                                    for index, risk in (
                                        [(0, False), (224, True), (242, False)] if code == codes[0] else [(0, False)]
                                    )
                                ],
                            )
                        cutoff = datetime.combine(days[-1], time(15), CN)
                        actual_train = runtime.train_artifact

                        def train(frequency, sizes):
                            run = create_run(
                                db,
                                settings,
                                {
                                    "purpose": "training",
                                    "frequency": frequency,
                                    "as_of": cutoff,
                                    "request_key": "canonical-" + frequency,
                                },
                            )
                            preparation = db.claim("qlib-data", "canonical-data")
                            generation_id = build_generation(db, settings, run, preparation)
                            job = db.claim("qlib-train", "canonical-trainer")
                            with db.transaction() as conn:
                                conn.execute(
                                    "UPDATE jobs SET lease_until=now()+interval '10 minutes' WHERE id=%s", (job["id"],)
                                )
                            with patch.object(
                                runtime,
                                "train_artifact",
                                side_effect=lambda *args, **kwargs: actual_train(
                                    *args,
                                    **kwargs,
                                    sizes=sizes,
                                    rounds=16,
                                ),
                            ):
                                runtime.train_model(db, settings, run, job)
                            generation = db.rows("SELECT * FROM qlib_generations WHERE id=%s", (generation_id,))[0]
                            model = db.rows("SELECT * FROM model_versions WHERE generation_id=%s", (generation_id,))[0]
                            self.assertEqual(
                                db.rows("SELECT state FROM pipeline_runs WHERE id=%s", (run["id"],))[0]["state"],
                                "succeeded",
                            )
                            return run, generation, model

                        daily_run, _, daily_model = train("day", (50, 20, 60))
                        self.assertTrue(
                            daily_model["metadata"]["evaluation"]["passed"], daily_model["metadata"]["evaluation"]
                        )
                        with db.transaction() as conn:
                            pools = historical_pools(conn, days[-1], daily_run["snapshot"]["policy_revision"])
                        self.assertGreater(len(pools), 40)
                        self.assertGreater(len({tuple(sorted(row["data"]["symbols"])) for row in pools}), 1)
                        self.assertTrue(all(row["data"]["daily_model_id"] == str(daily_model["id"]) for row in pools))
                        settings.minute_history_sessions = len(pools)
                        minute_rows = []
                        for code in codes:
                            for pool in pools:
                                day = pool["day"]
                                row = raw[code][day]
                                for slot, minute in enumerate((*range(575, 691, 5), *range(785, 901, 5))):
                                    stamp = datetime.combine(day, time(minute // 60, minute % 60), CN)
                                    price = row["open"] * (1 + 0.0001 * slot)
                                    volume = row["volume"] / 48
                                    minute_rows.append(
                                        (
                                            code,
                                            stamp,
                                            stamp - timedelta(minutes=5),
                                            stamp + timedelta(seconds=1),
                                            price,
                                            price * 1.001,
                                            price * 0.999,
                                            price,
                                            volume,
                                            price * volume,
                                        )
                                    )
                        with db.transaction() as conn, conn.cursor() as cursor:
                            mid = db.dataset(
                                conn, "stk_mins", "canonical-fixture", {"rows": len(minute_rows)}, {"fixture": True}
                            )
                            cursor.executemany(
                                "INSERT INTO minute_bars(symbol,bar_end,bar_start,available_at,open,high,low,close,volume,amount,dataset_id,finalized) "
                                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true)",
                                [(*row, mid) for row in minute_rows],
                            )
                        minute_run, generation, minute_model = train("5min", (20, 8, 8))
                        manifest = generation["manifest"]
                        self.assertEqual(manifest["market_coverage"], 1)
                        self.assertEqual(len(minute_run["snapshot"]["pools"]), len(pools))
                        self.assertEqual(
                            manifest["point_in_time_pools"], {str(row["day"]): row["data"] for row in pools}
                        )
                        self.assertIn("daily_baseline", minute_model["metadata"]["evaluation"])
                        self.assertGreater(minute_model["metadata"]["evaluation"]["observations"], 0)
                        scores, _, _ = runtime.predict_artifact(
                            Path(temporary) / generation["path"],
                            manifest,
                            Path(temporary) / minute_model["path"],
                            minute_model["metadata"],
                        )
                        self.assertTrue(scores and all(math.isfinite(score) for score in scores.values()))
                        self.assertFalse(db.rows("SELECT * FROM recommendations"))
                        self.assertFalse(db.rows("SELECT * FROM model_release_events"))
                        self.assertFalse(db.rows("SELECT * FROM model_versions WHERE state='active'"))
                    finally:
                        db.close()
            finally:
                admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))

    def test_daily_and_minute_train_predict_evaluate(self):
        from quant_platform.adapters.qlib.runtime import train_artifact, predict_artifact, validate_generation

        for frequency in ("day", "5min"):
            with self.subTest(frequency=frequency), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                path, manifest = fixture_generation(root, frequency)
                validate_generation(path, manifest)
                daily = {day: {"SH600000": 0.05, "SH600001": 0.05} for day in manifest["days"]}
                model_path = root / "model"
                artifact = train_artifact(
                    path,
                    manifest,
                    model_path,
                    root / "experiments",
                    sizes=(40, 12, 12) if frequency == "day" else (4, 2, 2),
                    rounds=4,
                    daily_targets=daily if frequency == "5min" else None,
                )
                self.assertIn("rank_ic", artifact["evaluation"])
                self.assertGreater(artifact["evaluation"]["observations"], 0)
                self.assertTrue((model_path / "model.txt").exists())
                self.assertFalse((model_path / "bundle.pkl").exists())
                recorder = artifact["recorder"]
                verify(root / recorder["path"], recorder["manifest"])
                self.assertIn("quap-owner.json", recorder["manifest"]["files"])
                self.assertEqual(
                    json.loads((root / recorder["path"] / "quap-owner.json").read_text()),
                    {
                        "format": "quap-qlib-recorder-v1",
                        "recorder_id": recorder["manifest"]["recorder_id"],
                    },
                )
                if frequency == "day":
                    pools = json.loads((model_path / "research_pools.json").read_text())
                    self.assertEqual(len(pools), artifact["research_pool_count"])
                    self.assertTrue(all(pool["out_of_sample"] for pool in pools.values()))
                    self.assertTrue(artifact["evaluation"]["historical_pool_coverage_passed"])
                scores, _, bundle = predict_artifact(path, manifest, model_path, artifact)
                self.assertEqual(len(scores), 8)
                self.assertTrue(all(math.isfinite(score) for score in scores.values()))
                before = bundle["processors"][1].mean_train.copy()
                _, _, again = predict_artifact(path, manifest, model_path, artifact)
                np.testing.assert_array_equal(before, again["processors"][1].mean_train)
                with (model_path / "model.txt").open("a") as stream:
                    stream.write("corrupt")
                with self.assertRaises(PipelineBlocked):
                    predict_artifact(path, manifest, model_path, artifact)


if __name__ == "__main__":
    unittest.main()
