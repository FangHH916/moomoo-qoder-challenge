"""Validation replay of the frozen book: the production scorer driving a simulated EOD account.

Nothing here re-implements factors, fits, book construction or the caps; every session is scored by
live.daily_scores.make_targets. No model fitting, research main, network, broker execution or
automatic promotion. These are new risk-overlay portfolio results, not the original research metrics.
"""
from __future__ import annotations

import argparse
from datetime import timezone
from pathlib import Path

import numpy as np
import pandas as pd

from live.contracts import ROOT, canonical_bytes, digest, hedge_keys, load_config, seal, validate_targets
from live.daily_scores import (ACCOUNTS, FIELDS, HISTORY, Hold, SYNTHETIC_SOURCE, assemble_panel, file_hash,
                               fixture_panel, frame_hash, make_targets, normalize_bars, number, sessions,
                               validate_bars, validate_config)
from research.factors import market_beta
from rules.trading_calendar import exchange

LIMITATIONS = [
    "New frozen-book replay; not original backtest_v2/v3 metrics and not evidence for promotion.",
    "Signals at close t are filled at adjusted open t+1 and valued at adjusted open t+2.",
    "A daily-step approximation of the 5-session-hold research book; each session is re-valued.",
    "Ideal fractional target fills; no lot rounding, queue, spread, recall or partial-fill modelling.",
    "Eligibility/borrow are static flags from the saved universe, not point-in-time broker rows.",
    "Costs use configured slippage, order fee, borrow and financing; actual broker costs may differ.",
    "Portfolio drifts against NET equity (fees included), unlike the research gross denominator.",
    "Hedge ETFs share the SPY slippage setting; the frozen config exposes no per-ETF cost key.",
    "Gross 35% and the 38%-of-initial hedge caps size a rebalance; between anchors the restated held "
    "book drifts past them and the replay reports rather than enforces that drift.",
    "The walk bootstraps the carry-forward coefficient history by scoring earlier sessions first.",
]

COLUMNS = ("gross_short", "short_names", "largest_short", "margin_used", "residual_beta",
           "nonzero_factors", "distinct_scores", "drift_beyond_gross_cap", "drift_beyond_hedge_cap")


def forward_returns(opens: pd.DataFrame, signal_dates: pd.DatetimeIndex) -> pd.DataFrame:
    if not opens.index.is_unique or not opens.columns.is_unique or not opens.index.is_monotonic_increasing:
        raise Hold("Open prices must have unique, ascending session dates and symbols")
    result = pd.DataFrame(index=signal_dates, columns=opens.columns, dtype=float)
    for day in signal_dates:
        if not exchange().is_session(day):
            raise Hold("Replay signal is not an exchange session")
        entry = exchange().next_session(day)
        exit_ = exchange().next_session(entry)
        if entry not in opens.index or exit_ not in opens.index:
            raise Hold("Saved bars do not contain both next-session opens; no unmatured returns assumed")
        start, stop = opens.loc[entry], opens.loc[exit_]
        result.loc[day] = (stop / start - 1).where((start > 0) & (stop > 0))
    return result


def panel_asof(panel: dict, day: str, horizon: int) -> dict:
    """Cut every frame at the signal session, then rebuild the forward labels from the cut opens.

    assemble_panel shifted the labels from the full frame, so replaying with sliced rows alone would
    hand a fit labels that were not observable at that session's close.
    """
    cut = pd.Timestamp(day)
    p = {k: v.loc[:cut] for k, v in panel["p"].items()}
    if p["close"].empty or p["close"].index[-1] != cut:
        raise Hold("Panel does not contain the requested signal session")
    p["fwd_ret"] = p["open"].shift(-2) / p["open"].shift(-1) - 1
    p["fwd_horizon"] = p["open"].shift(-(horizon + 1)) / p["open"].shift(-1) - 1
    return {"p": {k: v.astype("float32") for k, v in p.items()}, "U": panel["U"].loc[:cut],
            "industry": panel["industry"]}


