from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from bot.backtest.instances import (
    OUT_OF_RANGE_WARN,
    REPLAY_WINDOW_DAYS,
    out_of_range_share,
    replay_dca,
    replay_grid,
    replay_windows,
)
from bot.instances import (
    DcaParams,
    GridParams,
    cooldown_ends,
    creation_problems,
    dca_ladder,
    dca_plan,
    dca_stop,
    grid_plan,
)

DCA = DcaParams(base_usd=10, so_count=2, step_pct=0.02, take_profit_pct=0.03, stop_pct=0.05)
GRID = GridParams(lower=90, upper=110, grids=4, invest_usd=40, stop_price=85)


def _frame(closes: list[float], *, freq: str = "1h") -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=len(closes), freq=freq, tz="UTC")
    return pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1.0}, index=index)


def _problems(*, live_instances: int | None = None, max_instances: int | None = None) -> list[str]:
    plan = dca_plan(DCA, 100.0, taker=0.001)
    extra: dict[str, int | None] = {}
    if live_instances is not None:
        extra = {"live_instances": live_instances, "max_instances": max_instances}
    return creation_problems(
        "dca", plan, min_notional=5, order_cap=1000, sizing_capital=10_000, exposure_room=1000,
        risk_per_trade_pct=0.02, taker=0.001, spread=0.0, params=DCA, **extra,
    )


# -- max instances ----------------------------------------------------------------


def test_creation_refuses_over_max_instances() -> None:
    assert _problems() == []
    assert _problems(live_instances=2, max_instances=3) == []
    assert _problems(live_instances=3, max_instances=3) == ["já há 3 robôs ativos (máximo de 3)"]
    assert _problems(live_instances=5, max_instances=3) == ["já há 5 robôs ativos (máximo de 3)"]
    # No limit given: any number of robots passes, as before the limit existed.
    assert _problems(live_instances=50, max_instances=None) == []


def test_the_max_instances_check_applies_to_grids_too() -> None:
    plan = grid_plan(GRID, 100.0, taker=0.001, spread=0.0)
    problems = creation_problems(
        "grid", plan, min_notional=5, order_cap=1000, sizing_capital=10_000, exposure_room=1000,
        risk_per_trade_pct=0.02, taker=0.001, spread=0.0, params=GRID, live_instances=1, max_instances=1,
    )
    assert "já há 1 robôs ativos (máximo de 1)" in problems


# -- cooldown ---------------------------------------------------------------------


def test_cooldown_defaults_to_one_minute_and_old_params_load() -> None:
    old_dca = {"base_usd": 10, "so_count": 2, "step_pct": 0.02, "take_profit_pct": 0.03, "stop_pct": 0.05, "repeat": True}
    old_grid = {"lower": 90, "upper": 110, "grids": 4, "invest_usd": 40, "stop_price": 85, "max_orders_per_day": 20}
    assert DcaParams.model_validate(old_dca).cooldown_minutes == 1
    assert GridParams.model_validate(old_grid).cooldown_minutes == 1
    # What a new instance stores reads back unchanged.
    stored = DcaParams.model_validate({**old_dca, "cooldown_minutes": 15}).model_dump()
    assert DcaParams.model_validate(stored).cooldown_minutes == 15
    assert GridParams.model_validate({**old_grid, "cooldown_minutes": 0}).cooldown_minutes == 0
    for bad in (-1, 1441):
        with pytest.raises(ValidationError):
            DcaParams.model_validate({**old_dca, "cooldown_minutes": bad})
        with pytest.raises(ValidationError):
            GridParams.model_validate({**old_grid, "cooldown_minutes": bad})
    with pytest.raises(ValidationError):
        GridParams.model_validate({**old_grid, "cooldown": 5})


def test_cooldown_ends_is_utc_whatever_the_stamp() -> None:
    expected = datetime(2026, 10, 20, 12, 1, tzinfo=timezone.utc)
    assert cooldown_ends("2026-10-20T12:00:00+00:00", 1) == expected
    assert cooldown_ends("2026-10-20T12:00:00Z", 1) == expected
    assert cooldown_ends("2026-10-20T12:00:00", 1) == expected
    assert cooldown_ends("2026-10-20T09:00:00-03:00", 1) == expected
    assert cooldown_ends("2026-10-20T12:00:00.123456+00:00", 0) == datetime(2026, 10, 20, 12, 0, 0, 123456, tzinfo=timezone.utc)
    assert cooldown_ends("2026-10-20T23:30:00+00:00", 60).utcoffset() == timedelta(0)


