import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from c17watch.config import Config, ConfigError, load_config
from c17watch.geo import closest_approach_miles, haversine_miles
from c17watch.providers import Aircraft, Provider, RateLimited, build_provider
from c17watch.ratelimit import BudgetExhausted, RateLimiter
from c17watch.state import StateStore
from c17watch.watcher import Watcher, evaluate, format_message

HOME = (38.8977, -77.0365)
MI_PER_DEG_LAT = 69.09


def cfg(**kw):
    base = dict(
        lat=HOME[0], lon=HOME[1], radius_miles=15, cooldown_s=1800, lookahead=True,
        ignore_on_ground=True, max_position_age_s=60, aircraft_type="C17",
        respect_privacy_flags=True, provider="adsblol", poll_interval_s=60, contact=None,
        notify={}, state_path=Path(tempfile.mkdtemp()) / "s.json",
    )
    base.update(kw)
    return Config(**base)


def c17(hex="ae07e1", miles_north=5.0, track=None, gs=None, **kw):
    d = dict(hex=hex, type_code="C17", callsign="RCH123", registration="05-5147",
             lat=HOME[0] + miles_north / MI_PER_DEG_LAT, lon=HOME[1], alt_baro=12000,
             gs=gs, track=track, seen_pos=1.0, db_flags=1)
    d.update(kw)
    return Aircraft(**d)


class GeoTests(unittest.TestCase):
    def test_haversine_known_distance(self):
        # DC to Baltimore is ~35 statute miles
        self.assertAlmostEqual(haversine_miles(38.8977, -77.0365, 39.2904, -76.6122), 35.5, delta=1.0)

    def test_closest_approach_head_on(self):
        lat = HOME[0] + 30 / MI_PER_DEG_LAT  # 30 mi north, flying south at 450 kt
        d = closest_approach_miles(*HOME, lat, HOME[1], 180, 450, 600)
        self.assertLess(d, 0.5)

    def test_closest_approach_flying_away(self):
        lat = HOME[0] + 30 / MI_PER_DEG_LAT
        d = closest_approach_miles(*HOME, lat, HOME[1], 0, 450, 600)
        self.assertAlmostEqual(d, 30, delta=0.5)


class EvaluateTests(unittest.TestCase):
    def test_alert_on_entry_then_cooldown(self):
        c, tracked = cfg(), {}
        self.assertEqual(len(evaluate(c, [c17(miles_north=5)], tracked, 1000, 60)), 1)
        # still inside -> no repeat
        self.assertEqual(evaluate(c, [c17(miles_north=4)], tracked, 1060, 60), [])
        # leaves past hysteresis, comes back within cooldown -> no alert
        evaluate(c, [c17(miles_north=40)], tracked, 1120, 60)
        self.assertEqual(evaluate(c, [c17(miles_north=5)], tracked, 1180, 60), [])
        # comes back after cooldown -> alert
        evaluate(c, [c17(miles_north=40)], tracked, 3000, 60)
        self.assertEqual(len(evaluate(c, [c17(miles_north=5)], tracked, 3100, 60)), 1)

    def test_hysteresis_prevents_flapping(self):
        c, tracked = cfg(cooldown_s=60), {}
        evaluate(c, [c17(miles_north=14.9)], tracked, 0, 60)
        # wobbles just outside the radius but within 10% -> stays "inside"
        evaluate(c, [c17(miles_north=15.5)], tracked, 100, 60)
        self.assertTrue(tracked["ae07e1"]["inside"])
        self.assertEqual(evaluate(c, [c17(miles_north=14.9)], tracked, 200, 60), [])

    def test_filters(self):
        c = cfg(lookahead=False)
        cases = {
            "wrong type": c17(type_code="KC10"),
            "no position": c17(lat=None),
            "stale": c17(seen_pos=300),
            "on ground": c17(alt_baro="ground"),
            "LADD": c17(db_flags=8),
            "PIA": c17(db_flags=4),
            "too far": c17(miles_north=25),
        }
        for name, ac in cases.items():
            with self.subTest(name):
                self.assertEqual(evaluate(c, [ac], {}, 0, 60), [])

    def test_lookahead_catches_fast_pass(self):
        # 25 mi north heading south at 450 kt: outside now, inside within 60 s? no.
        # within 180 s it covers ~26 mi -> passes overhead.
        ac = c17(miles_north=25, track=180, gs=450)
        self.assertEqual(evaluate(cfg(), [ac], {}, 0, 60), [])
        hits = evaluate(cfg(), [ac], {}, 0, 180)
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0].predicted)

    def test_multiple_aircraft_one_message(self):
        hits = evaluate(cfg(), [c17("a1", 3), c17("a2", 6, callsign="RCH124")], {}, 0, 60)
        msg = format_message(hits, build_provider("adsblol"))
        self.assertEqual(msg.title, "2 C-17s overhead")
        self.assertIn("adsb.lol", msg.body)
        self.assertIn("a1", msg.click_url)


