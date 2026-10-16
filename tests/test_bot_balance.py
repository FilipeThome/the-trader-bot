from __future__ import annotations

from typing import Any

import pytest

from bot.api.app import ctx
from bot.exchange import sdk, service


@pytest.fixture
def account(monkeypatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "balances": [
            {"asset": "USDT", "free": 900.0, "used": 100.0, "total": 1000.0},
            {"asset": "BTC", "free": 0.01, "used": 0.0, "total": 0.01},
            {"asset": "ETH", "free": 0.5, "used": 0.5, "total": 1.0},
            {"asset": "XYZ", "free": 5.0, "used": 0.0, "total": 5.0},
        ],
        "reads": 0,
    }
    monkeypatch.setattr(sdk, "build_config", lambda *_a, **_k: sdk.BinanceConfig(profile="paper", api_key="k", api_secret="s"))

    def snapshot(_config: Any = None) -> dict[str, Any]:
        state["reads"] += 1
        return {"status": "ok", "balances": state["balances"]}

    monkeypatch.setattr(sdk, "get_account_snapshot", snapshot)
    monkeypatch.setattr(sdk, "get_last_prices", lambda symbols, config=None: {"BTC/USDT": 60000.0, "ETH/USDT": 3000.0})
    return state


def test_balances_are_valued_in_usdt_and_sorted_by_value(account) -> None:
    result = service.account_balances("binance-paper-trade")
    assert result["status"] == "ok" and result["is_testnet"] is True
    assert [row["asset"] for row in result["balances"]] == ["ETH", "USDT", "BTC", "XYZ"]
    eth = result["balances"][0]
    assert eth["used"] == 0.5 and eth["value_usdt"] == 3000.0
    assert result["total_usdt"] == pytest.approx(3000.0 + 1000.0 + 600.0)
    assert result["unpriced"] == 1 and result["balances"][-1]["value_usdt"] is None


def test_missing_keys_are_reported_without_a_read(monkeypatch) -> None:
    monkeypatch.setattr(sdk, "build_config", lambda *_a, **_k: sdk.BinanceConfig(profile="paper"))
    monkeypatch.setattr(sdk, "get_account_snapshot", lambda *_a: pytest.fail("no read without keys"))
    assert service.account_balances("binance-paper-trade") == {"status": "error", "error": "credentials_missing"}


def test_a_failed_price_read_still_lists_the_balances(account, monkeypatch) -> None:
    def offline(*_a: Any, **_k: Any) -> dict[str, float]:
        raise TimeoutError("prices")

    monkeypatch.setattr(sdk, "get_last_prices", offline)
    result = service.account_balances("binance-paper-trade")
    assert result["total_usdt"] == 1000.0 and result["unpriced"] == 3


def test_last_prices_skips_pairs_the_host_does_not_list(monkeypatch) -> None:
    class Exchange:
        def load_markets(self) -> dict[str, Any]:
            return {"BTC/USDT": {}, "ETH/USDT": {}}

        def fetch_last_prices(self, symbols: list[str]) -> dict[str, Any]:
            assert symbols == ["BTC/USDT"]
            return {"BTC/USDT": {"price": 60000.0}}

    monkeypatch.setattr(sdk, "_public_exchange", lambda _cfg: Exchange())
    monkeypatch.setattr(sdk, "_assert_host", lambda _cfg: None)
    prices = sdk.get_last_prices(["BTC/USDT", "XYZ/USDT"], config=sdk.BinanceConfig(profile="paper"))
    assert prices == {"BTC/USDT": 60000.0}


def test_balance_route_caches_and_reports_missing_keys(api_client, account, monkeypatch) -> None:
    first = api_client.get("/bot/balance")
    second = api_client.get("/bot/balance")
    assert first.status_code == 200 and first.json()["mode"] == "testnet"
    assert second.json() == first.json() and account["reads"] == 1
    ctx.balance_cache = None
    monkeypatch.setattr(sdk, "build_config", lambda *_a, **_k: sdk.BinanceConfig(profile="paper"))
    missing = api_client.get("/bot/balance")
    assert missing.status_code == 503 and "chaves" in missing.json()["detail"]
