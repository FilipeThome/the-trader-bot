# Ported from Vibe-Trading d7f1b9e22b42c10197497a8e2cac4f0e2cc9ca1e.
# Behavior of every profile other than binance-live-trade-bot is unchanged.
"""Binance ccxt connector with host-separated spot profiles.

The live read-only profile may opt into ``market_type="usdm"`` for strict
Shadow Account evidence. USD-M permits only signed account and position reads
against ``fapi.binance.com``; all order and market-data surfaces are rejected.
"""

from __future__ import annotations

import hmac
import json
import math
import os
import re
import tempfile
import contextvars
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Mapping
from urllib.parse import urlparse
from urllib.request import getproxies

from bot.paths import get_runtime_root
from bot.exchange.shaping import (
    as_iter as _as_iter,
    nonzero_balances as _nonzero_balances,
    normalize_symbol,
    obj_get as _obj_get,
    ohlcv_to_dict as _ohlcv_to_dict,
    order_to_dict as _order_to_dict,
    to_float as _to_float,
    trade_to_dict as _trade_to_dict,
)
from bot.exchange.usdm import (
    DEFAULT_OBSERVATION_ABSOLUTE_TOLERANCE,
    UsdMObservationError,
    assert_exchange_endpoints,
    read_account_observation,
)

CONFIG_FILENAME = "binance.json"

#: Profiles this connector understands and their default account environment.
PROFILE_ENVIRONMENTS = {
    "paper": "paper",
    "live-readonly": "live",
    "live": "live",
    # Bot production writer. Every other live profile stays read-only.
    "live-trade-bot": "live",
}

DEFAULT_TESTNET_HOST = "https://testnet.binance.vision"
TESTNET_HOSTNAME = "testnet.binance.vision"
LIVE_HOST = "https://api.binance.com"
USDM_LIVE_HOST = "https://fapi.binance.com"

#: binance.json fields holding a dedicated Spot Testnet key pair. The
#: top-level ``api_key``/``api_secret`` belong to the file's saved profile.
TESTNET_CREDENTIAL_FIELDS = ("testnet_api_key", "testnet_api_secret")
#: Environment variables holding the Spot Testnet pair (paper profiles only).
TESTNET_CREDENTIAL_ENV = ("BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET")

#: Binance refuses a signed request whose timestamp is older than this when it
#: arrives. Reconciliation's acceptance window assumes it; never above 60000.
RECV_WINDOW_MS = 10_000

#: The only Spot Testnet REST surfaces a client may use. They replace ccxt's
#: sandbox table, so isolation does not depend on that third-party table.
TESTNET_SPOT_API = {
    "public": f"{DEFAULT_TESTNET_HOST}/api/v3",
    "private": f"{DEFAULT_TESTNET_HOST}/api/v3",
    "v1": f"{DEFAULT_TESTNET_HOST}/api/v1",
}


def _official_testnet_host(value: Any) -> str:
    """Return the Spot Testnet host, refusing any other configured value.

    ccxt sandbox mode always talks to ``testnet.binance.vision``; a different
    ``testnet_host`` would only make the reported host and host guards lie.
    """
    host = str(value or DEFAULT_TESTNET_HOST).strip().rstrip("/")
    if host != DEFAULT_TESTNET_HOST:
        raise BinanceConfigError(f"testnet_host must be {DEFAULT_TESTNET_HOST}")
    return DEFAULT_TESTNET_HOST

class BinanceDependencyError(RuntimeError):
    """Raised when the optional ``ccxt`` package is not installed."""


class BinanceConfigError(RuntimeError):
    """Raised when the connector configuration is missing or invalid."""


@dataclass(frozen=True)
class BinanceConfig:
    """Binance connector connection settings.
    """

    api_key: str = ""
    api_secret: str = ""
    profile: str = "paper"
    market_type: str = "spot"
    observation_absolute_tolerance: float = DEFAULT_OBSERVATION_ABSOLUTE_TOLERANCE
    testnet_host: str = DEFAULT_TESTNET_HOST
    timeout: float = 15.0
    readonly: bool = True

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None = None) -> "BinanceConfig":
        """Build a config from a JSON-like mapping, normalizing the profile."""
        payload = dict(data or {})
        profile = str(payload.get("profile") or "paper").strip().lower()
        if profile not in PROFILE_ENVIRONMENTS:
            raise BinanceConfigError(
                "profile must be 'paper', 'live-readonly', 'live' or 'live-trade-bot'"
            )
        market_type = str(payload.get("market_type") or "spot").strip().lower()
        if market_type not in {"spot", "usdm"}:
            raise BinanceConfigError("market_type must be 'spot' or 'usdm'")
        if market_type == "usdm" and profile != "live-readonly":
            raise BinanceConfigError(
                "market_type 'usdm' is available only through the live-readonly profile"
            )
        raw_tolerance = payload.get("observation_absolute_tolerance")
        try:
            tolerance = float(
                raw_tolerance
                if raw_tolerance is not None
                else DEFAULT_OBSERVATION_ABSOLUTE_TOLERANCE
            )
        except (TypeError, ValueError) as exc:
            raise BinanceConfigError(
                "observation_absolute_tolerance must be non-negative and finite"
            ) from exc
        if not math.isfinite(tolerance) or tolerance < 0:
            raise BinanceConfigError(
                "observation_absolute_tolerance must be non-negative and finite"
            )
        return cls(
            api_key=str(payload.get("api_key") or "").strip(),
            api_secret=str(payload.get("api_secret") or "").strip(),
            profile=profile,
            market_type=market_type,
            observation_absolute_tolerance=tolerance,
            testnet_host=_official_testnet_host(payload.get("testnet_host")),
            timeout=float(payload.get("timeout") or 15.0),
            readonly=bool(payload.get("readonly", True)),
        )

    def with_overrides(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        profile: str | None = None,
        market_type: str | None = None,
        observation_absolute_tolerance: float | None = None,
        testnet_host: str | None = None,
    ) -> "BinanceConfig":
        """Return a copy with CLI/tool overrides applied."""
        payload = asdict(self)
        if api_key is not None:
            payload["api_key"] = api_key
        if api_secret is not None:
            payload["api_secret"] = api_secret
        if profile is not None:
            payload["profile"] = profile
        if market_type is not None:
            payload["market_type"] = market_type
        if observation_absolute_tolerance is not None:
            payload["observation_absolute_tolerance"] = observation_absolute_tolerance
        if testnet_host is not None:
            payload["testnet_host"] = testnet_host
        return BinanceConfig.from_mapping(payload)

    @property
    def environment(self) -> str:
        """Return ``paper`` or ``live`` for this profile."""
        return PROFILE_ENVIRONMENTS.get(self.profile, "paper")

    @property
    def is_testnet(self) -> bool:
        """Return whether this profile targets the testnet host/key."""
        return self.environment == "paper"

    @property
    def host(self) -> str:
        """Return the REST host this profile connects to."""
        if self.market_type == "usdm":
            return USDM_LIVE_HOST
        return self.testnet_host if self.is_testnet else LIVE_HOST


_OVERRIDE_KEYS = (
    "api_key", "api_secret", "profile", "market_type",
    "observation_absolute_tolerance", "testnet_host",
)


