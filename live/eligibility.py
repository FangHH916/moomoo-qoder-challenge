"""Pre-trade eligibility: confirm today that the frozen book can actually be traded.

Runs before the first order on each account and checks every instrument the strategy may trade —
the ten ETFs of the long sleeve (SPY QQQ IWM TLT IEF GLD SLV USO UUP DIA) against the competition's
own limits (price >= $1, market value >= $500M), and each short the targets ask for against the
broker's borrow state. A failure never changes the book: the name is skipped, the deviation is
logged and reported to Slack, and the weight is not redistributed. That keeps the live book a
subset of the frozen research book rather than a silently re-optimised one.

Nothing here places orders; the output is an input to `live/order_planner.py` and the rule gate.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from live.contracts import finite, timestamp

SYMBOL_RE = re.compile(r"[A-Z0-9][A-Z0-9.-]*")


def _flags(eligibility: dict, name: str) -> dict:
    record = (eligibility or {}).get(name)
    return record if isinstance(record, dict) else {}


def _quote(quotes: dict, name: str) -> dict:
    record = (quotes or {}).get(name)
    return record if isinstance(record, dict) else {}


def _fresh(record: dict, now: datetime, seconds: int) -> bool:
    try:
        age = (now - timestamp(record["as_of"])).total_seconds()
    except (KeyError, ValueError, TypeError):
        return False
    return 0 <= age <= seconds


def market_value(flags: dict) -> float | None:
    """ETF fund size, falling back to the broker's market-cap field; unknown stays unknown."""
    for key in ("market_value", "market_cap", "fund_aum", "asset_netting_value"):
        if finite(flags.get(key)):
            return float(flags[key])
    return None


def check_etfs(config: dict, eligibility: dict, quotes: dict, now: datetime) -> dict:
    """The ten sleeve/routing ETFs, each confirmed tradable and competition-eligible."""
    tradable, skipped = [], []
    for name in config["etf_universe"]:
        flags, quote = _flags(eligibility, name), _quote(quotes, name)
        reasons = []
        if flags.get("verified_by_relay") is not True:
            reasons.append("no verified broker record")
        if flags.get("security_type") != "ETF":
            reasons.append(f"not classified as an ETF ({flags.get('security_type')})")
        if flags.get("market") != "US":
            reasons.append(f"not a US listing ({flags.get('market')})")
        if flags.get("halted") is not False:
            reasons.append("halted or halt state unknown")
        value = market_value(flags)
        if value is None:
            reasons.append("market value unknown")
        elif value < config["min_etf_market_value"]:
            reasons.append(f"market value ${value:,.0f} below the ${config['min_etf_market_value']:,.0f} limit")
        price = quote.get("price")
        if quote.get("verified_by_relay") is not True:
            reasons.append("quote not relay-verified")
        elif not finite(price) or price <= 0:
            reasons.append("no usable quote")
        elif price < config["min_price"]:
            reasons.append(f"price ${price} below the ${config['min_price']:g} limit")
        elif not _fresh(quote, now, config["quote_max_age_seconds"]):
            reasons.append("quote stale")
        row = {"symbol": name, "price": price if finite(price) and price > 0 else None,
               "market_value": value, "reasons": reasons}
        (tradable if not reasons else skipped).append(row)
    return {"tradable": [row["symbol"] for row in tradable],
            "skipped": [row["symbol"] for row in skipped], "rows": tradable + skipped,
            "checked_at": now.astimezone(timezone.utc).isoformat()}


def check_shorts(config: dict, eligibility: dict, symbols) -> dict:
    """Every short the targets ask for, against the broker's borrow state. Nothing is replaced."""
    tradable, skipped = [], []
    for name in symbols:
        flags = _flags(eligibility, name)
        reasons = []
        if not isinstance(name, str) or not SYMBOL_RE.fullmatch(name):
            reasons.append("invalid symbol")
        if flags.get("verified_by_relay") is not True:
            reasons.append("no verified broker record")
        if flags.get("security_type") != "COMMON_STOCK":
            reasons.append(f"not a common stock ({flags.get('security_type')})")
        if flags.get("shortable") is not True or flags.get("easy_to_borrow") is not True:
            reasons.append("borrow is not confirmed shortable and easy to borrow")
        value = market_value(flags)
        if value is None:
            reasons.append("market cap unknown")
        elif value < config["min_short_market_cap"]:
            reasons.append(f"market cap ${value:,.0f} below the ${config['min_short_market_cap']:,.0f} limit")
        if flags.get("halted") is not False:
            reasons.append("halted or halt state unknown")
        (tradable if not reasons else skipped).append({"symbol": name, "reasons": reasons})
    return {"tradable": [row["symbol"] for row in tradable], "skipped": [row["symbol"] for row in skipped],
            "rows": tradable + skipped}


def plan_eligibility(config: dict, targets: dict, eligibility: dict, quotes: dict,
                     now: datetime) -> dict:
    """Combine the ETF and borrow checks with the target book and report the deviation for Slack."""
    weights = targets.get("weights") or {}
    longs = {s for s, w in weights.items() if w > 0}
    shorts = sorted(s for s, w in weights.items() if w < 0)
    etfs, borrows = check_etfs(config, eligibility, quotes, now), check_shorts(config, eligibility, shorts)
    blocked_rows = [row for row in etfs["rows"] if row["symbol"] in longs and row["reasons"]]
    short_rows = {row["symbol"]: row["reasons"] for row in borrows["rows"] if row["reasons"]}
    return {"etfs": etfs, "shorts": borrows,
            "blocked": sorted({row["symbol"] for row in blocked_rows} | (set(shorts) - set(borrows["tradable"]))),
            "blocked_detail": {**{row["symbol"]: row["reasons"] for row in blocked_rows},
                               **{name: reasons for name, reasons in short_rows.items() if name in shorts}},
            "slack_report": slack_report(blocked_rows, borrows, len(shorts))}


def slack_report(blocked_etf_rows: list, short_check: dict, requested_shorts: int) -> list[str]:
    """Plain lines for the daily Slack note; an empty list means the book was fully tradeable."""
    lines = []
    for row in blocked_etf_rows:
        lines.append(f"ETF {row['symbol']} skipped (not competition-eligible or not tradable): "
                     + "; ".join(reason.rstrip(".") for reason in row["reasons"])
                     + ". Weight not redistributed.")
    for name in short_check["skipped"]:
        reasons = next((r["reasons"] for r in short_check["rows"] if r["symbol"] == name), [])
        lines.append(f"short {name} skipped: " + "; ".join(reason.rstrip(".") for reason in reasons)
                     + ". Not replaced.")
    if len(short_check["tradable"]) < requested_shorts:
        lines.append(f"short book trades {len(short_check['tradable'])} of {requested_shorts} names; "
                     f"the frozen book is unchanged otherwise.")
    return lines