class RateLimitTests(unittest.TestCase):
    def provider(self, **kw):
        base = dict(name="p", url_template="x", map_url="x", attribution="", terms_url="",
                    min_interval_s=1)
        base.update(kw)
        return Provider(**base)

    def test_interval_raised_for_daily_budget(self):
        rl = RateLimiter(self.provider(daily_limit=500), 60, {})
        self.assertGreater(rl.interval_s, 170)

    def test_interval_respects_floor(self):
        rl = RateLimiter(self.provider(min_interval_s=30), 10, {})
        self.assertEqual(rl.interval_s, 30)

    def test_spacing_and_budget(self):
        t = [1_700_000_000.0]
        rl = RateLimiter(self.provider(daily_limit=2), 10, {}, clock=lambda: t[0])
        self.assertEqual(rl.seconds_until_allowed(), 0)
        rl.record_request()
        self.assertGreater(rl.seconds_until_allowed(), 0)
        rl.next_allowed = 0  # skip the spacing wait; same UTC day
        rl.record_request()
        rl.next_allowed = 0
        with self.assertRaises(BudgetExhausted):
            rl.seconds_until_allowed()

    def test_budget_resets_next_day(self):
        t = [1_700_000_000.0]
        usage = {}
        rl = RateLimiter(self.provider(daily_limit=1), 10, usage, clock=lambda: t[0])
        rl.record_request()
        t[0] += 86400
        rl.next_allowed = 0
        self.assertEqual(rl.seconds_until_allowed(), 0)

    def test_retry_after_and_backoff(self):
        t = [0.0]
        rl = RateLimiter(self.provider(), 60, {}, clock=lambda: t[0])
        self.assertEqual(rl.record_failure(retry_after=300), 300)
        d1 = rl.record_failure()
        d2 = rl.record_failure()
        self.assertGreater(d2, d1 * 1.2)
        for _ in range(20):
            rl.record_failure()
        self.assertLessEqual(rl.record_failure(), 15 * 60 * 1.2)

    def test_rapidapi_quota_header(self):
        usage = {}
        rl = RateLimiter(self.provider(monthly_limit=10000), 60, usage)
        rl.record_request()
        rl.record_success({"X-RateLimit-Requests-Remaining": "100"})
        self.assertEqual(usage["p"]["month_count"], 9900)
        self.assertGreater(rl.interval_s, 60)  # slowed to stretch remaining quota


class ConfigTests(unittest.TestCase):
    def write(self, text):
        p = Path(tempfile.mkdtemp()) / "c.toml"
        p.write_text(text)
        return p

    def test_env_overrides_location(self):
        p = self.write("[alert]\nradius_miles = 12\n")
        with mock.patch.dict("os.environ", {"C17WATCH_LAT": "40", "C17WATCH_LON": "-75"}):
            c = load_config(p)
        self.assertEqual((c.lat, c.lon, c.radius_miles), (40, -75, 12))

    def test_validation(self):
        with self.assertRaises(ConfigError):
            load_config(self.write("[location]\nlat=40\nlon=-75\n[provider]\npoll_interval_s=2\n"))
        with self.assertRaises(ConfigError):
            load_config(self.write("[alert]\nradius_miles=15\n"))  # no location

    def test_example_config_loads(self):
        load_config(Path(__file__).parent.parent / "config.example.toml")


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        return self._body