def build_config(profile_config: Mapping[str, Any] | None = None, overrides: Mapping[str, Any] | None = None) -> "BinanceConfig":
    """Resolve the effective config: saved file ← profile defaults ← CLI overrides.

    Credentials (``api_key`` / ``api_secret``) come from the saved
    ``~/.vibe-trading/binance.json``; the selected connector profile supplies the
    ``profile`` intent; CLI/tool overrides win last.

    The saved key pair is used only for the environment it was saved for (see
    ``_file_credentials``), so a production key is never sent to the testnet
    host and a testnet key is never sent to production.
    """
    raw = _read_config_mapping()
    base = asdict(_config_from_mapping(raw))
    for key, value in dict(profile_config or {}).items():
        if value is not None:
            base[key] = value
    cfg = BinanceConfig.from_mapping(base)
    clean = {k: v for k, v in dict(overrides or {}).items() if k in _OVERRIDE_KEYS and v not in (None, "")}
    if clean.get("profile") is not None:
        cfg = cfg.with_overrides(profile=clean["profile"])
    api_key, api_secret = _file_credentials(raw or {}, cfg.environment)
    cfg = replace(cfg, api_key=api_key, api_secret=api_secret)
    if clean:
        cfg = cfg.with_overrides(**clean)
    _refuse_production_key_on_testnet(cfg, raw or {})
    return cfg


def _refuse_production_key_on_testnet(cfg: "BinanceConfig", raw: Mapping[str, Any]) -> None:
    """Fail closed when the testnet would receive the saved production key pair."""
    if PROFILE_ENVIRONMENTS.get(str(raw.get("profile") or "paper").strip().lower()) != "live":
        return
    refuse_known_production_credentials(
        cfg, (str(raw.get("api_key") or ""), str(raw.get("api_secret") or ""))
    )


