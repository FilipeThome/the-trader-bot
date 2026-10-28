"""What the advisor's verdicts were worth, measured with the replay's rules (Q-A1, Q-A2, Q-A3).

Every logged verdict, entered or not, gets the same label: the R a position would have made from
the signal bar's close with the row's stop and target, filled as ``simulate_fixed`` fills them
(``runner.fixed_leg``): the stop on the low at ``min(open, stop)``, the target only on a close at
or above it, and the last close after ``COUNTERFACTUAL_BARS`` bars. The result is net of the
replay's cost, ``(ROUND_TRIP_COST + 2 × LEG_SLIPPAGE) / stop_pct`` in R.

The candles come from the collector's snapshot (closed bars of the row's timeframe); a row whose
window is not in it stays unresolved. A row joins an average only once its whole window has
passed since the signal bar, whatever its result: counting it as soon as it resolves would bring
the fast stops and targets first. The R of the rows that entered stays in its own column.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from bot.advisor.client import PRICES_AS_OF
from bot.backtest.runner import LEG_SLIPPAGE, ROUND_TRIP_COST, _prices, fixed_leg
from bot.collector import MarketSnapshot
from bot.cycle import market
from bot.store import Store

#: Bars after the signal bar a counterfactual position may stay open before it is closed at the close.
COUNTERFACTUAL_BARS = 48
#: The replay's round-trip cost, as a share of the entry.
COST = ROUND_TRIP_COST + 2 * LEG_SLIPPAGE
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


def resolve_pending(store: Store, snapshot: MarketSnapshot, moment: datetime) -> int:
    """Label every unresolved ``advice_log`` row whose window is in ``snapshot``; returns how many."""
    resolved = 0
    for row in store.advice_rows(unresolved=True):
        width = market._BAR.get(str(row["timeframe"]))
        if width is None:
            continue
        frame = market._closed(snapshot, str(row["symbol"]), str(row["timeframe"]), moment)
        if frame is None:
            continue
        position = _locate(frame, pd.Timestamp(str(row["signal_close_at"])) - width)
        if position is None:
            continue
        value = counterfactual_r(frame, position, float(row["stop_pct"]), float(row["target_pct"]))
        if value is None or not math.isfinite(value):
            continue
        store.resolve_advice(int(row["id"]), value, moment.isoformat())
        resolved += 1
    return resolved


def calibration(rows: Iterable[dict[str, Any]], *, min_veto_confidence: float, now: datetime) -> list[dict[str, Any]]:
    """Per (model, prompt version): every consulted row as the base, then each verdict, then the
    vetoes at or above ``min_veto_confidence`` (what ``filter`` would remove). Each line has n,
    the mean counterfactual R and its standard error over the rows whose window has passed;
    ``reduce`` adds the mean of ``(1 − factor) × R``, the R the smaller size gave up or saved.
    ``realized_*`` is the R of the closed positions the rows entered, never mixed in."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
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


def _summary(rows: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    matured = [float(row["counterfactual_r"]) for row in rows if _matured(row, now)]
    realized = [float(row["realized_r"]) for row in rows if row.get("realized_r") is not None]
    return {
        "n": len(matured),
        "mean_r": _mean(matured),
        "se": statistics.stdev(matured) / math.sqrt(len(matured)) if len(matured) >= 2 else None,
        "waiting": len(rows) - len(matured),
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


def _locate(frame: pd.DataFrame, opened: pd.Timestamp) -> int | None:
    """The position of the bar that opened at ``opened`` in ``frame``, or None."""
    index = frame.index
    if getattr(index, "tz", None) is None and opened.tzinfo is not None:
        opened = opened.tz_convert(None)
    elif getattr(index, "tz", None) is not None and opened.tzinfo is None:
        opened = opened.tz_localize("UTC")
    found = int(index.get_indexer([opened])[0])
    return found if found >= 0 else None
