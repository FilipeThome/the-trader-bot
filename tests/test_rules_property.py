from __future__ import annotations

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from bot.exchange.execution.rules import MarketRules, floor_to_step, size_order


@given(
    value=st.decimals(min_value="0.00000001", max_value="1000000", places=8, allow_nan=False, allow_infinity=False),
    step=st.decimals(min_value="0.00000001", max_value="1", places=8, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=40)
def test_floor_never_rounds_up(value: Decimal, step: Decimal) -> None:
    floored = floor_to_step(value, step)
    assert floored <= value
    assert floored % step == 0


def test_market_notional_is_floored_to_the_tick() -> None:
    rules = MarketRules(
        symbol="BTC/USDT",
        price_tick=Decimal("0.01"),
        amount_tick=Decimal("0.00001"),
        lot_step=Decimal("0.00001"),
        market_step=Decimal("0.00001"),
        min_qty=Decimal("0.00001"),
        max_qty=Decimal("1000"),
        market_min_qty=None,
        market_max_qty=None,
        min_notional=Decimal("5"),
        max_notional=Decimal("100000"),
        min_price=Decimal("0.01"),
        max_price=Decimal("1000000"),
        limit_ticks=(Decimal("0.01"),),
        taker_fee=Decimal("0.001"),
        active=True,
        spot=True,
    )
    sized, reasons = size_order(
        order_type="market",
        quantity=None,
        notional=Decimal("10.019"),
        limit_price=None,
        rules=rules,
        reference_price=Decimal("100"),
    )
    assert reasons == ()
    assert sized is not None
    assert sized.notional == Decimal("10.01")
