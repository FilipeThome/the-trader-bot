"""Tamper-evident ledger ported from Vibe-Trading."""

from bot.governance.ledger import (
    EXPORT_FORMAT,
    GENESIS_PREV_HASH,
    ChainBreak,
    ChainVerificationResult,
    LedgerCorruptionError,
    append_record,
    build_export,
    compute_record_hash,
    export_chain_to_file,
    verify_chain,
    verify_export,
)

__all__ = [
    "EXPORT_FORMAT",
    "GENESIS_PREV_HASH",
    "ChainBreak",
    "ChainVerificationResult",
    "LedgerCorruptionError",
    "append_record",
    "build_export",
    "compute_record_hash",
    "export_chain_to_file",
    "verify_chain",
    "verify_export",
]
