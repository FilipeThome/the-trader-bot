"""What the advisor asks and how its answer is read.

The system prompt is fixed and goes first, so DeepSeek's prefix cache serves it on every call;
``PROMPT_VERSION`` changes with it, and the calibration groups by it. The user message is built
by code from numbers and enums only: a validated pair, a known strategy and timeframe, the
row's plan and costs, the setup's regime, a summary of ``features.build_features``, the BTC 4h
trend and the account's risk level. No free text from an outside source reaches the prompt, so
there is nothing to inject through.

The answer is parsed by pydantic with ``allow_inf_nan=False``. ``size_factor`` is clipped to
[0.25, 1]; a non-finite number, an unknown verdict or anything that is not the JSON object
asked for becomes ``abstain``, which changes nothing.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from bot.config import is_usdt_pair
from bot.evaluator import Opportunity
from bot.indicators import MARKET_EMA_BARS, ema
from bot.risk import MIN_ADVICE_FACTOR
from bot.strategies import STRATEGIES

PROMPT_VERSION = "1"
Verdict = Literal["confirm", "reduce", "veto", "abstain"]
#: The timeframes a row can carry.
_TIMEFRAMES = frozenset({"15m", "1h", "4h", "8h"})
#: Setup regimes the strategies emit; any other value is left out.
_SETUP_REGIMES = frozenset({"trend_up", "trend_down", "range", "volatile", "funding"})
_RISK_LEVELS = frozenset({"normal", "cautious", "reducing", "halt"})
_ALPHA_NAME = re.compile(r"^[a-z0-9_]{1,40}$")
#: 1h closes summarised as returns, newest last.
RECENT_BARS = 12
MAX_FLAGS = 4
MAX_FLAG_CHARS = 60
MAX_RATIONALE_CHARS = 240

SYSTEM_PROMPT = f"""You are a risk reviewer for a long-only Binance spot trading bot (prompt version {PROMPT_VERSION}).
A deterministic engine already found a setup, sized it and checked every risk limit. You give a
second opinion on one proposed entry. You cannot create trades or increase sizes: you may confirm
the entry, reduce its size, or veto it.

The user message is a json object with numbers and enums only:
- symbol, strategy, timeframe, direction (1 = buy)
- plan: stop_pct and target_pct (distance from entry as a fraction), expected_r (net reward over risk),
  score, cost (round trip as a fraction), win_rate and win_rate_n (estimated hit rate and its sample)
- setup_regime, alphas (strategy factor values)
- features: indicator values of the pair on 1h and 15m bars, price levels as a fraction of the last close,
  recent_1h_returns (oldest first)
- btc_4h_trend: above_ema, below_ema or unknown (240-bar EMA of BTC 4h closes)
- account: risk_level, drawdown (fraction from the peak), open_positions

Judge whether the setup is consistent with the data: trend and momentum alignment, overextension,
volatility versus the stop, market context. Veto only for a clear contradiction. Reduce when the
setup is plausible but risk is elevated.

Answer with a single json object and nothing else, with exactly these fields:
verdict: "confirm", "reduce" or "veto"
confidence: number from 0 to 1
size_factor: number from 0.25 to 1 (used only with "reduce")
regime: "trend_up", "trend_down", "range" or "volatile"
risk_flags: list of at most 4 short strings
rationale: at most 240 characters