def cached_panel(config: dict, day: str, data_dir: Path) -> tuple[dict, dict]:
    """assemble_panel over saved parquet, because load_live_panel also demands a verified bar-cache
    manifest that only a network --update creates and a replay must not touch the broker."""
    paths = {"adjusted": data_dir / "bars_daily.parquet", "raw": data_dir / "bars_daily_raw.parquet",
             "universe": data_dir / "universe.parquet", "shares": data_dir / "shares_q.parquet"}
    frames = {name: pd.read_parquet(path) for name, path in paths.items()}
    adj, raw = [normalize_bars(frames[name]) for name in ("adjusted", "raw")]
    dates = sessions(day, HISTORY)
    adj, raw = [frame.loc[(frame.date >= dates[0]) & (frame.date <= dates[-1])] for frame in (adj, raw)]
    # Saved bars contain zero-volume and nonpositive delisting rows. Production refuses those and
    # demands a fresh fetch; the replay keeps only rows complete in both vintages and counts the rest.
    positive = [frame.loc[(frame[FIELDS] > 0).to_numpy().all(axis=1), ["date", "symbol"]] for frame in (adj, raw)]
    usable = positive[0].merge(positive[1], on=["date", "symbol"])
    dropped = len(adj) + len(raw) - 2 * len(usable)
    adj, raw = [frame.merge(usable, on=["date", "symbol"]) for frame in (adj, raw)]
    validate_bars(adj, raw, day, config)
    panel = assemble_panel(adj, raw, frames["universe"], frames["shares"], day, config)
    return panel, {"bars_sha256": {name: file_hash(path) for name, path in paths.items()},
                   "bars_through": day, "rows_dropped_incomplete": int(dropped),
                   "adjusted_verified": False, "offline_read_only": True}


def simulated_eligibility(universe: pd.DataFrame, config: dict) -> dict:
    """Static saved flags: the replay cannot reconstruct point-in-time borrow availability."""
    hedge = set(config["hedge_instruments"])
    flags = {str(row.symbol): {"security_type": "ETF" if " ETF" in str(row.name) else "COMMON_STOCK",
                               "shortable": bool(row.shortable), "easy_to_borrow": bool(row.easy_to_borrow)}
             for row in universe.itertuples(index=False)}
    for name in hedge:
        flags[name] = {"security_type": "ETF", "shortable": True, "easy_to_borrow": True}
    return flags


def account_snapshot(config: dict, day: str, equity: float, initial: float, peak: float,
                     previous_close: float, weights: pd.Series, prices: pd.Series,
                     beta: pd.Series, returns: list[float], eligibility: dict) -> dict:
    positions = []
    for name, weight in weights.items():
        if weight == 0:
            continue
        price = number(float(prices.get(name, np.nan)), f"{name} close", positive=True)
        quantity = float(weight) * equity / price
        if not np.isfinite(quantity):
            raise Hold(f"Simulated snapshot position {name} has no finite quantity")
        value = float(beta.get(name, np.nan))
        positions.append({"symbol": name, "quantity": quantity, "price": price,
                          "beta": value if np.isfinite(value) else 0.0})
    return {"mode": config["mode"], "account_id": ACCOUNTS[config["mode"]], "account_type": "SIMULATE",
            "as_of": exchange().session_close(pd.Timestamp(day)).isoformat(), "equity": float(equity),
            "initial_equity": float(initial), "peak_equity": float(peak),
            "previous_close_equity": float(previous_close), "positions": positions,
            "recent_daily_returns": [float(value) for value in returns], "eligibility": eligibility}


def measured_beta(weights: pd.Series, hedges: dict, beta: pd.Series, config: dict) -> float:
    """Book beta measured here, with the research estimator, instead of trusting the scorer's number."""
    instruments = list(config["hedge_instruments"])
    b = beta.reindex(weights.index).fillna(1.0)
    if not np.isfinite(b).all():
        raise Hold("Missing beta for a targeted short")
    b = b.clip(config["beta_clip_low"], config["beta_clip_high"])
    hedge_beta = {name: float(beta.get(name, np.nan)) for name in instruments}
    hedge_beta[instruments[0]] = 1.0                     # the hedge numeraire is SPY itself
    for name in instruments[1:]:
        if not np.isfinite(hedge_beta[name]) or hedge_beta[name] <= 0:
            raise Hold(f"Missing usable 60-day beta to SPY for {name}")
    return float((weights * b).sum() + sum(hedges[name] * hedge_beta[name] for name in instruments))


def measured_margin(weights: pd.Series, hedges: dict, config: dict) -> float:
    long_gross = sum(value for value in hedges.values() if value > 0)
    short_gross = -float(weights.sum()) + sum(-value for value in hedges.values() if value < 0)
    return float(config["margin_long_factor"] * long_gross + config["margin_short_factor"] * short_gross)


