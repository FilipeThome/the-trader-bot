from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from bot.config import MAX_MACRO_EVENTS, BotConfig, RiskLimits


def _load(tmp_path, data: dict) -> BotConfig:
    path = tmp_path / "bot.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return BotConfig.load(path)


def test_legacy_bot_json_keeps_its_saved_order_cap(tmp_path) -> None:
    config = _load(tmp_path, {"limits": {"max_order_notional_pct": 0.02, "max_exposure_pct": 0.5}})
    assert config.limits.max_order_notional_pct == 0.02
    assert config.limits.max_exposure_pct == 0.5


def test_new_risk_fields_have_their_defaults_and_bounds() -> None:
    limits = RiskLimits()
    assert limits.vol_cap_pct == 0.01
    assert limits.target_vol_annual == 0.60
    assert limits.dd_reduce_pct == 0.05
    assert limits.max_drawdown_pct == 0.10
    assert limits.halt_drawdown_pct == 0.15
    assert limits.vol_throttle_ratio == 2.0
    assert limits.cluster_corr == 0.70
    assert limits.max_instances == 3
    assert limits.slippage_budget_pct == 0.003
    assert limits.max_signal_age_seconds == 120
    # Zero switches each optional guard off.
    off = RiskLimits(
        vol_cap_pct=0, target_vol_annual=0, dd_reduce_pct=0, vol_throttle_ratio=0,
        max_instances=0, slippage_budget_pct=0, max_signal_age_seconds=0,
    )
    assert off.vol_cap_pct == 0 and off.dd_reduce_pct == 0 and off.max_signal_age_seconds == 0
    upper = RiskLimits(
        vol_cap_pct=0.05, target_vol_annual=3, vol_throttle_ratio=10, cluster_corr=1,
        max_instances=20, slippage_budget_pct=0.05, max_signal_age_seconds=3600,
    )
    assert upper.max_instances == 20 and upper.cluster_corr == 1
    for field, value in (
        ("vol_cap_pct", 0.051), ("vol_cap_pct", -0.01),
        ("target_vol_annual", 3.1), ("target_vol_annual", -0.1),
        ("dd_reduce_pct", -0.01), ("dd_reduce_pct", 0.51),
        ("halt_drawdown_pct", 0), ("halt_drawdown_pct", 1.01),
        ("vol_throttle_ratio", 10.5), ("vol_throttle_ratio", -1),
        ("cluster_corr", 0), ("cluster_corr", 1.01),
        ("max_instances", 21), ("max_instances", -1),
        ("slippage_budget_pct", 0.06), ("slippage_budget_pct", -0.001),
        ("max_signal_age_seconds", 3601), ("max_signal_age_seconds", -1),
        ("vol_cap_pct", float("nan")),
    ):
        with pytest.raises(ValidationError):
            RiskLimits(**{field: value})


def test_a_legacy_bot_json_derives_the_drawdown_steps_from_its_drawdown_limit(tmp_path) -> None:
    config = _load(tmp_path, {"limits": {"max_drawdown_pct": 0.2}})
    assert config.limits.dd_reduce_pct == pytest.approx(0.1)
    assert config.limits.halt_drawdown_pct == pytest.approx(0.3)
    # 1.5 times the limit, kept within [15%, 50%].
    assert RiskLimits(max_drawdown_pct=0.04).halt_drawdown_pct == 0.15
    assert RiskLimits(max_drawdown_pct=0.4).halt_drawdown_pct == 0.5
    # A step the file gives is kept; only the missing one is derived.
    partial = RiskLimits(max_drawdown_pct=0.2, dd_reduce_pct=0)
    assert partial.dd_reduce_pct == 0 and partial.halt_drawdown_pct == pytest.approx(0.3)
    # Saved and loaded again, the derived steps are written out and stay.
    path = tmp_path / "saved.json"
    path.write_text(config.model_dump_json(), encoding="utf-8")
    assert BotConfig.load(path).limits == config.limits


def test_the_drawdown_steps_must_be_in_order() -> None:
    with pytest.raises(ValidationError, match="dd_reduce_pct"):
        RiskLimits(dd_reduce_pct=0.10, max_drawdown_pct=0.10)
    with pytest.raises(ValidationError, match="halt_drawdown_pct"):
        RiskLimits(max_drawdown_pct=0.10, halt_drawdown_pct=0.10)
    with pytest.raises(ValidationError, match="halt_drawdown_pct"):
        RiskLimits(max_drawdown_pct=0.6, halt_drawdown_pct=0.5)
    assert RiskLimits(dd_reduce_pct=0, max_drawdown_pct=0.10, halt_drawdown_pct=0.12).dd_reduce_pct == 0
    # A bad drawdown limit is reported as itself, not as a derived step.
    with pytest.raises(ValidationError, match="percentage limits"):
        RiskLimits(max_drawdown_pct=0)


def test_new_bot_fields_have_their_defaults(tmp_path) -> None:
    config = _load(tmp_path, {"mode": "testnet"})
    assert config.market_filter == "off"
    assert config.exit_hysteresis is True
    assert config.flatten_day_on_daily_loss is False
    assert config.macro_events_utc == []
    assert config.macro_pause_minutes == 15
    assert BotConfig(market_filter="btc_ema40d").market_filter == "btc_ema40d"
    for field, value in (("market_filter", "eth"), ("macro_pause_minutes", 181), ("macro_pause_minutes", -1)):
        with pytest.raises(ValidationError):
            BotConfig(**{field: value})


def test_macro_events_are_parsed_to_utc() -> None:
    config = BotConfig(
        macro_events_utc=[
            "2026-11-05T13:30:00-05:00",
            "2026-11-05T18:30:00Z",
            " 2026-11-01T12:00:00 ",
            "2026-11-01T12:00:00+00:00",
        ]
    )
    assert config.macro_events_utc == ["2026-11-01T12:00:00+00:00", "2026-11-05T18:30:00+00:00"]
    with pytest.raises(ValidationError, match="ISO"):
        BotConfig(macro_events_utc=["amanhã às 9h"])
    hourly = [f"2026-12-{day:02d}T{hour:02d}:00:00+00:00" for day in range(1, 6) for hour in range(24)]
    assert len(BotConfig(macro_events_utc=hourly[:MAX_MACRO_EVENTS]).macro_events_utc) == MAX_MACRO_EVENTS
    with pytest.raises(ValidationError):
        BotConfig(macro_events_utc=hourly[: MAX_MACRO_EVENTS + 1])


@pytest.mark.parametrize(("drawdown", "halt"), [(0.4, 0.5), (0.5, 0.75), (0.6, 0.9), (0.7, 1.0), (1.0, 1.0)])
def test_a_legacy_bot_json_with_a_wide_drawdown_limit_still_loads(tmp_path, drawdown, halt) -> None:
    config = _load(tmp_path, {"limits": {"max_drawdown_pct": drawdown}})
    assert config.limits.max_drawdown_pct == drawdown
    assert config.limits.halt_drawdown_pct == pytest.approx(halt)
    assert config.limits.dd_reduce_pct == pytest.approx(drawdown / 2)