# -- preview windows ----------------------------------------------------------------


def test_out_of_range_share_and_warning() -> None:
    # Six closes inside (the bounds count as inside), four above the range.
    frame = _frame([90.0, 100.0, 110.0, 100.0, 95.0, 105.0, 120.0, 121.0, 122.0, 123.0])
    assert out_of_range_share(GRID, frame) == pytest.approx(0.4)
    assert out_of_range_share(GRID, _frame([80.0, 100.0, 100.0, 100.0])) == pytest.approx(0.25)
    assert out_of_range_share(GRID, frame.iloc[:0]) == 0.0
    assert out_of_range_share(DCA, frame) == 0.0

    (outside,) = replay_windows("grid", GRID, frame, taker=0.001, spread=0.0, days=(1,))
    assert outside["out_of_range_share"] == pytest.approx(0.4) and outside["warn"] is True
    calm = _frame([100.0] * 7 + [120.0] * 3)
    (inside,) = replay_windows("grid", GRID, calm, taker=0.001, spread=0.0, days=(1,))
    assert inside["out_of_range_share"] <= OUT_OF_RANGE_WARN and inside["warn"] is False
    (dca,) = replay_windows("dca", DCA, frame, taker=0.001, spread=0.0, days=(1,))
    assert dca["out_of_range_share"] == 0.0 and dca["warn"] is False


def test_replay_windows_30_90_180() -> None:
    hours = 200 * 24
    closes = [100.0 + 8.0 * math.sin(i / 20) for i in range(hours)]
    frame = _frame(closes)
    rows = replay_windows("grid", GRID, frame, taker=0.001, spread=0.0005)
    assert REPLAY_WINDOW_DAYS == (30, 90, 180)
    assert [row["days"] for row in rows] == [30, 90, 180]
    assert [row["bars"] for row in rows] == [30 * 24, 90 * 24, 180 * 24]
    for row in rows:
        # The window is the frame's tail, replayed by the unchanged replay.
        tail = frame.iloc[-row["days"] * 24:]
        expected = replay_grid(GRID, tail, taker=0.001, spread=0.0005)
        assert {key: row[key] for key in expected} == expected
        assert row["warn"] is False

    dca_rows = replay_windows("dca", DCA, frame, taker=0.001, spread=0.0, days=(90,))
    assert dca_rows[0] == {
        "days": 90,
        **replay_dca(DCA, frame.iloc[-90 * 24:], taker=0.001, spread=0.0),
        "out_of_range_share": 0.0,
        "warn": False,
    }


def test_a_young_pair_replays_its_whole_history_in_the_longer_windows() -> None:
    frame = _frame(list(np.linspace(95, 105, 40 * 24)))
    rows = replay_windows("grid", GRID, frame, taker=0.001, spread=0.0)
    assert [row["bars"] for row in rows] == [30 * 24, 40 * 24, 40 * 24]
    empty = replay_windows("grid", GRID, frame.iloc[:0], taker=0.001, spread=0.0)
    assert [row["bars"] for row in empty] == [0, 0, 0]


def _ohlc(rows: list[tuple[float, float, float]]) -> pd.DataFrame:
    """(open, low, close) per 1h bar."""
    index = pd.date_range("2026-01-01", periods=len(rows), freq="1h", tz="UTC")
    opens, lows, closes = zip(*rows)
    highs = [max(o, c) for o, c in zip(opens, closes)]
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": 1.0}, index=index)


