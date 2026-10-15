# Ported from Vibe-Trading d7f1b9e22b42c10197497a8e2cac4f0e2cc9ca1e.
# Behavior of every profile other than binance-live-trade-bot is unchanged.
"""Binance trading connector with opt-in read-only USD-M account evidence.

Read-only account/market access uses the ``ccxt`` library's unified ``binance``
client (REST). Spot order placement and cancellation exist on the SDK, but a
read-only profile and ``market_type="usdm"`` are refused before any write.
Tradable spot orders go through ``execution`` (preflight, journal, and, for
live, the mandate gate). The SDK does not treat a model request as permission
to trade.

Paper-vs-live separation is structural: the testnet (paper) and live mainnet use
DIFFERENT API keys and DIFFERENT hosts (``testnet.binance.vision`` vs
``api.binance.com``), so a testnet key physically cannot reach the live host. The
configured ``profile`` selects the host (via ``set_sandbox_mode``) and is the
authoritative discriminator; it is recorded on every payload and never flipped
implicitly. Binance spot has no positions — holdings are non-zero balances, so
``get_positions`` shapes the non-zero balances from ``fetch_balance`` as rows.
"""