def check_invariants(target: dict, config: dict, beta: pd.Series, initial: float) -> dict:
    """The frozen brief's replay assertions, measured from the published target and this beta series."""
    if target["status"] != "READY":
        raise Hold("check_invariants is only for READY targets")
    validate_targets(target, config)
    weights = pd.Series(target["weights"], dtype=float)
    hedges = {name: float(target[field]) for name, field in hedge_keys(config).items()}
    equity = float(target["sizing_equity"])
    cap = float(config["hedge_cap_of_initial_equity"]) * initial / equity
    gross = -float(weights.sum())
    margin = measured_margin(weights, hedges, config)
    residual = measured_beta(weights, hedges, beta, config)
    nonzero = int(target.get("model_nonzero_factors") or 0)
    guard = target["signal_guard"]
    breaches = [name for name, failed in (
        ("all_zero_traded", nonzero <= 0 or not any(value != 0 for value in target["model_coefficients"].values())),
        ("short_only", bool((weights >= 0).any())),
        ("name_count", len(weights) > int(config["n_shorts"])),
        ("gross_short", bool(target["rebalance_due"]) and gross > float(config["gross_short"]) + 1e-9),
        ("name_cap", float(weights.abs().max()) > float(config["max_name_weight"]) + 1e-9),
        ("hedge_cap", bool(target["rebalance_due"]) and any(abs(value) > cap + 1e-9 for value in hedges.values())),
        ("margin", margin > float(config["margin_cap"]) + 1e-9),
        ("residual_beta", bool(target["rebalance_due"]) and abs(residual) > float(config["max_abs_beta"]) + 1e-6),
        ("position_count", len(weights) + sum(1 for value in hedges.values() if value) > int(config["max_positions"])),
        ("signal_guard", guard.get("passed") is not True)) if failed]
    if breaches:
        raise Hold(f"Replay invariant breached ({', '.join(breaches)})")
    drift = not bool(target["rebalance_due"])
    return {"gross_short": gross, "short_names": int(len(weights)), "largest_short": float(weights.abs().max()),
            "margin_used": margin, "residual_beta": residual, "hedge_cap_of_equity": cap,
            "nonzero_factors": nonzero, "distinct_scores": int(guard["distinct_values"]),
            "drift_beyond_gross_cap": drift and gross > float(config["gross_short"]) + 1e-9,
            "drift_beyond_hedge_cap": drift and any(abs(value) > cap + 1e-9 for value in hedges.values())}


def walk(config: dict, panel: dict, beta: pd.DataFrame, eligibility: dict, days: pd.DatetimeIndex,
         asserted: int, cache_dir: Path | None = None, state_dir: Path | None = None,
         equity: float = 100000., inputs: dict | None = None, fixture: bool = False):
    """Score every session through make_targets on a simulated account, then account for the book."""
    validate_config(config)
    if len(days) < asserted or not days.is_unique or not days.is_monotonic_increasing:
        raise Hold("Replay sessions must be unique, ascending and cover the asserted window")
    opens, closes = panel["p"]["open"], panel["p"]["close"]
    if closes.index[-1] < days[-1]:
        raise Hold("Panel ends before the last replay session")
    returns = forward_returns(opens, days)
    hedges_in = list(config["hedge_instruments"])
    initial, peak, previous_close = float(equity), float(equity), float(equity)
    weights, history, rows, targets = pd.Series(dtype=float), [], [], []
    for day in days:
        signal = day.date().isoformat()
        priced = closes.loc[day].astype(float)
        held = weights[weights != 0]
        for name in held.index:
            if not np.isfinite(priced.get(name, np.nan)):
                raise Hold(f"Missing {signal} close for held {name}")
        snapshot = account_snapshot(config, signal, equity, initial, peak, previous_close, held,
                                    priced, beta.loc[day], history, eligibility)
        target = make_targets(config, snapshot, exchange().session_close(day).to_pydatetime(),
                              panel_asof(panel, signal, int(config["signal_horizon_days"])), inputs,
                              cache_dir, fixture, state_dir=state_dir)
        targets.append(target)
        if target["status"] == "READY":
            measured = check_invariants(target, config, beta.loc[day], initial)
            published = pd.Series(target["weights"], dtype=float)
            hedge_weights = {name: float(target[field]) for name, field in hedge_keys(config).items()}
            extra = pd.Series({name: value for name, value in hedge_weights.items() if value})
            target_weights = pd.concat([published, extra]) if len(extra) else published
        else:
            measured, target_weights = {}, weights.copy()    # HOLD keeps the drifted book, never flattens
        index = weights.index.union(target_weights.index)
        change = target_weights.reindex(index, fill_value=0.) - weights.reindex(index, fill_value=0.)
        stock = change.drop(hedges_in, errors="ignore")
        hedge_change = change.reindex(hedges_in).fillna(0.)
        orders = int((change.abs() > 1e-9).sum())
        cost = (float(stock.abs().sum()) * config["stock_slippage_bps"] / 1e4 +
                float(hedge_change.abs().sum()) * config["spy_slippage_bps"] / 1e4 +
                orders * config["order_platform_fee"] / equity)
        long_leg, short_leg = float(target_weights.clip(lower=0).sum()), float(-target_weights.clip(upper=0).sum())
        carry = (short_leg * config["short_borrow_apr"] +
                 max(0., long_leg - 1) * config["financing_apr"]) / 252
        realized = returns.loc[day].reindex(target_weights.index)
        if not np.isfinite(realized).all() or (realized <= -1).any():
            raise Hold(f"Missing/invalid realized return for {signal}; replay refuses zero filling")
        gross_return = float((target_weights * realized).sum())
        net = gross_return - cost - carry
        previous_close, equity = equity, equity * (1 + net)
        peak = max(peak, equity)
        rows.append({"signal_date": signal,
                     "entry_date": exchange().next_session(day).date().isoformat(),
                     "exit_date": exchange().next_session(exchange().next_session(day)).date().isoformat(),
                     "in_window": bool(day >= days[-asserted]), "status": target["status"],
                     "rebalance_due": bool(target["rebalance_due"]), "hold_reason": target.get("hold_reason", ""),
                     "gross": gross_return, "net": net, "cost": cost, "carry": carry,
                     "equity": max(0., equity), "orders": orders, "turnover": float(change.abs().sum()) / 2,
                     **{name: measured.get(name, np.nan) for name in COLUMNS},
                     "vol_multiplier": float(target["vol_multiplier"]),
                     "spy_hedge_weight": float(target["spy_hedge_weight"]),
                     "qqq_hedge_weight": float(target.get("qqq_hedge_weight", 0.)),
                     "model_fit_date": target.get("model_fit_date") or "",
                     "carried_forward": bool(target.get("model_carried_forward", False)),
                     "target_checksum": target["checksum"]})
        if equity <= 0:
            raise Hold("Simulated equity was wiped; the replay stops rather than trade on zero equity")
        history.append(net)
        weights = (target_weights * (1 + realized)).div(1 + net)
        weights = weights[weights != 0]
    return pd.DataFrame(rows).set_index("signal_date"), targets


