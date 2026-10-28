"""Market data over public REST: OHLCV, top of book, funding and open interest.

A series older than two intervals is stale and the pair is skipped. Clock skew
above one second blocks new entries. A pair without a USD-M perpetual has no
funding or open interest; that is not an error.
"""

from __future__ import annotations

import json
import threading
import time
from functools import partial
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx
import pandas as pd

from bot.exchange.weight import Venue, note_rate_limited, note_weight

INTERVALS = {"1m": 60, "15m": 900, "1h": 3600, "4h": 14400, "8h": 28800}
SPOT = "https://api.binance.com"
FAPI = "https://fapi.binance.com"
Fetcher = Callable[[str], tuple[Any, dict[str, str]]]
#: Parallel REST requests per cycle. Requests, not symbols, are the unit: each
#: symbol is three klines plus funding and open interest, and a symbol's reads
#: no longer wait on each other (about 270 ms each from a distant region).
_WORKERS = 16
#: Intervals the strategies and the price/exit checks read.
CYCLE_INTERVALS = ("15m", "1h", "4h")


@dataclass
class SymbolData:
    """One symbol's reads, made in a worker and merged by the cycle thread."""

    candles: dict[str, "Series"]
    book: "BookTop"
    perpetual: bool
    funding: tuple[list[float], list[dict[str, float | str]]] | None
    open_interest: float | None


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
    #: Symbols whose read failed in the refresh that produced this view; their data is old.
    failed: frozenset[str] = frozenset()

    def stale(self, symbol: str, interval: str, *, now: datetime | None = None) -> bool:
        series = self.candles.get((symbol, interval))
        if series is None or series.frame.empty:
            return True
        moment = now or datetime.now(timezone.utc)
        last = series.frame.index[-1]
        if getattr(last, "tzinfo", None) is None:
            last = last.tz_localize(timezone.utc)
        lag = moment - last.to_pydatetime()
        return bool(lag > timedelta(seconds=INTERVALS[interval] * 2))

    def slippage(self, symbol: str, notional: float) -> float:
        book = self.books.get(symbol)
        if book is None or book.bid <= 0 or book.ask <= 0:
            return 0.0005
        spread = (book.ask - book.bid) / book.bid
        depth = book.bid * book.bid_qty + book.ask * book.ask_qty
        impact = (notional / depth) if depth > 0 else 0.001
        return min(0.02, spread + impact)