def test_a_dca_stop_bar_fills_the_resting_safety_orders_above_the_stop_first() -> None:
    resting = DCA.model_copy(update={"resting_buys": True})
    ladder = dca_ladder(DCA, 100.0)
    stop = dca_stop(DCA, 100.0)
    assert [row["price"] for row in ladder] == pytest.approx([100.0, 98.0, 96.0]) and stop == pytest.approx(91.2)
    held = sum(row["usd"] * 0.999 / row["price"] for row in ladder)
    # The bar opens at 99 and falls through both safety orders to the stop: the stop sells all three buys.
    falls = _ohlc([(100, 100, 100), (99, 80, 81)])
    stopped = replay_dca(resting, falls, taker=0.001)
    assert stopped["ended"] == "stop"
    assert stopped["pnl_usd"] == pytest.approx(held * stop * 0.999 - 30, rel=1e-9)
    assert stopped["pnl_usd"] < replay_dca(DCA, falls, taker=0.001)["pnl_usd"]
    # A gap through the stop: the levels crossed in the gap fill, and the stop sells at the open.
    gapped = replay_dca(resting, _ohlc([(100, 100, 100), (90, 80, 81)]), taker=0.001)
    assert gapped["ended"] == "stop" and gapped["pnl_usd"] == pytest.approx(held * 90 * 0.999 - 30, rel=1e-9)


def test_a_grid_stop_bar_fills_the_resting_levels_above_the_stop_first() -> None:
    resting = GRID.model_copy(update={"resting_buys": True})
    # Started at 100: levels 100 and 105 held, 95 then 90 rest under them, the stop at 85.
    held = 10 / 90 + 10 / 95 + 10 / 100 + 10 / 105
    paid = (10 / 100 + 10 / 105) * 100 * 1.001 + 2 * 10 * 1.001
    opens_between = replay_grid(resting, _ohlc([(100, 100, 100), (96, 84, 86)]), taker=0.001)
    assert opens_between["ended"] == "stop"
    assert opens_between["pnl_usd"] == pytest.approx(held * 85 * 0.999 - paid, rel=1e-9)
    gapped = replay_grid(resting, _ohlc([(100, 100, 100), (84, 80, 82)]), taker=0.001)
    assert gapped["ended"] == "stop" and gapped["pnl_usd"] == pytest.approx(held * 84 * 0.999 - paid, rel=1e-9)


def test_a_grid_take_profit_bar_sells_the_level_its_wick_filled() -> None:
    resting = GRID.model_copy(update={"resting_buys": True, "take_profit_price": 112})
    # Started at 100 (levels 100 and 105 held); the next bar wicks to 94, filling 95, and closes at 113.
    bars = _ohlc([(100, 100, 100), (100, 94, 113)])
    result = replay_grid(resting, bars, taker=0.001)
    held = 10 / 95 + 10 / 100 + 10 / 105
    paid = 10 * 1.001 + (10 / 100 + 10 / 105) * 100 * 1.001
    assert result["ended"] == "take_profit"
    assert result["realized_usd"] == pytest.approx(held * 113 * 0.999 - paid, rel=1e-9)
    assert result["pnl_usd"] == pytest.approx(held * 113 * 0.999 - paid, rel=1e-9)
    # Without resting buys the take-profit sells only the two levels bought at the start.
    market = replay_grid(GRID.model_copy(update={"take_profit_price": 112}), bars, taker=0.001)
    two = 10 / 100 + 10 / 105
    assert market["pnl_usd"] == pytest.approx(two * 113 * 0.999 - two * 100 * 1.001, rel=1e-9)


def test_a_dca_take_profit_bar_sells_the_safety_order_its_wick_filled() -> None:
    resting = DCA.model_copy(update={"resting_buys": True})
    # The wick to 97.9 fills the safety order at 98, which lowers the take-profit under the 102.5 close.
    held = 10 * 0.999 / 100 + 10 * 0.999 / 98
    assert 20 / held * (1 + DCA.take_profit_pct) <= 102.5 < 100 / 0.999 * (1 + DCA.take_profit_pct)
    result = replay_dca(resting, _ohlc([(100, 100, 100), (100, 97.9, 102.5)]), taker=0.001)
    assert result["wins"] == 1 and result["ended"] is None
    assert result["pnl_usd"] == pytest.approx(held * 102.5 * 0.999 - 20, rel=1e-9)


def test_replay_windows_rejects_a_mismatched_kind_or_frame() -> None:
    frame = _frame([100.0] * 48)
    with pytest.raises(TypeError):
        replay_windows("dca", GRID, frame, taker=0.001, spread=0.0)
    with pytest.raises(TypeError):
        replay_windows("grid", GRID, frame.reset_index(drop=True), taker=0.001, spread=0.0)
    with pytest.raises(ValueError):
        replay_windows("grid", GRID, frame, taker=0.001, spread=0.0, days=(0,))
