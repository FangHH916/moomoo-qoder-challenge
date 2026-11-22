"""Risk monitor: the only thing that may trade between rebalances.

The frozen execution rules (prompt 06 item 6) allow exactly two risk actions outside a rebalance, plus
one halt: a ticker above 39% of initial assets is cut back to 38%, margin above 95% is cut back to 90%,
and a total drawdown of -8% from the peak flattens everything and halts. The thresholds and the
arithmetic are the ones the realistic backtest measured (`TRADE_BAND`, `TRIM_AT`, `MARGIN_ALERT` and the
whole-share handling in `research/r10_deploy.py::simulate`); nothing here is tunable.

Run it after the open fills and before every order batch. Discretionary and unclassified positions
count toward margin and the per-ticker cap, but the strategy never trades them.
"""
from __future__ import annotations

import math

from live.contracts import finite


class RiskError(ValueError):
    """The account state is not measurable, so no risk action may be claimed."""


def _shares(quantity: object) -> int:
    if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or not finite(quantity):
        raise RiskError("position quantity must be finite")
    if float(quantity) != int(quantity):
        raise RiskError(f"fractional share holding {quantity}; manual reconciliation required")
    return int(quantity)


def measure(config: dict, positions, equity: float, initial_equity: float,
            peak_equity: float) -> dict:
    """Values, gross exposure, margin and drawdown from the broker's own share counts and prices."""
    if not all(finite(v) and v > 0 for v in (equity, initial_equity, peak_equity)):
        raise RiskError("equity, initial equity and peak equity must be positive")
    if peak_equity + 1e-9 < equity:
        raise RiskError("peak equity is below current equity")
    values, prices, quantities = {}, {}, {}
    for row in positions or ():
        name = str(row["symbol"]).removeprefix("US.")
        if name in values:
            raise RiskError(f"duplicate position {name}")
        price = row.get("price")
        if not finite(price) or price <= 0:
            raise RiskError(f"{name} has no usable price")
        quantity = _shares(row.get("quantity"))
        values[name], prices[name], quantities[name] = quantity * price, float(price), quantity
    long_gross = sum(v for v in values.values() if v > 0)
    short_gross = -sum(v for v in values.values() if v < 0)
    margin = (config["margin_long_factor"] * long_gross
              + config["margin_short_factor"] * short_gross) / equity
    drawdown = equity / peak_equity - 1
    return {"values": values, "prices": prices, "quantities": quantities, "equity": equity,
            "initial_equity": initial_equity, "long_gross": long_gross, "short_gross": short_gross,
            "margin": margin, "drawdown": drawdown, "positions": len(values),
            "max_ticker": max(values, key=lambda s: abs(values[s])) if values else None,
            "max_ticker_vs_initial": (max(abs(v) for v in values.values()) / initial_equity) if values else 0.0,
            "over_trim": sorted(s for s, v in values.items() if abs(v) > config["trim_ticker_above_of_initial"]
                                * initial_equity)}


