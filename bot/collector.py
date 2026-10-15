"""Market data: public OHLCV, top of book, funding and open interest.

A silent websocket falls back to REST. A series older than two intervals is stale
and the pair is skipped. Clock skew above one second blocks new writes.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pandas as pd

from bot.exchange.weight import note_weight

INTERVALS = {"1m": 60, "15m": 900, "1h": 3600, "4h": 14400, "8h": 28800}
SPOT = "https://api.binance.com"
FAPI = "https://fapi.binance.com"
Fetcher = Callable[[str], tuple[Any, dict[str, str]]]


@dataclass
class Series:
    frame: pd.DataFrame
    fetched_at: datetime
    source: str


@dataclass
class BookTop:
    bid: float
    ask: float
    bid_qty: float
    ask_qty: float
    fetched_at: datetime


@dataclass
class MarketSnapshot:
    candles: dict[tuple[str, str], Series] = field(default_factory=dict)
    books: dict[str, BookTop] = field(default_factory=dict)
    funding: dict[str, list[float]] = field(default_factory=dict)
    funding_points: dict[str, list[dict[str, float | str]]] = field(default_factory=dict)
    open_interest: dict[str, float] = field(default_factory=dict)
    clock_blocked: bool = False
    stream_alive: bool = False
    last_stream_message: datetime | None = None

    def stale(self, symbol: str, interval: str, *, now: datetime | None = None) -> bool:
        series = self.candles.get((symbol, interval))
        if series is None or series.frame.empty:
            return True
        moment = now or datetime.now(timezone.utc)
        last = series.frame.index[-1]
        if getattr(last, "tzinfo", None) is None:
            last = last.tz_localize(timezone.utc)
        lag = moment - last.to_pydatetime()
        return lag > timedelta(seconds=INTERVALS[interval] * 2)

    def slippage(self, symbol: str, notional: float) -> float:
        book = self.books.get(symbol)
        if book is None or book.bid <= 0 or book.ask <= 0:
            return 0.0005
        spread = (book.ask - book.bid) / book.bid
        depth = book.bid * book.bid_qty + book.ask * book.ask_qty
        impact = (notional / depth) if depth > 0 else 0.001
        return min(0.02, spread + impact)


class Collector:
    def __init__(self, fetcher: Fetcher | None = None) -> None:
        self._fetch = fetcher or _http_fetch
        self.snapshot = MarketSnapshot()
        self._rest_fallback = False

    @property
    def using_rest_fallback(self) -> bool:
        return self._rest_fallback or not self.snapshot.stream_alive

    def note_stream_message(self, moment: datetime | None = None) -> None:
        self.snapshot.last_stream_message = moment or datetime.now(timezone.utc)
        self.snapshot.stream_alive = True
        self._rest_fallback = False

    def stream_silent(self, *, now: datetime | None = None, silence_seconds: float = 30) -> bool:
        moment = now or datetime.now(timezone.utc)
        last = self.snapshot.last_stream_message
        if last is None or (moment - last).total_seconds() >= silence_seconds:
            self.snapshot.stream_alive = False
            self._rest_fallback = True
            return True
        return False

    def sync_clock(self) -> bool:
        """Block writes when local time and Binance time differ by more than one second."""
        payload, _headers = self._fetch(f"{SPOT}/api/v3/time")
        server_ms = float(payload["serverTime"])
        skew = abs(time.time() - server_ms / 1000)
        self.snapshot.clock_blocked = skew > 1
        return not self.snapshot.clock_blocked

    def refresh(self, symbols: list[str], intervals: tuple[str, ...] = ("1m", "15m", "1h", "4h")) -> MarketSnapshot:
        if self.stream_silent():
            pass
        for symbol in symbols:
            for interval in intervals:
                if self.snapshot.stream_alive and (symbol, interval) in self.snapshot.candles:
                    continue
                self._load_klines(symbol, interval)
            self._load_book(symbol)
            self._load_funding(symbol)
            self._load_open_interest(symbol)
        return self.snapshot

    def ingest_kline(self, symbol: str, interval: str, row: dict[str, float], *, now: datetime | None = None) -> None:
        """Apply one websocket kline. ``row`` uses open/high/low/close/volume and open_time ms."""
        stamp = datetime.fromtimestamp(row["open_time"] / 1000, tz=timezone.utc)
        frame = pd.DataFrame(
            [{"open": row["open"], "high": row["high"], "low": row["low"], "close": row["close"], "volume": row["volume"]}],
            index=pd.DatetimeIndex([stamp]),
        )
        current = self.snapshot.candles.get((symbol, interval))
        if current is not None and not current.frame.empty:
            frame = pd.concat([current.frame, frame])
            frame = frame[~frame.index.duplicated(keep="last")].sort_index().tail(500)
        self.snapshot.candles[(symbol, interval)] = Series(frame, now or datetime.now(timezone.utc), "websocket")
        self.note_stream_message(now)

    def _load_klines(self, symbol: str, interval: str) -> None:
        pair = symbol.replace("/", "")
        query = urllib.parse.urlencode({"symbol": pair, "interval": interval, "limit": 300})
        payload, headers = self._fetch(f"{SPOT}/api/v3/klines?{query}")
        _absorb_weight(headers)
        rows = []
        index = []
        for item in payload:
            index.append(datetime.fromtimestamp(item[0] / 1000, tz=timezone.utc))
            rows.append(
                {
                    "open": float(item[1]),
                    "high": float(item[2]),
                    "low": float(item[3]),
                    "close": float(item[4]),
                    "volume": float(item[5]),
                }
            )
        frame = pd.DataFrame(rows, index=pd.DatetimeIndex(index))
        self.snapshot.candles[(symbol, interval)] = Series(
            frame, datetime.now(timezone.utc), "rest" if self._rest_fallback else "rest"
        )

    def _load_book(self, symbol: str) -> None:
        pair = symbol.replace("/", "")
        payload, headers = self._fetch(f"{SPOT}/api/v3/ticker/bookTicker?symbol={pair}")
        _absorb_weight(headers)
        self.snapshot.books[symbol] = BookTop(
            bid=float(payload["bidPrice"]),
            ask=float(payload["askPrice"]),
            bid_qty=float(payload["bidQty"]),
            ask_qty=float(payload["askQty"]),
            fetched_at=datetime.now(timezone.utc),
        )

    def _load_funding(self, symbol: str) -> None:
        pair = symbol.replace("/", "")
        payload, headers = self._fetch(f"{FAPI}/fapi/v1/fundingRate?symbol={pair}&limit=30")
        _absorb_weight(headers, limit=1200)
        rates: list[float] = []
        points: list[dict[str, float | str]] = []
        for item in payload:
            # Binance funding is an 8h rate. Annualize with three periods a day.
            annual = float(item["fundingRate"]) * 3 * 365
            rates.append(annual)
            if "fundingTime" in item:
                stamp = datetime.fromtimestamp(int(item["fundingTime"]) / 1000, tz=timezone.utc)
                points.append({"t": stamp.isoformat(), "v": annual})
        self.snapshot.funding[symbol] = rates
        self.snapshot.funding_points[symbol] = points

    def _load_open_interest(self, symbol: str) -> None:
        pair = symbol.replace("/", "")
        payload, headers = self._fetch(f"{FAPI}/fapi/v1/openInterest?symbol={pair}")
        _absorb_weight(headers, limit=1200)
        self.snapshot.open_interest[symbol] = float(payload.get("openInterest") or 0)


def _absorb_weight(headers: dict[str, str], *, limit: int = 6000) -> None:
    raw = headers.get("X-MBX-USED-WEIGHT-1M") or headers.get("x-mbx-used-weight-1m")
    if raw is not None and str(raw).isdigit():
        note_weight(int(raw), limit=limit)


def _http_fetch(url: str) -> tuple[Any, dict[str, str]]:
    request = urllib.request.Request(url, headers={"User-Agent": "the-trader-bot"})
    with urllib.request.urlopen(request, timeout=15) as response:
        headers = {key: value for key, value in response.headers.items()}
        return json.loads(response.read().decode("utf-8")), headers
