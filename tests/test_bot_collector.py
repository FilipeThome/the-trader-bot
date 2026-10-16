from __future__ import annotations

import io
import time
import urllib.error
from datetime import datetime, timedelta, timezone

from bot.collector import Collector
from bot.exchange.weight import reset_weight, weight_pressure


def _klines(open_ms: int) -> list:
    return [[open_ms, "100", "101", "99", "100.5", "10"]]


def _fetcher(now: datetime, *, old_hourly: bool = False, perpetual: bool = True):
    old = int((now - timedelta(hours=3)).timestamp() * 1000)

    def fetch(url: str):
        if url.endswith("/api/v3/time"):
            return {"serverTime": int((time.time() + 5) * 1000)}, {}
        if "klines" in url and "interval=1h" in url and old_hourly:
            return _klines(old), {"X-MBX-USED-WEIGHT-1M": "5000"}
        if "klines" in url:
            return _klines(int(now.timestamp() * 1000)), {}
        if "bookTicker" in url:
            return {"bidPrice": "100", "askPrice": "100.1", "bidQty": "2", "askQty": "2"}, {}
        if "fapi" in url and not perpetual:
            raise urllib.error.HTTPError(url, 400, "Invalid symbol.", {}, io.BytesIO(b"{}"))  # type: ignore[arg-type]
        if "fundingRate" in url:
            return [{"fundingRate": "0.0001"}, {"fundingRate": "0.0001"}, {"fundingRate": "0.0001"}], {}
        if "openInterest" in url:
            return {"openInterest": "10"}, {}
        raise AssertionError(url)

    return fetch


def test_stale_series_weight_pressure_and_clock_skew() -> None:
    reset_weight()
    now = datetime.now(timezone.utc)
    collector = Collector(_fetcher(now, old_hourly=True))
    collector.refresh(["BTC/USDT"], intervals=("1h",))
    assert collector.snapshot.stale("BTC/USDT", "1h", now=now) is True
    assert weight_pressure() is True
    assert collector.sync_clock() is False
    assert collector.snapshot.clock_blocked is True


def test_a_pair_without_a_perpetual_does_not_break_the_cycle() -> None:
    now = datetime.now(timezone.utc)
    collector = Collector(_fetcher(now, perpetual=False))
    snapshot = collector.refresh(["ABC/USDT"], intervals=("1h",))
    assert snapshot.funding["ABC/USDT"] == []
    assert "ABC/USDT" in collector.without_perpetual
    assert ("ABC/USDT", "1h") in snapshot.candles
    # A second cycle skips the USD-M reads for that pair.
    collector._fetch = _fetcher(now, perpetual=True)
    collector.refresh(["ABC/USDT"], intervals=("1h",))
    assert snapshot.funding["ABC/USDT"] == []


def test_a_server_error_on_funding_still_fails_the_cycle() -> None:
    now = datetime.now(timezone.utc)
    inner = _fetcher(now)

    def fetch(url: str):
        if "fundingRate" in url:
            raise urllib.error.HTTPError(url, 503, "busy", {}, io.BytesIO(b"{}"))  # type: ignore[arg-type]
        return inner(url)

    collector = Collector(fetch)
    try:
        collector.refresh(["BTC/USDT"], intervals=("1h",))
    except urllib.error.HTTPError as exc:
        assert exc.code == 503
    else:
        raise AssertionError("a 503 must not read as 'no perpetual'")
    assert "BTC/USDT" not in collector.without_perpetual



def test_one_failing_symbol_is_named_and_the_others_are_merged() -> None:
    now = datetime.now(timezone.utc)
    inner = _fetcher(now)

    def fetch(url: str):
        if "symbol=ETHUSDT" in url and "klines" in url:
            raise TimeoutError("eth")
        return inner(url)

    collector = Collector(fetch)
    view = collector.refresh(["BTC/USDT", "ETH/USDT", "SOL/USDT"], intervals=("1h",))
    assert view.failed == frozenset({"ETH/USDT"})
    assert ("BTC/USDT", "1h") in view.candles and ("SOL/USDT", "1h") in view.candles
    assert ("ETH/USDT", "1h") not in view.candles


def test_when_every_symbol_fails_the_error_reaches_the_cycle() -> None:
    def fetch(url: str):
        raise TimeoutError("binance down")

    collector = Collector(fetch)
    try:
        collector.refresh(["BTC/USDT", "ETH/USDT"], intervals=("1h",))
    except TimeoutError as exc:
        assert str(exc) == "binance down"
    else:
        raise AssertionError("a total failure must reach the cycle")


def test_weight_keeps_the_highest_report_within_a_minute_and_resets_after(monkeypatch) -> None:
    from bot.exchange import weight

    reset_weight()
    clock = [120.0]
    monkeypatch.setattr(weight, "_clock", lambda: clock[0])
    weight.note_weight(5000)
    weight.note_weight(100)
    assert weight.weight_snapshot()["spot"]["used"] == 5000 and weight_pressure() is True
    clock[0] = 180.0
    weight.note_weight(100)
    assert weight.weight_snapshot()["spot"]["used"] == 100 and weight_pressure() is False


