"""Spot symbols Binance currently allows to trade, and which of them to analyse."""

from __future__ import annotations

import logging
import math
import statistics
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

from bot.collector import SPOT, Collector
from bot.config import MAX_SYMBOLS, is_usdt_pair
from bot.exchange.weight import note_weight

logger = logging.getLogger(__name__)

_QUOTE_RANK = {"USDT": 0, "USDC": 1, "FDUSD": 2, "BTC": 3, "ETH": 4, "BNB": 5}
#: Dollar-pegged or fiat bases: a USDT pair of these is not a trade.
_DOLLAR_BASES = frozenset({"USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "PYUSD", "USDE", "USD1", "EUR", "EURI", "AEUR", "XUSD"})
_LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
_MAX_AGE_SECONDS = 3600
#: The Spot Testnet lists its own subset of pairs; testnet orders only reach those.
TESTNET_SPOT = "https://testnet.binance.vision"
#: Trades a testnet pair needs in 24h, about one every 6 seconds. The order gate
#: refuses a ticker older than 15 seconds, and the ticker time is the last trade,
#: so a thinner testnet book leaves stops and exits refused as ``quote_stale``.
#: Measured 2026-10-16: BTC 73k, ETH 29k, SOL 18k; the next pair (DOGE) had 8.5k.
TESTNET_MIN_TRADES = 15_000
#: Liquid pairs ranked by momentum, and the days of the momentum return.
RANK_POOL = 40
MOMENTUM_DAYS = 28
#: A pair listed less than ``MOMENTUM_DAYS`` ago is ranked on its day instead: the
#: last ``YOUNG_HOURS`` closed 1h candles, and it needs at least ``YOUNG_MIN_HOURS``.
YOUNG_HOURS = 24
YOUNG_MIN_HOURS = 12


def parse_exchange_info(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Keep spot symbols whose status is TRADING and that Binance allows to trade."""
    rows: list[dict[str, str]] = []
    for item in payload.get("symbols") or []:
        if item.get("status") != "TRADING" or item.get("isSpotTradingAllowed") is False:
            continue
        base = str(item.get("baseAsset") or "")
        quote = str(item.get("quoteAsset") or "")
        if not base or not quote:
            continue
        rows.append({"symbol": f"{base}/{quote}", "base": base, "quote": quote})
    rows.sort(key=lambda row: (_QUOTE_RANK.get(row["quote"], 9), row["symbol"]))
    return rows


def tradable_symbols(collector: Collector) -> list[dict[str, str]]:
    """Return the cached catalog, refreshing from exchangeInfo at most once an hour."""
    now = datetime.now(timezone.utc)
    cached = collector.market_cache
    if cached is not None and (now - cached[0]).total_seconds() < _MAX_AGE_SECONDS:
        return cached[1]
    payload = _read(collector, f"{SPOT}/api/v3/exchangeInfo")
    rows = parse_exchange_info(payload)
    collector.market_cache = (now, rows)
    return rows


def venue_symbols(collector: Collector, mode: str) -> frozenset[str] | None:
    """Pairs the order venue of ``mode`` can trade, or ``None`` when it is the live catalog.

    Analysis reads the live market, but Spot Testnet orders only reach the pairs
    the testnet lists, and only an active testnet book (``TESTNET_MIN_TRADES``)
    keeps quotes fresh enough for the stop and the exit. In testnet every chosen
    pair must be one of those. The testnet has its own weight limit, so these
    reads do not count against the live budget.
    """
    if mode != "testnet":
        return None
    now = datetime.now(timezone.utc)
    cached = collector.testnet_cache
    if cached is not None and (now - cached[0]).total_seconds() < _MAX_AGE_SECONDS:
        return cached[1]
    payload, _headers = collector._fetch(f"{TESTNET_SPOT}/api/v3/exchangeInfo")
    pairs = {row["base"] + row["quote"]: row["symbol"] for row in parse_exchange_info(payload)}
    tickers, _headers = collector._fetch(f"{TESTNET_SPOT}/api/v3/ticker/24hr")
    active: set[str] = set()
    for item in tickers if isinstance(tickers, list) else []:
        pair = pairs.get(str(item.get("symbol") or ""))
        try:
            busy = int(item.get("count") or 0) >= TESTNET_MIN_TRADES
        except (TypeError, ValueError):
            busy = False
        if pair is not None and busy:
            active.add(pair)
    if not active:
        raise ValueError("the Spot Testnet has no active pair")
    listed = frozenset(active)
    collector.testnet_cache = (now, listed)
    return listed


#: Candle sizes a trade chart may use, smallest that keeps the span within one request.
_CHART_INTERVALS = (("1m", 1), ("5m", 5), ("15m", 15), ("1h", 60), ("4h", 240))
MAX_CHART_CANDLES = 1000


def chart_interval(start: datetime, end: datetime) -> tuple[str, int]:
    """The smallest interval that shows ``start``..``end`` in at most 1000 candles, and the count."""
    minutes = max(1.0, (end - start).total_seconds() / 60)
    for name, size in _CHART_INTERVALS:
        count = math.ceil(minutes / size) + 1
        if count <= MAX_CHART_CANDLES:
            return name, count
    return _CHART_INTERVALS[-1][0], MAX_CHART_CANDLES


def venue_klines(collector: Collector, symbol: str, mode: str, *, start: datetime, end: datetime) -> tuple[str, list[dict[str, Any]]]:
    """Candles of ``symbol`` from the order venue of ``mode`` between ``start`` and ``end``.

    A testnet trade fills on the testnet book, so its chart reads
    ``testnet.binance.vision`` (its own weight limit, not noted here); a
    production trade reads the live market and notes its weight.
    """
    interval, count = chart_interval(start, end)
    query = urllib.parse.urlencode(
        {
            "symbol": symbol.replace("/", ""),
            "interval": interval,
            "startTime": int(start.timestamp() * 1000),
            "endTime": int(end.timestamp() * 1000),
            "limit": count,
        }
    )
    if mode == "testnet":
        payload, _headers = collector._fetch(f"{TESTNET_SPOT}/api/v3/klines?{query}")
    else:
        payload = _read(collector, f"{SPOT}/api/v3/klines?{query}")
    candles = [
        {
            "t": datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc).isoformat(),
            "o": float(row[1]),
            "h": float(row[2]),
            "l": float(row[3]),
            "c": float(row[4]),
        }
        for row in payload if isinstance(payload, list)
    ]
    return interval, candles


#: Padding before the entry so the chart shows how the price came in.
CHART_LEAD = timedelta(minutes=30)


def top_liquid_usdt(collector: Collector, count: int, *, listed: frozenset[str] | None = None) -> list[str]:
    """The ``count`` USDT spot pairs with the largest 24h quote volume.

    Stablecoin and fiat bases are left out, and so are leveraged tokens
    (``BTCUP``, ``ETHDOWN``: a suffix on a base that is itself listed).
    With ``listed``, only those pairs count (see ``venue_symbols``).
    """
    catalog = tradable_symbols(collector)
    bases = {row["base"] for row in catalog}
    usdt: dict[str, str] = {}
    for row in catalog:
        base = row["base"]
        if row["quote"] != "USDT" or base in _DOLLAR_BASES:
            continue
        if listed is not None and row["symbol"] not in listed:
            continue
        if any(base.endswith(suffix) and base[: -len(suffix)] in bases for suffix in _LEVERAGED_SUFFIXES):
            continue
        usdt[f"{base}USDT"] = row["symbol"]
    payload = _read(collector, f"{SPOT}/api/v3/ticker/24hr")
    ranked: list[tuple[float, str]] = []
    for item in payload if isinstance(payload, list) else []:
        pair = usdt.get(str(item.get("symbol") or ""))
        if pair is None:
            continue
        try:
            ranked.append((float(item.get("quoteVolume") or 0), pair))
        except (TypeError, ValueError):
            continue
    ranked.sort(reverse=True)
    return [pair for _volume, pair in ranked[:count]]


def rank_promising(
    collector: Collector, count: int, *, pool: int = RANK_POOL, listed: frozenset[str] | None = None
) -> list[str]:
    """The ``count`` liquid USDT pairs with the best risk-adjusted momentum.

    The score is the 28-day return over the standard deviation of the daily
    returns, from closed daily candles of the ``pool`` most liquid pairs. A pair
    too young for 28 days is scored the same way on its day (``_day_score``). Only
    pairs with a positive score are returned, best first. ``listed`` limits the
    pool to the pairs the order venue lists. Momentum here only
    picks which pairs the strategies look at; it never buys by itself.
    """
    candidates = [pair for pair in top_liquid_usdt(collector, pool, listed=listed) if is_usdt_pair(pair)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        scores = list(executor.map(lambda pair: _momentum_score(collector, pair), candidates))
    ranked = sorted(
        ((score, pair) for score, pair in zip(scores, candidates) if score is not None and score > 0),
        reverse=True,
    )
    return [pair for _score, pair in ranked[:count]]


def merge_symbols(live: list[str], chosen: list[str]) -> list[str]:
    """Pairs with a live position first, so they always stay, then the chosen ones."""
    return list(dict.fromkeys([*live, *chosen]))[:MAX_SYMBOLS]


def _momentum_score(collector: Collector, pair: str) -> float | None:
    query = urllib.parse.urlencode({"symbol": pair.replace("/", ""), "interval": "1d", "limit": MOMENTUM_DAYS + 2})
    try:
        payload = _read(collector, f"{SPOT}/api/v3/klines?{query}")
        # The last candle is today's and still open.
        closes = [float(row[4]) for row in payload[:-1]]
    except Exception as exc:  # noqa: BLE001 - one unreadable pair leaves the ranking, not the cycle
        logger.warning("daily candles of %s failed: %s", pair, type(exc).__name__)
        return None
    if len(closes) < MOMENTUM_DAYS + 1:
        # Listed less than a month ago: its day is the history there is.
        return _day_score(collector, pair)
    return _risk_adjusted(closes[-(MOMENTUM_DAYS + 1) :])


def _day_score(collector: Collector, pair: str) -> float | None:
    """The young pair's last 24h: return over the deviation of its closed 1h returns."""
    query = urllib.parse.urlencode({"symbol": pair.replace("/", ""), "interval": "1h", "limit": YOUNG_HOURS + 1})
    try:
        payload = _read(collector, f"{SPOT}/api/v3/klines?{query}")
        closes = [float(row[4]) for row in payload[:-1]]
    except Exception as exc:  # noqa: BLE001 - one unreadable pair leaves the ranking, not the cycle
        logger.warning("hourly candles of %s failed: %s", pair, type(exc).__name__)
        return None
    if len(closes) < YOUNG_MIN_HOURS:
        return None
    return _risk_adjusted(closes[-YOUNG_HOURS:])


def _risk_adjusted(window: list[float]) -> float | None:
    """The window's return over the population deviation of its bar returns."""
    if len(window) < 2 or min(window) <= 0:
        return None
    returns = [later / earlier - 1 for earlier, later in zip(window, window[1:])]
    deviation = statistics.pstdev(returns)
    if deviation <= 0:
        return None
    return (window[-1] / window[0] - 1) / deviation


def _read(collector: Collector, url: str) -> Any:
    payload, headers = collector._fetch(url)
    raw = headers.get("X-MBX-USED-WEIGHT-1M") or headers.get("x-mbx-used-weight-1m")
    if raw is not None and str(raw).isdigit():
        note_weight(int(raw))
    return payload
