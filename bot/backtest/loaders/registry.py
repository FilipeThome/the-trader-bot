"""Tiny loader registry. Crypto resolves to the public Binance loader."""

from __future__ import annotations

from bot.backtest.loaders.base import DataLoader, NoAvailableSourceError

_LOADERS: dict[str, DataLoader] = {}


def register(cls: type[DataLoader]) -> type[DataLoader]:
    _LOADERS[cls.name] = cls()
    return cls


def resolve_loader(market: str) -> DataLoader:
    # Import for side effect so the Binance loader registers itself.
    from bot.backtest.loaders import binance_loader as _binance_loader  # noqa: F401

    for loader in _LOADERS.values():
        if market in loader.markets:
            return loader
    raise NoAvailableSourceError(f"no loader for {market}")