def summarize(daily: pd.DataFrame, config: dict, initial_equity: float) -> dict:
    if daily.empty or not daily.in_window.any():
        raise Hold("Empty replay window")
    window = daily[daily.in_window]
    ready = window[window.status == "READY"]
    rebalanced = ready[ready.rebalance_due]
    curve = pd.Series([initial_equity, *daily.equity.tolist()])
    net = daily.net
    vol = float(net.std(ddof=0) * np.sqrt(252))
    breaches = [name for name, failed in (
        ("all_zero_traded", bool(len(ready)) and (ready.nonzero_factors <= 0).any()),
        ("name_count", bool(len(ready)) and ready.short_names.max() > config["n_shorts"]),
        ("gross_short", bool(len(rebalanced)) and rebalanced.gross_short.max() > config["gross_short"] + 1e-9),
        ("margin", bool(len(ready)) and ready.margin_used.max() > config["margin_cap"] + 1e-9),
        ("residual_beta", bool(len(rebalanced)) and
         rebalanced.residual_beta.abs().max() > config["max_abs_beta"] + 1e-6)) if failed]
    if breaches:
        raise Hold(f"Replay summary refuses breached invariants: {', '.join(breaches)}")
    return {"result_type": "frozen-book-validation-replay", "strategy_id": config["strategy_id"],
            "mode": config["mode"], "sessions_walked": int(len(daily)),
            "asserted_sessions": int(window.shape[0]), "start_signal": str(daily.index[0]),
            "asserted_start": str(window.index[0]), "end_signal": str(daily.index[-1]),
            "initial_equity": float(initial_equity), "final_equity": float(curve.iloc[-1]),
            "total_return": float(curve.iloc[-1] / initial_equity - 1), "annualized_vol": vol,
            "annualized_sharpe": float(net.mean() * 252 / vol) if vol > 0 else None,
            "max_drawdown": float((curve / curve.cummax() - 1).min()),
            "ready_sessions": int(len(ready)), "hold_sessions": int((window.status == "HOLD").sum()),
            "rebalance_ready": int(len(rebalanced)), "orders": int(daily.orders.sum()),
            "drift_sessions_over_gross_cap": int(ready[~ready.rebalance_due].gross_short.gt(
                config["gross_short"] + 1e-9).sum()),
            "drift_sessions_over_hedge_cap": int(ready[~ready.rebalance_due].drift_beyond_hedge_cap.sum()),
            "carried_forward_sessions": int(ready.carried_forward.sum()),
            "distinct_model_fits": int(ready.model_fit_date.nunique()),
            "max_gross_short": float(rebalanced.gross_short.max()) if len(rebalanced) else None,
            "max_margin_used": float(rebalanced.margin_used.max()) if len(rebalanced) else None,
            "max_abs_residual_beta": float(rebalanced.residual_beta.abs().max()) if len(rebalanced) else None,
            "invariant_breaches": breaches, "limitations": LIMITATIONS}