def _same_secret(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def refuse_known_production_credentials(cfg: "BinanceConfig", production: Any) -> None:
    """Fail closed when a Spot Testnet config carries any known production value.

    ``production`` holds production key or secret strings from any source
    (binance.json, an OS-vault live connection). Comparison is constant time
    and the error never contains a credential.
    """
    if not cfg.is_testnet:
        return
    known = [str(value or "").strip() for value in production]
    known = [value for value in known if value]
    for candidate in (cfg.api_key, cfg.api_secret):
        text = str(candidate or "").strip()
        if text and any(_same_secret(text, value) for value in known):
            raise BinanceConfigError("Spot Testnet credentials must not reuse the production key")


def _file_credentials(raw: Mapping[str, Any], environment: str) -> tuple[str, str]:
    """Return the binance.json key pair for ``environment``, or blanks.

    A dedicated Spot Testnet pair lives in ``testnet_api_key`` /
    ``testnet_api_secret``, or else in the ``BINANCE_TESTNET_API_KEY`` /
    ``BINANCE_TESTNET_API_SECRET`` environment variables (``agent/.env``). The
    top-level ``api_key`` / ``api_secret`` belong to the environment of the
    file's saved ``profile`` and are never reused for the other environment.
    Live profiles never read the testnet fields or variables.
    """
    if environment == "paper" and any(raw.get(field) for field in TESTNET_CREDENTIAL_FIELDS):
        return (
            str(raw.get("testnet_api_key") or "").strip(),
            str(raw.get("testnet_api_secret") or "").strip(),
        )
    if environment == "paper":
        env_key, env_secret = _testnet_env_credentials()
        if env_key or env_secret:
            return env_key, env_secret
    saved_profile = str(raw.get("profile") or "paper").strip().lower()
    if PROFILE_ENVIRONMENTS.get(saved_profile) != environment:
        return "", ""
    return str(raw.get("api_key") or "").strip(), str(raw.get("api_secret") or "").strip()


def _testnet_env_credentials() -> tuple[str, str]:
    """Read the Spot Testnet pair from the environment, loading ``.env`` first."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:  # noqa: BLE001 - an unloadable .env leaves the process env as is
        pass
    return (
        os.environ.get(TESTNET_CREDENTIAL_ENV[0], "").strip(),
        os.environ.get(TESTNET_CREDENTIAL_ENV[1], "").strip(),
    )


def config_path() -> Path:
    """Return the user-level Binance config path."""
    return get_runtime_root() / CONFIG_FILENAME


def _read_config_mapping() -> dict[str, Any] | None:
    """Read ``binance.json`` as a mapping, or ``None`` when it is absent."""
    path = config_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise BinanceConfigError(f"invalid Binance config at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise BinanceConfigError(f"invalid Binance config at {path}: expected an object")
    return data


def _config_from_mapping(raw: Mapping[str, Any] | None) -> BinanceConfig:
    if raw is None:
        return BinanceConfig()
    try:
        return BinanceConfig.from_mapping(raw)
    except (BinanceConfigError, TypeError, ValueError) as exc:
        raise BinanceConfigError(f"invalid Binance config at {config_path()}: {exc}") from exc


def load_config() -> BinanceConfig:
    """Load Binance settings from ``~/.vibe-trading/binance.json``."""
    return _config_from_mapping(_read_config_mapping())


def save_config(config: BinanceConfig) -> Path:
    """Persist Binance settings with owner-only permissions, merging, never dropping.

    Rules (each refusal leaves the file untouched):

    * The existing file is the base. Keys that are not ``BinanceConfig``
      fields (every ``testnet_*`` safety setting and testnet credential) are
      kept verbatim, so a saved ``testnet_audit_only: false`` stays in effect
      and an absent one stays absent (audit-only on).
    * An unreadable existing file is refused, never overwritten.
    * A blank ``api_key`` / ``api_secret`` keeps the stored value; it never
      clears it.
    * A stored top-level key pair saved for another environment is never
      replaced (a paper save cannot overwrite production keys).
    * A dedicated Spot Testnet credential (``testnet_api_*`` or
      ``BINANCE_TESTNET_API_*``) is never saved as the top-level pair, for any
      profile.
    * A field whose value equals the ``BinanceConfig`` default keeps the value
      already stored (for example ``timeout``), so a partial config does not
      reset settings. To store a default explicitly, edit the file. The
      ``profile`` is the exception: it is always saved as given.

    The read-modify-write runs under a lock file. The new content goes to a
    unique, owner-only temporary file (``mkstemp``), is fsynced, then replaces
    the file atomically. On Windows the 0o600/0o700 modes are best effort: the
    file inherits the directory ACL.
    """
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    from bot.exchange._filelock import exclusive_file_lock

    with exclusive_file_lock(path.with_name(f".{path.name}.lock")):
        existing = _read_config_mapping() or {}
        payload = dict(existing)
        defaults = asdict(BinanceConfig())
        for key, value in asdict(config).items():
            if key in {"api_key", "api_secret"}:
                if not str(value or "").strip():
                    continue
            elif key != "profile" and key in existing and value == defaults.get(key):
                # The profile is never "kept": it decides which environment the
                # top-level key pair belongs to, so it is always the caller's.
                continue
            payload[key] = value
        stored_pair = any(str(existing.get(key) or "").strip() for key in ("api_key", "api_secret"))
        saved_environment = PROFILE_ENVIRONMENTS.get(str(existing.get("profile") or "paper").strip().lower())
        if stored_pair and saved_environment != config.environment:
            raise BinanceConfigError(
                "binance.json holds a key pair for another environment; refusing to replace it"
            )
        dedicated = [str(existing.get(field) or "").strip() for field in TESTNET_CREDENTIAL_FIELDS]
        dedicated.extend(value.strip() for value in _testnet_env_credentials())
        dedicated = [value for value in dedicated if value]
        for candidate in (config.api_key.strip(), config.api_secret.strip()):
            if candidate and any(_same_secret(candidate, value) for value in dedicated):
                raise BinanceConfigError(
                    "Spot Testnet credentials belong in testnet_api_key/testnet_api_secret or agent/.env, "
                    "not in the top-level key pair"
                )
        body = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        # mkstemp: O_EXCL, unique per call, owner-only from creation.
        descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def _repo_config_path() -> Path:
    """Return a ``binance.json`` kept at the repository root (not read as config).

    The connector never loads it, but a file there holds credentials that an
    unsandboxed process could read, so its presence counts as configured.
    """
    return Path(__file__).resolve().parents[2] / CONFIG_FILENAME


def binance_trading_configured() -> bool:
    """Return whether any Binance credential source is configured. Read-only.

    True when binance.json exists (any profile, readable or not) in this
    process's runtime root, in the default ``~/.vibe-trading`` root (another
    process may use that one), or at the repository root; when Spot Testnet
    credentials are set in the environment or ``.env``; or when the connection
    store holds any Binance connection. Any error while checking returns True.
    The agent's shell tools and unsandboxed generated code are withheld while
    this is True, because either could read the credentials, edit binance.json,
    clear the halt flag, or call this module directly.
    """
    try:
        if config_path().exists():
            return True
        if (Path.home() / ".vibe-trading" / CONFIG_FILENAME).exists():
            return True
        if _repo_config_path().exists():
            return True
        if any(_testnet_env_credentials()):
            return True
        return False
    except Exception:  # noqa: BLE001 - an undeterminable state counts as configured
        return True


def ccxt_available() -> bool:
    """Return whether the optional ``ccxt`` library can be imported."""
    try:
        _require_ccxt()
        return True
    except BinanceDependencyError:
        return False


def check_status(config: BinanceConfig | None = None) -> dict[str, Any]:
    """Check SDK readiness, config completeness, and host separation.

    Returns a JSON-serializable health report. Does not place or mutate any
    broker state. The host-allowlist guard asserts that the client's resolved
    host is the testnet host for a paper profile, or ``api.binance.com`` for a
    live profile, so a key/host mismatch fails closed before any read.
    """
    cfg = config or load_config()
    report: dict[str, Any] = {
        "status": "ok",
        "config": _public_config(cfg),
        "sdk": {"package": "ccxt", "installed": ccxt_available()},
        "paper_guard": "host_separated",
        "host": cfg.host,
    }

    missing = _missing_fields(cfg)
    if missing:
        report["status"] = "error"
        report["error"] = f"Binance connector not configured: missing {', '.join(missing)}."
        if cfg.is_testnet:
            report["error"] += (
                " Set BINANCE_TESTNET_API_KEY/BINANCE_TESTNET_API_SECRET in agent/.env, "
                "testnet_api_key/testnet_api_secret in binance.json, or an OS-vault "
                "connection for this profile. The top-level key pair is used for the testnet "
                "only when binance.json is saved with profile 'paper'; production keys are "
                "never used for the testnet."
            )
        return report

    if not report["sdk"]["installed"]:
        report["status"] = "error"
        report["error"] = "Optional dependency missing: install with `pip install ccxt`."
        return report

    try:
        _assert_host(cfg)
    except BinanceConfigError as exc:
        report["status"] = "error"
        report["error"] = str(exc)
        return report

    try:
        snapshot = get_account_snapshot(cfg)
    except Exception as exc:  # noqa: BLE001 - health endpoint reports cleanly
        report["status"] = "error"
        report["error"] = str(exc)
        return report

    account_summary = {
        "profile": cfg.profile,
        "is_testnet": cfg.is_testnet,
    }
    count_field = "positions" if cfg.market_type == "usdm" else "balances"
    account_summary[count_field] = len(snapshot.get(count_field, []))
    report["account"] = account_summary
    return report


def get_account_snapshot(config: BinanceConfig | None = None) -> dict[str, Any]:
    """Fetch spot balances or a strict USD-M Shadow Account observation.

    Returns the non-zero balances (each with ``free`` / ``used`` / ``total``)
    from ccxt's unified ``fetch_balance``.
    """
    cfg = config or load_config()
    _assert_host(cfg)
    ex = _exchange(cfg)
    if cfg.market_type == "usdm":
        return _get_usdm_observation(cfg, ex)
    balance = _read_with_clock_retry(ex, ex.fetch_balance)
    rows = _nonzero_balances(balance)
    return {
        "status": "ok",
        "profile": cfg.profile,
        "is_testnet": cfg.is_testnet,
        "host": cfg.host,
        "venue": "binance",
        "paper_guard": "host_separated",
        "balances": rows,
    }


def get_positions(config: BinanceConfig | None = None) -> dict[str, Any]:
    """Fetch spot holdings or strict USD-M position evidence.

    Binance spot has no positions; holdings are the non-zero balances. On live
    accounts Binance may expose Simple Earn Flexible collateral in the spot
    payload as synthetic ``LD<ASSET>`` balances. When the read-only Simple Earn
    endpoint is available, replace those wrappers with their underlying asset
    and authoritative ``totalAmount`` so portfolio valuation neither misses nor
    double-counts flexible holdings.
    """
    cfg = config or load_config()
    _assert_host(cfg)
    ex = _exchange(cfg)
    if cfg.market_type == "usdm":
        observed = _get_usdm_observation(cfg, ex)
        return {
            **observed,
            "holdings_kind": "derivative_positions",
            "venue": "binance",
        }
    balance = _read_with_clock_retry(ex, ex.fetch_balance)
    spot_balances = _nonzero_balances(balance)
    earn_rows: list[dict[str, Any]] = []
    earn_wrappers: set[str] = set()
    earn_note: str | None = None
    if not cfg.is_testnet:
        try:
            payload = _read_with_clock_retry(
                ex, lambda: ex.sapi_get_simple_earn_flexible_position({"size": 100})
            )
            for item in payload.get("rows", []) if isinstance(payload, Mapping) else []:
                asset = str(_obj_get(item, "asset", "")).strip().upper()
                quantity = _to_float(_obj_get(item, "totalAmount"))
                if not asset or not quantity:
                    continue
                earn_wrappers.add(f"LD{asset}")
                earn_rows.append(
                    {
                        "symbol": asset,
                        "quantity": quantity,
                        "free": 0.0,
                        "used": quantity,
                        "source": "simple_earn_flexible",
                    }
                )
        except Exception as exc:  # noqa: BLE001 - spot holdings still remain usable
            earn_note = f"Simple Earn Flexible holdings unavailable: {type(exc).__name__}"

    rows = [
        {
            "symbol": _obj_get(row, "asset"),
            "quantity": _obj_get(row, "total"),
            "free": _obj_get(row, "free"),
            "used": _obj_get(row, "used"),
            "source": "spot",
        }
        for row in spot_balances
        if str(_obj_get(row, "asset", "")).upper() not in earn_wrappers
    ] + earn_rows
    result = {
        "status": "ok",
        "profile": cfg.profile,
        "is_testnet": cfg.is_testnet,
        "paper_guard": "host_separated",
        "venue": "binance",
        "holdings_kind": "spot_balances",
        "positions": rows,
    }
    if earn_note:
        result["note"] = earn_note
    return result


def get_open_orders(
    config: BinanceConfig | None = None,
    *,
    symbol: str | None = None,
    include_executions: bool = False,
) -> dict[str, Any]:
    """Read open spot orders. An empty list is returned only when Binance did.

    Account-wide retrieval is supported. ccxt raises ``ExchangeError`` for that
    call unless ``warnWithoutSymbol`` is acknowledged, because the request
    weight is higher. This function acknowledges that warning and reports it.
    A failed retrieval is ``status=error`` and does not include ``open_orders``.
    """
    cfg = config or load_config()
    _reject_unsupported_usdm_surface(cfg)
    _assert_host(cfg)
    clean = None
    if symbol is not None and str(symbol).strip():
        clean = normalize_symbol(str(symbol))
        if "/" not in clean:
            return {
                "status": "error",
                "error": f"could not resolve a valid trading pair from symbol '{symbol}'",
                "open_orders_scope": "symbol",
                "retrieval_complete": False,
                "venue": "binance",
            }
    try:
        ex = _exchange(cfg)
    except (BinanceConfigError, BinanceDependencyError) as exc:
        return {
            "status": "error",
            "error": str(exc),
            "retrieval_complete": False,
            "venue": "binance",
        }
    scope = "symbol" if clean else "account"
    if clean is None:
        fetch_options = ex.options.setdefault("fetchOpenOrders", {})
        if isinstance(fetch_options, dict):
            fetch_options["warnWithoutSymbol"] = False
        ex.options["warnOnFetchOpenOrdersWithoutSymbol"] = False
    try:
        raw = ex.fetch_open_orders(clean) if clean else ex.fetch_open_orders()
    except Exception as exc:  # noqa: BLE001 - a failed read must not look empty
        return {
            "status": "error",
            "error": f"open order retrieval failed ({type(exc).__name__})",
            "open_orders_scope": scope,
            "retrieval_complete": False,
            "venue": "binance",
        }
    if raw is None:
        return {
            "status": "error",
            "error": "open order retrieval failed (empty payload)",
            "open_orders_scope": scope,
            "retrieval_complete": False,
            "venue": "binance",
        }
    result: dict[str, Any] = {
        "status": "ok",
        "profile": cfg.profile,
        "is_testnet": cfg.is_testnet,
        "paper_guard": "host_separated",
        "venue": "binance",
        "open_orders_scope": scope,
        "retrieval_complete": True,
        "open_orders": [_order_to_dict(item) for item in _as_iter(raw)],
    }
    if clean is None:
        result["warnings"] = [
            {
                "code": "account_wide_open_orders_rate_limit",
                "message": (
                    "Binance spot account-wide open orders use a stricter request "
                    "weight than a query for one symbol."
                ),
            }
        ]
    if include_executions:
        if clean is None:
            result["executions_complete"] = False
            result["executions_error"] = "symbol is required to read trade history"
        else:
            try:
                trades = ex.fetch_my_trades(clean)
            except Exception as exc:  # noqa: BLE001
                result["executions_complete"] = False
                result["executions_error"] = f"trade retrieval failed ({type(exc).__name__})"
            else:
                result["executions"] = [_trade_to_dict(item) for item in _as_iter(trades)]
                result["executions_complete"] = True
    return result


def get_quote(symbol: str, *, config: BinanceConfig | None = None, **_: Any) -> dict[str, Any]:
    """Fetch a latest ticker snapshot for ``symbol`` (ccxt unified format)."""
    cfg = config or load_config()
    _reject_unsupported_usdm_surface(cfg)
    _assert_host(cfg)
    ex = _public_exchange(cfg)
    clean = normalize_symbol(symbol)
    ticker = ex.fetch_ticker(clean)
    return {
        "status": "ok",
        "symbol": clean,
        "quote": {
            "bid": _obj_get(ticker, "bid"),
            "ask": _obj_get(ticker, "ask"),
            "last": _obj_get(ticker, "last"),
            "high": _obj_get(ticker, "high"),
            "low": _obj_get(ticker, "low"),
            "volume": _obj_get(ticker, "baseVolume"),
            "time": str(_obj_get(ticker, "timestamp", "")),
        },
    }


def search_instruments(
    query: str,
    *,
    config: BinanceConfig | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Resolve an exact spot pair against the selected Binance market catalog.

    Binance's exchange-info catalog has symbols and assets, but no stable
    human-readable asset names. The connector therefore resolves only explicit
    pair spellings (``ETH-USDT``, ``ETH/USDT`` or ``ETHUSDT``) and never guesses
    from prose such as ``Ethereum``.
    """
    cfg = config or load_config()
    _reject_unsupported_usdm_surface(cfg)
    _assert_host(cfg)

    requested = normalize_symbol(query)
    if "/" not in requested:
        return {"status": "ok", "query": query, "instruments": []}

    try:
        bounded_limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError, OverflowError):
        bounded_limit = 10

    markets = _public_exchange(cfg).load_markets()
    if not isinstance(markets, Mapping):
        raise BinanceConfigError("Binance load_markets returned a non-mapping payload")

    instruments: list[dict[str, Any]] = []
    for key, raw_market in markets.items():
        if not isinstance(raw_market, Mapping):
            continue
        native_symbol = normalize_symbol(str(raw_market.get("symbol") or key))
        if native_symbol != requested:
            continue
        if raw_market.get("spot") is False or raw_market.get("active") is False:
            continue
        base, quote = native_symbol.split("/", 1)
        instruments.append(
            {
                "symbol": f"{base}-{quote}",
                "native_symbol": native_symbol,
                "exchange_symbol": str(raw_market.get("id") or "").strip() or None,
                "base": base,
                "quote": quote,
                "market": "crypto",
                "type": "cryptocurrency",
                "exchange": "BINANCE",
                "active": raw_market.get("active"),
            }
        )
        if len(instruments) >= bounded_limit:
            break

    return {"status": "ok", "query": query, "instruments": instruments}