def test_spot_and_usdm_budgets_are_counted_apart(monkeypatch) -> None:
    from bot.exchange import weight

    reset_weight()
    monkeypatch.setattr(weight, "_clock", lambda: 600.0)
    weight.note_weight(1000)
    weight.note_weight(20, venue="usdm")
    assert weight_pressure() is False
    weight.note_weight(1000, venue="usdm")
    assert weight_pressure() is True
    assert weight.weight_snapshot()["spot"] == {"used": 1000, "limit": 6000, "pressure": False}


def test_the_cycle_gets_a_copy_it_can_iterate_while_the_panel_fills_the_snapshot() -> None:
    now = datetime.now(timezone.utc)
    collector = Collector(_fetcher(now))
    view = collector.refresh(["BTC/USDT"], intervals=("1h",))
    collector._load_klines("ETH/USDT", "1h")
    assert ("ETH/USDT", "1h") in collector.snapshot.candles
    assert ("ETH/USDT", "1h") not in view.candles and ("BTC/USDT", "1h") in view.candles


def test_a_venue_that_stopped_reporting_is_not_under_pressure(monkeypatch) -> None:
    from bot.exchange import weight

    reset_weight()
    clock = [600.0]
    monkeypatch.setattr(weight, "_clock", lambda: clock[0])
    weight.note_weight(5500)
    assert weight_pressure() is True
    # A minute later Binance has reset its count, even if no new response told us so.
    clock[0] = 661.0
    assert weight_pressure() is False and weight.weight_snapshot()["spot"]["pressure"] is False


def test_books_come_in_one_batched_request(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    inner = _fetcher(now)
    urls: list[str] = []

    def fetch(url: str):
        urls.append(url)
        if "bookTicker" in url and "symbols=" in url:
            return [
                {"symbol": pair, "bidPrice": "100", "askPrice": "100.1", "bidQty": "2", "askQty": "2"}
                for pair in ("BTCUSDT", "ETHUSDT")
            ], {}
        return inner(url)

    view = Collector(fetch).refresh(["BTC/USDT", "ETH/USDT"], intervals=("1h",))
    assert view.failed == frozenset() and set(view.books) == {"BTC/USDT", "ETH/USDT"}
    assert sum(1 for url in urls if "bookTicker" in url) == 1


def test_a_rejected_batch_falls_back_to_one_book_per_symbol() -> None:
    now = datetime.now(timezone.utc)
    inner = _fetcher(now)
    urls: list[str] = []

    def fetch(url: str):
        urls.append(url)
        if "bookTicker" in url and "symbols=" in url:
            # One invalid symbol makes Binance reject the whole batch.
            raise urllib.error.HTTPError(url, 400, "Invalid symbol.", {}, io.BytesIO(b"{}"))  # type: ignore[arg-type]
        return inner(url)

    view = Collector(fetch).refresh(["BTC/USDT", "ETH/USDT"], intervals=("1h",))
    assert view.failed == frozenset() and set(view.books) == {"BTC/USDT", "ETH/USDT"}
    assert sum(1 for url in urls if "bookTicker" in url and "symbols=" not in url) == 2


def test_the_pooled_client_raises_http_errors_with_their_code(monkeypatch) -> None:
    import httpx

    from bot import collector as module

    transport = httpx.MockTransport(lambda request: httpx.Response(400, json={"code": -1121}))
    monkeypatch.setattr(module, "_CLIENT", httpx.Client(transport=transport))
    try:
        module._http_fetch("https://fapi.binance.com/fapi/v1/fundingRate?symbol=ABCUSDT")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
    else:
        raise AssertionError("a 400 must raise")
    # So a missing perpetual still reads as None, not as a failed cycle.
    assert Collector(module._http_fetch)._get_funding("ABC/USDT") is None


def test_a_rate_limit_answer_holds_the_readers_back(monkeypatch) -> None:
    import httpx

    from bot import collector as module
    from bot.exchange.weight import reset_weight, weight_pressure

    reset_weight()
    transport = httpx.MockTransport(lambda request: httpx.Response(429, json={"code": -1003}))
    monkeypatch.setattr(module, "_CLIENT", httpx.Client(transport=transport))
    try:
        module._http_fetch("https://api.binance.com/api/v3/klines?symbol=BTCUSDT")
    except urllib.error.HTTPError as exc:
        assert exc.code == 429
    else:
        raise AssertionError("a 429 must raise")
    assert weight_pressure()
    from bot.exchange.weight import weight_snapshot

    # The panel shows the venue full, not an absurd count.
    assert weight_snapshot()["spot"]["used"] == weight_snapshot()["spot"]["limit"]
    reset_weight()