Example:
{{"verdict": "reduce", "confidence": 0.7, "size_factor": 0.5, "regime": "volatile",
"risk_flags": ["atr high versus stop"], "rationale": "Breakout is valid but 1h volatility is twice the stop distance."}}
"""


@dataclass(frozen=True)
class Advice:
    """A parsed verdict. ``abstain`` is no verdict: nothing changes."""

    verdict: Verdict
    confidence: float = 0.0
    size_factor: float = 1.0
    regime: str | None = None
    risk_flags: tuple[str, ...] = field(default_factory=tuple)
    rationale: str = ""


ABSTAIN = Advice("abstain")


class _Output(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="ignore")

    verdict: Literal["confirm", "reduce", "veto"]
    confidence: float = Field(ge=0, le=1)
    size_factor: float = 1.0
    regime: Literal["trend_up", "trend_down", "range", "volatile"]
    risk_flags: list[str] = Field(default_factory=list)
    rationale: str = ""

    @field_validator("size_factor")
    @classmethod
    def _clip(cls, value: float) -> float:
        return min(1.0, max(MIN_ADVICE_FACTOR, value))

    @field_validator("risk_flags")
    @classmethod
    def _short_flags(cls, value: list[str]) -> list[str]:
        return [_plain(item, MAX_FLAG_CHARS) for item in value[:MAX_FLAGS]]

    @field_validator("rationale")
    @classmethod
    def _short_rationale(cls, value: str) -> str:
        return _plain(value, MAX_RATIONALE_CHARS)


def parse_advice(content: str) -> Advice:
    """The verdict in ``content``, or ``ABSTAIN`` when it is not the JSON object asked for."""
    try:
        raw = json.loads(content)
        output = _Output.model_validate(raw)
    except (ValueError, ValidationError, TypeError, RecursionError):
        return ABSTAIN
    return Advice(
        output.verdict,
        output.confidence,
        output.size_factor if output.verdict == "reduce" else 1.0,
        output.regime,
        tuple(output.risk_flags),
        output.rationale,
    )


def user_data(
    opportunity: Opportunity,
    *,
    features: dict[str, Any] | None,
    btc_trend: str,
    risk_level: str,
    drawdown: float,
    open_positions: int,
) -> dict[str, Any]:
    """The user message's data for one row. Raises ``ValueError`` for a pair, strategy or
    timeframe outside the known sets, so nothing unvalidated is ever sent."""
    if not is_usdt_pair(opportunity.symbol):
        raise ValueError("par fora do formato BASE/USDT")
    if opportunity.strategy not in STRATEGIES or opportunity.timeframe not in _TIMEFRAMES:
        raise ValueError("estratégia ou timeframe desconhecido")
    stop = opportunity.risk_pct
    alphas = {
        name: _num(value) for name, value in (opportunity.alphas or {}).items() if isinstance(name, str) and _ALPHA_NAME.match(name)
    }
    return {
        "symbol": opportunity.symbol,
        "strategy": opportunity.strategy,
        "timeframe": opportunity.timeframe,
        "direction": _num(opportunity.direction),
        "plan": {
            "stop_pct": _num(stop),
            "target_pct": _num(opportunity.plan_target_pct),
            "expected_r": _num(opportunity.net_return / stop) if stop > 0 else None,
            "score": _num(opportunity.score),
            "cost": _num(opportunity.cost),
            "win_rate": _num(opportunity.win_rate),
            "win_rate_n": int(opportunity.win_rate_n),
        },
        "setup_regime": opportunity.regime if opportunity.regime in _SETUP_REGIMES else None,
        "alphas": alphas,
        "features": feature_summary(features) if features else None,
        "btc_4h_trend": btc_trend if btc_trend in ("above_ema", "below_ema") else "unknown",
        "account": {
            "risk_level": risk_level if risk_level in _RISK_LEVELS else None,
            "drawdown": _num(drawdown),
            "open_positions": int(open_positions),
        },
    }


def feature_summary(features: dict[str, Any]) -> dict[str, Any]:
    """The last values of ``build_features``' series, prices as a fraction of the last close.

    The panel's 120-bar series would cost tokens without telling more than their last values
    and a few recent returns.
    """
    trend = features.get("trend") or {}
    mean = features.get("mean_reversion") or {}
    close_1h = _last_close(trend)
    close_15m = _last_close(mean)
    closes = [candle[1] for candle in trend.get("candles") or [] if len(candle) > 1]
    recent = [_num(after / before - 1) for before, after in zip(closes[-RECENT_BARS - 1 : -1], closes[-RECENT_BARS:]) if before]
    volume, volume_avg = _last(trend, "volume"), _last(trend, "volume_avg")
    return {
        "h1": {
            "ema50": _relative(_last(trend, "ema50"), close_1h),
            "breakout_high": _relative(_last(trend, "breakout_high"), close_1h),
            "breakout_low": _relative(_last(trend, "breakout_low"), close_1h),
            "adx": _num(_last(trend, "adx")),
            "volume_ratio": _num(volume / volume_avg) if volume is not None and volume_avg else None,
        },
        "m15": {
            "bollinger_mid": _relative(_last(mean, "mid"), close_15m),
            "bollinger_upper": _relative(_last(mean, "upper"), close_15m),
            "bollinger_lower": _relative(_last(mean, "lower"), close_15m),
            "rsi": _num(_last(mean, "rsi")),
        },
        "recent_1h_returns": recent,
        "funding_latest": _num((features.get("funding_carry") or {}).get("latest")),
    }


def btc_trend(closes_4h: Any) -> str:
    """``above_ema`` or ``below_ema`` for the last closed BTC 4h close against its 240-bar EMA;
    ``unknown`` with fewer bars (``indicators.btc_trend_ok`` lets those through, but the model
    should not read them as a trend)."""
    if closes_4h is None:
        return "unknown"
    values = np.asarray(closes_4h, dtype=float)
    if len(values) < MARKET_EMA_BARS or not np.isfinite(values[-1]):
        return "unknown"
    average = float(ema(pd.Series(values), MARKET_EMA_BARS).iloc[-1])
    return "above_ema" if values[-1] >= average else "below_ema"


def render(data: dict[str, Any]) -> str:
    """The user message: sorted keys, so the same data hashes to the same prompt."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)


def prompt_sha256(user: str) -> str:
    """The hash of the whole prompt (system and user), recorded in the ledger."""
    return hashlib.sha256(f"{SYSTEM_PROMPT}\n{user}".encode("utf-8")).hexdigest()


def _num(value: Any, digits: int = 6) -> float | None:
    """A finite number rounded to ``digits``; None otherwise (a NaN never reaches the JSON)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, np.floating, np.integer)):
        return None
    number = float(value)
    return round(number, digits) if math.isfinite(number) else None


def _last(block: dict[str, Any], name: str) -> float | None:
    series = block.get(name) or []
    return _num(series[-1]) if series else None


def _last_close(block: dict[str, Any]) -> float | None:
    candles = block.get("candles") or []
    return _num(candles[-1][1]) if candles and len(candles[-1]) > 1 else None


def _relative(level: float | None, close: float | None) -> float | None:
    if level is None or not close:
        return None
    return _num(level / close - 1)


def _plain(text: str, limit: int) -> str:
    """One line of printable text, at most ``limit`` characters."""
    return "".join(char if char.isprintable() else " " for char in text).strip()[:limit]
