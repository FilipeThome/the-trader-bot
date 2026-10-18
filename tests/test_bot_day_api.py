from __future__ import annotations

from bot.config import BotConfig
from bot.live.mandate.store import load_mandate
from bot.mandate_io import ORDERS_PER_DAY_ENTRY


def test_the_day_preset_previews_then_applies_and_reconfirms_the_mandate(api_client) -> None:
    preview = api_client.get("/bot/presets/day")
    assert preview.status_code == 200
    body = preview.json()
    fields = {change["field"] for change in body["changes"]}
    assert {"limits.risk_per_trade_pct", "strategies.intraday_momentum", "strategies.vwap_pullback"} <= fields
    assert body["mandate_orders_per_day"] == ORDERS_PER_DAY_ENTRY * 8
    # Nothing changes before the POST.
    assert BotConfig.load().strategies.get("intraday_momentum") is False

    applied = api_client.post("/bot/presets/day")
    assert applied.status_code == 200
    config = BotConfig.load()
    assert config.strategies["intraday_momentum"] and config.strategies["vwap_pullback"]
    assert not config.strategies["trend"]
    assert config.limits.risk_per_trade_pct == 0.005 and config.limits.max_trades_per_day == 8
    assert config.cost_multiple == 3.0 and config.min_reward_risk == 1.2
    mandate = load_mandate("binance")
    assert mandate is not None and mandate.hard_caps.max_trades_per_day == ORDERS_PER_DAY_ENTRY * 8


def test_positions_show_the_day_deadline_and_first_target(api_client) -> None:
    from bot.api.app import ctx

    api_client.get("/bot/status")
    assert ctx.runtime is not None
    store = ctx.runtime.store
    position_id = store.open_pending(
        "BTC/USDT", "vwap_pullback", stop_price=98.0, target_price=103.0, exit_policy="day", exit_deadline="2026-10-16T18:00:00+00:00"
    )
    store.update_position(position_id, status="open", qty=1.0, avg_price=100.0)
    rows = api_client.get("/bot/positions").json()["rows"]
    row = next(item for item in rows if item["id"] == position_id)
    assert row["exit_deadline"] == "2026-10-16T18:00:00+00:00" and row["tp1_done"] == 0


def test_a_day_validation_reads_90_days_of_15m_and_the_1h_bars(api_client, monkeypatch) -> None:
    import threading

    import pandas as pd

    fetched: list[tuple[str, int]] = []

    class Loader:
        def fetch(self, symbols, start, end, interval="1d", *, on_symbol=None):
            fetched.append((interval, (pd.Timestamp(end) - pd.Timestamp(start)).days))
            freq = "15min" if interval == "15m" else "h"
            index = pd.date_range("2026-07-01", periods=200, freq=freq, tz="UTC")
            frame = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10.0}, index=index)
            return {symbol: frame for symbol in symbols}

    monkeypatch.setattr("bot.backtest.loaders.registry.resolve_loader", lambda _market: Loader())
    assert api_client.post("/bot/strategies/intraday_momentum/validate").status_code == 202
    for thread in threading.enumerate():
        if thread.name.startswith("validate-"):
            thread.join(10)
    assert fetched[0] == ("15m", 90)
    assert fetched[1][0] == "1h" and fetched[1][1] >= 90
    job = api_client.get("/bot/status").json()["validations"]["intraday_momentum"]
    assert job["status"] == "done" and job["result"]["exit_policy"] == "day"


def test_status_reports_the_day_margin(api_client) -> None:
    assert api_client.get("/bot/status").json()["day_win_rate_margin"] == 0.03
