"""Account fees (D19): a daily signed read that overrides the market's taker fee."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest

from bot.exchange import sdk, service

PAPER = "binance-paper-trade"
_MARKET: dict[str, Any] = {
    "symbol": "BTC/USDT",
    "active": True,
    "spot": True,
    "taker": 0.001,
    "precision": {"price": 0.01, "amount": 0.00001},
    "info": {
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.01", "minPrice": "0.01", "maxPrice": "1000000"},
            {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001", "maxQty": "9000"},
            {"filterType": "NOTIONAL", "minNotional": "5", "maxNotional": "9000000"},
        ]
    },
}


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def fees(monkeypatch) -> Iterator[dict[str, Any]]:
    """Scripted commission reads on a frozen clock; never a network call."""
    state: dict[str, Any] = {"reads": 0, "answer": {"status": "ok", "maker": "0.0009", "taker": "0.00075"}}
    clock = _Clock()
    state["clock"] = clock
    service.reset_fee_cache()
    monkeypatch.setattr(service, "_monotonic", clock)
    monkeypatch.setattr(sdk, "build_config", lambda *_a, **_k: sdk.BinanceConfig(profile="paper", api_key="k", api_secret="s"))
    monkeypatch.setattr(sdk, "get_spot_market", lambda symbol, config=None: {"status": "ok", "market": dict(_MARKET)})

    def commission(symbol: str, *, config: Any = None) -> dict[str, Any]:
        state["reads"] += 1
        answer = state["answer"]
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)

    monkeypatch.setattr(sdk, "get_account_commission", commission)
    yield state
    service.reset_fee_cache()


def test_account_commission_overrides_the_market_taker(fees) -> None:
    before = service.market_rules("BTC/USDT", PAPER)
    assert before is not None and before.taker_fee == Decimal("0.001")
    assert fees["reads"] == 0  # market_rules never reads the account itself

    assert service.trading_fees("BTC/USDT", PAPER) == (Decimal("0.0009"), Decimal("0.00075"))
    after = service.market_rules("BTC/USDT", PAPER)
    assert after is not None and after.taker_fee == Decimal("0.00075")
    assert after.lot_step == before.lot_step and after.min_notional == before.min_notional
    assert service.market_rules("ETH/USDT", PAPER).taker_fee == Decimal("0.001")
    assert fees["reads"] == 1


def test_the_fee_read_is_cached_for_a_day(fees) -> None:
    for _ in range(3):
        service.trading_fees("BTC/USDT", PAPER)
    assert fees["reads"] == 1
    fees["clock"].now += service.FEES_TTL_SECONDS - 1
    service.trading_fees("BTC/USDT", PAPER)
    assert fees["reads"] == 1
    fees["answer"] = {"status": "ok", "maker": "0.001", "taker": "0.0012"}
    fees["clock"].now += 1
    assert service.trading_fees("BTC/USDT", PAPER) == (Decimal("0.001"), Decimal("0.0012"))
    assert fees["reads"] == 2
    service.trading_fees("ETH/USDT", PAPER)
    assert fees["reads"] == 3  # one read per symbol


def test_a_failed_fee_read_keeps_the_ccxt_taker_and_retries_after_an_hour(fees) -> None:
    fees["answer"] = {"status": "error", "error": "read_failed (BadRequest)"}
    assert service.trading_fees("BTC/USDT", PAPER) is None
    assert service.market_rules("BTC/USDT", PAPER).taker_fee == Decimal("0.001")
    fees["clock"].now += service.FEES_RETRY_SECONDS - 1
    assert service.trading_fees("BTC/USDT", PAPER) is None
    assert fees["reads"] == 1

    fees["answer"] = {"status": "ok", "maker": "0.0009", "taker": "0.00075"}
    fees["clock"].now += 1
    assert service.trading_fees("BTC/USDT", PAPER) == (Decimal("0.0009"), Decimal("0.00075"))
    assert fees["reads"] == 2
    assert service.market_rules("BTC/USDT", PAPER).taker_fee == Decimal("0.00075")


def test_a_failed_refresh_keeps_the_last_known_fee_and_retries_after_an_hour(fees) -> None:
    service.trading_fees("BTC/USDT", PAPER)
    fees["answer"] = TimeoutError("commission")
    fees["clock"].now += service.FEES_TTL_SECONDS
    assert service.trading_fees("BTC/USDT", PAPER) == (Decimal("0.0009"), Decimal("0.00075"))
    assert service.market_rules("BTC/USDT", PAPER).taker_fee == Decimal("0.00075")
    fees["clock"].now += service.FEES_RETRY_SECONDS - 1
    service.trading_fees("BTC/USDT", PAPER)
    assert fees["reads"] == 2
    fees["clock"].now += 1
    service.trading_fees("BTC/USDT", PAPER)
    assert fees["reads"] == 3


def test_an_unknown_profile_reads_nothing(fees) -> None:
    assert service.trading_fees("BTC/USDT", "no-such-profile") is None
    assert fees["reads"] == 0


def test_concurrent_readers_share_one_cache(fees) -> None:
    service.trading_fees("BTC/USDT", PAPER)
    results: list[Any] = []
    lock = threading.Lock()

    def read() -> None:
        fees = service.trading_fees("BTC/USDT", PAPER)
        with lock:
            results.append(fees)

    threads = [threading.Thread(target=read) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [(Decimal("0.0009"), Decimal("0.00075"))] * 16
    assert fees["reads"] == 1


class _Exchange:
    def __init__(self, payload: Any = None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def privateGetAccountCommission(self, params: dict[str, Any]) -> Any:  # noqa: N802 - ccxt's name
        self.calls.append(params)
        if self.error is not None:
            raise self.error
        return self.payload


_PAYLOAD = {
    "symbol": "BTCUSDT",
    "standardCommission": {"maker": "0.00080000", "taker": "0.00100000", "buyer": "0", "seller": "0"},
    "taxCommission": {"maker": "0.00000000", "taker": "0.00020000", "buyer": "0", "seller": "0"},
    "discount": {"enabledForAccount": True, "enabledForSymbol": True, "discountAsset": "BNB", "discount": "0.75"},
}


def _config() -> sdk.BinanceConfig:
    return sdk.BinanceConfig(profile="paper", api_key="secret-key", api_secret="secret-value")


def test_commission_is_standard_plus_tax_without_the_bnb_discount(monkeypatch) -> None:
    exchange = _Exchange(payload=_PAYLOAD)
    monkeypatch.setattr(sdk, "_exchange", lambda _cfg: exchange)
    result = sdk.get_account_commission("btcusdt", config=_config())
    assert result["status"] == "ok"
    assert Decimal(result["maker"]) == Decimal("0.0008")
    assert Decimal(result["taker"]) == Decimal("0.0012")
    assert exchange.calls == [{"symbol": "BTCUSDT"}]


def test_a_testnet_commission_error_is_reported_not_raised(monkeypatch) -> None:
    class BadRequest(Exception):
        pass

    monkeypatch.setattr(sdk, "_exchange", lambda _cfg: _Exchange(error=BadRequest("binance {\"code\":-1003}")))
    result = sdk.get_account_commission("BTC/USDT", config=_config())
    assert result["status"] == "error" and result["error"] == "read_failed (BadRequest)"
    assert "secret-key" not in repr(result) and "secret-value" not in repr(result)

    monkeypatch.setattr(sdk, "_exchange", lambda _cfg: _Exchange(payload={"symbol": "BTCUSDT"}))
    assert sdk.get_account_commission("BTC/USDT", config=_config())["error"] == "read_failed (unreadable commission)"
    broken = {**_PAYLOAD, "standardCommission": {"maker": "0.001", "taker": "abc"}}
    monkeypatch.setattr(sdk, "_exchange", lambda _cfg: _Exchange(payload=broken))
    assert sdk.get_account_commission("BTC/USDT", config=_config())["status"] == "error"


def test_commission_without_keys_builds_no_client(monkeypatch) -> None:
    monkeypatch.setattr(sdk, "_exchange", lambda _cfg: pytest.fail("no client without keys"))
    result = sdk.get_account_commission("BTC/USDT", config=sdk.BinanceConfig(profile="paper"))
    assert result["status"] == "error" and result["error"] == "credentials_missing"