#: Project/canonical period token → ccxt unified timeframe (lowercase).
_TIMEFRAME_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "1H": "1h", "4h": "4h", "4H": "4h",
    "1d": "1d", "1D": "1d", "1w": "1w", "1W": "1w", "1M": "1M",
}


def get_historical_bars(
    symbol: str,
    *,
    config: BinanceConfig | None = None,
    period: str = "1d",
    limit: int = 90,
    **_: Any,
) -> dict[str, Any]:
    """Fetch historical OHLCV bars for ``symbol`` (ccxt unified format)."""
    cfg = config or load_config()
    _reject_unsupported_usdm_surface(cfg)
    _assert_host(cfg)
    ex = _public_exchange(cfg)
    clean = normalize_symbol(symbol)
    timeframe = _TIMEFRAME_MAP.get(period.strip(), "1d")
    bars = ex.fetch_ohlcv(clean, timeframe=timeframe, limit=int(limit))
    return {
        "status": "ok",
        "symbol": clean,
        "period": period,
        "bars": [_ohlcv_to_dict(item) for item in _as_iter(bars)],
    }


def get_spot_market(symbol: str, *, config: BinanceConfig | None = None) -> dict[str, Any]:
    """Return one ccxt spot market dict, including an inactive market.

    This is a read. It does not place an order and it does not drop markets
    that fail the trading filters; the execution preflight decides that.
    """
    cfg = config or load_config()
    _reject_unsupported_usdm_surface(cfg)
    _assert_host(cfg)
    clean = normalize_symbol(symbol)
    if "/" not in clean:
        return {"status": "error", "error": "symbol not found", "symbol": clean}
    try:
        markets = _public_exchange(cfg).load_markets()
    except Exception:  # noqa: BLE001 - catalog failure must not look like a missing symbol
        return {"status": "error", "error": "read_failed", "symbol": clean}
    if not isinstance(markets, Mapping):
        return {"status": "error", "error": "symbol not found", "symbol": clean}
    market = markets.get(clean)
    if not isinstance(market, Mapping):
        return {"status": "error", "error": "symbol not found", "symbol": clean}
    return {"status": "ok", "symbol": clean, "market": dict(market)}