class Collector:
    """Market reads. ``lock`` owns ``snapshot`` and ``without_perpetual``.

    Network reads run outside the lock; only the merge into the snapshot takes
    it, so the panel's chart reads and the cycle never block each other on
    Binance. The cycle works on a copy (``view``), never on the live snapshot.
    """

    def __init__(self, fetcher: Fetcher | None = None) -> None:
        self._fetch = fetcher or _http_fetch
        self.lock = threading.RLock()
        self.snapshot = MarketSnapshot()
        #: Pairs Binance answered 400 for on the USD-M API: no perpetual.
        self.without_perpetual: set[str] = set()
        self.market_cache: tuple[datetime, list[dict[str, str]]] | None = None
        #: Pairs the Spot Testnet lists, read at most once an hour.
        self.testnet_cache: tuple[datetime, frozenset[str]] | None = None
        self.recommendation_cache: tuple[datetime, list[dict[str, Any]]] | None = None
        #: The liquid pool the last automatic ranking read (``markets.rank_promising``).
        self.universe_cache: list[str] | None = None

    def sync_clock(self) -> bool:
        """Block writes when local time and Binance time differ by more than one second."""
        sent = time.time()
        payload, _headers = self._fetch(f"{SPOT}/api/v3/time")
        received = time.time()
        server_ms = float(payload["serverTime"])
        # Binance stamps the answer mid-flight: compare it with the middle of the round
        # trip, or a 300 ms request alone eats a third of the tolerance.
        skew = abs((sent + received) / 2 - server_ms / 1000)
        self.snapshot.clock_blocked = skew > 1
        return not self.snapshot.clock_blocked

    def refresh(self, symbols: list[str], intervals: tuple[str, ...] = CYCLE_INTERVALS) -> MarketSnapshot:
        """Read every symbol in parallel, then merge under ``lock`` and return a copy.

        Workers only read; the snapshot and ``without_perpetual`` are written
        in the merge, so a reader never sees a half-merged symbol. Every symbol
        that succeeded is merged. Symbols that failed are named in the view's
        ``failed``, so one bad pair does not stop the others; when every symbol
        fails, the first error is raised and the cycle counts it.
        """
        with self.lock:
            known_without = set(self.without_perpetual)
        books = self._get_books(symbols)
        reads: list[tuple[str, str, Callable[[], Any]]] = []
        for symbol in symbols:
            for interval in intervals:
                reads.append((symbol, interval, partial(self._get_klines, symbol, interval)))
            if books is None:
                reads.append((symbol, "book", partial(self._get_book, symbol)))
            if symbol not in known_without:
                # Open interest is read alongside funding; it is dropped when funding says
                # the pair has no perpetual.
                reads.append((symbol, "funding", partial(self._get_funding, symbol)))
                reads.append((symbol, "oi", partial(self._get_open_interest, symbol)))
        results: dict[str, dict[str, Any]] = {symbol: {} for symbol in symbols}
        errors: dict[str, BaseException] = {}
        with ThreadPoolExecutor(max_workers=max(1, min(_WORKERS, len(reads))), thread_name_prefix="collect") as pool:
            futures = [(symbol, kind, pool.submit(read)) for symbol, kind, read in reads]
            for symbol, kind, future in futures:
                value, error = _outcome(future)
                if error is not None:
                    errors.setdefault(symbol, error)
                else:
                    results[symbol][kind] = value
        first_error: BaseException | None = None
        failed: set[str] = set()
        with self.lock:
            for symbol in symbols:
                if symbol in errors:
                    first_error = first_error or errors[symbol]
                    failed.add(symbol)
                    continue
                read = results[symbol]
                book = books[symbol] if books is not None else read["book"]
                funding = read.get("funding") if symbol not in known_without else None
                perpetual = symbol not in known_without and funding is not None
                data = SymbolData(
                    {interval: read[interval] for interval in intervals},
                    book,
                    perpetual,
                    funding,
                    read.get("oi") if perpetual else None,
                )
                self._merge(symbol, data)
            # Copied in the same lock block: the view is exactly this merge.
            view = self.view()
        if first_error is not None and len(failed) == len(symbols):
            raise first_error
        view.failed = frozenset(failed)
        return view

    def view(self) -> MarketSnapshot:
        """A copy of the snapshot's maps: safe to iterate while the panel fills the live one."""
        with self.lock:
            current = self.snapshot
            return MarketSnapshot(
                candles=dict(current.candles),
                books=dict(current.books),
                funding=dict(current.funding),
                funding_points=dict(current.funding_points),
                open_interest=dict(current.open_interest),
                clock_blocked=current.clock_blocked,
            )

    def _get_books(self, symbols: list[str]) -> dict[str, BookTop] | None:
        """Every symbol's best bid and ask in one request (weight 4 instead of 2 each).

        None sends the caller to one request per symbol: Binance rejects the
        whole batch when one symbol is invalid, and a failed batch must not
        cost every symbol of the cycle.
        """
        if not symbols:
            return {}
        pairs = {symbol.replace("/", ""): symbol for symbol in symbols}
        query = urllib.parse.urlencode({"symbols": json.dumps(list(pairs), separators=(",", ":"))})
        try:
            payload, headers = self._fetch(f"{SPOT}/api/v3/ticker/bookTicker?{query}")
        except Exception:  # noqa: BLE001 - the per-symbol reads that follow report their own errors
            return None
        _absorb_weight(headers)
        if not isinstance(payload, list):
            return None
        now = datetime.now(timezone.utc)
        books: dict[str, BookTop] = {}
        for item in payload:
            symbol = pairs.get(str(item.get("symbol") or ""))
            if symbol is None:
                continue
            books[symbol] = BookTop(
                bid=float(item["bidPrice"]),
                ask=float(item["askPrice"]),
                bid_qty=float(item["bidQty"]),
                ask_qty=float(item["askQty"]),
                fetched_at=now,
            )
        return books if len(books) == len(pairs) else None

    def _merge(self, symbol: str, data: SymbolData) -> None:
        for interval, series in data.candles.items():
            self.snapshot.candles[(symbol, interval)] = series
        self.snapshot.books[symbol] = data.book
        if not data.perpetual:
            self._mark_without_perpetual(symbol)
            return
        if data.funding is not None:
            self.snapshot.funding[symbol], self.snapshot.funding_points[symbol] = data.funding
        if data.open_interest is not None:
            self.snapshot.open_interest[symbol] = data.open_interest

    def _mark_without_perpetual(self, symbol: str) -> None:
        with self.lock:
            self.without_perpetual.add(symbol)
            self.snapshot.funding[symbol] = []
            self.snapshot.funding_points[symbol] = []
            self.snapshot.open_interest.pop(symbol, None)

    def _load_klines(self, symbol: str, interval: str) -> None:
        series = self._get_klines(symbol, interval)
        with self.lock:
            self.snapshot.candles[(symbol, interval)] = series

    def _load_funding(self, symbol: str) -> None:
        funding = self._get_funding(symbol)
        if funding is None:
            self._mark_without_perpetual(symbol)
            return
        with self.lock:
            self.snapshot.funding[symbol], self.snapshot.funding_points[symbol] = funding

    def _get_klines(self, symbol: str, interval: str) -> Series:
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
        return Series(frame, datetime.now(timezone.utc), "rest")

    def _get_book(self, symbol: str) -> BookTop:
        pair = symbol.replace("/", "")
        query = urllib.parse.urlencode({"symbol": pair})
        payload, headers = self._fetch(f"{SPOT}/api/v3/ticker/bookTicker?{query}")
        _absorb_weight(headers)
        return BookTop(
            bid=float(payload["bidPrice"]),
            ask=float(payload["askPrice"]),
            bid_qty=float(payload["bidQty"]),
            ask_qty=float(payload["askQty"]),
            fetched_at=datetime.now(timezone.utc),
        )

    def _get_funding(self, symbol: str) -> tuple[list[float], list[dict[str, float | str]]] | None:
        """Annualized funding history, or None when the pair has no USD-M perpetual."""
        pair = symbol.replace("/", "")
        query = urllib.parse.urlencode({"symbol": pair, "limit": 30})
        fetched = self._fetch_perpetual(f"{FAPI}/fapi/v1/fundingRate?{query}")
        if fetched is None:
            return None
        payload, headers = fetched
        _absorb_weight(headers, venue="usdm")
        rates: list[float] = []
        points: list[dict[str, float | str]] = []
        for item in payload:
            # Binance funding is an 8h rate. Annualize with three periods a day.
            annual = float(item["fundingRate"]) * 3 * 365
            rates.append(annual)
            if "fundingTime" in item:
                stamp = datetime.fromtimestamp(int(item["fundingTime"]) / 1000, tz=timezone.utc)
                points.append({"t": stamp.isoformat(), "v": annual})
        return rates, points

    def _get_open_interest(self, symbol: str) -> float | None:
        pair = symbol.replace("/", "")
        query = urllib.parse.urlencode({"symbol": pair})
        fetched = self._fetch_perpetual(f"{FAPI}/fapi/v1/openInterest?{query}")
        if fetched is None:
            return None
        payload, headers = fetched
        _absorb_weight(headers, venue="usdm")
        return float(payload.get("openInterest") or 0)

    def _fetch_perpetual(self, url: str) -> tuple[Any, dict[str, str]] | None:
        """USD-M read. HTTP 400 means Binance lists no perpetual for the pair."""
        try:
            return self._fetch(url)
        except urllib.error.HTTPError as exc:
            if exc.code != 400:
                raise
        return None


