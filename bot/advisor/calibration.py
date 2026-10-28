"""What the advisor's verdicts were worth, measured with the replay's rules (Q-A1, Q-A2, Q-A3).

Every logged verdict, entered or not, gets the same label: the R a position would have made from
the signal bar's close with the row's stop and target, filled as ``simulate_fixed`` fills them
(``runner.fixed_leg``): the stop on the low at ``min(open, stop)``, the target only on a close at
or above it, and the last close after ``COUNTERFACTUAL_BARS`` bars. The result is net of the
replay's cost, ``(ROUND_TRIP_COST + 2 × LEG_SLIPPAGE) / stop_pct`` in R.

The candles come from the collector's snapshot (closed bars of the row's timeframe). A row whose
signal bar is not in it (the pair left the analysed list, or the bar scrolled out) reads its
window by time through the backtest loader's weight-accounted klines read, a few rows per pass and
never under weight pressure. A row still without a label once its window and ``EXPIRY_GRACE``
passed is marked expired: never tried again, out of every mean, counted apart. Leaving it
waiting would keep only the fast stops and targets of the pairs that rotate out.

A row joins an average only once its whole window has passed since the signal bar, whatever its
result: counting it as soon as it resolves would bring the fast stops and targets first. One row
counts per signal. The R of the rows that entered stays in its own column.
"""

from __future__ import annotations

import logging
import math
import statistics
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from bot.advisor.client import PRICES_AS_OF
from bot.backtest.loaders.binance_loader import BinanceLoader
from bot.backtest.runner import LEG_SLIPPAGE, ROUND_TRIP_COST, _prices, fixed_leg
from bot.collector import MarketSnapshot
from bot.cycle import market
from bot.exchange.weight import weight_pressure
from bot.store import Store

logger = logging.getLogger(__name__)

#: Bars after the signal bar a counterfactual position may stay open before it is closed at the close.
COUNTERFACTUAL_BARS = 48
#: The replay's round-trip cost, as a share of the entry.
COST = ROUND_TRIP_COST + 2 * LEG_SLIPPAGE
#: Klines reads per resolution pass for rows whose signal bar is not in the snapshot.
MAX_FETCHES = 5
#: How long after its window a row may still be labelled before it expires.
EXPIRY_GRACE = timedelta(days=1)
#: The fields that name one signal: a duplicate of them counts once.
_SIGNAL_KEY = ("symbol", "strategy", "timeframe", "signal_close_at", "prompt_version", "model")
#: Reads the closed candles of (symbol, timeframe) opened from ``start`` and closed by ``end``.
KlinesFetch = Callable[[str, str, datetime, datetime], pd.DataFrame | None]
#: Each verdict's calibration line, in the panel's order, with its label.
_VERDICT_LABELS = {"confirm": "confirmar", "reduce": "reduzir", "veto": "vetar", "abstain": "sem veredito"}


def counterfactual_r(
    frame: pd.DataFrame, signal_index: int, stop_pct: float, target_pct: float, *, max_bars: int | None = COUNTERFACTUAL_BARS
) -> float | None:
    """The R of a fixed stop and target entered at the close of bar ``signal_index``, net of
    ``COST``; None while the bars run out first. ``target_pct`` 0 or less is no target: the
    position runs to the stop or the bar limit."""
    if not (stop_pct > 0 and math.isfinite(stop_pct)):
        return None
    target = target_pct if target_pct > 0 and math.isfinite(target_pct) else math.inf
    leg = fixed_leg(_prices(frame), signal_index, stop_pct, target, max_bars=max_bars)
    if leg is None:
        return None
    net = (leg.fill - leg.entry) / leg.entry - COST
    return net / stop_pct