def get_order(
    config: BinanceConfig | None = None,
    *,
    symbol: str,
    order_id: str | None = None,
    client_order_id: str | None = None,
) -> dict[str, Any]:
    """Read one spot order by exchange id or client order id. Read-only.

    Returns ``status="ok"`` with the order, ``status="not_found"`` only when
    Binance definitely reports no such order, and ``status="error"`` for any
    other failure so a failed read never looks like an absent order.
    """
    cfg = config or load_config()
    _reject_unsupported_usdm_surface(cfg)
    _assert_host(cfg)
    order_clean = str(order_id or "").strip()
    client_clean = str(client_order_id or "").strip()
    if bool(order_clean) == bool(client_clean):
        return {"status": "error", "error": "exactly one of order_id or client_order_id is required"}
    if client_clean and not _CLIENT_ORDER_ID.match(client_clean):
        return {"status": "error", "error": "client_order_id is not a valid Binance client order id"}
    clean = normalize_symbol(symbol)
    if "/" not in clean:
        return {"status": "error", "error": f"could not resolve a valid trading pair from symbol '{symbol}'"}
    base = {"symbol": clean, "profile": cfg.profile, "is_testnet": cfg.is_testnet, "venue": "binance"}
    try:
        ex = _exchange(cfg)
    except (BinanceConfigError, BinanceDependencyError) as exc:
        return {**base, "status": "error", "error": str(exc)}
    try:
        if client_clean:
            raw = ex.fetch_order(None, clean, {"origClientOrderId": client_clean})
        else:
            raw = ex.fetch_order(order_clean, clean)
    except Exception as exc:  # noqa: BLE001 - only OrderNotFound means absent
        if type(exc).__name__ == "OrderNotFound":
            return {**base, "status": "not_found"}
        return {**base, "status": "error", "error": f"read_failed ({type(exc).__name__})"}
    if raw is None:
        return {**base, "status": "error", "error": "read_failed (empty payload)"}
    return {**base, "status": "ok", "order": _order_to_dict(raw)}


def paper_order_notional_limit(quote_asset: str) -> Decimal | None:
    """Return the Spot Testnet per-order notional cap for ``quote_asset``.

    The cap lives in binance.json as ``testnet_max_order_notional``, a mapping
    of quote asset to a positive decimal string. It is not a caller override.
    ``None`` means no usable cap is configured; callers must deny the order.
    """
    try:
        raw = _read_config_mapping() or {}
    except BinanceConfigError:
        return None
    limits = raw.get("testnet_max_order_notional")
    if not isinstance(limits, Mapping):
        return None
    # Keys are exchange quote assets. "USD" never stands in for "USDT".
    value = limits.get(str(quote_asset or "").strip().upper())
    if isinstance(value, (bool, float)) or value is None:
        return None
    try:
        limit = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    if not limit.is_finite() or limit <= 0:
        return None
    return limit


#: Daily Spot Testnet operation limit when binance.json does not set one.
DEFAULT_TESTNET_DAILY_OPERATIONS = 2


def testnet_audit_only() -> tuple[bool, str]:
    """Return whether Spot Testnet audit-only mode is on, and why.

    Fail closed: audit-only is on unless binance.json sets
    ``testnet_audit_only`` to the JSON literal ``false``. A missing key, any
    other value, or an unreadable file keeps it on. Audit-only blocks every
    Spot Testnet place and cancel; reads are unaffected.
    """
    try:
        raw = _read_config_mapping()
    except BinanceConfigError:
        return True, "config_error"
    if raw is None or "testnet_audit_only" not in raw:
        return True, "default"
    if raw["testnet_audit_only"] is False:
        return False, "binance.json"
    if raw["testnet_audit_only"] is True:
        return True, "binance.json"
    return True, "invalid_value"


def testnet_daily_operation_limit() -> int | None:
    """Return the Spot Testnet daily operation limit, or ``None`` when invalid.

    ``testnet_max_daily_operations`` in binance.json; absent means
    ``DEFAULT_TESTNET_DAILY_OPERATIONS``. A non-integer, boolean, negative
    value or an unreadable file returns ``None`` and callers must deny.
    """
    try:
        raw = _read_config_mapping() or {}
    except BinanceConfigError:
        return None
    if "testnet_max_daily_operations" not in raw:
        return DEFAULT_TESTNET_DAILY_OPERATIONS
    value = raw["testnet_max_daily_operations"]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def write_latch_open() -> bool:
    """Report whether this context holds the exchange-write latch. Read-only."""
    return _execution_latch_open()


# ---------------------------------------------------------------------------
# Order placement (exchange adapter; policy lives in execution/)
# ---------------------------------------------------------------------------

_CLIENT_ORDER_ID = re.compile(r"^[.A-Z:/a-z0-9_-]{1,36}$")
_DECIMAL_TEXT = re.compile(r"^(?:0|[1-9]\d*)(?:\.\d+)?$")
_EXECUTION_LATCH: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "binance_execution_latch", default=False
)


@contextmanager
def authorize_exchange_write():
    """Allow one exchange write from the execution package only.

    A diagnostic report, a preview, or a direct SDK call does not hold this
    latch, so it cannot submit or cancel an order.
    """
    token = _EXECUTION_LATCH.set(True)
    try:
        yield
    finally:
        _EXECUTION_LATCH.reset(token)


def _execution_latch_open() -> bool:
    return _EXECUTION_LATCH.get() is True


def _gate_blocked() -> dict[str, Any]:
    return {
        "status": "error",
        "error": "order gate blocked: direct SDK writes are not authorized",
        "order_gate": "blocked",
        "trading_authorized": False,
        "diagnostic_authorizes_trading": False,
    }
_DEFINITE_REJECT_TYPES = frozenset(
    {
        "InsufficientFunds",
        "InvalidOrder",
        "BadRequest",
        "BadSymbol",
        "ArgumentsRequired",
        "AuthenticationError",
        "PermissionDenied",
        "AccountSuspended",
        "OrderNotFound",
    }
)