def _outcome(future: Any) -> tuple[Any, BaseException | None]:
    try:
        return future.result(), None
    except Exception as exc:  # noqa: BLE001 - re-raised by refresh after the merge
        return None, exc


def _absorb_weight(headers: dict[str, str], *, venue: Venue = "spot") -> None:
    raw = headers.get("X-MBX-USED-WEIGHT-1M") or headers.get("x-mbx-used-weight-1m")
    if raw is not None and str(raw).isdigit():
        note_weight(int(raw), venue=venue)


_CLIENT: httpx.Client | None = None
_CLIENT_LOCK = threading.Lock()


def _client() -> httpx.Client:
    """One pooled HTTP client for the process: keep-alive connections instead of a TLS handshake per read."""
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _CLIENT = httpx.Client(
                timeout=15.0,
                headers={"User-Agent": "the-trader-bot"},
                limits=httpx.Limits(max_connections=_WORKERS * 2, max_keepalive_connections=_WORKERS * 2),
            )
        return _CLIENT


def _http_fetch(url: str) -> tuple[Any, dict[str, str]]:
    """GET a Binance JSON endpoint. An HTTP error status raises ``urllib.error.HTTPError`` with its code,
    the contract the callers (and ``_fetch_perpetual``'s 400 = no perpetual) rely on."""
    response = _client().get(url)
    if response.status_code >= 400:
        venue: Venue = "usdm" if "fapi." in url else "spot"
        _absorb_weight(dict(response.headers), venue=venue)
        if response.status_code in (418, 429):
            # Rate limited or banned: hold every reader back for the rest of the minute.
            note_rate_limited(venue=venue)
        raise urllib.error.HTTPError(url, response.status_code, response.reason_phrase, None, None)  # type: ignore[arg-type]
    return response.json(), dict(response.headers)
