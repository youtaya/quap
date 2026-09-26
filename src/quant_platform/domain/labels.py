"""Calendar-aligned label windows shared by training and realized diagnostics."""

from datetime import date, datetime, time, timedelta
import math

from quant_platform.domain import CN

DAILY_LABEL = "Ref($open,-6)/Ref($open,-1)-1"
MINUTE_LABEL = "lagged-entry-open-to-next-session-close-v1"


def label_window(days, cutoff, frequency):
    days = [date.fromisoformat(day) if isinstance(day, str) else day for day in days]
    cutoff = cutoff.astimezone(CN) if cutoff.tzinfo else cutoff.replace(tzinfo=CN)
    try:
        index = days.index(cutoff.date())
        if frequency == "day":
            return datetime.combine(days[index + 1], time(9, 30), CN), datetime.combine(
                days[index + 6], time(9, 30), CN
            )
        if frequency != "5min":
            raise ValueError("Unsupported label frequency.")
        entry = cutoff + timedelta(minutes=10)
        if cutoff.minute % 5 or not (
            time(9, 35) <= cutoff.time() <= time(11, 20) or time(13, 5) <= cutoff.time() <= time(14, 50)
        ):
            return None
        return entry, datetime.combine(days[index + 1], time(15), CN)
    except (ValueError, IndexError):
        return None


def adjusted_return(entry_price, entry_factor, exit_price, exit_factor):
    values = (entry_price, entry_factor, exit_price, exit_factor)
    if any(
        value is None or isinstance(value, bool) or not math.isfinite(float(value)) or float(value) <= 0
        for value in values
    ):
        return None
    result = float(exit_price) * float(exit_factor) / (float(entry_price) * float(entry_factor)) - 1
    return result if math.isfinite(result) else None
