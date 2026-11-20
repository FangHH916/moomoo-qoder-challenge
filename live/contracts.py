from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 2               # the published plan-A contract, kept so its caches and artifacts verify
PSTAR_SCHEMA_VERSION = 3         # P* adds variant provenance, dollars, sleeves and run diagnostics
STRATEGY_ID = "frozen-short-alpha-half"
LEGACY_STRATEGY = "legacy-a"
PSTAR_STRATEGY = "pstar"
ACTIVE_STRATEGY = PSTAR_STRATEGY        # prompt 06: P* is the deployment default
STRATEGY_CONFIGS = {PSTAR_STRATEGY: ROOT / "live/config.pstar.json", LEGACY_STRATEGY: ROOT / "live/config.v2.json"}
SYMBOL_RE = re.compile(r"[A-Z0-9][A-Z0-9.-]*")


def strict_json(text: str | bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON number")))


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def seal(payload: dict) -> dict:
    body = {k: v for k, v in payload.items() if k != "checksum"}
    return {**body, "checksum": digest(body)}


def verify(payload: dict) -> bool:
    try:
        return isinstance(payload.get("checksum"), str) and payload["checksum"] == seal(payload)["checksum"]
    except (ValueError, TypeError):
        return False


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timezone-aware timestamp required")
    return result.astimezone(timezone.utc)


def finite(value: object) -> bool:
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def frozen_factor_names(config: dict | None = None) -> list[str]:
    """The configured factor list; the scorer additionally checks it against the research definitions."""
    names = (config or load_config()).get("factor_names")
    if (not isinstance(names, list) or not names or len(set(names)) != len(names) or
            not all(isinstance(n, str) and re.fullmatch(r"[a-z0-9_]+", n) for n in names)):
        raise ValueError("Configured factor_names is not a unique non-empty list")
    return list(names)


def hedge_keys(config: dict) -> dict[str, str]:
    """Hedge instrument -> the target payload field carrying its weight."""
    return {instrument: f"{instrument.lower()}_hedge_weight" for instrument in config["hedge_instruments"]}


def validate_targets(payload: dict, config: dict | None = None) -> None:
    config = config or load_config()
    if config.get("strategy") == PSTAR_STRATEGY:
        return validate_pstar_targets(payload, config)
    if (type(payload.get("schema_version")) is not int or payload["schema_version"] != SCHEMA_VERSION
            or not verify(payload)):
        raise ValueError("Invalid target schema or checksum")
    for key in ("generated_at", "data_timestamp"):
        timestamp(payload[key])
    datetime.strptime(payload["signal_date"], "%Y-%m-%d")
    if payload["strategy_id"] != STRATEGY_ID:
        raise ValueError("Strategy configuration is not approved")
    if payload.get("mode") not in ("test", "competition"):
        raise ValueError("Invalid mode")
    if payload.get("status") not in ("READY", "HOLD"):
        raise ValueError("Invalid target status")
    weights = payload["weights"]
    hedges = hedge_keys(config)
    if not isinstance(weights, dict):
        raise ValueError("Weights must be an object")
    for instrument, field in hedges.items():
        if instrument in weights or not finite(payload.get(field)) or abs(payload[field]) > 1:
            raise ValueError(f"{instrument} must be a separate hedge weight")
    for symbol, weight in weights.items():
        if (not isinstance(symbol, str) or not SYMBOL_RE.fullmatch(symbol)
                or not finite(weight) or abs(weight) > 1):
            raise ValueError("Invalid canonical symbol or target weight")
        if weight > 0:
            raise ValueError("Frozen strategy holds no single-stock longs")
    if not finite(payload["vol_multiplier"]) or not 0 < payload["vol_multiplier"] <= 1:
        raise ValueError("Invalid volatility multiplier")
    if not isinstance(payload["model_coefficients"], dict) or not all(
            finite(v) for v in payload["model_coefficients"].values()):
        raise ValueError("Invalid model coefficients")
    for key in ("config_checksum", "position_snapshot_checksum"):
        if not isinstance(payload.get(key), str) or len(payload[key]) != 64:
            raise ValueError(f"Missing {key}")
    if payload["status"] != "READY":
        return
    # READY is only executable with frozen-model provenance, and every exposure bound below is
    # re-checked by the deterministic gate against the live snapshot.
    if set(payload["model_coefficients"]) != set(frozen_factor_names(config)):
        raise ValueError("READY requires the configured frozen factor set")
    if not any(v != 0 for v in payload["model_coefficients"].values()):
        raise ValueError("READY refuses an all-zero coefficient vector")
    for key in ("model_carried_forward", "model_retrained_today"):
        if type(payload.get(key)) is not bool:
            raise ValueError(f"READY requires a boolean {key}")
    if payload["model_carried_forward"] and payload["model_fit_date"] >= payload["signal_date"]:
        raise ValueError("Carried-forward coefficients must predate the signal date")
    datetime.strptime(payload["model_fit_date"], "%Y-%m-%d")
    guard = payload.get("signal_guard")
    if not isinstance(guard, dict) or guard.get("passed") is not True or not finite(guard.get("distinct_values")):
        raise ValueError("READY requires a passed signal guard")
    if not finite(payload.get("sizing_equity")) or payload["sizing_equity"] <= 0:
        raise ValueError("READY requires the equity used for sizing")
    if payload.get("rebalance_due") is not False:
        # Outside an anchored rebalance the target only restates drifted held weights; the gate
        # still rechecks exposure against the live snapshot before any increasing order.
        gross = -sum(weights.values())
        if gross > config["gross_short"] * payload["vol_multiplier"] + 1e-9:
            raise ValueError("READY breaches the frozen 35% gross short")
    if len(weights) > config["n_shorts"]:
        raise ValueError("READY exceeds the frozen short name count")
    if payload.get("fixture") is True and payload.get("mode") != "test":
        raise ValueError("Synthetic fixture targets may not name the competition account")


def pstar_variant_config(config: dict) -> dict:
    """The frozen constants of the configured variant, cross-checked against the research tag."""
    variant, variants = config.get("variant"), config.get("variants")
    if not isinstance(variant, str) or not isinstance(variants, dict) or variant not in variants:
        raise ValueError("Configured P* variant is not a frozen variant")
    block = variants[variant]
    tag = block.get("research_tag")
    if (not isinstance(tag, str) or block.get("strategy_id") != f"{tag.lower()}-{variant}"
            or block.get("targets_prefix") != tag.lower()
            or not isinstance(block.get("factors"), list) or not block["factors"]
            or not finite(block.get("k_scale")) or block["k_scale"] <= 0):
        raise ValueError("Incomplete P* variant configuration")
    return block


def source_checksum(source_targets: object) -> str:
    """The checksum `research/r10_deploy.py cmd_live` seals into its own output file."""
    return hashlib.sha256(json.dumps(source_targets, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def validate_pstar_targets(payload: dict, config: dict) -> None:
    """P* targets contract (schema 3): the sealed conversion of one `r10_deploy.py live` run.

    The strategy itself is never recomputed here: the numbers must arrive from
    `research/r10_deploy.py`, and the payload keeps the run's own checksum, diagnostics and
    consistency measurements so they can be re-verified rather than trusted.
    """
    if (type(payload.get("schema_version")) is not int or payload["schema_version"] != PSTAR_SCHEMA_VERSION
            or payload.get("strategy") != PSTAR_STRATEGY or not verify(payload)):
        raise ValueError("Invalid target schema or checksum")
    variant_config = pstar_variant_config(config)
    for key in ("generated_at", "data_timestamp"):
        timestamp(payload[key])
    datetime.strptime(payload["signal_date"], "%Y-%m-%d")
    if payload.get("session") != payload["signal_date"]:
        raise ValueError("Target session must be the signal date")
    if payload.get("variant") != config["variant"]:
        raise ValueError("Target variant is not the configured variant")
    if payload.get("strategy_id") != variant_config["strategy_id"]:
        raise ValueError("Strategy configuration is not approved for the configured variant")
    if payload.get("mode") not in ("test", "competition"):
        raise ValueError("Invalid mode")
    if payload.get("status") not in ("READY", "HOLD"):
        raise ValueError("Invalid target status")
    if type(payload.get("rebalance_due")) is not bool:
        raise ValueError("A rebalance flag is required")
    if set(payload.get("factors") or []) != set(variant_config["factors"]) \
            or payload.get("k_scale") != variant_config["k_scale"]:
        raise ValueError("Target does not carry the frozen composite and scale of the configured variant")
    weights, dollars, sleeves = payload["weights"], payload["dollars"], payload["sleeves"]
    if not all(isinstance(item, dict) for item in (weights, dollars, sleeves)):
        raise ValueError("Weights, dollars and sleeves must be objects")
    if not set(weights) == set(dollars) == set(sleeves):
        raise ValueError("Target symbols must have a weight, a dollar amount and a sleeve")
    if len([w for w in weights.values() if w < 0]) > config["n_short"]:
        raise ValueError(f"P* takes at most {config['n_short']} shorts")
    etfs = set(config["etf_universe"])
    discretionary = set(config.get("discretionary_symbols") or [])
    for name, weight in weights.items():
        if (not isinstance(name, str) or not SYMBOL_RE.fullmatch(name)
                or not finite(weight) or abs(weight) > 1):
            raise ValueError("Invalid canonical symbol or target weight")
        if not isinstance(sleeves[name], str) or not sleeves[name]:
            raise ValueError("Invalid target sleeve")
        if weight > 0 and name not in etfs and name not in discretionary:
            raise ValueError("P* longs are limited to the ETF sleeve and the discretionary whitelist")
    source = payload.get("source")
    for key in ("config_checksum", "position_snapshot_checksum"):
        if not isinstance(payload.get(key), str) or len(payload[key]) != 64:
            raise ValueError(f"Missing {key}")
    if payload["status"] != "READY":
        return
    if (not isinstance(source, dict) or not isinstance(source.get("file"), str)
            or not isinstance(source.get("checksum_sha256"), str) or len(source["checksum_sha256"]) != 64
            or source["checksum_sha256"] != source_checksum(payload.get("source_targets"))
            or {row["symbol"] for row in payload["source_targets"]} != set(weights)):
        raise ValueError("READY needs the run provenance and its own checksum to match")
    if source["file"] != f"{config['targets_dir']}/{variant_config['targets_prefix']}_targets_" \
                         f"{payload['signal_date']}.json":
        raise ValueError("READY comes from a run file that is not the configured variant for this session")
    if payload.get("fixture") is True and payload.get("mode") != "test":
        raise ValueError("Synthetic fixture targets may not name the competition account")
    if not finite(payload.get("sizing_equity")) or payload["sizing_equity"] <= 0:
        raise ValueError("READY requires the equity used for sizing")
    for name, weight in weights.items():
        if abs(dollars[name] - weight * payload["sizing_equity"]) > 0.01:
            raise ValueError("Target dollars do not match the weight and the sizing equity")
    diag = payload.get("diagnostics")
    if not isinstance(diag, dict) or diag.get("ok") is not True:
        raise ValueError("READY requires passing competition diagnostics")
    if diag.get("equity") != payload["sizing_equity"] or not finite(diag.get("margin")):
        raise ValueError("Diagnostics were not measured on the sizing equity")
    if not finite(diag.get("positions")) or diag["positions"] > config["max_positions"]:
        raise ValueError("READY exceeds the position cap")
    if not finite(diag.get("n_short")) or diag["n_short"] > config["n_short"]:
        raise ValueError("READY exceeds the short-name cap")
    if diag["margin"] > config["margin_cap"] + 1e-9:
        raise ValueError("READY breaches the margin cap")
    cons = payload.get("consistency_with_research")
    if (not isinstance(cons, dict) or not finite(cons.get("sleeve_weight_max_abs_diff"))
            or not finite(cons.get("return_max_abs_diff")) or cons.get("sessions_compared", 0) < 1
            or cons["sleeve_weight_max_abs_diff"] >= config["consistency_tolerance_sleeve_weights"]
            or cons["return_max_abs_diff"] >= config["consistency_tolerance_returns"]):
        raise ValueError("READY requires agreement with the frozen research run within tolerance")


def write_targets(path: Path, payload: dict, config: dict | None = None) -> None:
    validate_targets(payload, config)
    contents = canonical_bytes(payload) + b"\n"
    if path.exists():
        if path.read_bytes() == contents:
            return
        raise FileExistsError("Refusing to replace published target bytes")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(contents)


def load_config(path: str | Path | None = None) -> dict:
    """Explicit path, else the retired plan-A config. New deployment code uses `load_strategy_config`."""
    return json.loads(Path(path or STRATEGY_CONFIGS[LEGACY_STRATEGY]).read_text(encoding="utf-8"))


def load_strategy_config(strategy: str | None = None, path: str | Path | None = None) -> dict:
    """The deployed strategy's configuration; P* is the default, plan A only on explicit request."""
    name = strategy or ACTIVE_STRATEGY
    if name not in STRATEGY_CONFIGS:
        raise ValueError(f"Unknown strategy: {name}")
    return json.loads(Path(path or STRATEGY_CONFIGS[name]).read_text(encoding="utf-8"))
