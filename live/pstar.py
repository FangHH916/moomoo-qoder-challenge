"""P* live entry point: run the frozen research command, verify it, and publish the target contract.

The strategy is never recomputed here. All numbers come from one source of truth,
``research/r10_deploy.py live``, which refreshes the bars, rebuilds the lean cache and re-runs the whole
P* backtest through the latest completed session. This module only (a) invokes it with the account's
current equity and its discretionary holdings, (b) verifies the JSON it wrote, and (c) converts that JSON
into the sealed targets contract the rule gate and the order planner consume. If any check fails the
result is HOLD: no new strategy orders, while the risk rules stay active.

usage (project venv, from the repository root):
  python -m live.pstar --snapshot snapshot.json                  # runs r10_deploy.py live (~10-15 min)
  python -m live.pstar --snapshot snapshot.json --no-run         # verify/re-convert the newest run file
  python -m live.pstar --snapshot snapshot.json --variant pstar  # the round-10 fallback variant
``snapshot.json`` is your own paper-account snapshot; ``live/example-snapshot.json`` is a fictional demo
file and pinning ``--as-of`` is what makes the offline walkthrough reproducible.
Never places orders.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from live.contracts import (ROOT, PSTAR_SCHEMA_VERSION, digest, finite, load_strategy_config,
                            pstar_variant_config, seal, source_checksum, strict_json, timestamp,
                            validate_pstar_targets, write_targets)
from rules.trading_calendar import completed_session, exchange

PSTAR_STRATEGY = "pstar"
ACCOUNTS = {"test": "5322438", "competition": "5832812"}
OPEN_ACTIONS = {"ALLOW", "SUBMITTED", "FILLED"}          # a journal decision that put the name on the book
STATE = ROOT / "live" / "state"
DEPLOYED_RE = re.compile(r'^DEPLOYED\s*=\s*["\']([a-z0-9]+)["\']', re.MULTILINE)
LIMITATIONS = [
    "Targets come from one `r10_deploy.py live` run; the run is not re-executed when --no-run is used.",
    "Discretionary detection trusts the journal: a position with no journaled strategy order is never sold.",
    "Whole-share rounding, the trade band and the risk rules are applied by live/order_planner.py, not here.",
    "The research run uses Alpaca bars and SEC 8-K dates; broker borrow and fills are rechecked by the gate.",
    "A variant change is one config line; `DEPLOYED` in research/r10_deploy.py only records the user's choice.",
]


class Hold(ValueError):
    """The research run is missing, stale or inconsistent; publish HOLD instead of executable targets."""


# ------------------------------------------------------------------ the research command
def python_executable() -> Path:
    venv = (ROOT / ".venv/Scripts/python.exe") if sys.platform == "win32" else (ROOT / ".venv/bin/python")
    return venv if venv.exists() else Path(sys.executable)


def research_dir() -> Path:
    return ROOT / "research"


def build_command(variant: str, equity: float, hold: dict, config: dict,
                  skip_events: bool = False) -> list[str]:
    """argv for the single source of truth; run with cwd=research/ so its relative paths resolve."""
    cmd = [str(python_executable()), "r10_deploy.py", "live", "--variant", variant,
           "--equity", f"{float(equity):.2f}"]
    for name in sorted(hold):
        cmd += ["--hold", f"{name}={float(hold[name]):.2f}"]
    if skip_events:
        cmd.append("--skip-events")
    return cmd


def run_research(command: list[str], cwd: Path | None = None, runner=None,
                 timeout: int = 2400) -> dict:
    """Execute the research command. `runner` is injectable so tests never start a real run."""
    runner = runner or subprocess.run
    completed = runner(command, cwd=str(cwd or research_dir()), timeout=timeout,
                       capture_output=True, text=True)
    if getattr(completed, "returncode", 1) != 0:
        raise Hold(f"research run failed (exit {getattr(completed, 'returncode', 'unknown')}): "
                   f"{(getattr(completed, 'stderr', '') or '')[-400:]}")
    return {"returncode": 0, "stdout": getattr(completed, "stdout", "") or ""}


def deployed_variant() -> str | None:
    """The variant the user froze in the research file, read without importing numpy/pandas."""
    match = DEPLOYED_RE.search((research_dir() / "r10_deploy.py").read_text(encoding="utf-8"))
    return match.group(1) if match else None


# ------------------------------------------------------------------ the run file
def run_path(config: dict, variant_config: dict, session: str) -> Path:
    return ROOT / config["targets_dir"] / f"{variant_config['targets_prefix']}_targets_{session}.json"


def read_json(path: Path) -> dict:
    try:
        value = strict_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Hold(f"Cannot read {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise Hold(f"{path.name} is not a JSON object")
    return value


def validate_run(run: dict, config: dict, variant_config: dict, session: str, now: datetime) -> None:
    """Every check the deployment needs before a research run may become executable targets."""
    for key in ("strategy_id", "schema", "session", "generated_at_utc",
                "rebalance_session", "last_rebalance_session", "sessions_until_next_rebalance",
                "equity_input", "initial_equity", "k_scale", "sleeve_weights", "targets", "diagnostics",
                "checksum_sha256", "data", "consistency_with_research"):
        if key not in run:
            raise Hold(f"research run is missing `{key}`")
    if run["schema"] != 1:
        raise Hold(f"unsupported research run schema {run['schema']}")
    # `variant` and `factors` were added to the run file after the round-10 fixture was produced;
    # strategy_id and the frozen scale identify the variant in that record.
    if run.get("variant", run["strategy_id"].split("-")[-1]) != config["variant"]:
        raise Hold(f"research run variant {run.get('variant')} is not the configured variant")
    if run["strategy_id"] != variant_config["strategy_id"]:
        raise Hold(f"research run strategy_id {run['strategy_id']} does not match the configured variant")
    if set(run.get("factors") or variant_config["factors"]) != set(variant_config["factors"]) \
            or run["k_scale"] != variant_config["k_scale"]:
        raise Hold("research run does not carry the frozen composite and scale of the configured variant")
    if run["session"] != session:
        raise Hold(f"research run session {run['session']} is not the latest completed session {session}")
    generated = timestamp(run["generated_at_utc"])
    if generated > now:
        raise Hold("research run is timestamped in the future")
    if run["data"].get("last_bar_session") != session:
        raise Hold(f"research run bars end at {run['data'].get('last_bar_session')}, not {session}")
    if run["checksum_sha256"] != source_checksum(run["targets"]):
        raise Hold("research run checksum does not match its own targets")
    diag = run["diagnostics"]
    if diag.get("ok") is not True:
        raise Hold(f"research diagnostics did not pass: {diag}")
    if not finite(diag.get("equity")) or diag["equity"] != run["equity_input"]:
        raise Hold("research diagnostics were not measured on the requested equity")
    if not finite(diag.get("positions")) or diag["positions"] > config["max_positions"]:
        raise Hold(f"research book needs {diag.get('positions')} positions, over the {config['max_positions']} cap")
    if not finite(diag.get("n_short")) or diag["n_short"] > config["n_short"]:
        raise Hold(f"research book has {diag.get('n_short')} shorts, over the {config['n_short']} cap")
    if not finite(diag.get("margin")) or diag["margin"] > config["margin_cap"] + 1e-9:
        raise Hold(f"research book margin {diag.get('margin')} breaches {config['margin_cap']}")
    cons = run["consistency_with_research"]
    if not finite(cons.get("sessions_compared")) or cons["sessions_compared"] < 1:
        raise Hold("research run compared no session with the frozen research record")
    if not finite(cons.get("sleeve_weight_max_abs_diff")) \
            or cons["sleeve_weight_max_abs_diff"] >= config["consistency_tolerance_sleeve_weights"]:
        raise Hold(f"sleeve weights differ from the research run by "
                   f"{cons.get('sleeve_weight_max_abs_diff')} (tolerance "
                   f"{config['consistency_tolerance_sleeve_weights']})")
    if not finite(cons.get("return_max_abs_diff")) \
            or cons["return_max_abs_diff"] >= config["consistency_tolerance_returns"]:
        raise Hold(f"daily returns differ from the research run by {cons.get('return_max_abs_diff')} "
                   f"(tolerance {config['consistency_tolerance_returns']})")
    for row in run["targets"]:
        if set(row) != {"symbol", "side", "weight", "dollars", "uncapped_weight", "sleeve"}:
            raise Hold(f"unexpected fields in target row {row}")
        if row["side"] != ("long" if row["weight"] > 0 else "short"):
            raise Hold(f"target row {row['symbol']} side does not match its weight sign")
        if abs(row["dollars"] - row["weight"] * run["equity_input"]) > 0.01:
            raise Hold(f"target row {row['symbol']} dollars do not match its weight")


def newest_run(config: dict, variant_config: dict) -> Path:
    pattern = f"{variant_config['targets_prefix']}_targets_*.json"
    files = sorted((ROOT / config["targets_dir"]).glob(pattern))
    if not files:
        raise Hold(f"no research run file matches {pattern} in {config['targets_dir']}")
    return files[-1]


# ------------------------------------------------------------------ holdings and attribution
def journal_strategy_symbols(records, strategy_id: str) -> set[str]:
    """Symbols this strategy version actually opened, from the append-only decision journal."""
    return {row["symbol"] for row in records or ()
            if row.get("strategy_id") == strategy_id and row.get("action") in OPEN_ACTIONS
            and isinstance(row.get("symbol"), str)}


def read_journal(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            raise Hold(f"decision journal line is not JSON: {line[:80]}")
    return rows


def classify(snapshot: dict, config: dict, strategy_id: str, journal_records) -> dict:
    """Split the account into what the strategy opened, what it may never trade, and what is unclassified.

    Positions outside the strategy are handed to `apply_caps` as `hold` so they still consume margin,
    per-ticker cap and position budget, and they are never closed by a strategy order.
    """
    opened = journal_strategy_symbols(journal_records, strategy_id)
    whitelisted = set(config.get("discretionary_symbols") or [])
    positions, hold, discretionary, unclassified = {}, {}, [], []
    for row in snapshot.get("positions") or ():
        name = str(row["symbol"]).removeprefix("US.")
        if name in positions:
            raise Hold(f"duplicate snapshot position {name}")
        dollars = float(row["quantity"]) * float(row["price"])
        positions[name] = dollars
        if name in opened:
            continue
        hold[name] = dollars
        (discretionary if name in whitelisted else unclassified).append(name)
    return {"positions": positions, "hold": hold, "strategy_opened": sorted(opened),
            "discretionary": sorted(discretionary), "unclassified": sorted(unclassified)}


# ------------------------------------------------------------------ conversion
def make_targets(config: dict, snapshot: dict, now: datetime, run: dict | None = None,
                 journal_records=(), runner=None, no_run: bool = False,
                 skip_events: bool = False) -> dict:
    """Run (or reuse) the research command and convert its output into the sealed targets contract."""
    variant_config = pstar_variant_config(config)
    session = completed_session(now)
    payload = {"schema_version": PSTAR_SCHEMA_VERSION, "strategy": PSTAR_STRATEGY,
               "strategy_id": variant_config["strategy_id"], "variant": config["variant"],
               "mode": config.get("mode", "test"), "status": "HOLD",
               "generated_at": now.astimezone(timezone.utc).isoformat(),
               "data_timestamp": exchange().session_close(session).isoformat(),
               "signal_date": session, "session": session, "rebalance_due": False,
               "last_rebalance_session": None, "sessions_until_next_rebalance": None,
               "sizing_equity": None, "initial_equity": config["initial_equity"],
               "weights": {}, "dollars": {}, "sleeves": {}, "source_targets": [],
               "source": {"file": None, "checksum_sha256": "0" * 64, "run_generated_at_utc": None},
               "sleeve_weights": {}, "diagnostics": {}, "consistency_with_research": {},
               "factors": list(variant_config["factors"]), "k_scale": variant_config["k_scale"],
               "discretionary": {"hold": {}, "whitelisted": [], "unclassified": []},
               "config_checksum": digest(config), "position_snapshot_checksum": digest(snapshot),
               "limitations": LIMITATIONS}
    try:
        if config.get("strategy") != PSTAR_STRATEGY:
            raise Hold(f"config strategy {config.get('strategy')} is not pstar")
        if config.get("mode") not in ACCOUNTS:
            raise Hold("unknown account mode")
        if snapshot.get("mode") != config["mode"] or str(snapshot.get("account_id")) != ACCOUNTS[config["mode"]]:
            raise Hold("snapshot account/mode mismatch")
        equity = number(snapshot["equity"], "snapshot equity")
        holdings = classify(snapshot, config, variant_config["strategy_id"], journal_records)
        payload["discretionary"] = {"hold": {k: round(v, 2) for k, v in holdings["hold"].items()},
                                    "whitelisted": holdings["discretionary"],
                                    "unclassified": holdings["unclassified"]}
        if run is None:
            if no_run:
                run = read_json(newest_run(config, variant_config))
            else:
                run_research(build_command(config["variant"], equity, holdings["hold"], config, skip_events),
                             runner=runner, timeout=config.get("live_run_timeout_seconds", 2400))
                run = read_json(run_path(config, variant_config, session))
        validate_run(run, config, variant_config, session, now)
        payload.update(status="READY",
                       rebalance_due=bool(run["rebalance_session"]),
                       last_rebalance_session=run["last_rebalance_session"],
                       sessions_until_next_rebalance=int(run["sessions_until_next_rebalance"]),
                       sizing_equity=float(run["equity_input"]), initial_equity=float(run["initial_equity"]),
                       weights={row["symbol"]: float(row["weight"]) for row in run["targets"]},
                       dollars={row["symbol"]: float(row["dollars"]) for row in run["targets"]},
                       sleeves={row["symbol"]: row["sleeve"] for row in run["targets"]},
                       source_targets=run["targets"],
                       source={"file": f"{config['targets_dir']}/"
                                      f"{variant_config['targets_prefix']}_targets_{session}.json",
                               "checksum_sha256": run["checksum_sha256"],
                               "run_generated_at_utc": run["generated_at_utc"],
                               "strategy_id": run["strategy_id"], "command_cwd": "research/"},
                       sleeve_weights={k: float(v) for k, v in run["sleeve_weights"].items()},
                       diagnostics=run["diagnostics"],
                       consistency_with_research=run["consistency_with_research"],
                       data=run["data"])
    except (Hold, KeyError, ValueError, TypeError, OSError) as exc:
        payload["hold_reason"] = str(exc)
    payload = seal(payload)
    try:
        validate_pstar_targets(payload, config)
    except ValueError as exc:
        body = {k: v for k, v in payload.items() if k not in ("checksum", "status", "hold_reason")}
        payload = seal({**body, "status": "HOLD", "hold_reason": f"contract violation: {exc}"})
    return payload


def number(value, label: str) -> float:
    if isinstance(value, bool) or not finite(value) or value <= 0:
        raise Hold(f"invalid {label}")
    return float(value)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True, help="moomoo/paper account snapshot JSON")
    parser.add_argument("--as-of", type=timestamp)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--variant", choices=("pstar", "pstar15"))
    parser.add_argument("--journal", type=Path, default=ROOT / "journal/decisions.jsonl")
    parser.add_argument("--no-run", action="store_true", help="verify and convert the newest run file only")
    parser.add_argument("--skip-events", action="store_true", help="pass through to r10_deploy.py live")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    config = load_strategy_config(PSTAR_STRATEGY, args.config)
    if args.variant:
        config = {**config, "variant": args.variant}
    try:
        snapshot = json.loads(args.snapshot.resolve().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        parser.error("cannot read snapshot JSON")
    now = args.as_of or datetime.now(timezone.utc)
    records = read_journal(args.journal) if args.journal else []
    result = make_targets(config, snapshot, now, journal_records=records, no_run=args.no_run,
                          skip_events=args.skip_events)
    out = args.out or (ROOT / "live/targets" / result["mode"] / f"{result['signal_date']}-pstar.json")
    try:
        write_targets(out, result, config)
    except (ValueError, FileExistsError) as exc:
        print(f"HOLD: {exc}")
        return 2
    flag = "" if deployed_variant() == config["variant"] else \
        f" (note: research DEPLOYED is {deployed_variant()}, config is {config['variant']})"
    print(f"{result['status']}: {out.resolve()}"
          + (f" ({result['hold_reason']})" if "hold_reason" in result else "") + flag)
    return 0 if result["status"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
