# Based on Vibe-Trading d7f1b9e22b42c10197497a8e2cac4f0e2cc9ca1e and changed in this
# repository. ORIGIN.md lists what changed.
"""Built-in Binance connector profiles.

Read-only paper (testnet) and live profiles plus an order-placing paper
(Spot Testnet) profile. Paper and live use different key pairs and different
hosts, so they are distinct profiles.

Binance Production is read-only. ``binance-live-trade`` is kept only so saved
connections that name it still resolve; it has read capabilities only, and the
service, the execution package and the SDK adapter each refuse any write that
is not on the Spot Testnet, whatever this table says.
"""

from __future__ import annotations

from bot.exchange.types import READ_CAPABILITIES, TradingProfile

BINANCE_PROFILES: tuple[TradingProfile, ...] = (
    TradingProfile(
        id="binance-paper-sdk",
        connector="binance",
        label="Binance Spot Testnet · ccxt",
        environment="paper",
        transport="broker_sdk",
        capabilities=READ_CAPABILITIES,
        readonly=True,
        config={"profile": "paper"},
        notes=(
            "Reads a Binance spot testnet account (testnet.binance.vision) via ccxt. "
            "Testnet keys cannot reach the live host."
        ),
    ),
    TradingProfile(
        id="binance-live-sdk-readonly",
        connector="binance",
        label="Binance Live · ccxt Read-Only",
        environment="live",
        transport="broker_sdk",
        capabilities=READ_CAPABILITIES,
        readonly=True,
        config={"profile": "live-readonly"},
        notes=(
            "Reads a Binance spot live account (api.binance.com). Account and "
            "position reads may explicitly select USD-M Shadow Account mode "
            "(fapi.binance.com); that mode exposes no order or market-data surface."
        ),
    ),
    TradingProfile(
        id="binance-paper-trade",
        connector="binance",
        label="Binance Spot Testnet · ccxt Trading",
        environment="paper",
        transport="broker_sdk",
        capabilities=READ_CAPABILITIES + ("orders.place",),
        readonly=False,
        config={"profile": "paper"},
        notes=(
            "Places and cancels orders on a Binance spot testnet account "
            "(testnet.binance.vision) via ccxt. Testnet keys cannot reach the live host, "
            "so no order from this profile can touch real funds."
        ),
    ),
    TradingProfile(
        id="binance-live-trade",
        connector="binance",
        label="Binance Spot Live · ccxt (writes disabled)",
        environment="live",
        transport="broker_sdk",
        capabilities=READ_CAPABILITIES,
        readonly=True,
        config={"profile": "live"},
        notes=(
            "Binance Production is read-only: this profile cannot place, modify or "
            "cancel orders on api.binance.com. Kept so existing connections resolve."
        ),
    ),
    TradingProfile(
        id="binance-live-trade-bot",
        connector="binance",
        label="Binance Spot Live · bot",
        environment="live",
        transport="broker_sdk",
        capabilities=READ_CAPABILITIES + ("orders.place", "orders.cancel"),
        readonly=False,
        config={"profile": "live-trade-bot"},
        notes=(
            "Production writer for this bot only. Writes are refused unless the "
            "exchange-write latch is open and a valid Binance mandate is loaded. "
            "Every other live profile stays read-only."
        ),
    ),
)


def profile_by_id(profile_id: str | None) -> TradingProfile:
    """Return one built-in Binance profile. Unknown ids fail closed."""
    key = str(profile_id or "").strip()
    for profile in BINANCE_PROFILES:
        if profile.id == key:
            return profile
    raise KeyError(key)