def _reject_write_surface(cfg: BinanceConfig) -> dict[str, Any] | None:
    """Refuse surfaces that must never reach ``create_order`` or ``cancel_order``.

    ``live-trade-bot`` is the only production writer. It still needs the
    exchange-write latch (checked by the caller) and a loaded mandate. Every
    other live profile, including a forged writable ``live`` config, stays
    read-only.
    """
    if cfg.market_type == "usdm":
        return {"status": "error", "error": "Binance USD-M Shadow Account is read-only"}
    if cfg.profile == "live-trade-bot":
        host = (urlparse(cfg.host).hostname or "").lower()
        if host != urlparse(LIVE_HOST).hostname:
            return {"status": "error", "error": "bot production writes must use api.binance.com", "reasons": ["host_rejected"]}
        try:
            from bot.live.mandate.store import load_mandate

            mandate = load_mandate("binance")
        except Exception:  # noqa: BLE001 - an unreadable mandate blocks the write
            mandate = None
        if mandate is None:
            return {
                "status": "error",
                "error": "production writes require a valid mandate",
                "reasons": ["mandate_required"],
            }
        return None
    if cfg.profile == "live-readonly":
        return {"status": "error", "error": "Binance live-readonly profile cannot place or cancel orders"}
    if cfg.profile != "paper" or not cfg.is_testnet:
        # Binance Production is read-only except for the gated bot profile above.
        return {"status": "error", "error": "Binance Production is read-only", "reasons": ["production_writes_disabled"]}
    if testnet_audit_only()[0]:
        return {"status": "error", "error": "Spot Testnet audit-only mode blocks order writes"}
    host = (urlparse(cfg.host).hostname or "").lower()
    if host != TESTNET_HOSTNAME:
        return {"status": "error", "error": "Spot Testnet writes must use testnet.binance.vision"}
    return None


def _wire_decimal(value: Any, label: str) -> tuple[str | None, dict[str, Any] | None]:
    """Accept an exact decimal string. Floats are rejected so they cannot round up."""
    if isinstance(value, bool) or isinstance(value, float) or value is None:
        return None, {"status": "error", "error": f"{label} must be a decimal string"}
    if isinstance(value, Decimal):
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        return None, {"status": "error", "error": f"{label} must be a decimal string"}
    if not _DECIMAL_TEXT.match(text):
        return None, {"status": "error", "error": f"{label} must be a decimal string"}
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None, {"status": "error", "error": f"{label} must be a positive decimal"}
    if not number.is_finite() or number <= 0:
        return None, {"status": "error", "error": f"{label} must be a positive decimal"}
    return text, None


def _exchange_outcome(exc: BaseException) -> dict[str, Any]:
    """A definite reject is an exchange response. Anything else may have been sent."""
    name = type(exc).__name__
    if name in _DEFINITE_REJECT_TYPES:
        return {"status": "error", "error": f"exchange rejected the order ({name})"}
    return {"status": "unknown", "error": f"exchange outcome is unknown ({name})"}

#: ccxt ``timeInForce`` values Binance spot accepts for limit orders. Binance
#: has no DAY policy; the unified ``"day"`` intent maps to GTC, which is the
#: Binance default and the closest equivalent.
_TIME_IN_FORCE_MAP = {
    "day": "GTC",
    "gtc": "GTC",
    "ioc": "IOC",
    "fok": "FOK",
}


_PROTECTED_TYPES = frozenset({"stop_loss_limit", "take_profit_limit"})


def place_order(
    config: BinanceConfig | None = None,
    *,
    symbol: str,
    side: str,
    quantity: Any = None,
    notional: Any = None,
    order_type: str = "market",
    limit_price: Any = None,
    stop_price: Any = None,
    time_in_force: str = "day",
    client_order_id: str | None = None,
) -> dict[str, Any]:
    """Submit one already-sized spot order.

    ``quantity``, ``notional``, and ``limit_price`` must be exact decimal
    strings. A float is rejected so it cannot round the size up. A market
    notional is sent as ``quoteOrderQty`` with no price: ccxt treats a missing
    ``quoteOrderQty`` as a base-asset quantity, and a non-empty price makes it
    multiply amount by price into the quote spend.

    Every result carries ``request_sent``: ``False`` only when the function
    returned before ``create_order`` was called, so the exchange cannot have
    received the request. A halt flag set at any time before that point blocks
    the order here, as the last check before the exchange write.
    """
    prepared = _prepare_order(
        config,
        symbol=symbol,
        side=side,
        quantity=quantity,
        notional=notional,
        order_type=order_type,
        limit_price=limit_price,
        stop_price=stop_price,
        time_in_force=time_in_force,
        client_order_id=client_order_id,
    )
    if isinstance(prepared, dict):
        return {**prepared, "request_sent": False}
    cfg, clean_symbol, type_clean, side_clean, amount, price, params, client_id = prepared
    unsent = _unsent_before_write(cfg)
    if unsent is not None:
        return unsent
    try:
        ex = _exchange(cfg)
    except (BinanceConfigError, BinanceDependencyError) as exc:
        return {"status": "error", "error": str(exc), "request_sent": False}
    except Exception as exc:  # noqa: BLE001 - client construction happens before the order request
        return {
            "status": "error",
            "error": f"exchange client was not created ({type(exc).__name__})",
            "request_sent": False,
        }
    # ccxt's create_order first loads exchangeInfo and syncs the clock (both
    # GETs). Do those reads now, so the halt recheck below is the last step
    # before the order request rather than preceding network round trips.
    load_markets = getattr(ex, "load_markets", None)
    if callable(load_markets):
        try:
            load_markets()
        except Exception as exc:  # noqa: BLE001 - reads only; no order request was made
            return {
                "status": "error",
                "error": f"exchange metadata was not loaded ({type(exc).__name__})",
                "request_sent": False,
            }
    unsent = _unsent_before_write(cfg)
    if unsent is not None:
        return unsent

    try:
        order = ex.create_order(clean_symbol, type_clean, side_clean, amount, price, params)
    except Exception as exc:  # noqa: BLE001 - the request may already have left the process
        outcome = _exchange_outcome(exc)
        if client_id:
            outcome["client_order_id"] = client_id
        return {**outcome, "request_sent": True}

    order_id = str(_obj_get(order, "id", "") or "")
    if not order_id:
        return {
            "status": "unknown",
            "error": "exchange outcome is unknown (missing order id)",
            "client_order_id": client_id,
            "symbol": clean_symbol,
            "request_sent": True,
        }
    return {
        "status": "ok",
        "order_id": order_id,
        "client_order_id": str(_obj_get(order, "clientOrderId", client_id) or client_id),
        "symbol": _obj_get(order, "symbol", clean_symbol),
        "side": str(_obj_get(order, "side", side_clean)),
        "profile": cfg.profile,
        "is_testnet": cfg.is_testnet,
        "paper_guard": "host_separated",
        "order_type": str(_obj_get(order, "type", type_clean)),
        "order_status": str(_obj_get(order, "status", "")),
        "filled": _obj_get(order, "filled"),
        "amount": _obj_get(order, "amount"),
        "price": _obj_get(order, "price"),
        "request_sent": True,
    }


