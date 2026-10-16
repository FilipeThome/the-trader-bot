"""Loader errors the ported enforcement gate already understands."""

from __future__ import annotations

from typing import Callable

import pandas as pd


class NoAvailableSourceError(LookupError):
    """No public source could satisfy the request."""


class DataLoader:
    name = "base"
    markets: set[str] = set()

    def fetch(
        self,
        symbols: list[str],
        start: str,
        end: str,
        interval: str = "1d",
        *,
        on_symbol: Callable[[str, int], None] | None = None,
    ) -> dict[str, pd.DataFrame]:
        raise NotImplementedError
