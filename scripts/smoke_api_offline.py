"""Offline smoke test for arcus_mcp.api - no live API calls.

Monkeypatches urllib.request.urlopen against tests/fixtures/ JSON and
exercises assets()/asset()/prices()/corporate_actions() plus TTL expiry
via a fake time.monotonic. Run: python3 scripts/smoke_api_offline.py
"""
import email.message
import importlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures"
sys.path.insert(0, str(REPO))


class FakeClock:
    """time.monotonic stand-in we can advance by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, dt):
        self.now += dt


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


CALLS = []


def install_mock(clock):
    """Route every urlopen call to a fixture by path suffix."""

    def fake_urlopen(req, timeout=None):
        path = req.full_url.replace(api.BASE, "").split("?")[0]
        CALLS.append(path)
        if path == "assets":
            payload = json.loads((FIXTURES / "assets.json").read_text())
        elif path.startswith("prices/"):
            sym = path.split("/", 1)[1]
            f = FIXTURES / f"prices_{sym}.json"
            if not f.exists():
                raise urllib.error.HTTPError(
                    req.full_url, 404, "Not Found", email.message.Message(), None)
            payload = json.loads(f.read_text())
        elif path == "corporate-actions":
            payload = {"corporateActions": [
                {"tokenSymbol": "AAPL", "kind": "DIVIDEND",
                 "amount": "0.25", "exDate": "2026-08-11"},
                {"tokenSymbol": "MSFT", "kind": "DIVIDEND",
                 "amount": "0.83", "exDate": "2026-08-18"},
            ]}
        else:
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
        return FakeResponse(payload)

    urllib.request.urlopen = fake_urlopen
    api.urllib.request.urlopen = fake_urlopen  # the module's own reference


def reset_module(clock):
    """Reimport api with the fake clock grafted onto time."""
    import time as real_time
    real_time.monotonic, api.time.monotonic = clock, clock
    importlib.reload(api)
    api.time.monotonic = clock


checks = []


def check(name, cond, detail=""):
    checks.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


import time as real_time
real_monotonic = real_time.monotonic

clock = FakeClock()
import arcus_mcp.api as api
install_mock(clock)
reset_module(clock)

# --- assets() -----------------------------------------------------------
a = api.assets()
check("assets() returns the full raw list", isinstance(a, list) and len(a) == 194,
      f"{len(a)} items")
aapl_meta = api.asset("aapl")
check("asset() case-insensitive lookup", aapl_meta and aapl_meta["tokenSymbol"] == "AAPL",
      f"status={aapl_meta and aapl_meta.get('status')}")
check("asset() strips whitespace", api.asset("  AAPL ") is not None)
check("asset() unknown -> None", api.asset("NOPE") is None)
check("asset() empty -> None", api.asset("") is None)

# --- cache: no refetch while fresh, refetch after TTL --------------------
n0 = len(CALLS)
_ = api.assets()
check("assets cache hit (no HTTP within TTL)", len(CALLS) == n0)
clock.advance(301)
_ = api.assets()
check("assets cache expired -> refetch after 300s", len(CALLS) == n0 + 1)

# --- prices() ------------------------------------------------------------
q = api.prices("aapl")
check("prices() returns first quote, normalized symbol", q and q["tokenSymbol"] == "AAPL",
      f"bid={q and q.get('bid')} ask={q and q.get('ask')}")
check("prices() unknown symbol -> None (404)", api.prices("ZZZZ") is None)
n1 = len(CALLS)
_ = api.prices("aapl")
check("prices cache hit (no HTTP within 15s TTL)", len(CALLS) == n1)
clock.advance(16)
_ = api.prices("aapl")
check("prices cache expired -> refetch after 15s", len(CALLS) == n1 + 1)

# --- corporate_actions() -------------------------------------------------
acts = api.corporate_actions()
check("corporate_actions() unwraps dict-wrapped list", isinstance(acts, list) and len(acts) == 2,
      f"kinds={[x.get('kind') for x in acts]}")
aapl_acts = api.corporate_actions("aapl")
check("corporate_actions(symbol) filters locally", len(aapl_acts) == 1
      and aapl_acts[0]["tokenSymbol"] == "AAPL")
check("corporate_actions(limit) caps results", api.corporate_actions(limit=1).__len__() == 1)
n2 = len(CALLS)
_ = api.corporate_actions()
check("corporate-actions cache hit (3600s TTL)", len(CALLS) == n2)
clock.advance(3601)
_ = api.corporate_actions()
check("corporate-actions cache expired -> refetch", len(CALLS) == n2 + 1)

# --- retries on 429 then success -----------------------------------------
class FlakyUrtopen:
    """Fails twice with 429, then succeeds - as a class to keep state."""

    def __init__(self):
        self.calls = 0

    def __call__(self, req, timeout=None):
        if self.calls < 2:
            self.calls += 1
            raise urllib.error.HTTPError(
                req.full_url, 429, "Too Many Requests",
                email.message.Message(), None)
        return FakeResponse(
            {"quotes": [{"tokenSymbol": "AAPL", "bid": "1", "ask": "2"}]})


flaky = FlakyUrtopen()
api.urllib.request.urlopen = flaky
snoozes = []
real_sleep = api.time.sleep
api.time.sleep = lambda s: snoozes.append(s)
q2 = api.get("prices/AAPL")
api.time.sleep = real_sleep
backoffs = [s for s in snoozes if s >= 1.0]  # drop 0.02s rate-limit waits
check("get() retries 429 and succeeds", q2["quotes"][0]["bid"] == "1"
      and flaky.calls == 2, f"all_sleeps={snoozes}")
check("429 retry backoff 2s*(n+1)", backoffs == [2.0, 4.0])

# --- rate limit: >=20ms between consecutive requests -----------------------
# The fake clock never advances, so every request must wait the full
# interval - exactly 0.02s each, one wait per request.
CALLS.clear()
api.urllib.request.urlopen = lambda req, timeout=None: FakeResponse({})
stamps = []
real_sleep2 = api.time.sleep
api.time.sleep = lambda s: stamps.append(s) if s < 1 else None
for _ in range(4):
    api.get("prices/AAPL")
api.time.sleep = real_sleep2
check("rate limiter enforces 0.02s interval",
      len(stamps) == 4 and all(abs(s - 0.02) < 1e-9 for s in stamps),
      f"waits={[round(s, 4) for s in stamps]}")

real_time.monotonic = real_monotonic
api.time.monotonic = real_monotonic
failed = [c for c in checks if not c[1]]
print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