def _unsent_before_write(cfg: BinanceConfig) -> dict[str, Any] | None:
    """Final checks before the exchange write: latch, write surface, halt."""
    if not _execution_latch_open():
        return {**_gate_blocked(), "request_sent": False}
    blocked = _reject_write_surface(cfg)
    if blocked:
        return {**blocked, "request_sent": False}
    try:
        from bot.live.halt import halt_flag_set

        halted = bool(halt_flag_set("binance"))
    except Exception:  # noqa: BLE001 - an unreadable halt flag blocks the write
        halted = True
    if halted:
        return {"status": "error", "error": "order blocked: halted", "reasons": ["halted"], "request_sent": False}
    return None


def _prepare_order(
    config: BinanceConfig | None,
    *,
    symbol: str,
    side: str,
    quantity: Any,
    notional: Any,
    order_type: str,
    limit_price: Any,
    stop_price: Any,
    time_in_force: str,
    client_order_id: str | None,
) -> dict[str, Any] | tuple:
    """Validate one order request without contacting the exchange."""
    cfg = config or load_config()
    blocked = _reject_write_surface(cfg)
    if blocked:
        return blocked

    side_clean = str(side or "").strip().lower()
    if side_clean not in ("buy", "sell"):
        return {"status": "error", "error": "side must be 'buy' or 'sell'."}

    type_clean = str(order_type or "").strip().lower()
    if type_clean not in ("market", "limit", *_PROTECTED_TYPES):
        return {
            "status": "error",
            "error": "order_type must be 'market', 'limit', 'stop_loss_limit' or 'take_profit_limit'.",
        }

    if (quantity is None) == (notional is None):
        return {"status": "error", "error": "provide exactly one of 'quantity' or 'notional'."}

    quantity_text = notional_text = price_text = None
    if quantity is not None:
        quantity_text, error = _wire_decimal(quantity, "quantity")
        if error:
            return error
    if notional is not None:
        notional_text, error = _wire_decimal(notional, "notional")
        if error:
            return error
    stop_text = None
    if type_clean in ("limit", *_PROTECTED_TYPES):
        if notional_text is not None:
            return {"status": "error", "error": "limit orders require 'quantity', not 'notional'."}
        price_text, error = _wire_decimal(limit_price, "limit_price")
        if error:
            return error
        if type_clean in _PROTECTED_TYPES:
            stop_text, error = _wire_decimal(stop_price, "stop_price")
            if error:
                return error
    elif limit_price is not None or stop_price is not None:
        return {"status": "error", "error": "market orders do not take limit_price."}

    client_id = str(client_order_id or "").strip()
    if client_order_id is not None and not _CLIENT_ORDER_ID.match(client_id):
        return {"status": "error", "error": "client_order_id must match Binance newClientOrderId."}

    try:
        _assert_host(cfg)
    except BinanceConfigError as exc:
        return {"status": "error", "error": str(exc)}

    clean_symbol = normalize_symbol(symbol)
    if not clean_symbol or "/" not in clean_symbol:
        return {"status": "error", "error": f"could not resolve a valid trading pair from symbol '{symbol}'."}

    params: dict[str, Any] = {}
    if client_id:
        params["newClientOrderId"] = client_id
    if stop_text is not None:
        params["stopPrice"] = stop_text
    if type_clean in ("limit", *_PROTECTED_TYPES):
        tif = _TIME_IN_FORCE_MAP.get(str(time_in_force or "").strip().lower())
        if tif is None:
            return {"status": "error", "error": "time_in_force must be one of 'day', 'gtc', 'ioc', 'fok'."}
        params["timeInForce"] = tif
        amount: str | None = quantity_text
        price: str | None = price_text
    elif notional_text is not None:
        params["quoteOrderQty"] = notional_text
        amount = notional_text
        price = None
    else:
        amount = quantity_text
        price = None
    return cfg, clean_symbol, type_clean, side_clean, amount, price, params, client_id


