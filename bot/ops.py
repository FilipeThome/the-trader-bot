"""Operator recovery for the execution journal. ``python -m bot.ops <command>``.

A crash in the middle of a submission, an unknown cancel, or a contradiction
between the journal and Binance blocks every new order, stops and exits
included, until it is settled. The API and the panel never settle those
states: only an operator at a shell does, with exchange evidence.

Commands (``--mode`` defaults to the mode in ``bot.json``):

* ``status``: blocking journal keys, the halt flag and the journal error, if any.
* ``reconcile``: ask Binance about every blocking key and record what it reports.
* ``confirm-absent STATE_KEY --reason TEXT``: mark a stuck ``SUBMISSION_PENDING``
  absent. Run it only after the process that submitted it is gone; Binance must
  still confirm that the order does not exist.
* ``ack-conflict STATE_KEY --reason TEXT``: record that an ``EXECUTION_CONFLICT``
  was investigated.
* ``adopt-checkpoint --reason TEXT``: re-anchor the journal checkpoint after a
  restore or a migration. Refused while the checkpoint anchors lost records.

Every command prints one JSON object and exits 1 when the result is an error.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from bot.config import BotConfig
from bot.paths import pin_mode_root
from bot.runtime import mode_root, profile_for


def _context(mode: str | None) -> tuple[Any, Any]:
    from bot.exchange import sdk
    from bot.exchange.profiles import profile_by_id

    config = BotConfig.load()
    if mode is not None:
        config = config.model_copy(update={"mode": mode})
    pin_mode_root(mode_root(config))
    profile = profile_by_id(profile_for(config))
    return profile, sdk.build_config(dict(profile.config))


def run(argv: list[str]) -> tuple[int, dict[str, Any]]:
    parser = argparse.ArgumentParser(prog="python -m bot.ops", description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("testnet", "production"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("reconcile")
    for name in ("confirm-absent", "ack-conflict"):
        command = sub.add_parser(name)
        command.add_argument("state_key")
        command.add_argument("--reason", required=True)
    adopt = sub.add_parser("adopt-checkpoint")
    adopt.add_argument("--reason", required=True)
    args = parser.parse_args(argv)

    from bot.exchange import service
    from bot.exchange.execution import journal, reconcile
    from bot.live.halt import halt_flag_set, read_halt

    profile, config = _context(args.mode)
    result: dict[str, Any]
    if args.command == "status":
        result = {"status": "ok", "journal": service.journal_status(), "halted": halt_flag_set("binance"), "halt": read_halt()}
    elif args.command == "reconcile":
        result = reconcile.reconcile_journal(profile, config)
    elif args.command == "confirm-absent":
        result = reconcile.confirm_submission_absent(profile, config, args.state_key, reason=args.reason)
    elif args.command == "ack-conflict":
        result = reconcile.acknowledge_conflict(args.state_key, reason=args.reason)
    else:
        try:
            result = {"status": "ok", **journal.adopt_journal_checkpoint(reason=args.reason)}
        except (ValueError, journal.PersistError) as exc:
            result = {"status": "error", "error": str(exc)}
    return (0 if result.get("status") == "ok" else 1), result


def main() -> None:
    code, result = run(sys.argv[1:])
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
