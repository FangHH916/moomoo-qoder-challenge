"""Frozen strategy scorer: short alpha (20 names, 35% gross) plus a SPY/QQQ beta hedge.

The factor, cleaning and target definitions are imported from ``research/`` so live and backtest
cannot drift; the model, carry-forward, signal guard and book follow ``reports/lab/FINAL.json``
(trial ``R4_E11cf_shortspy_n20``) at half the researched gross. Never places orders.

Run:  python -m live.daily_scores --fixture
"""
from __future__ import annotations

import argparse
import contextlib
import gc
import hashlib
import io
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from live.contracts import (ROOT, SCHEMA_VERSION, STRATEGY_ID, canonical_bytes, digest, finite,
                            frozen_factor_names, hedge_keys, load_config, seal, timestamp, write_targets)
from rules.trading_calendar import completed_session, exchange, rebalance_signal

RESEARCH = ROOT / "research"
if str(RESEARCH) not in sys.path:
    sys.path.insert(0, str(RESEARCH))
# The research definitions are the specification: imported, never copied into this module.
from factors import compute_all, market_beta                     # noqa: E402  research/factors.py
from lab_factors import library                                  # noqa: E402  research/lab_factors.py
from pipeline import preprocess                                  # noqa: E402  research/pipeline.py
from pipeline_v3 import extra_factors, residual_target           # noqa: E402  research/pipeline_v3.py

FIELDS = ["open", "high", "low", "close", "volume", "vwap", "trades"]
ACCOUNTS = {"test": "5322438", "competition": "5832812"}
STATE = ROOT / "live" / "state"
HISTORY = 620        # warmup + 252 mature training sessions + target maturity + smoothing tail
WARMUP = 260         # longest factor lookback (mom12_1/high52 need 253 sessions)
SYNTHETIC_SOURCE = "deterministic-synthetic"
LASSO_ALPHAS, LASSO_MAX_ITER, PREPROCESS_BATCH = 30, 10000, 8
LIMITATIONS = [
    "Research row-based TimeSeriesSplit(5) can split a session; no purge/embargo (CV leakage limitation).",
    "Static research universe/SEC shares with 60-day lag retain survivorship and revision limitations.",
    "Tradability uses today's broker flags for the whole training window, as the research backtest did.",
    "Production requires all 252 mature training sessions to have >=100 tradable names; sparse windows HOLD.",
    "DC factors are trade-size/close-location approximations, not Level-2 money flow.",
    "Broker snapshot borrow flags, not static research flags, determine short eligibility; the gate rechecks.",
    "EOD snapshot may be one session old; execution requires a separate fresh account/quote gate.",
    "The QQQ residual hedge and the margin cap are deployment additions absent from the research backtest.",
    "Incremental adjustment checks use overlap and historical sentinels, not a corporate-action feed.",
    "An anchored fit is reused for its whole five-session block; revised bars inside a block cannot "
    "change the vector that anchor published, and the bar guard HOLDs instead.",
    "Deployed gross short is half the researched 70% (35%); holdout Sharpe was -0.30, deflated 0.435.",
]


class Hold(ValueError):
    """An input is unsafe or insufficient for executable targets."""


def symbol(value: str) -> str:
    if not isinstance(value, str):
        raise Hold("Invalid symbol")
    value = value.removeprefix("US.")
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]*", value):
        raise Hold("Invalid symbol")
    return value