def cancel_order(
    config: BinanceConfig | None = None,
    order_id: str = "",
    *,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Cancel one spot order. Binance requires the order id and the symbol.

    A halt does not block a cancel (it reduces risk). Every result carries
    ``request_sent``, ``False`` only when ``cancel_order`` was never called.
    """
    cfg = config or load_config()

    def unsent(body: dict[str, Any]) -> dict[str, Any]:
        return {**body, "request_sent": False}

    blocked = _reject_write_surface(cfg)
    if blocked:
        return unsent(blocked)

    order_id_clean = str(order_id or "").strip()
    if not order_id_clean:
        return unsent({"status": "error", "error": "order_id is required to cancel an order."})
    if symbol is None or not str(symbol).strip():
        return unsent({"status": "error", "error": "Binance cancel requires symbol."})

    try:
        _assert_host(cfg)
    except BinanceConfigError as exc:
        return unsent({"status": "error", "error": str(exc)})

    clean_symbol = normalize_symbol(symbol)
    if not clean_symbol or "/" not in clean_symbol:
        return unsent({"status": "error", "error": f"could not resolve a valid trading pair from symbol '{symbol}'."})

    if not _execution_latch_open():
        return unsent(_gate_blocked())

    try:
        ex = _exchange(cfg)
    except (BinanceConfigError, BinanceDependencyError) as exc:
        return unsent({"status": "error", "error": str(exc)})
    except Exception as exc:  # noqa: BLE001 - client construction happens before the cancel request
        return unsent({"status": "error", "error": f"exchange client was not created ({type(exc).__name__})"})

    try:
        order = ex.cancel_order(order_id_clean, clean_symbol)
    except Exception as exc:  # noqa: BLE001 - the request may already have left the process
        return {**_exchange_outcome(exc), "request_sent": True}

    return {
        "request_sent": True,
        "status": "ok",
        "order_id": str(_obj_get(order, "id", order_id_clean)),
        "symbol": _obj_get(order, "symbol", clean_symbol),
        "side": str(_obj_get(order, "side", "")),
        "profile": cfg.profile,
        "is_testnet": cfg.is_testnet,
        "paper_guard": "host_separated",
        "order_status": str(_obj_get(order, "status", "")),
    }



# ---------------------------------------------------------------------------
# SDK plumbing
# ---------------------------------------------------------------------------


def _require_ccxt() -> ModuleType:
    try:
        import ccxt  # type: ignore
    except ModuleNotFoundError as exc:
        raise BinanceDependencyError("ccxt is not installed; run `pip install ccxt`.") from exc
    return ccxt


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _reject_unsupported_usdm_surface(cfg: BinanceConfig) -> None:
    if cfg.market_type == "usdm":
        raise BinanceConfigError(
            "Binance USD-M Shadow Account exposes account and position reads only"
        )


def _get_usdm_observation(cfg: BinanceConfig, exchange: Any) -> dict[str, Any]:
    try:
        return read_account_observation(
            exchange,
            source_profile="binance-live-sdk-readonly",
            host=cfg.host,
            now=_utc_now,
            absolute_tolerance=cfg.observation_absolute_tolerance,
        )
    except UsdMObservationError as exc:
        raise BinanceConfigError(str(exc)) from None


def _read_with_clock_retry(exchange: Any, read: Callable[[], Any]) -> Any:
    """Retry one rejected signed read after refreshing Binance's clock offset.

    Binance rejects timestamps more than one second ahead regardless of the
    configured recvWindow. CCXT calibrates on market loading, but a sleeping
    laptop or inconsistent network timing can still invalidate a later read.
    The extra one-second cushion applies only after that specific rejection.
    """
    try:
        return read()
    except Exception as exc:
        message = str(exc)
        if not (
            re.search(r'["\']?code["\']?\s*:\s*-1021\b', message)
            and "ahead of the server" in message.lower()
        ):
            raise
        exchange.load_time_difference()
        exchange.options["timeDifference"] += 1_000
        return read()


def _public_exchange(cfg: BinanceConfig):
    """Use the selected Binance host without credentials for public market data.

    CCXT otherwise loads Binance currencies through a signed SAPI request when
    market loading sees an API key, even for an ordinary public ticker.
    """
    return _exchange(replace(cfg, api_key="", api_secret=""))


def _exchange(cfg: BinanceConfig):
    """Build a ccxt Binance client bound to the configured market/environment."""
    ccxt = _require_ccxt()
    client_config: dict[str, Any] = {
        "apiKey": cfg.api_key,
        "secret": cfg.api_secret,
        "enableRateLimit": True,
        "timeout": int(cfg.timeout * 1000),
        # Signed Binance requests have a narrow timestamp window. Let ccxt
        # measure the exchange clock before the first private request so a
        # sleeping laptop or an imperfect system clock does not force users to
        # reconnect or recreate an otherwise valid read-only API key.
        "options": {
            "adjustForTimeDifference": True,
            "recvWindow": RECV_WINDOW_MS,
        },
    }
    # ``requests``/ccxt does not consistently inherit the macOS System Proxy.
    # urllib resolves both conventional proxy environment variables and the
    # active macOS network proxy, so local desktop connectors follow the same
    # route as the user's browser without persisting proxy details or secrets.
    system_proxies = getproxies()
    proxies = {
        scheme: str(system_proxies[scheme]).strip()
        for scheme in ("http", "https")
        if str(system_proxies.get(scheme) or "").strip()
    }
    if proxies:
        client_config["proxies"] = proxies
    exchange_class = ccxt.binanceusdm if cfg.market_type == "usdm" else ccxt.binance
    ex = exchange_class(client_config)
    ex.set_sandbox_mode(cfg.is_testnet)
    if cfg.is_testnet:
        # Pin the Spot Testnet surfaces instead of trusting ccxt's sandbox
        # table, which also lists futures-testnet hosts.
        if not isinstance(getattr(ex, "urls", None), dict):
            raise BinanceConfigError("Binance client exposes no endpoint table")
        ex.urls["api"] = dict(TESTNET_SPOT_API)
        # load_markets() fetches every type in fetchMarkets.types (ccxt 4.5
        # defaults to spot, linear and inverse). The futures types need fapi/
        # dapi surfaces this pinned table deliberately lacks, so ccxt raised
        # NotSupported after the spot exchangeInfo read. Load spot only, and
        # never call the sapi currency endpoint (not part of the Spot Testnet).
        options = getattr(ex, "options", None)
        if not isinstance(options, dict):
            raise BinanceConfigError("Binance client exposes no options table")
        fetch_markets = options.get("fetchMarkets")
        options["fetchMarkets"] = {**(fetch_markets if isinstance(fetch_markets, dict) else {}), "types": ["spot"]}
        options["fetchCurrencies"] = False
    session = getattr(ex, "session", None)
    if session is not None and hasattr(session, "max_redirects"):
        # A redirect would re-send signed requests to whatever host it names.
        session.max_redirects = 0
    if cfg.market_type == "spot":
        _assert_client_hosts(ex, cfg)
    if cfg.market_type == "usdm":
        try:
            assert_exchange_endpoints(ex)
        except UsdMObservationError as exc:
            raise BinanceConfigError(str(exc)) from None
    return ex


def _assert_client_hosts(ex: Any, cfg: BinanceConfig) -> None:
    """Fail closed unless the client's REST endpoints use the profile's host.

    Spot Testnet: every surface must be ``https://testnet.binance.vision``
    with no port, credentials, query or fragment, and ``public``/``private``
    must exist. Live: ``public``/``private`` must be ``api.binance.com`` and no
    surface may point at a testnet host (no mixed environments).
    """
    expected = TESTNET_HOSTNAME if cfg.is_testnet else urlparse(LIVE_HOST).hostname
    urls = getattr(ex, "urls", None)
    api = urls.get("api") if isinstance(urls, Mapping) else None
    if not isinstance(api, Mapping):
        raise BinanceConfigError("Binance client exposes no endpoint table")
    for surface in ("public", "private"):
        if surface not in api:
            raise BinanceConfigError(f"Binance {surface} endpoint is missing")
    for surface, url in api.items():
        parsed = urlparse(str(url or "")) if isinstance(url, str) else None
        host = ((parsed.hostname if parsed else "") or "").lower()
        if cfg.is_testnet:
            clean = (
                parsed is not None
                and parsed.scheme == "https"
                and host == expected
                and parsed.port in (None, 443)
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
            )
            if not clean:
                raise BinanceConfigError(
                    f"Binance {surface} endpoint does not resolve to the {cfg.environment} host '{expected}'."
                )
        elif surface in ("public", "private") and host != expected:
            raise BinanceConfigError(
                f"Binance {surface} endpoint does not resolve to the {cfg.environment} host '{expected}'."
            )
        elif "testnet" in host:
            raise BinanceConfigError(f"Binance {surface} endpoint points at a testnet host on a live profile.")


def _assert_host(cfg: BinanceConfig) -> None:
    """Fail closed when the resolved host does not match the declared environment.

    The host is the authoritative discriminator: testnet keys cannot reach the
    live host. A live profile must resolve to ``api.binance.com``; a paper
    profile must resolve to the official Spot Testnet host
    (``testnet.binance.vision``), whatever ``testnet_host`` says.
    """
    host = (urlparse(cfg.host).hostname or cfg.host or "").lower()
    if cfg.is_testnet:
        expected = TESTNET_HOSTNAME
        if host != expected:
            raise BinanceConfigError(
                f"Configured profile is paper, but the resolved host '{host}' is not the testnet host '{expected}'."
            )
        return
    expected_url = USDM_LIVE_HOST if cfg.market_type == "usdm" else LIVE_HOST
    expected = urlparse(expected_url).hostname or expected_url
    if host != expected:
        raise BinanceConfigError(
            f"Configured profile is live, but the resolved host '{host}' is not the live host '{expected}'."
        )


def _missing_fields(cfg: BinanceConfig) -> list[str]:
    missing = []
    if not cfg.api_key:
        missing.append("api_key")
    if not cfg.api_secret:
        missing.append("api_secret")
    return missing


def _public_config(cfg: BinanceConfig) -> dict[str, Any]:
    """Config snapshot with secrets redacted."""
    data = asdict(cfg)
    # ``readonly`` in binance.json is not enforced; the trading profile's
    # ``readonly`` and capabilities are the only write authority.
    data.pop("readonly", None)
    if data.get("api_secret"):
        data["api_secret"] = "***redacted***"
    if data.get("api_key"):
        data["api_key"] = data["api_key"][:4] + "***"
    data["host"] = cfg.host
    return data