class WatcherTests(unittest.TestCase):
    def test_end_to_end_with_fixture(self):
        fixture = {"ac": [
            {"hex": "ae07e1", "t": "C17", "flight": "RCH123  ", "r": "05-5147",
             "lat": HOME[0] + 0.05, "lon": HOME[1], "alt_baro": 9000, "gs": 300, "track": 90,
             "seen_pos": 0.5, "dbFlags": 1},
            {"hex": "ae1234", "t": "C17", "lat": HOME[0] + 2, "lon": HOME[1], "alt_baro": 30000,
             "seen_pos": 0.5},
        ], "now": 0, "total": 2}
        c = cfg()
        session = mock.Mock()
        session.headers = {}
        session.get.return_value = FakeResponse(200, fixture)
        notifier = mock.Mock()
        notifier.send_with_retry.return_value = True
        w = Watcher(c, build_provider("adsblol"), notifier, session)

        self.assertEqual(w.poll_once(), 1)
        sent = notifier.send_with_retry.call_args[0][0]
        self.assertIn("RCH123", sent.body)
        self.assertIn("c17watch/", session.headers["User-Agent"])
        saved = json.loads(c.state_path.read_text())
        self.assertIn("ae07e1", saved["aircraft"])
        self.assertEqual(saved["usage"]["adsb.lol"]["day_count"], 1)

        # Restart: state persists, so no duplicate alert.
        w2 = Watcher(c, build_provider("adsblol"), notifier, session)
        w2.limiter.next_allowed = 0
        self.assertEqual(w2.poll_once(), 0)

    def test_429_backs_off(self):
        c = cfg()
        session = mock.Mock()
        session.headers = {}
        session.get.return_value = FakeResponse(429, headers={"Retry-After": "120"})
        w = Watcher(c, build_provider("adsblol"), mock.Mock(), session)
        w.poll_once()
        self.assertGreaterEqual(w.limiter.seconds_until_allowed(), 119)

    def test_failed_notification_retries_next_poll(self):
        c = cfg()
        session = mock.Mock()
        session.headers = {}
        session.get.return_value = FakeResponse(200, {"ac": [
            {"hex": "ae07e1", "t": "C17", "lat": HOME[0], "lon": HOME[1], "alt_baro": 5000, "seen_pos": 1}]})
        notifier = mock.Mock()
        notifier.send_with_retry.return_value = False
        w = Watcher(c, build_provider("adsblol"), notifier, session)
        w.poll_once()
        self.assertIsNone(w.state.aircraft["ae07e1"]["last_alert"])
        self.assertFalse(w.state.aircraft["ae07e1"]["inside"])



class DiagnoseTests(unittest.TestCase):
    def test_sinkhole_detected(self):
        from c17watch.providers import diagnose_connection
        fake = [(None, None, None, None, ("0.0.0.0", 443))]
        with mock.patch("socket.getaddrinfo", return_value=fake):
            self.assertIn("DNS blocker", diagnose_connection("https://api.adsb.lol/v2/type/C17"))

    def test_dns_failure(self):
        import socket
        from c17watch.providers import diagnose_connection
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror()):
            self.assertIn("DNS lookup", diagnose_connection("https://api.adsb.lol/x"))



class FailoverTests(unittest.TestCase):
    """Second address is tried when the first opens but then hangs/resets."""

    ADDRS = [(2, 1, 6, "", ("89.58.11.153", 443)), (2, 1, 6, "", ("185.233.105.52", 443))]

    def session(self, outcomes):
        import requests
        s = requests.Session()
        calls = []
        def get(url, **kw):
            calls.append((url, kw["headers"].get("Host")))
            o = outcomes.pop(0)
            if isinstance(o, Exception):
                raise o
            return o
        s.get = get
        return s, calls

    def test_fails_over_and_remembers(self):
        import requests
        ok = FakeResponse(200, {"ac": [{"hex": "ae145f", "t": "C17"}]})
        s, calls = self.session([requests.ConnectionError("reset"), ok, ok])
        p = build_provider("adsblol")
        with mock.patch("socket.getaddrinfo", return_value=self.ADDRS):
            ac, _ = p.fetch(s, "C17")
            self.assertEqual(ac[0].hex, "ae145f")
            self.assertEqual([c[0].split("/")[2] for c in calls], ["89.58.11.153", "185.233.105.52"])
            self.assertTrue(all(h == "api.adsb.lol" for _, h in calls))  # real Host header kept
            p.fetch(s, "C17")  # next poll: good server first
        self.assertEqual(calls[-1][0].split("/")[2], "185.233.105.52")

    def test_pins_tls_to_real_hostname(self):
        from c17watch.providers import _PinnedHostAdapter
        ok = FakeResponse(200, {"ac": []})
        s, _ = self.session([ok])
        with mock.patch("socket.getaddrinfo", return_value=self.ADDRS):
            build_provider("adsblol").fetch(s, "C17")
        a = s.adapters["https://89.58.11.153/"]
        self.assertIsInstance(a, _PinnedHostAdapter)
        self.assertEqual(a.hostname, "api.adsb.lol")

    def test_all_addresses_fail(self):
        import requests
        from c17watch.providers import ProviderError
        s, calls = self.session([requests.Timeout(), requests.ConnectionError()])
        with mock.patch("socket.getaddrinfo", return_value=self.ADDRS), self.assertRaises(ProviderError):
            build_provider("adsblol").fetch(s, "C17")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
