"""Rate limiting and backoff.

Three layers, all of which must allow a request before it is sent:

1. A minimum spacing between requests (the provider's hard floor, or your
   configured poll interval if that is longer).
2. A daily / monthly request budget, persisted to disk so restarts don't
   reset it. The budget is spread evenly across the period rather than
   allowing a burst followed by a long silence.
3. Server pushback: a 429 Retry-After, or exponential backoff with jitter
   after errors.
"""

from __future__ import annotations

import calendar
import logging
import random
import time
from datetime import datetime, timezone

from .providers import Provider

log = logging.getLogger(__name__)

MAX_BACKOFF_S = 15 * 60


class BudgetExhausted(Exception):
    def __init__(self, wait_s: float, which: str):
        super().__init__(f"{which} request budget exhausted; resumes in {wait_s / 60:.0f} min")
        self.wait_s = wait_s


def _utc(now: float) -> datetime:
    return datetime.fromtimestamp(now, tz=timezone.utc)


def _seconds_until_next_day(dt: datetime) -> float:
    return 86400 - (dt.hour * 3600 + dt.minute * 60 + dt.second)


def _seconds_until_next_month(dt: datetime) -> float:
    days_in_month = calendar.monthrange(dt.year, dt.month)[1]
    return (days_in_month - dt.day) * 86400 + _seconds_until_next_day(dt)


class RateLimiter:
    def __init__(self, provider: Provider, poll_interval_s: float, usage: dict, clock=time.time):
        self.provider = provider
        self.clock = clock
        self.usage = usage.setdefault(provider.name, {})
        self.interval_s = self._effective_interval(poll_interval_s)
        self.next_allowed = 0.0
        self.failures = 0

    def _effective_interval(self, requested: float) -> float:
        p = self.provider
        interval = max(requested, p.min_interval_s)
        reasons = []
        if p.daily_limit:
            budget_interval = 86400 / (p.daily_limit * 0.95)  # keep 5% headroom
            if budget_interval > interval:
                reasons.append(f"{p.daily_limit}/day limit")
            interval = max(interval, budget_interval)
        if p.monthly_limit:
            budget_interval = 31 * 86400 / (p.monthly_limit * 0.95)
            if budget_interval > interval:
                reasons.append(f"{p.monthly_limit}/month quota")
            interval = max(interval, budget_interval)
        if interval > requested:
            why = ", ".join(reasons) or f"{p.name} minimum spacing"
            log.warning("poll interval raised from %.0fs to %.0fs to respect %s", requested, interval, why)
        return interval

    # -- budget bookkeeping -------------------------------------------------

    def _roll_periods(self, now: float) -> None:
        dt = _utc(now)
        day, month = dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m")
        if self.usage.get("day") != day:
            self.usage["day"], self.usage["day_count"] = day, 0
        if self.usage.get("month") != month:
            self.usage["month"], self.usage["month_count"] = month, 0

    def _check_budget(self, now: float) -> None:
        self._roll_periods(now)
        p, dt = self.provider, _utc(now)
        if p.daily_limit and self.usage["day_count"] >= p.daily_limit:
            raise BudgetExhausted(_seconds_until_next_day(dt), "daily")
        if p.monthly_limit and self.usage["month_count"] >= p.monthly_limit:
            raise BudgetExhausted(_seconds_until_next_month(dt), "monthly")

    # -- public API ---------------------------------------------------------

    def seconds_until_allowed(self) -> float:
        """0 if a request may be sent now. Raises BudgetExhausted."""
        now = self.clock()
        self._check_budget(now)
        return max(0.0, self.next_allowed - now)

    def record_request(self) -> None:
        now = self.clock()
        self._roll_periods(now)
        self.usage["day_count"] += 1
        self.usage["month_count"] += 1
        self.next_allowed = max(self.next_allowed, now + self.interval_s)

    def record_success(self, headers: dict[str, str] | None = None) -> None:
        self.failures = 0
        if headers:
            self._apply_quota_headers(headers)

    def record_failure(self, retry_after: float | None = None) -> float:
        """Push the next request out. Returns the delay applied."""
        self.failures += 1
        if retry_after is not None:
            delay = retry_after
        else:
            delay = min(MAX_BACKOFF_S, self.interval_s * (2 ** min(self.failures, 6)))
            delay *= random.uniform(0.8, 1.2)  # jitter so restarts don't sync up
        delay = max(delay, self.interval_s)
        self.next_allowed = self.clock() + delay
        return delay

    def _apply_quota_headers(self, headers: dict[str, str]) -> None:
        """RapidAPI reports the real remaining quota; trust it over our count."""
        lower = {k.lower(): v for k, v in headers.items()}
        remaining = lower.get("x-ratelimit-requests-remaining")
        if remaining is None or not self.provider.monthly_limit:
            return
        try:
            remaining_n = int(remaining)
        except ValueError:
            return
        used = self.provider.monthly_limit - remaining_n
        if used > self.usage.get("month_count", 0):
            self.usage["month_count"] = used
        if remaining_n <= 0:
            return
        # Re-spread whatever is left across the rest of the month.
        left_s = _seconds_until_next_month(_utc(self.clock()))
        needed = left_s / (remaining_n * 0.95)
        if needed > self.interval_s * 1.05:
            log.warning("quota running low (%d left); slowing to every %.0fs", remaining_n, needed)
            self.interval_s = needed