def write_validation(daily: pd.DataFrame, summary: dict, directory: Path) -> tuple[Path, Path]:
    allowed = (ROOT / "reports/validation").resolve()
    directory = directory.resolve()
    if directory != allowed and allowed not in directory.parents:
        raise ValueError("Replay output must be under reports/validation")
    directory.mkdir(parents=True, exist_ok=True)
    summary = seal({**summary, "daily_checksum": frame_hash(daily)})
    tag = summary["checksum"][:16]
    paths = directory / f"replay-{tag}.csv", directory / f"replay-{tag}.json"
    for path, contents in zip(paths, (daily.to_csv().encode(), canonical_bytes(summary) + b"\n")):
        if path.exists():
            if path.read_bytes() != contents:
                raise FileExistsError("Refusing to overwrite validation results")
        else:
            with path.open("xb") as handle:
                handle.write(contents)
    return paths


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("cached", "fixture"), default="cached")
    parser.add_argument("--sessions", type=int, default=30, help="Asserted trading days at the end of the walk")
    parser.add_argument("--bootstrap", type=int, default=60,
                        help="Extra scored sessions before the window, so carry-forward history exists")
    parser.add_argument("--end", help="Last panel session (default: the newest saved bar)")
    parser.add_argument("--equity", type=float, default=100000.)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--cache-dir", type=Path,
                        help="default: live/cache/replay (--source cached) or live/cache/fixture/replay (--source fixture)")
    parser.add_argument("--state-dir", type=Path, help="default: <cache-dir>/state")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "reports/validation")
    args = parser.parse_args(argv)
    if args.sessions < 1 or args.bootstrap < 0:
        parser.error("--sessions must be positive and --bootstrap nonnegative")
    # The synthetic panel's dates overlap the cached ones, so sharing one carry-forward state file
    # would let a fixture coefficient vector be carried forward into a real-bar replay.
    if args.cache_dir is None:
        args.cache_dir = ROOT / ("live/cache/fixture/replay" if args.source == "fixture" else "live/cache/replay")
    if args.state_dir is None:
        args.state_dir = args.cache_dir / "state"
    config = load_config(args.config)
    data_dir = args.data_dir.resolve()
    try:
        validate_config(config)
        if args.source == "fixture":
            day = args.end or "2026-11-18"
            panel, snapshot = fixture_panel(day)
            close = panel["p"]["close"]
            beta = market_beta(close, close["SPY"].pct_change(), int(config["beta_window"]))
            eligibility = snapshot["eligibility"]
            inputs = {"source": SYNTHETIC_SOURCE, "panel": frame_hash(close)}
        else:
            if args.end:
                day = args.end
            else:
                dates = pd.to_datetime(pd.read_parquet(data_dir / "bars_daily.parquet", columns=["date"])["date"])
                if dates.dt.tz is not None:
                    dates = dates.dt.tz_convert("America/New_York").dt.tz_localize(None)
                day = str(dates.max().date())
            panel, inputs = cached_panel(config, day, data_dir)
            close = panel["p"]["close"]
            beta = market_beta(close, close["SPY"].pct_change(), int(config["beta_window"]))
            eligibility = simulated_eligibility(pd.read_parquet(data_dir / "universe.parquet"), config)
        days = close.index[-(args.sessions + args.bootstrap + 2):-2]
        daily, targets = walk(config, panel, beta, eligibility, days, args.sessions, args.cache_dir,
                              args.state_dir, args.equity, inputs, args.source == "fixture")
        summary = summarize(daily, config, args.equity)
        paths = write_validation(daily, {**summary, "config_checksum": digest(config), "inputs": inputs,
                                         "target_checksums": [target["checksum"] for target in targets]},
                                args.output)
    except (Hold, ValueError, KeyError, OSError) as exc:
        print(f"HOLD: {exc}")
        return 2
    print(f"Validation only ({summary['ready_sessions']} READY / {summary['hold_sessions']} HOLD of "
          f"{summary['asserted_sessions']} asserted of {summary['sessions_walked']} walked): "
          f"{', '.join(str(path) for path in paths)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