def fetch_closed(symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame | None:
    """The closed candles of ``symbol`` on ``timeframe`` from ``start`` to ``end``, through the
    backtest loader (``BinanceLoader``: weight accounted, stops under pressure, closed bars only);
    None when the read failed or came back empty."""
    try:
        frame = BinanceLoader().fetch([symbol], start.isoformat(), end.isoformat(), interval=timeframe).get(symbol)
    except Exception as exc:  # noqa: BLE001 - the row is tried again on a later pass
        logger.warning("advice resolution: klines of %s %s unavailable (%s)", symbol, timeframe, type(exc).__name__)
        return None
    return frame if frame is not None and not frame.empty else None


def resolve_pending(store: Store, snapshot: MarketSnapshot, moment: datetime, *, fetch: KlinesFetch | None = None) -> int:
    """Label the unresolved ``advice_log`` rows; returns how many were labelled.

    A row reads its window from ``snapshot``. When its signal bar is not there, the window
    (the signal bar to ``COUNTERFACTUAL_BARS`` bars after it, by time) is read with ``fetch``
    (``fetch_closed`` by default), at most ``MAX_FETCHES`` times per pass and never under weight
    pressure. A row that was tried and is still without a label once its window and
    ``EXPIRY_GRACE`` passed is marked expired.
    """
    read = fetch or fetch_closed
    resolved = fetched = 0
    now = pd.Timestamp(moment)
    for row in store.advice_rows(unresolved=True):
        symbol, timeframe = str(row["symbol"]), str(row["timeframe"])
        width = market._BAR.get(timeframe)
        if width is None:
            continue
        signal_close = _utc(str(row["signal_close_at"]))
        opened = signal_close - width
        window_end = signal_close + COUNTERFACTUAL_BARS * width
        stop, target = float(row["stop_pct"]), float(row["target_pct"])
        frame = market._closed(snapshot, symbol, timeframe, moment)
        position = _locate(frame, opened) if frame is not None else None
        value = None
        if frame is not None and position is not None:
            value = counterfactual_r(frame, position, stop, target)
        tried = position is not None
        if position is None and fetched < MAX_FETCHES and not weight_pressure():
            fetched += 1
            tried = True
            history = read(symbol, timeframe, opened.to_pydatetime(), min(now, window_end).to_pydatetime())
            found = _locate(history, opened) if history is not None and not history.empty else None
            if history is not None and found is not None:
                value = counterfactual_r(history, found, stop, target)
        if value is not None and math.isfinite(value):
            store.resolve_advice(int(row["id"]), value, moment.isoformat())
            resolved += 1
        elif tried and now >= window_end + EXPIRY_GRACE:
            store.expire_advice(int(row["id"]), moment.isoformat())
    return resolved


def calibration(rows: Iterable[dict[str, Any]], *, min_veto_confidence: float, now: datetime) -> list[dict[str, Any]]:
    """Per (model, prompt version): every consulted row as the base, then each verdict, then the
    vetoes at or above ``min_veto_confidence`` (what ``filter`` would remove). Each line has n,
    the mean counterfactual R and its standard error over the rows whose window has passed;
    ``reduce`` adds the mean of ``(1 − factor) × R``, the R the smaller size gave up or saved.
    ``realized_*`` is the R of the closed positions the rows entered, never mixed in."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in _one_per_signal(rows):
        groups.setdefault((str(row["model"]), str(row["prompt_version"])), []).append(row)
    out: list[dict[str, Any]] = []
    for (model, version), members in sorted(groups.items()):
        lines: list[tuple[str, str, list[dict[str, Any]]]] = [("all", "todas as consultadas", members)]
        for verdict, label in _VERDICT_LABELS.items():
            chosen = [row for row in members if row["verdict"] == verdict]
            if chosen:
                lines.append((verdict, label, chosen))
        strong = [row for row in members if row["verdict"] == "veto" and float(row["confidence"]) >= min_veto_confidence]
        if strong:
            lines.append(("veto_confident", f"vetos com confiança ≥ {min_veto_confidence:.0%}", strong))
        for group, label, chosen in lines:
            line = {"model": model, "prompt_version": version, "group": group, "label": label, **_summary(chosen, now)}
            if group == "reduce":
                matured = [row for row in chosen if _matured(row, now)]
                line["reduced_mean_r"] = _mean([(1 - float(row["size_factor"])) * float(row["counterfactual_r"]) for row in matured])
            out.append(line)
    return out


def usage(store: Store, now: datetime) -> dict[str, Any]:
    """Calls, tokens, estimated cost, verdicts and errors of today and of the last 30 UTC days."""
    today = now.date()
    return {
        "today": store.advisor_usage(today.isoformat(), today.isoformat()),
        "last_30_days": store.advisor_usage((today - timedelta(days=29)).isoformat()),
        "prices_as_of": PRICES_AS_OF,
        "estimate": True,
    }


def _one_per_signal(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The first row of each signal (``_SIGNAL_KEY``); the rows come oldest first. A signal asked
    twice would count twice in n and shrink the standard error."""
    seen: set[tuple[str, ...]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = tuple(str(row[name]) for name in _SIGNAL_KEY)
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


def _summary(rows: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    matured = [float(row["counterfactual_r"]) for row in rows if _matured(row, now)]
    realized = [float(row["realized_r"]) for row in rows if row.get("realized_r") is not None]
    # Their window passed without the candles to label them: out of the means, counted apart.
    expired = sum(1 for row in rows if row.get("expired_at") is not None)
    return {
        "n": len(matured),
        "mean_r": _mean(matured),
        "se": statistics.stdev(matured) / math.sqrt(len(matured)) if len(matured) >= 2 else None,
        "waiting": len(rows) - len(matured) - expired,
        "expired": expired,
        "realized_n": len(realized),
        "realized_mean_r": _mean(realized),
    }


def _matured(row: dict[str, Any], now: datetime) -> bool:
    """Resolved, and the whole window of ``COUNTERFACTUAL_BARS`` bars has passed since the signal bar."""
    width = market._BAR.get(str(row["timeframe"]))
    if row.get("counterfactual_r") is None or width is None:
        return False
    return bool(pd.Timestamp(str(row["signal_close_at"])) + COUNTERFACTUAL_BARS * width <= pd.Timestamp(now))


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _utc(stamp: str) -> pd.Timestamp:
    moment = pd.Timestamp(stamp)
    return moment if moment.tzinfo is not None else moment.tz_localize("UTC")


def _locate(frame: pd.DataFrame, opened: pd.Timestamp) -> int | None:
    """The position of the bar that opened at ``opened`` in ``frame``, or None."""
    index = frame.index
    if getattr(index, "tz", None) is None and opened.tzinfo is not None:
        opened = opened.tz_convert(None)
    elif getattr(index, "tz", None) is not None and opened.tzinfo is None:
        opened = opened.tz_localize("UTC")
    found = int(index.get_indexer([opened])[0])
    return found if found >= 0 else None