def warn_lines(config: dict, ops: dict, state: dict) -> list[str]:
    """The WARN line, which posts to Slack and changes no order.

    The thresholds are in `live/ops.json` because they are monitoring knobs, not frozen rules: every ALERT and
    KILL number this function is deliberately softer than lives in `live/config.pstar.json`, whose digest is
    bound into the sealed target contract. Both the `pstar-risk-check` skill and `golive.py plan --risk-action`
    call this so a warning means the same thing on both sides.
    """
    risk = ops.get("risk_check") or {}
    try:
        ticker, margin = float(risk["warn_ticker_share_of_initial"]), float(risk["warn_margin"])
        drawdown = float(risk["warn_drawdown"])
        positions = int(config["max_positions"] * float(risk["position_count_warn_fraction"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise RiskError(f"live/ops.json risk_check is missing a WARN threshold: {exc}") from exc
    lines = []
    if state["max_ticker"] and state["max_ticker_vs_initial"] > ticker:
        lines.append(f"{state['max_ticker']} is {state['max_ticker_vs_initial']:.1%} of initial assets, above "
                     f"the {ticker:.0%} warning line (the trim rule starts at "
                     f"{config['trim_ticker_above_of_initial']:.0%}).")
    if state["margin"] > margin:
        lines.append(f"margin {state['margin']:.1%} is above the {margin:.0%} warning line (the cut starts at "
                     f"{config['delever_margin_above']:.0%}).")
    if state["drawdown"] <= drawdown:
        lines.append(f"drawdown {state['drawdown']:.2%} of the peak is past the {drawdown:.0%} warning line "
                     f"(the flatten rule starts at {config['drawdown_limit']:.0%}).")
    if state["positions"] > positions:
        lines.append(f"{state['positions']} positions is past the {positions}-position warning line "
                     f"({config['max_positions']} is the cap).")
    return lines


def flatten(state: dict, protected=()) -> dict:
    """Reduce every position the strategy owns to zero; protected names are left alone."""
    return {name: 0 for name, quantity in state["quantities"].items()
            if quantity and name not in set(protected)}


def actions(config: dict, state: dict, protected=()) -> dict:
    """The three risk actions, in the order the realistic backtest applied them.

    Trims first (a name over 39% of initial assets goes back to 38% of it, rounded down so the cap
    holds), then the margin cut measured on the pre-action book, then the drawdown halt, which
    overrides everything else because it flattens the account.
    """
    protected, trim_to = set(protected), config["ticker_cap_of_initial"] * state["initial_equity"]
    halt = state["drawdown"] <= config["drawdown_limit"] + 1e-12
    report: list[str] = []
    targets = dict(state["quantities"])
    for name in state["over_trim"]:
        if name in protected:
            report.append(f"{name} is above {config['trim_ticker_above_of_initial']:.0%} of initial assets "
                          f"but is not a strategy position: not trimmed, alert only.")
            continue
        shares = int(math.copysign(math.floor(trim_to / state["prices"][name]), state["quantities"][name]))
        if abs(shares) < abs(state["quantities"][name]):
            targets[name] = int(shares)
            report.append(f"trim {name} to {trim_to:,.0f} of initial assets "
                          f"({abs(state['quantities'][name]) - abs(shares)} shares).")
    delever_scale = 1.0
    if not halt and state["margin"] > config["delever_margin_above"]:
        delever_scale = config["delever_to"] / state["margin"]
        for name, shares in list(targets.items()):
            if name in protected:
                continue
            targets[name] = int(round(shares * delever_scale))
        report.append(f"margin {state['margin']:.1%} above {config['delever_margin_above']:.0%}: "
                      f"scaled the strategy book by {delever_scale:.4f} to reach "
                      f"{config['delever_to']:.0%}.")
    if halt:
        targets = {**targets, **flatten(state, protected)}
        report.append(f"drawdown {state['drawdown']:.2%} reached {config['drawdown_limit']:.0%} of the peak: "
                      f"flatten everything and halt.")
    residual = _residual_margin(config, state, targets, protected)
    if delever_scale < 1.0 and residual > config["delever_to"] + 1e-9:
        report.append(f"after the cut the book is still at {residual:.1%} margin because protected "
                      f"positions are not tradable by the strategy: alert, no further selling.")
    changes = {name: targets[name] - state["quantities"][name] for name in targets
               if targets[name] != state["quantities"][name]}
    return {"halt": halt, "flatten": bool(halt), "delever_scale": delever_scale,
            "target_shares": targets, "share_changes": changes,
            "orders_required": len(changes), "margin_after": residual,
            "protected_skipped": sorted(protected & set(state["over_trim"])),
            "slack_report": report}


def _residual_margin(config: dict, state: dict, targets: dict, protected) -> float:
    """Margin of the book the strategy can actually hold, with protected positions unchanged."""
    value = 0.0
    for name, quantity in targets.items():
        if name in protected:
            quantity = state["quantities"][name]
        value += config["margin_long_factor"] * max(quantity, 0) * state["prices"][name]
        value += config["margin_short_factor"] * max(-quantity, 0) * state["prices"][name]
    return value / state["equity"]
