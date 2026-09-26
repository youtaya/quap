"""The shared Qlib strategy used for target proposals and simulated execution."""

import math
from datetime import date

import numpy as np
import pandas as pd
from qlib.backtest.decision import Order
from qlib.backtest.exchange import Exchange
from qlib.contrib.strategy.signal_strategy import WeightStrategyBase
from qlib.data import D

from quant_platform.domain import board
from quant_platform.domain.workflow import PipelineBlocked, ScanPolicy, action_for

STRATEGY_VERSION = "target-weights-v1"


def capped_weights(scores, policy):
    positive = {code: float(value) for code, value in scores.items() if math.isfinite(value) and value > 0}
    chosen = dict(sorted(positive.items(), key=lambda item: (-item[1], item[0]))[: policy.top_n])
    total = sum(chosen.values())
    if total == 0:
        return {}
    # Caps leave residual cash; there is no forced investment or silent normalization.
    return {code: min(policy.max_weight, (1 - policy.minimum_cash) * score / total) for code, score in chosen.items()}


def market_features(codes, at, frequency="day"):
    expressions = ["$raw_close", "$risk", "$tradable", "$listed_sessions", "$limit_up", "$limit_down"]
    names = ["price", "risk", "tradable", "listed_sessions", "limit_up", "limit_down"]
    if frequency == "day":
        expressions += ["Mean($amount,20)"]
        names += ["average_turnover20"]
    frame = D.features(codes, expressions, start_time=at, end_time=at, freq=frequency)
    frame.columns = names
    return {str(code): row.to_dict() for code, row in frame.droplevel("datetime").iterrows()}


def finite(value):
    return isinstance(value, (int, float, np.number)) and math.isfinite(value)


def proposals(scores, features, policy, baseline=None, daily_weights=None, threshold=None):
    policy = ScanPolicy.model_validate(policy)
    baseline = baseline or {}
    required = ["risk", "tradable", "price", "limit_up", "limit_down"]
    if daily_weights is None:
        required += ["listed_sessions", "average_turnover20"]
    protected = {code for code, weight in baseline.items() if weight > 0}
    protected.update(code for code, weight in (daily_weights or {}).items() if weight > 0)
    missing = [
        code
        for code in protected
        if not finite(scores.get(code))
        or any(not finite(features.get(code, {}).get(field)) for field in required)
        or features[code]["price"] <= 0
        or features[code]["tradable"] != 1
    ]
    if missing:
        raise PipelineBlocked("Portfolio inputs unavailable: " + ", ".join(sorted(missing)))
    qualified, candidates, excluded = {}, [], {}
    for code, score in sorted(scores.items()):
        f = features.get(code, {})
        reasons = []
        if not finite(score):
            reasons.append("missing_model_prediction")
        if not finite(f.get("risk")):
            reasons.append("unknown_security_state")
        elif f["risk"]:
            reasons.append("risk_warning")
        if f.get("tradable") != 1 or not finite(f.get("limit_up")) or not finite(f.get("limit_down")):
            reasons.append("tradability_unverified_or_suspended")
        if daily_weights is None:
            if not finite(f.get("listed_sessions")) or f["listed_sessions"] < policy.minimum_sessions:
                reasons.append("insufficient_listing_history")
            if not finite(f.get("average_turnover20")) or f["average_turnover20"] < policy.minimum_turnover:
                reasons.append("insufficient_liquidity")
        if not finite(f.get("price")) or f["price"] <= 0:
            reasons.append("missing_raw_price")
        low_price = not reasons and f["price"] <= policy.price_ceiling
        if reasons:
            excluded[code] = reasons
            continue
        if low_price:
            candidates.append(
                {
                    "symbol": code,
                    "score": score,
                    "raw_price": f["price"],
                    "reason": "Qlib model rank; low nominal price only",
                }
            )
        elif code not in protected:
            excluded[code] = ["above_price_ceiling"]
        if low_price or code in protected:
            qualified[code] = score
    candidates.sort(key=lambda r: (-r["score"], r["symbol"]))
    if daily_weights is None:
        weights = capped_weights(qualified, policy)
    else:
        if threshold is None or not math.isfinite(threshold):
            raise PipelineBlocked("Intraday calibration threshold unavailable.")
        missing = sorted(set(daily_weights) - set(qualified))
        if missing:
            raise PipelineBlocked("Intraday overlay lacks qualified daily targets: " + ", ".join(missing))
        weights = {code: weight * (0.5 if scores[code] < threshold else 1) for code, weight in daily_weights.items()}
    rows = []
    for code in sorted(set(scores) | set(baseline)):
        unavailable = not finite(scores.get(code)) or any(
            not finite(features.get(code, {}).get(field)) for field in required
        )
        target = None if unavailable else weights.get(code, 0)
        rows.append(
            {
                "symbol": code,
                "score": scores.get(code),
                "baseline_weight": baseline.get(code, 0),
                "target_weight": target,
                "action": action_for(baseline.get(code, 0), target, policy.change_band),
                "restrictions": excluded.get(code, []),
                "engine": "qlib",
                "strategy": STRATEGY_VERSION,
            }
        )
    return {
        "weights": weights,
        "cash_weight": 1 - sum(weights.values()),
        "stocks": rows,
        "candidates": candidates[: policy.top_n],
        "shortlist": candidates[:200],
        "excluded": excluded,
        "strategy": STRATEGY_VERSION,
        "baseline_type": "accepted_model_portfolio",
        "orders": False,
    }


