"""Poll health: success/failure counts, outages, the hourly status line and the
daily heartbeat notification.

Everything lives in the state file, so restarts don't reset the numbers. Two
windows are kept: "hour" for the log line and "period" for the heartbeat.
"""

from __future__ import annotations

from .notify import Message

COUNTERS = ("ok", "failed", "failovers", "alerts")
FEED_DOWN_AFTER_S = 10 * 60  # heartbeat title flips to "feed down" after this


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def fmt_duration(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60}m"
    return f"{s // 86400}d {s % 86400 // 3600}h"


class Health:
    def __init__(self, store: dict, now: float):
        self.d = store
        for key in ("hour", "period"):
            self._ensure_window(key, now)
        self.d.setdefault("fail_streak", 0)

    # -- recording ------------------------------------------------------------

    def record_success(self, now: float, used_failover: bool = False) -> tuple[int, float] | None:
        """Returns (failed_polls, outage_seconds) if this ends an outage."""
        self._bump("ok")
        if used_failover:
            self._bump("failovers")
        self.d["last_success"] = now
        recovered = None
        if self.d["fail_streak"]:
            outage = now - self.d.get("streak_start", now)
            for key in ("hour", "period"):
                w = self.d[key]
                w["longest_outage_s"] = max(w["longest_outage_s"], outage)
            recovered = (self.d["fail_streak"], outage)
        self.d["fail_streak"] = 0
        self.d.pop("streak_start", None)
        return recovered

    def record_failure(self, now: float, error: str) -> None:
        self._bump("failed")
        if not self.d["fail_streak"]:
            self.d["streak_start"] = now
        self.d["fail_streak"] += 1
        self.d["last_error"] = error[:300]

    def record_alerts(self, n: int) -> None:
        if n:
            self._bump("alerts", n)

    def current_outage(self, now: float) -> float:
        return now - self.d.get("streak_start", now) if self.d["fail_streak"] else 0.0

    # -- hourly log line -------------------------------------------------------

    def hourly_due(self, now: float) -> bool:
        return now - self.d["hour"]["since"] >= 3600

    def hourly_line(self, now: float) -> str:
        w = self.d["hour"]
        parts = [f"{w['ok']} ok", f"{w['failed']} failed"]
        if w["failovers"]:
            parts.append(f"{w['failovers']} via backup address")
        parts.append(f"{w['alerts']} alerts")
        longest = max(w["longest_outage_s"], self.current_outage(now))
        if longest:
            parts.append(f"longest outage {fmt_duration(longest)}")
        parts.append(self._last_success_text(now))
        line = f"status (last {fmt_duration(now - w['since'])}): " + ", ".join(parts)
        self._reset("hour", now)
        return line

    # -- heartbeat -------------------------------------------------------------

    def heartbeat_due(self, now: float, interval_s: float) -> bool:
        if interval_s <= 0:
            return False
        last = self.d.get("last_heartbeat")
        return last is None or now - last >= interval_s

    def heartbeat_message(self, now: float, interval_s: float = 0) -> Message:
        w = self.d["period"]
        if self.d.get("last_heartbeat") is None:
            # First run (or first run of this version): confirm it's alive and say what to expect.
            nxt = f" Next status update in {fmt_duration(interval_s)}." if interval_s else ""
            status = "First poll OK." if not self.d["fail_streak"] else (
                f"First poll failed: {self.d.get('last_error', '')[:120].rstrip('.')}.")
            return Message(title="c17watch started", body=f"Watching for C-17s. {status}{nxt}", priority="low")
        lines = [f"Last {fmt_duration(now - w['since'])}: {_n(w['ok'], 'poll')} OK, {w['failed']} failed"]
        longest = max(w["longest_outage_s"], self.current_outage(now))
        if longest:
            lines.append(f"Longest outage: {fmt_duration(longest)}")
        lines.append(f"C-17 alerts: {w['alerts']}")
        lines.append(self._last_success_text(now).capitalize())
        down = self.current_outage(now)
        if down:
            lines.append(f"Failing for {fmt_duration(down)}: {self.d.get('last_error', '')[:120]}")
        feed_down = down >= FEED_DOWN_AFTER_S
        return Message(
            title="c17watch: data feed down" if feed_down else "c17watch running",
            body="\n".join(lines),
            priority="default" if feed_down else "low",
        )

    def heartbeat_sent(self, now: float) -> None:
        self.d["last_heartbeat"] = now
        self._reset("period", now)

    def heartbeat_failed(self, now: float, interval_s: float) -> None:
        # Try again in an hour rather than on every poll.
        self.d["last_heartbeat"] = now - interval_s + 3600

    # -- internals -------------------------------------------------------------

    def _last_success_text(self, now: float) -> str:
        last = self.d.get("last_success")
        return f"last success {fmt_duration(now - last)} ago" if last else "no successful poll yet"

    def _bump(self, counter: str, n: int = 1) -> None:
        for key in ("hour", "period"):
            self.d[key][counter] += n

    def _ensure_window(self, key: str, now: float) -> None:
        w = self.d.setdefault(key, {})
        w.setdefault("since", now)
        w.setdefault("longest_outage_s", 0.0)
        for c in COUNTERS:
            w.setdefault(c, 0)

    def _reset(self, key: str, now: float) -> None:
        self.d[key] = {}
        self._ensure_window(key, now)