def number(value, label: str, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not np.isfinite(value):
        raise Hold(f"Invalid {label}")
    if positive and value <= 0:
        raise Hold(f"Nonpositive {label}")
    return float(value)


def sessions(end: str, count: int) -> pd.DatetimeIndex:
    days = exchange().sessions_in_range(exchange().first_session, end)
    if len(days) < count:
        raise Hold("Insufficient exchange history")
    return pd.DatetimeIndex(days[-count:]).tz_localize(None)


def fit_day(day: str) -> str:
    dates = exchange().sessions_in_range("2021-11-18", day)
    if not len(dates):
        raise Hold("Signal precedes the frozen strategy anchor")
    return dates[((len(dates) - 1) // 5) * 5].date().isoformat()


def validate_config(config: dict) -> None:
    fixed = {
        "schema_version": 2, "strategy_id": STRATEGY_ID, "factor_set": "frozen56",
        "train_days": 252, "retrain_every": 5, "cv_splits": 5, "cv_policy": "research-row-time-series",
        "max_training_rows": 300000, "signal_horizon_days": 5, "target_maturity_days": 6,
        "carry_forward": True, "signal_guard_min_distinct": 50, "min_fit_samples_per_session": 100,
        "rebalance_every": 5, "signal_anchor": "2021-11-18", "smooth_days": 5,
        "n_shorts": 20, "buffer_rank": 60, "gross_short": .35, "researched_gross_short": .70,
        "single_stock_longs_allowed": False, "hedge_instruments": ["SPY", "QQQ"],
        "hedge_cap_of_initial_equity": .38, "competition_name_initial_equity_cap": .40,
        "max_name_weight": .10, "inverse_vol_floor": .005, "name_weight_cap_multiple": 3.0,
        "target_vol": .10, "vol_window": 20, "vol_floor": .30, "beta_window": 60, "max_abs_beta": .10,
        "beta_clip_low": -1.0, "beta_clip_high": 3.0, "margin_long_factor": .5,
        "margin_short_factor": .6, "margin_cap": .90, "min_price": 1., "min_market_cap": 5e8,
        "min_dollar_volume_20d": 5e6, "min_short_market_cap": 1e9, "max_positions": 100,
        "options_enabled": False,
    }
    for key, expected in fixed.items():
        if config.get(key) != expected:
            raise Hold(f"Unapproved frozen configuration: {key}")
    if config.get("mode") not in ACCOUNTS:
        raise Hold("Unknown account mode")
    if config["mode"] == "test" and config.get("execution_enabled") is not False:
        raise Hold("Test execution must be disabled")
    names = frozen_factor_names(config)
    if len(names) != 56:
        raise Hold(f"factor_set {config['factor_set']} declares {len(names)} names, expected 56")


def validate_snapshot(snapshot: dict | None, config: dict, now: datetime, day: str) -> pd.Series:
    if not isinstance(snapshot, dict):
        raise Hold("Explicit EOD --snapshot is required; flat holdings are never assumed")
    mode = config["mode"]
    if snapshot.get("mode") != mode or str(snapshot.get("account_id")) != ACCOUNTS[mode]:
        raise Hold("Snapshot account/mode mismatch")
    if snapshot.get("account_type") != "SIMULATE":
        raise Hold("Only the designated SIMULATE account is supported")
    as_of = timestamp(snapshot["as_of"])
    if as_of > now:
        raise Hold("Future snapshot")
    snap_day = as_of.astimezone(exchange().tz).date().isoformat()
    allowed = sessions(day, 2).strftime("%Y-%m-%d")
    if snap_day not in allowed or completed_session(as_of) != snap_day:
        raise Hold("Snapshot must be EOD of the signal or previous session")
    for name in ("equity", "initial_equity", "previous_close_equity", "peak_equity"):
        number(snapshot[name], name, positive=True)
    if snapshot["peak_equity"] < snapshot["equity"]:
        raise Hold("Peak equity is below current equity")
    if not isinstance(snapshot.get("positions"), list) or not isinstance(snapshot.get("eligibility"), dict):
        raise Hold("Snapshot requires positions and per-symbol broker eligibility")
    if not isinstance(snapshot.get("recent_daily_returns"), list):
        raise Hold("Snapshot requires recent_daily_returns")
    for value in snapshot["recent_daily_returns"]:
        if number(value, "daily return") <= -1:
            raise Hold("Invalid daily return")
    weights = {}
    for position in snapshot["positions"]:
        name = symbol(position["symbol"])
        if name in weights:
            raise Hold("Duplicate snapshot position")
        quantity = number(position["quantity"], "quantity")
        price = number(position["price"], "price", positive=True)
        number(position["beta"], "position beta")
        weights[name] = quantity * price / snapshot["equity"]
    return pd.Series(weights, dtype=float)


def broker_eligibility(snapshot: dict) -> dict:
    result = {}
    for name, flags in snapshot["eligibility"].items():
        name = symbol(name)
        if name in result or not isinstance(flags, dict):
            raise Hold("Invalid/duplicate broker eligibility")
        result[name] = flags
    return result


def borrow_confirmed(flags: dict | None) -> bool:
    return bool(flags and flags.get("shortable") is True and flags.get("easy_to_borrow") is True)


def eligible(flags: dict | None, short: bool = False) -> bool:
    if not flags or flags.get("security_type") != "COMMON_STOCK":
        return False
    return not short or borrow_confirmed(flags)


def tradable_mask(config: dict, flags: dict, mcap: pd.Series, columns: pd.Index) -> pd.Series:
    """Frozen tradable universe: eligible common stock, confirmed borrow, market cap >= $1B."""
    hedge = set(config["hedge_instruments"])
    mcap = mcap.reindex(columns).astype(float)          # float32 panels are not Python floats
    ok = [c not in hedge and eligible(flags.get(c), short=True)
          and finite(mcap[c]) and mcap[c] >= config["min_short_market_cap"] for c in columns]
    return pd.Series(ok, index=columns, dtype=bool)


def vol_multiplier(returns: list[float], config: dict) -> float:
    window = int(config["vol_window"])
    if len(returns) < window:
        return 1.0
    rv = float(np.std(returns[-window:]) * np.sqrt(252))
    return float(np.clip(config["target_vol"] / max(rv, 1e-6), config["vol_floor"], 1.0))


def smooth_scores(scores: pd.DataFrame, config: dict) -> pd.DataFrame:
    return scores.rolling(int(config["smooth_days"]), min_periods=1).mean().where(scores.notna())


def signal_guard(row: pd.Series, config: dict) -> dict:
    """Reject a day without a real cross-sectional ranking. The all-zero LASSO fits produced a
    constant score, which made rank ties select names by index/alphabetical order; those days now
    HOLD existing positions instead of inventing a book."""
    s = row.replace([np.inf, -np.inf], np.nan).dropna()
    distinct = int(s.nunique())
    std = float(s.std()) if len(s) > 1 else 0.0
    return {"passed": bool(distinct >= int(config["signal_guard_min_distinct"]) and std > 1e-12),
            "distinct_values": distinct, "std": std, "scored_names": int(len(s)),
            "min_distinct_required": int(config["signal_guard_min_distinct"])}


# ------------------------------------------------------------------ book construction
def select_shorts(scores: pd.Series, previous: pd.Series, tradable: pd.Series, vol: pd.Series,
                  config: dict, mult: float) -> pd.Series:
    """Short the n lowest-scored tradable names with inverse-60d-vol weights capped at 3x equal
    weight, a rank buffer, drifted weights kept for held names, and the volatility multiplier only
    on new entries. Never produces a long single-stock position."""
    s = scores.where(tradable).dropna().sort_index()
    n, gross = int(config["n_shorts"]), float(config["gross_short"])
    if len(s) < 4 * n:
        raise Hold(f"Insufficient tradable short coverage ({len(s)} names, need {4 * n})")
    rank = s.rank(ascending=True)                        # 1 = lowest score = most attractive short
    order = rank.sort_values(kind="stable")              # residual exact ties break by symbol
    held = [c for c in previous[previous < 0].index if c in rank.index and rank[c] <= config["buffer_rank"]]
    shorts = (held + [c for c in order.index if c not in held])[:n]
    v = vol.reindex(shorts).clip(lower=config["inverse_vol_floor"])
    iv = 1.0 / v.fillna(v.median())
    if not np.isfinite(iv).all() or (iv <= 0).any():
        raise Hold("Invalid 60-day volatility for the selected shorts")
    w = -(iv / iv.sum() * gross)
    cap = gross / n * config["name_weight_cap_multiple"]
    w = w.clip(lower=-cap)
    w = w / -w.sum() * gross
    kept = w.index.intersection(previous.index)
    w[kept] = previous[kept].where(previous[kept] < 0, w[kept])
    new = w.index.difference(previous.index)
    w[new] = w[new] * mult
    if (w > 0).any():
        raise Hold("Book construction produced a long single-stock position")
    return w.astype(float)


def allocate_hedge(shorts: pd.Series, beta: pd.Series, config: dict, equity: float,
                   initial: float) -> tuple[dict, dict]:
    """Beta-neutral with hedge ETFs only: SPY first, the remainder in QQQ sized by QQQ's 60-day
    beta to SPY, each capped at 38% of initial assets. hedge_scale < 1 means the caps bind and the
    whole book has to shrink (item 7b)."""
    b = beta.reindex(shorts.index).fillna(1.0)
    if not np.isfinite(b).all():
        raise Hold("Missing finite beta for a selected short")     # a zero-variance beta would clip to a number
    b = b.clip(config["beta_clip_low"], config["beta_clip_high"])
    instruments = list(config["hedge_instruments"])
    hedge_beta = {name: float(beta.get(name, np.nan)) for name in instruments}
    hedge_beta[instruments[0]] = 1.0                     # the hedge numeraire is SPY itself
    for name in instruments[1:]:
        if not finite(hedge_beta[name]) or hedge_beta[name] <= 0:
            raise Hold(f"Missing usable 60-day beta to SPY for {name}")
    book_beta = float((shorts * b).sum())
    needed = -book_beta
    cap = float(config["hedge_cap_of_initial_equity"]) * initial / equity
    remaining, hedges = needed, {}
    for name in instruments[:-1]:
        take = float(np.clip(remaining, -cap, cap))
        hedges[name] = take
        remaining -= take
    last = instruments[-1]
    hedges[last] = float(np.clip(remaining / hedge_beta[last], -cap, cap))
    unhedged = needed - sum(w * hedge_beta[n] for n, w in hedges.items())
    scale = 1.0 if abs(unhedged) <= 1e-9 else float(np.clip(cap * (1.0 + hedge_beta[last]) / abs(needed),
                                                            0.0, 1.0))
    info = {"hedge_target_beta": needed, "book_beta_before_hedge": book_beta, "hedge_scale": scale,
            "hedge_cap_of_equity": cap, "residual_unhedged_beta": unhedged, "hedge_betas": hedge_beta}
    return hedges, info


def margin_scale(shorts: pd.Series, hedges: dict, config: dict) -> tuple[float, dict]:
    """Competition margin usage: 50% of long gross plus 60% of short gross, kept under 90%."""
    long_gross = sum(max(w, 0.0) for w in hedges.values())
    short_gross = -float(shorts.sum()) + sum(max(-w, 0.0) for w in hedges.values())
    used = float(config["margin_long_factor"] * long_gross + config["margin_short_factor"] * short_gross)
    scale = min(1.0, config["margin_cap"] / used) if used > 0 else 1.0
    return scale, {"margin_used": used, "margin_cap": config["margin_cap"],
                   "long_gross_including_hedges": long_gross, "short_gross_including_hedges": short_gross}


def build_book(scores: pd.Series, previous: pd.Series, tradable: pd.Series, vol: pd.Series,
               beta: pd.Series, config: dict, mult: float, equity: float,
               initial: float) -> tuple[pd.Series, dict, dict]:
    """Short book, hedge ETF caps and the margin cap, in the order the frozen spec states them."""
    shorts = select_shorts(scores, previous.drop(config["hedge_instruments"], errors="ignore"),
                           tradable, vol, config, mult)
    for _ in range(3):
        hedges, hedge_info = allocate_hedge(shorts, beta, config, equity, initial)
        if hedge_info["hedge_scale"] >= 1.0 - 1e-12:
            break
        shorts = shorts * hedge_info["hedge_scale"]      # caps bind: shrink the whole book
    else:
        raise Hold("Hedge ETF capacity never converged")
    scale, margin = margin_scale(shorts, hedges, config)
    shorts, hedges = shorts * scale, {k: v * scale for k, v in hedges.items()}
    gross = -float(shorts.sum())
    info = {"gross_short": gross, "short_names": int(len(shorts)),
            "largest_short": float(shorts.abs().max()), "margin_scale": scale, **hedge_info, **margin}
    if gross > config["gross_short"] + 1e-9:
        raise Hold("Gross short breaches the frozen 35% limit")
    return shorts, hedges, info


def keep_drifted(previous: pd.Series, config: dict, flags: dict) -> tuple[pd.Series, dict]:
    """Outside an anchored rebalance the book is not re-sized; every held short still needs a
    current broker borrow confirmation, otherwise the whole target HOLDs."""
    shorts = previous.drop(config["hedge_instruments"], errors="ignore")
    hedges = {name: float(previous.get(name, 0.0)) for name in config["hedge_instruments"]}
    if shorts.empty and not any(hedges.values()):
        # A flat account has nothing to restate; READY would publish an empty plan and imply trading.
        raise Hold("No holdings outside an anchored rebalance; wait for the next rebalance session")
    for name, weight in shorts.items():
        if weight > 0:
            raise Hold(f"Frozen strategy holds no long position in {name}")
        if weight < 0 and not borrow_confirmed(flags.get(name)):
            raise Hold(f"Held short {name} lacks a current broker borrow confirmation")
    return shorts, hedges


# ------------------------------------------------------------------ data plumbing
def normalize_bars(frame: pd.DataFrame) -> pd.DataFrame:
    needed = ["date", "symbol", *FIELDS]
    if not set(needed).issubset(frame.columns):
        raise Hold("Bars lack required OHLCV/vwap/trades columns")
    frame = frame[needed].copy()
    dates = pd.to_datetime(frame["date"])
    if dates.dt.tz is not None:
        dates = dates.dt.tz_convert("America/New_York").dt.tz_localize(None)
    frame["date"] = dates.dt.normalize()
    frame["symbol"] = frame["symbol"].map(symbol)
    return frame.sort_values(["date", "symbol"]).reset_index(drop=True)


def validate_bars(adj: pd.DataFrame, raw: pd.DataFrame, day: str, config: dict, minimum: int = 100) -> None:
    hedge = set(config["hedge_instruments"])
    if adj.empty or raw.empty:
        raise Hold("Empty bars")
    for frame in (adj, raw):
        if frame.duplicated(["date", "symbol"]).any():
            raise Hold("Duplicate bars")
        valid_dates = exchange().sessions_in_range(frame.date.min(), day)
        if not frame.date.isin(valid_dates).all() or frame.date.max() != pd.Timestamp(day):
            raise Hold("Stale, future, or non-session bars")
        values = frame[FIELDS].to_numpy(float)
        if not np.isfinite(values).all() or (values <= 0).any():
            raise Hold("Incomplete/nonpositive bars; explicit --update required")
        if ((frame.high < frame[["open", "close", "low"]].max(axis=1)) |
                (frame.low > frame[["open", "close", "high"]].min(axis=1))).any():
            raise Hold("Invalid OHLC ordering")
    if not adj[["date", "symbol"]].equals(raw[["date", "symbol"]]):
        raise Hold("Raw and adjusted bars are not aligned")
    latest = set(adj.loc[adj.date == pd.Timestamp(day), "symbol"])
    if not hedge.issubset(latest) or len(latest - hedge) < minimum:
        raise Hold(f"Stale/inadequate latest stock bars or missing hedge in {sorted(hedge)}")


def frame_hash(frame: pd.DataFrame | pd.Series) -> str:
    hashed = pd.util.hash_pandas_object(frame, index=True).to_numpy().tobytes()
    columns = list(map(str, frame.columns)) if isinstance(frame, pd.DataFrame) else [str(frame.name)]
    return hashlib.sha256(canonical_bytes(columns) + hashed).hexdigest()


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def assemble_panel(adj: pd.DataFrame, raw: pd.DataFrame, meta: pd.DataFrame,
                   shares_q: pd.DataFrame, day: str, config: dict) -> dict:
    """Port of panel.build_panel with an explicit clock, bounded input and no DATA mutation."""
    idx = sessions(day, HISTORY)
    hedge = list(config["hedge_instruments"])
    cols = pd.Index(sorted((set(meta.symbol) | set(hedge)) & set(adj.symbol)))

    def wide(frame, field):
        return frame.pivot(index="date", columns="symbol", values=field).reindex(index=idx, columns=cols)

    p = {f: wide(adj, f) for f in FIELDS}
    o, c = p["open"], p["close"]
    p["open"] = o.mask(((o / c.shift(1) - 1).abs() > .5) & ((o / c - 1).abs() > .5))
    raw_close, raw_vol, raw_vwap = (wide(raw, f) for f in ("close", "volume", "vwap"))
    sh = shares_q.merge(meta[["symbol", "cik"]], on="cik")
    sh["avail"] = pd.to_datetime(sh["period_end"]) + pd.Timedelta(days=60)
    sh = sh.sort_values("avail").drop_duplicates(["symbol", "avail"], keep="last")
    sw = sh.pivot(index="avail", columns="symbol", values="shares")
    sw = sw.reindex(sw.index.union(idx)).sort_index().ffill().reindex(index=idx, columns=cols)
    p.update(amt=raw_vwap * raw_vol, mcap=raw_close * sw, tvr=raw_vol / sw, raw_close=raw_close)
    industry = meta.drop_duplicates("symbol").set_index("symbol")["industry"].reindex(cols)
    u = ((raw_close >= config["min_price"]) & (p["mcap"] >= config["min_market_cap"]) &
         (p["amt"].rolling(20, min_periods=15).median() >= config["min_dollar_volume_20d"])
         & c.notna() & sw.notna())
    u.loc[:, industry[industry.isna()].index] = False
    missing = [name for name in hedge if name not in c.columns]
    if missing:
        raise Hold(f"Missing hedge instrument history: {missing}")
    u.loc[:, hedge] = False
    horizon = int(config["signal_horizon_days"])
    p["fwd_ret"] = p["open"].shift(-2) / p["open"].shift(-1) - 1
    p["fwd_horizon"] = p["open"].shift(-(horizon + 1)) / p["open"].shift(-1) - 1
    return {"p": {k: v.astype("float32") for k, v in p.items()}, "U": u, "industry": industry}


def cleaned_factors(raw: dict, u: pd.DataFrame, industry: pd.Series, mcap: pd.DataFrame,
                    names: list[str]) -> dict:
    """research.pipeline.preprocess in batches so the 56-frame stack never exists twice."""
    out: dict[str, pd.DataFrame] = {}
    keys = list(names)
    for i in range(0, len(keys), PREPROCESS_BATCH):
        part = {k: raw.pop(k) for k in keys[i:i + PREPROCESS_BATCH]}
        out.update(preprocess(part, u, industry, mcap))
        del part
        gc.collect()
    return out


# ------------------------------------------------------------------ model
def training_dates(dates: pd.DatetimeIndex, day: str, count: int = 252, horizon: int = 5) -> pd.DatetimeIndex:
    """Trailing mature sessions: a 5-day target measured at s is only usable from s+horizon+1."""
    t = dates.get_loc(pd.Timestamp(day))
    hi = t - 1 - horizon
    if hi - count < 0:
        raise Hold("Insufficient trailing mature-label sessions")
    return dates[hi - count:hi]


def training_rows(features: dict, target: pd.DataFrame, tradable: pd.DataFrame, dates: pd.DatetimeIndex,
                  names: list[str], config: dict):
    xs, ys, used = [], [], []
    minimum, maximum = int(config["min_fit_samples_per_session"]), int(config["max_training_rows"])
    for date in dates:
        x = np.column_stack([features[n].loc[date].to_numpy(np.float32) for n in names])
        y = target.loc[date].to_numpy(float)
        mask = tradable.loc[date].to_numpy(bool) & np.isfinite(y) & np.isfinite(x).all(1)
        if int(mask.sum()) < minimum:
            continue
        xs.append(x[mask])
        ys.append(y[mask])
        used.append(date.date().isoformat())
    if len(used) != len(dates):
        raise Hold(f"Training coverage: {len(used)}/{len(dates)} mature sessions with >= {minimum} names")
    x, y = np.vstack(xs).astype(np.float64), np.concatenate(ys).astype(np.float64)
    before = len(y)
    if before > maximum:
        selection = np.sort(np.random.default_rng(0).choice(before, maximum, replace=False))
        x, y = x[selection], y[selection]
    return x, y, {"train_dates": used, "train_start": used[0], "train_end": used[-1],
                  "training_rows": len(y), "training_rows_before_sample": before}


def fit_coefficients(x: np.ndarray, y: np.ndarray, names: list[str], config: dict) -> dict:
    from sklearn.linear_model import LassoCV
    from sklearn.model_selection import TimeSeriesSplit
    estimator = LassoCV(cv=TimeSeriesSplit(int(config["cv_splits"])), alphas=LASSO_ALPHAS,
                        max_iter=LASSO_MAX_ITER, fit_intercept=True)
    estimator.fit(x, y)
    coef = np.asarray(estimator.coef_, dtype=float)
    return {"coefficients": dict(zip(names, map(float, coef))), "alpha": float(estimator.alpha_),
            "intercept": float(estimator.intercept_), "nonzero_count": int((coef != 0).sum()),
            "all_zero": bool(not (coef != 0).any())}


def score_row(normalized: dict, raw: dict, tradable: pd.Series, coefficients: dict,
              names: list[str], minimum: int = 1) -> pd.Series:
    """Score only names the frozen model may short; require the raw factor value for every
    non-zero coefficient (research preprocessing imputes a missing raw factor to zero)."""
    if set(coefficients) != set(names):
        raise Hold("Model coefficient set is not the configured factor set")
    coef = np.array([coefficients[n] for n in names], dtype=float)
    active = np.flatnonzero(coef != 0)
    if not active.size:
        raise Hold("Model has no nonzero learned coefficients")
    columns = tradable.index
    x = np.column_stack([normalized[n].reindex(columns).to_numpy(float) for n in names])
    raw_active = np.column_stack([raw[names[j]].reindex(columns).to_numpy(float) for j in active])
    mask = tradable.to_numpy(bool) & np.isfinite(x).all(1) & np.isfinite(raw_active).any(1)
    if int(mask.sum()) < minimum:
        raise Hold("Score row has no tradable coverage")
    result = pd.Series(np.nan, index=columns, dtype=float)
    result.iloc[np.flatnonzero(mask)] = x[mask] @ coef
    return result


def carry_forward(fit_days: list[str], models: dict, entries: list[dict]) -> dict[str, dict]:
    """An all-zero retrain reuses the last non-zero coefficient vector (research
    lab_fix.rescore_carry_forward); with no non-zero fit in history the day cannot be traded."""
    resolved, prior = {}, None
    for fit in sorted(fit_days):
        record = models[fit]
        if record["all_zero"]:
            if prior is None:
                earlier = [e for e in entries if not e["all_zero"] and e["fit_date"] < fit]
                if not earlier:
                    raise Hold("All-zero fit with no earlier non-zero coefficients: HOLD, no new positions")
                prior = earlier[-1]
            resolved[fit] = {**prior, "carried_forward": True}
        else:
            prior = record
            resolved[fit] = {**record, "carried_forward": False}
    return resolved


# ------------------------------------------------------------------ state
def state_path(mode: str, directory: Path | None = None) -> Path:
    return (directory or STATE) / f"coefficients-{mode}.json"


def new_history(config: dict, mode: str) -> dict:
    return {"schema_version": SCHEMA_VERSION, "strategy_id": STRATEGY_ID, "mode": mode,
            "factor_names": frozen_factor_names(config), "entries": []}


def dataset_kind(inputs: dict | None) -> str:
    """Which kind of data a panel was built from, read from the provenance the run publishes."""
    given = inputs or {}
    if given.get("source") == SYNTHETIC_SOURCE:
        return SYNTHETIC_SOURCE
    if "bars_sha256" in given or "source" in given:
        return "real-bars"
    return "unlabeled"


def panel_identity(config: dict, inputs: dict | None) -> str:
    """Identity of the data source and factor definitions a persisted vector was fitted against.

    Training dates cannot separate two panels: the synthetic fixture walk is dated with real business
    days, so a fixture vector looks like a valid earlier anchor to a real-bar replay. The data source can.
    The symbol list is deliberately absent: an anchor is frozen for its whole block, so a name listing or
    delisting between sessions must not make the job refuse the vectors it already published.
    """
    return digest({"implementation": 3, "dataset": dataset_kind(inputs),
                   "factor_names": frozen_factor_names(config), "source_hashes": SOURCE_HASHES,
                   "config": digest(config)})


def load_history(path: Path, config: dict, mode: str, identity: str | None = None) -> dict:
    """Persisted fit history; a corrupt or foreign file is refused, never silently reset."""
    if not path.exists():
        history = new_history(config, mode)
        if identity is not None:
            history["panel_identity"] = identity
        return history
    value = _verified_json(path)
    names = frozen_factor_names(config)
    if (value.get("schema_version") != SCHEMA_VERSION or value.get("strategy_id") != STRATEGY_ID or
            value.get("mode") != mode or value.get("factor_names") != names or
            not isinstance(value.get("entries"), list)):
        raise Hold("Coefficient history is not from this strategy/mode/factor set")
    if identity is not None and value.get("panel_identity") != identity:
        raise Hold("Coefficient history was written against different data (synthetic vs real bars);"
                   " delete it deliberately")
    for entry in value["entries"]:
        non_zero = any(v != 0 for v in entry["coefficients"].values())
        if set(entry["coefficients"]) != set(names) or entry["all_zero"] == non_zero:
            raise Hold("Coefficient history entry is inconsistent")
    return value


def save_history(history: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(canonical_bytes(seal(history)) + b"\n")
    temp.replace(path)


def append_entries(history: dict, models: dict) -> dict:
    fresh = {e["fit_date"]: e for e in history["entries"]}
    fresh.update({record["fit_date"]: record for record in models.values()})
    history["entries"] = [fresh[date] for date in sorted(fresh)][-120:]
    return history


# ------------------------------------------------------------------ scoring
SOURCE_HASHES = {"research/factors.py": file_hash(RESEARCH / "factors.py"),
                 "research/lab_factors.py": file_hash(RESEARCH / "lab_factors.py"),
                 "research/pipeline.py": file_hash(RESEARCH / "pipeline.py"),
                 "research/pipeline_v3.py": file_hash(RESEARCH / "pipeline_v3.py"),
                 "live/daily_scores.py": file_hash(Path(__file__).resolve())}


def score_panel(panel: dict, day: str, config: dict, tradable_today: pd.Series,
                cache_dir: Path | None = None, history: dict | None = None):
    """Weekly anchored LASSO fits; one vector per anchor, published to every session in its block."""
    names = frozen_factor_names(config)
    p, u, industry = panel["p"], panel["U"], panel["industry"]
    horizon, smooth = int(config["signal_horizon_days"]), int(config["smooth_days"])
    score_days = u.index[-smooth:]
    fit_days = sorted({fit_day(d.date().isoformat()) for d in score_days})
    tradable = u & tradable_today.reindex(u.columns).fillna(False)
    models, paths, keys = {}, {}, {}
    persisted = {entry["fit_date"]: entry for entry in (history or {}).get("entries", [])}
    for fit in fit_days:
        index = u.index.get_loc(pd.Timestamp(fit))
        train = training_dates(u.index, fit, int(config["train_days"]), horizon)
        start = u.index.get_loc(train[0]) - WARMUP
        if start < 0:
            raise Hold("Insufficient factor warmup before the frozen training window")
        stop = index + 1
        inputs = {k: frame_hash(v.iloc[start:stop]) for k, v in p.items()
                  if k not in ("fwd_ret", "fwd_horizon")}
        key = digest({"implementation": 2, "factor_names": names, "source_hashes": SOURCE_HASHES,
                      "fit_day": fit, "config": digest(config), "inputs": inputs,
                      "universe": frame_hash(tradable.iloc[start:stop]), "industry": frame_hash(industry)})
        keys[fit] = {"checksum": key, "train": train, "start": start}
        if cache_dir is not None:
            paths[fit] = cache_dir / "models" / f"{fit}-{key}.json"
            if paths[fit].exists():
                model = _verified_json(paths[fit])
                if (model.get("input_checksum") != key or set(model["coefficients"]) != set(names) or
                        model.get("fit_date") != fit or
                        model.get("train_dates") != train.strftime("%Y-%m-%d").tolist()):
                    raise Hold("Cached model provenance mismatch")
                number(model["alpha"], "model alpha", positive=True)
                models[fit] = {k: v for k, v in model.items() if k != "checksum"}
    # An anchor is fitted once: inside its five-session block the persisted record is reused, because
    # each session's universe vintage re-selects the 300,000 training rows and would otherwise publish
    # a different vector for a session the frozen strategy already trained. Reuse therefore identifies
    # the record by data source and training window, not by the exact input hash (that stricter identity
    # belongs to the file cache above). `load_history` has already refused a state file written against
    # different data, which is what keeps a synthetic-fixture vector out of a real-bar replay.
    for fit in fit_days:
        record = persisted.get(fit)
        if (fit not in models and record is not None and set(record["coefficients"]) == set(names) and
                record.get("train_dates") == keys[fit]["train"].strftime("%Y-%m-%d").tolist()):
            models[fit] = {k: v for k, v in record.items() if k != "checksum"}
    # A scored session needs the longest factor lookback, not just the beta window: on a day whose
    # weekly fits are all cached, starting inside mom12_1/high52's 253-session requirement would
    # leave those factors NaN and score_row would refuse every name.
    tail_start = max(0, u.index.get_loc(score_days[0]) - WARMUP)
    needed = min([tail_start] + [keys[fit]["start"] for fit in fit_days if fit not in models])
    window = {k: v.iloc[needed:] for k, v in p.items()}
    u_window = tradable.iloc[needed:]
    # The research helpers print per-session progress; a scheduled job needs one clean status line.
    with contextlib.redirect_stdout(io.StringIO()):
        spy_ret = window["close"]["SPY"].pct_change()
        beta_panel = market_beta(window["close"], spy_ret, int(config["beta_window"]))
        raw = dict(compute_all(window))
        raw.update(extra_factors(window["close"], spy_ret))
        raw.update(library(window, industry, spy_ret, beta_panel))
        if set(raw) != set(names):
            raise Hold(f"Research factor definitions changed: {len(raw)} names vs configured {len(names)}")
        raw_tail = {n: raw[n].reindex(score_days) for n in names}
        features = cleaned_factors(raw, u_window, industry, window["mcap"], names)
        target = residual_target(window["fwd_horizon"].where(u_window), u_window, industry,
                                 window["mcap"], beta_panel)
    del raw
    gc.collect()
    for fit in fit_days:
        if fit in models:
            continue
        x, y, info = training_rows(features, target, u_window, keys[fit]["train"], names, config)
        models[fit] = {"fit_date": fit, "input_checksum": keys[fit]["checksum"], **info,
                       "cv_policy": config["cv_policy"], "factor_names": names,
                       **fit_coefficients(x, y, names, config)}
        if cache_dir is not None:
            _cache_json(paths[fit], models[fit])
    history = append_entries(history or new_history(config, config["mode"]), models)
    used = carry_forward(fit_days, models, history["entries"])
    scores = pd.DataFrame(np.nan, index=score_days, columns=u.columns)
    for date in score_days:
        model = used[fit_day(date.date().isoformat())]
        normalized = {n: features[n].loc[date] for n in names}
        raw_row = {n: raw_tail[n].loc[date] for n in names}
        scores.loc[date] = score_row(normalized, raw_row, tradable.loc[date], model["coefficients"], names)
    today = used[fit_day(day)]
    coverage = {"score_sessions": [d.date().isoformat() for d in score_days],
                "smoothing_model_dates": {d.date().isoformat(): fit_day(d.date().isoformat())
                                          for d in score_days},
                "scored_names": int(scores.iloc[-1].notna().sum()),
                "tradable_names": int(tradable_today.reindex(u.columns).fillna(False).sum()),
                "used_fit_date": today["fit_date"], "used_nonzero_factors": today["nonzero_count"],
                "source_hashes": SOURCE_HASHES}
    beta = beta_panel.iloc[-1].astype(float)
    # research market_beta divides a biased covariance by an unbiased variance, so SPY's own beta
    # is (n-1)/n; the hedge itself is priced in SPY-return units (allocate_hedge pins it to 1.0).
    if not np.isfinite(beta.get("SPY", np.nan)) or not 0.9 <= beta["SPY"] <= 1.0001:
        raise Hold("SPY beta numeraire is unusable over the frozen window")
    vol = window["close"].pct_change().rolling(60, min_periods=40).std().iloc[-1].astype(float)
    smoothed = smooth_scores(scores, config).iloc[-1]
    return smoothed, beta, vol, today, coverage, history


def _cache_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(canonical_bytes(seal(value)) + b"\n")
    temp.replace(path)


def _verified_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("checksum") != seal(value)["checksum"]:
        raise Hold("Cache checksum mismatch")
    return value


# ------------------------------------------------------------------ bar cache
def _fetch_bars(symbols: list[str], start: str, end: str, adjustment: str, getter=None) -> pd.DataFrame:
    # This is the ONLY path importing the credential-loading client; --update opts in.
    if getter is None:
        from research.alpaca_client import get
        getter = get
    rows = []
    for i in range(0, len(symbols), 100):
        params = {"symbols": ",".join(symbols[i:i + 100]), "timeframe": "1Day", "start": start,
                  "end": end, "adjustment": adjustment, "feed": "sip", "limit": 10000, "sort": "asc"}
        seen = set()
        while True:
            response = getter("https://data.alpaca.markets/v2/stocks/bars", params=params)
            response.raise_for_status()
            value = response.json()
            for name, bars in (value.get("bars") or {}).items():
                rows.extend({**bar, "symbol": name} for bar in bars)
            token = value.get("next_page_token")
            if not token:
                break
            if token in seen:
                raise Hold("Repeated data page token")
            seen.add(token)
            params["page_token"] = token
    frame = pd.DataFrame(rows).rename(columns={"t": "date", "o": "open", "h": "high", "l": "low",
                                               "c": "close", "v": "volume", "n": "trades", "vw": "vwap"})
    if frame.empty:
        raise Hold("Data provider returned no bars")
    return normalize_bars(frame)


def adjustment_consistent(old: pd.DataFrame, new: pd.DataFrame, required: set[str]) -> bool:
    keys = ["date", "symbol"]
    overlap = old.merge(new, on=keys, suffixes=("_old", "_new"))
    if not required.issubset(set(overlap.symbol)):
        return False
    a = overlap[[f + "_old" for f in FIELDS]].to_numpy(float)
    b = overlap[[f + "_new" for f in FIELDS]].to_numpy(float)
    return bool(np.isclose(a, b, rtol=1e-6, atol=1e-6).all())


def incremental_start(adj: pd.DataFrame, raw: pd.DataFrame, day: str, config: dict) -> str:
    through = min(day, adj.date.max().date().isoformat())
    start = sessions(through, 5)[0]
    dates = sessions(day, HISTORY)
    present = adj.assign(present=True).pivot(index="date", columns="symbol", values="present")
    present = present.reindex(index=dates).fillna(False).astype(bool)
    first = adj.groupby("symbol").date.min().reindex(present.columns)
    expected = dates.to_numpy()[:, None] >= first.to_numpy()[None, :]
    missing = (expected & ~present.to_numpy()).any(axis=1)
    if missing.any():
        start = min(start, dates[np.flatnonzero(missing)[0]])
    pair = adj.merge(raw, on=["date", "symbol"], how="outer", indicator=True, suffixes=("_a", "_r"))
    bad = pair["_merge"] != "both"
    for suffix in ("_a", "_r"):
        v = pair[[f + suffix for f in FIELDS]].to_numpy(float)
        bad |= (~np.isfinite(v) | (v <= 0)).any(axis=1)
        bad |= (pair["high" + suffix] < pair[[f + suffix for f in ("open", "close", "low")]].max(axis=1))
        bad |= (pair["low" + suffix] > pair[[f + suffix for f in ("open", "close", "high")]].min(axis=1))
    if bad.any():
        start = min(start, pair.loc[bad, "date"].min())
    return max(start, dates[0]).date().isoformat()


def load_live_panel(day: str, config: dict, cache_dir: Path, update: bool = False,
                    full_refresh_adjusted: bool = False, invalidate_adjusted: bool = False) -> tuple[dict, dict]:
    data = ROOT / "data"
    meta_path, shares_path = data / "universe.parquet", data / "shares_q.parquet"
    meta, shares = pd.read_parquet(meta_path), pd.read_parquet(shares_path)
    dates = sessions(day, HISTORY)
    hedge = list(config["hedge_instruments"])
    symbols = sorted(set(meta.symbol) | set(hedge))
    bars_dir = cache_dir / "bars"
    manifest_path = bars_dir / "manifest.json"
    manifest = _verified_json(manifest_path) if manifest_path.exists() else None
    if invalidate_adjusted:
        manifest = seal({**(manifest or {}), "adjusted_invalidated": True})
        _cache_json(manifest_path, manifest)
    if (not manifest or manifest.get("adjusted_invalidated") or not manifest.get("adjusted_verified")) and \
            not (update and full_refresh_adjusted):
        raise Hold("Adjusted history unverified/invalidated: explicit --update --full-refresh-adjusted required")
    if full_refresh_adjusted and not update:
        raise Hold("--full-refresh-adjusted requires --update")
    adj = raw = None
    if manifest and not full_refresh_adjusted:
        frames = []
        for name in ("adjusted", "raw"):
            path = bars_dir / manifest[name]
            if path.parent.resolve() != bars_dir.resolve() or file_hash(path) != manifest[name + "_sha256"]:
                raise Hold("Bar cache integrity failure")
            frames.append(normalize_bars(pd.read_parquet(path)))
        adj, raw = frames
        if manifest.get("through", "") > day:
            raise Hold("Cache was adjusted after requested as-of; use saved historical input instead")
    if update:
        end = exchange().session_close(day).isoformat()
        start = dates[0].date().isoformat()
        if full_refresh_adjusted:
            adj = _fetch_bars(symbols, start, end, "all")
            raw = _fetch_bars(symbols, start, end, "raw")
        else:
            overlap_day = incremental_start(adj, raw, day, config)
            fresh_adj = _fetch_bars(symbols, overlap_day, end, "all")
            fresh_raw = _fetch_bars(symbols, overlap_day, end, "raw")
            required = set(adj.symbol)
            # Verify an early-history sentinel as well as the recent overlap before any splice.
            first_days = adj.groupby("symbol").date.min()
            sentinels = []
            for sentinel in first_days.drop_duplicates().sort_values():
                names = sorted(first_days.index[first_days == sentinel])
                sentinels.append(_fetch_bars(names, sentinel.date().isoformat(),
                                             exchange().session_close(sentinel).isoformat(), "all"))
            checked = pd.concat([fresh_adj, *sentinels]).drop_duplicates(["date", "symbol"])
            if set(fresh_adj.symbol) - required or not adjustment_consistent(adj, checked, required):
                _cache_json(manifest_path, seal({**manifest, "adjusted_invalidated": True}))
                raise Hold("Adjusted prices changed/unverifiable; cache invalidated, full refresh required")
            adj = pd.concat([adj, fresh_adj]).drop_duplicates(["date", "symbol"], keep="last")
            raw = pd.concat([raw, fresh_raw]).drop_duplicates(["date", "symbol"], keep="last")
        adj, raw = [normalize_bars(f.loc[(f.date >= dates[0]) & (f.date <= dates[-1])]) for f in (adj, raw)]
        validate_bars(adj, raw, day, config)
        bars_dir.mkdir(parents=True, exist_ok=True)
        filenames, hashes = {}, {}
        for name, frame in (("adjusted", adj), ("raw", raw)):
            filename = f"{name}-{day}-{frame_hash(frame)}.parquet"
            path = bars_dir / filename
            if not path.exists():
                frame.to_parquet(path, index=False)
            elif frame_hash(normalize_bars(pd.read_parquet(path))) != frame_hash(frame):
                raise Hold("Existing immutable bar cache content differs from its name")
            filenames[name] = filename
            hashes[name + "_sha256"] = file_hash(path)
        manifest = seal({**filenames, **hashes, "through": day, "adjusted_verified": True,
                         "adjusted_invalidated": False, "full_refresh": full_refresh_adjusted,
                         "verification": "full-history-fetch" if full_refresh_adjusted
                         else "overlap-and-historical-sentinels",
                         "refreshed_at": datetime.now(timezone.utc).isoformat()})
        _cache_json(manifest_path, manifest)
    adj, raw = [normalize_bars(f.loc[(f.date >= dates[0]) & (f.date <= dates[-1])]) for f in (adj, raw)]
    validate_bars(adj, raw, day, config)
    inputs = {"adjusted": frame_hash(adj), "raw": frame_hash(raw), "universe": file_hash(meta_path),
              "shares": file_hash(shares_path), "manifest": digest(manifest),
              "adjusted_verified": True, "adjusted_invalidated": False,
              "source": str(bars_dir.resolve()), "through": day}
    return assemble_panel(adj, raw, meta, shares, day, config), inputs


def fixture_panel(day: str = "2026-11-18", stocks: int = 120) -> tuple[dict, dict]:
    """Deterministic 620-session synthetic panel; never reads or writes research data.

    The slow AR(1) return drift is deliberately learnable: a pure-noise panel legitimately makes
    LASSO return all-zero coefficients, which the frozen pipeline answers with carry-forward/HOLD
    rather than a book. That degenerate path is covered by the unit tests instead.
    """
    if stocks < 120:
        raise ValueError("Fixture requires at least 120 stocks")
    dates = sessions(day, HISTORY)
    cols = [f"S{i:03d}" for i in range(stocks)] + ["SPY", "QQQ"]
    rng = np.random.default_rng(20261118)
    market = rng.normal(.0002, .008, (HISTORY, 1))
    innovations = rng.normal(0, .013, (HISTORY, len(cols)))
    slow = np.zeros_like(innovations)
    for t in range(1, HISTORY):
        slow[t] = .97 * slow[t - 1] + rng.normal(0, .0035, len(cols))
    idio = np.zeros_like(innovations)
    for t in range(1, HISTORY):
        idio[t] = .90 * idio[t - 1] + innovations[t]
    close = 50 * np.exp(np.cumsum(market, axis=0) * rng.uniform(.6, 1.4, (1, len(cols)))
                        + np.cumsum(slow + idio, axis=0) * .35)
    open_ = close * np.exp(rng.normal(0, .005, close.shape))

    def df(v):
        return pd.DataFrame(v, index=dates, columns=cols).astype("float32")

    volume = rng.uniform(1e6, 4e6, close.shape)
    p = {"close": df(close), "open": df(open_), "high": df(np.maximum(open_, close) * 1.012),
         "low": df(np.minimum(open_, close) * .988), "volume": df(volume),
         "vwap": df((open_ + close) / 2), "trades": df(volume / rng.uniform(30, 100, close.shape)),
         "amt": df(volume * (open_ + close) / 2), "mcap": df(close * 1e8),
         "tvr": df(volume / 1e8), "raw_close": df(close)}
    p["fwd_ret"] = p["open"].shift(-2) / p["open"].shift(-1) - 1
    p["fwd_horizon"] = p["open"].shift(-6) / p["open"].shift(-1) - 1
    u = pd.DataFrame(True, index=dates, columns=cols)
    u[["SPY", "QQQ"]] = False
    industry = pd.Series({c: f"sector{i % 8}" for i, c in enumerate(cols[:-2])}).reindex(cols)
    snapshot = {"as_of": exchange().session_close(day).isoformat(), "mode": "test", "account_id": 5322438,
                "account_type": "SIMULATE", "equity": 100000., "initial_equity": 100000.,
                "previous_close_equity": 100000., "peak_equity": 100000., "positions": [],
                "recent_daily_returns": [float(x) for x in rng.normal(0, .007, 20)],
                "eligibility": {c: {"security_type": "COMMON_STOCK", "shortable": True, "easy_to_borrow": True}
                                for c in cols[:-2]}}
    for name in ("SPY", "QQQ"):
        snapshot["eligibility"][name] = {"security_type": "ETF", "shortable": True, "easy_to_borrow": True}
    return {"p": p, "U": u, "industry": industry}, snapshot


# ------------------------------------------------------------------ targets
def make_targets(config: dict, snapshot: dict | None, now: datetime, panel: dict | None = None,
                 inputs: dict | None = None, cache_dir: Path | None = None, fixture: bool = False,
                 update: bool = False, full_refresh_adjusted: bool = False,
                 invalidate_adjusted: bool = False, state_dir: Path | None = None) -> dict:
    if now.tzinfo is None:
        raise ValueError("Timezone-aware --as-of required")
    day = completed_session(now)
    names = config.get("factor_names") or []
    payload = {"schema_version": SCHEMA_VERSION, "strategy_id": STRATEGY_ID,
               "mode": config.get("mode", "test"), "status": "HOLD",
               "generated_at": now.astimezone(timezone.utc).isoformat(),
               "data_timestamp": exchange().session_close(day).isoformat(), "signal_date": day,
               "weights": {}, "spy_hedge_weight": 0., "qqq_hedge_weight": 0., "vol_multiplier": 1.,
               "model_coefficients": dict.fromkeys(names, 0.), "model_alpha": None,
               "model_fit_date": None, "model_retrained_today": False, "model_carried_forward": False,
               "train_dates": [], "factor_names": list(names), "model_fitted": False,
               "signal_guard": {"passed": False, "distinct_values": 0, "std": None, "scored_names": 0,
                                "min_distinct_required": config.get("signal_guard_min_distinct")},
               "config_checksum": digest(config), "position_snapshot_checksum": digest(snapshot),
               "coverage": {}, "rebalance_due": rebalance_signal(day), "data_inputs": inputs or {},
               "data_timestamp_verified": False, "fixture": fixture, "limitations": LIMITATIONS,
               "sizing_equity": snapshot.get("equity") if isinstance(snapshot, dict) else None,
               "snapshot_as_of": snapshot.get("as_of") if isinstance(snapshot, dict) else None}
    try:
        validate_config(config)
        if fixture and config["mode"] != "test":
            raise Hold("Synthetic fixture cannot target the competition account")
        previous = validate_snapshot(snapshot, config, now, day)
        flags = broker_eligibility(snapshot)
        if panel is None:
            panel, inputs = load_live_panel(day, config, cache_dir or ROOT / "live/cache", update,
                                            full_refresh_adjusted, invalidate_adjusted)
            payload["data_inputs"] = inputs
        if panel["U"].index[-1] != pd.Timestamp(day):
            raise Hold("Stale panel")
        close = panel["p"]["close"]
        for name in config["hedge_instruments"]:
            if name not in close or close[name].iloc[-61:].isna().any():
                raise Hold(f"Incomplete 60-session {name} return history")
        payload["data_timestamp_verified"] = True
        for name in previous[previous != 0].index:
            if name not in close or not np.isfinite(close.loc[day, name]):
                raise Hold("Stale/missing bars for a held position")
            if name not in config["hedge_instruments"] and not eligible(flags.get(name)):
                raise Hold("Held security is not a verified common stock")
        tradable_today = tradable_mask(config, flags, panel["p"]["mcap"].iloc[-1], panel["U"].columns)
        if int((panel["U"].iloc[-1] & tradable_today).sum()) < 100:
            raise Hold("Insufficient broker-classified tradable short universe (minimum 100)")
        path = state_path(config["mode"], state_dir)
        s, beta, vol, model, coverage, history = score_panel(
            panel, day, config, tradable_today, cache_dir,
            load_history(path, config, config["mode"], panel_identity(config, inputs)))
        guard = signal_guard(s, config)
        payload["signal_guard"] = guard
        save_history(history, path)
        payload.update(model_coefficients=model["coefficients"], model_alpha=model["alpha"],
                       model_fit_date=model["fit_date"], model_fitted=True,
                       model_input_checksum=model["input_checksum"],
                       model_retrained_today=model["fit_date"] == day,
                       model_carried_forward=model["carried_forward"],
                       model_nonzero_factors=model["nonzero_count"],
                       train_dates=model["train_dates"], train_start=model["train_start"],
                       train_end=model["train_end"], coverage=coverage)
        if not guard["passed"]:
            raise Hold(f"Signal guard: {guard['distinct_values']} distinct scores below "
                       f"{config['signal_guard_min_distinct']} or zero dispersion; positions kept")
        mult = vol_multiplier(snapshot["recent_daily_returns"], config)
        if payload["rebalance_due"]:
            shorts, hedges, risk = build_book(s, previous, tradable_today, vol, beta, config, mult,
                                              snapshot["equity"], snapshot["initial_equity"])
        else:
            shorts, hedges = keep_drifted(previous, config, flags)
            risk = {"gross_short": -float(shorts.sum()), "short_names": int((previous < 0).sum()),
                    "drift_only": True}
        for name, weight in hedges.items():
            if weight < 0 and not borrow_confirmed(flags.get(name)):
                raise Hold(f"{name} hedge short requires explicit broker borrow confirmation")
        # On a drift-only day the book is not re-sized, so the residual beta is left to the
        # execution gate, which recomputes it from the live snapshot rather than from estimates.
        residual_beta = (float(risk["residual_unhedged_beta"] * risk["margin_scale"])
                         if payload["rebalance_due"] else None)
        payload.update(status="READY", weights={c: float(v) for c, v in shorts.items() if v != 0},
                       **{field: float(hedges.get(name, 0.)) for name, field in hedge_keys(config).items()},
                       vol_multiplier=mult,
                       risk_overlay={**risk, "book_beta_after_hedge": residual_beta},
                       coverage={**coverage, "tradable_scored": int(s.notna().sum()),
                                 "target_shorts": int((shorts < 0).sum()),
                                 "target_longs": int((shorts > 0).sum())})
    except (Hold, KeyError, ValueError, TypeError, RuntimeError, OSError) as exc:
        payload["hold_reason"] = str(exc)
    return seal(payload)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", action="store_true", default=False)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--as-of", type=timestamp)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--update", action="store_true", default=False)
    parser.add_argument("--full-refresh-adjusted", action="store_true", default=False)
    parser.add_argument("--invalidate-adjusted", action="store_true", default=False)
    args = parser.parse_args(argv)
    if args.fixture and (args.update or args.snapshot or args.invalidate_adjusted or args.full_refresh_adjusted):
        parser.error("--fixture cannot use real snapshots or update flags")
    config = load_config(args.config)
    now = args.as_of or (timestamp("2026-11-18T22:00:00Z") if args.fixture else datetime.now(timezone.utc))
    panel = snapshot = inputs = None
    cache = ROOT / "live/cache" / ("fixture" if args.fixture else "production")
    state = cache / "state" if args.fixture else STATE
    if args.fixture:
        # Pick an anchored rebalance so the default fixture demonstrates target construction.
        day = completed_session(now) if args.as_of else fit_day(completed_session(now))
        if args.as_of is None:
            now = exchange().session_close(day).to_pydatetime()
        panel, snapshot = fixture_panel(day)
        inputs = {"source": SYNTHETIC_SOURCE, "seed": 20261118,
                  "panel_checksum": digest({k: frame_hash(v) for k, v in panel["p"].items()})}
    elif args.snapshot:
        try:
            snapshot = json.loads(args.snapshot.resolve().read_text(encoding="utf-8"))
        except (ValueError, OSError):
            parser.error("Cannot read snapshot JSON")
    result = make_targets(config, snapshot, now, panel, inputs, cache, args.fixture, args.update,
                          args.full_refresh_adjusted, args.invalidate_adjusted, state)
    out = (cache / "targets" if args.fixture else ROOT / "live/targets") / result["mode"] / f"{result['signal_date']}.json"
    try:
        write_targets(out, result)
    except (ValueError, FileExistsError) as exc:
        print(f"HOLD: {exc}")
        return 2
    print(f"{result['status']}: {out.resolve()}" + (f" ({result['hold_reason']})" if "hold_reason" in result else ""))
    return 0 if result["status"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