class TargetWeightStrategy(WeightStrategyBase):
    def __init__(self, policy=None, frequency="day", daily_targets=None, threshold=None, **kwargs):
        super().__init__(risk_degree=1.0, **kwargs)
        self.policy = ScanPolicy.model_validate(policy or {})
        self.frequency = frequency
        self.daily_targets = daily_targets
        self.threshold = threshold

    def generate_target_weight_position(self, score, current, trade_start_time, trade_end_time):
        if isinstance(score, pd.DataFrame):
            score = score.iloc[:, 0]
        lag = 2 if self.frequency == "5min" else 1
        prediction_start, prediction_end = self.trade_calendar.get_step_time(
            self.trade_calendar.get_trade_step(), shift=lag
        )
        if self.frequency == "5min":
            if pd.Timestamp(prediction_start) + pd.Timedelta(minutes=10) != pd.Timestamp(trade_start_time):
                return None
            score = self.signal.get_signal(start_time=prediction_start, end_time=prediction_end)
            if score is None:
                return None
            if isinstance(score, pd.DataFrame):
                score = score.iloc[:, 0]
        features = market_features(list(score.index), prediction_start, self.frequency)
        baseline = current.get_stock_weight_dict(only_stock=False)
        daily = None
        if self.daily_targets is not None:
            daily = self.daily_targets.get(str(pd.Timestamp(trade_start_time).date()))
            if daily is None:
                raise PipelineBlocked("Historical out-of-sample daily targets unavailable.")
        result = proposals(score.to_dict(), features, self.policy, baseline, daily, self.threshold)
        return result["weights"]


class ChinaExchange(Exchange):
    """Dated price limits, conservative costs, board lots, and T+1 in simulation."""

    def __init__(self, **kwargs):
        kwargs.update(
            trade_unit=1,
            open_cost=0.0003,
            close_cost=0.0008,
            min_cost=5,
            deal_price=("$open*1.001", "$open*0.999"),
            limit_threshold=(
                "Or(Or($limit_up!=$limit_up,$tradable!=1),$open/$factor>=$limit_up)",
                "Or(Or($limit_down!=$limit_down,$tradable!=1),$open/$factor<=$limit_down)",
            ),
        )
        super().__init__(**kwargs)
        self._bought = {}
        self._session = None

    @staticmethod
    def lot_amount(order, amount, factor, held):
        physical = max(0, math.floor(amount * factor + 1e-6))
        star = board(order.stock_id) == "STAR Market"
        if order.direction == Order.BUY:
            physical = (physical if physical >= 200 else 0) if star else physical // 100 * 100
        elif physical < held * factor - 1e-5:
            physical = (physical if physical >= 200 else 0) if star else physical // 100 * 100
        return physical / factor

    def _calc_trade_info_by_order(self, order, position, dealt_order_amount):
        price, value, cost = super()._calc_trade_info_by_order(order, position, dealt_order_amount)
        held = position.get_stock_amount(order.stock_id) if position and position.check_stock(order.stock_id) else 0
        # Qlib clips by available cash/volume after submission; enforce board lots again before account mutation.
        order.deal_amount = self.lot_amount(order, order.deal_amount, order.factor, held)
        rounded_value = order.deal_amount * price
        rounded_cost = max(self.min_cost, cost * rounded_value / value) if value > 0 and rounded_value > 0 else 0.0
        return price, rounded_value, rounded_cost

    def deal_order(self, order, trade_account=None, position=None, dealt_order_amount=None):
        day = pd.Timestamp(order.start_time).date()
        if day != self._session:
            self._session, self._bought = day, {}
        self.close_cost = 0.0003 + (0.0005 if day >= date(2023, 8, 28) else 0.001)
        factor = self.get_factor(order.stock_id, order.start_time, order.end_time)
        if factor is None or not np.isfinite(factor) or factor <= 0:
            order.deal_amount = 0
            return 0.0, 0.0, np.nan
        current = trade_account.current_position if trade_account else position
        held = current.get_stock_amount(order.stock_id) if current and order.stock_id in current.get_stock_list() else 0
        if order.direction == Order.SELL:
            order.amount = min(order.amount, max(0, held - self._bought.get(order.stock_id, 0)))
        order.amount = self.lot_amount(order, order.amount, factor, held)
        if not order.amount:
            order.deal_amount = 0
            return 0.0, 0.0, np.nan
        result = super().deal_order(
            order, trade_account, position, dealt_order_amount if dealt_order_amount is not None else {}
        )
        if order.direction == Order.BUY and order.deal_amount:
            self._bought[order.stock_id] = self._bought.get(order.stock_id, 0) + order.deal_amount
        return result
