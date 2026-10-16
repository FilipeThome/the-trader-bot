from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from bot.exchange import sdk


class FakeClient:
    """The slice of a ccxt client the market cache touches."""

    loads = 0
    clock_reads = 0

    def __init__(self, table: dict[str, Any] | None = None, offset: int = 250) -> None:
        self.urls = {"api": {"public": "https://testnet.binance.vision/api/v3"}}
        self.options: dict[str, Any] = {}
        self.markets: dict[str, Any] = {}
        self.currencies: Any = None
        self._table = {"BTC/USDT": {"id": "BTCUSDT", "symbol": "BTC/USDT"}} if table is None else table
        self._offset = offset

    def load_markets(self, reload: bool = False, params: dict[str, Any] | None = None) -> dict[str, Any]:
        FakeClient.loads += 1
        # Binance's fetch_markets measures the clock offset while loading.
        self.options["timeDifference"] = self._offset
        self.markets = dict(self._table)
        return self.markets

    def load_time_difference(self) -> int:
        FakeClient.clock_reads += 1
        self.options["timeDifference"] = self._offset
        return self._offset

    def set_markets(self, markets: list[Any], currencies: Any = None) -> dict[str, Any]:
        self.markets = {item["symbol"]: item for item in markets}
        self.currencies = currencies
        return self.markets


def _client(cfg: Any, **kwargs: Any) -> FakeClient:
    client = FakeClient(**kwargs)
    sdk._install_market_cache(client, cfg)
    return client


def test_a_second_signed_client_reuses_the_markets_and_measures_the_clock_again() -> None:
    FakeClient.loads = FakeClient.clock_reads = 0
    cfg = SimpleNamespace(market_type="spot", is_testnet=True, api_key="key")
    _client(cfg).load_markets()
    second = _client(cfg, offset=900)
    assert second.load_markets() == {"BTC/USDT": {"id": "BTCUSDT", "symbol": "BTC/USDT"}}
    assert FakeClient.loads == 1
    # A stop or exit signed with an hour-old offset fails -1021: the offset is measured now.
    assert FakeClient.clock_reads == 1 and second.options["timeDifference"] == 900


def test_a_signed_client_whose_clock_read_fails_loads_everything() -> None:
    FakeClient.loads = 0
    cfg = SimpleNamespace(market_type="spot", is_testnet=True, api_key="key")
    _client(cfg).load_markets()
    second = _client(cfg, offset=700)

    def unreachable() -> int:
        raise ConnectionError("time endpoint down")

    second.load_time_difference = unreachable  # type: ignore[method-assign]
    second.load_markets()
    assert FakeClient.loads == 2 and second.options["timeDifference"] == 700


def test_public_and_signed_clients_do_not_share_a_table() -> None:
    FakeClient.loads = 0
    _client(SimpleNamespace(market_type="spot", is_testnet=True, api_key="key")).load_markets()
    _client(SimpleNamespace(market_type="spot", is_testnet=True, api_key="")).load_markets()
    assert FakeClient.loads == 2


def test_an_empty_table_is_not_cached_and_the_cache_expires(monkeypatch) -> None:
    FakeClient.loads = 0
    cfg = SimpleNamespace(market_type="spot", is_testnet=False, api_key="")
    _client(cfg, table={}).load_markets()
    _client(cfg).load_markets()
    assert FakeClient.loads == 2
    monkeypatch.setattr(sdk, "MARKETS_TTL_SECONDS", 0.0)
    _client(cfg).load_markets()
    assert FakeClient.loads == 3


def test_a_forced_reload_reaches_the_exchange() -> None:
    FakeClient.loads = 0
    cfg = SimpleNamespace(market_type="spot", is_testnet=True, api_key="")
    client = _client(cfg)
    client.load_markets()
    client.load_markets(reload=True)
    assert FakeClient.loads == 2


def test_a_loaded_client_answers_from_its_own_table() -> None:
    FakeClient.loads = FakeClient.clock_reads = 0
    cfg = SimpleNamespace(market_type="spot", is_testnet=True, api_key="key")
    _client(cfg).load_markets()
    second = _client(cfg)
    second.load_markets()
    # ccxt calls load_markets before every method: no clock read per call.
    second.load_markets()
    second.load_markets()
    assert FakeClient.loads == 1 and FakeClient.clock_reads == 1
