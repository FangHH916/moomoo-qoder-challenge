"""The shared pause switch: the one piece of state both executors have to agree on.

The user's Slack `PAUSE` reaches a cloud Coordinator, and the 2026-11-23 entry runs in this Desktop chat,
so the switch has to live somewhere both can read and that survives between cloud sessions. `live/ops.json`
records what was measured on 2026-11-22 about the three candidate channels; the moomoo watchlist is the only
one that is proven to work from both sides, so it is the transport, with its limits stated here rather than
papered over:

* the channel carries only *which symbols are in the group*. There is no timestamp, no comment field and no
  metadata, so time, source and reason live in the journal row and the Slack thread, and the flag itself can
  only carry a coarse expiry -- the day ring in `ops.json`, whose token is chosen from the Eastern date the
  lease was taken (aliasing after 12 days is inherent to that scheme and is reported as such).
* the MCP cannot create a watchlist (`quote_modify_user_security` only knows ADD, DEL and MOVE_OUT and fails
  on an unknown group), so the user provisions `PSTAR-PAUSE` once, in the app.
* `DEL` removes a symbol from *every* group, including the user's own, so every removal here is `MOVE_OUT`,
  scoped to the switch group.
* the sentinel codes must be real instruments the broker will accept and must never be tradable by the
  strategy; `validate()` enforces the second half against the config and the sealed target weights.

Nothing in this module calls the broker. It parses the raw MCP response the chat agent saved and produces the
exact payload list the agent sends back, which is the same relay discipline `live/golive.py` uses: a value the
raw response does not carry stays `unknown`, and `unknown` blocks entries while never blocking a reduction.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from live.contracts import ROOT, timestamp

OPS_PATH = ROOT / "live" / "ops.json"
READ_TOOL = "quote_user_security"
MODIFY_TOOL = "quote_modify_user_security"
RUNNING, PAUSED, UNKNOWN = "running", "paused", "unknown"
LEASED, FREE, EXPIRED, AMBIGUOUS, ABSENT = "leased", "free", "expired", "ambiguous", "absent"
# A reduction the account must be allowed to make even while paused (R9/R16 semantics, prompt 08 item 1).
ALWAYS_ALLOWED_INTENTS = ("FINAL_EXIT", "EMERGENCY_EXIT")


class SwitchError(ValueError):
    """The switch registry is not usable, so no decision may be claimed from it."""


def load_ops(path=None) -> dict:
    try:
        ops = json.loads(Path(path or OPS_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SwitchError(f"cannot read the operational parameters: {exc}") from exc
    if ops.get("schema_version") != "pstar-ops/1":
        raise SwitchError(f"{path or OPS_PATH}: unsupported ops schema {ops.get('schema_version')!r}")
    return ops


def _bare(code) -> str:
    return str(code or "").strip().upper().removeprefix("US.")


def registry(ops: dict) -> dict:
    switch = ops.get("pause_switch") or {}
    try:
        codes = {**switch["state"], **switch["lease"]}
        full = {_bare(v): str(v) for v in list(codes.values()) + list(switch["lease_day_ring"])}
        return {"group": switch["group"], "state": {k: _bare(v) for k, v in switch["state"].items()},
                "lease": {k: _bare(v) for k, v in switch["lease"].items()},
                "full": full,
                "ring": [_bare(c) for c in switch["lease_day_ring"]],
                "hours": {"local-entry": switch["lease_hours_local_entry"],
                          "cloud-executor": switch["lease_hours_cloud_executor"]},
                "block_on_unknown": bool(switch.get("ambiguous_is_paused", True)),
                "never_blocks": list(switch.get("never_blocks") or ALWAYS_ALLOWED_INTENTS)}
    except (KeyError, TypeError) as exc:
        raise SwitchError(f"live/ops.json pause_switch is incomplete: missing {exc}") from exc


def validate(ops: dict, config: dict, weights: dict | None = None) -> list[str]:
    """Errors only: a sentinel that the strategy could ever trade makes the switch ambiguous, so refuse it."""
    reg, problems = registry(ops), []
    tradable = {_bare(s) for s in (list((config or {}).get("etf_universe") or ())
                                   + list((config or {}).get("etf_sleeve") or ())
                                   + list((config or {}).get("spy_route") or ())
                                   + list((config or {}).get("discretionary_symbols") or ())
                                   + list((weights or {}).keys()))}
    sentinels = list(reg["state"].values()) + list(reg["lease"].values()) + list(reg["ring"])
    if len(sentinels) != len(set(sentinels)):
        problems.append("a sentinel code is used twice in live/ops.json")
    for name in sorted(set(sentinels) & tradable):
        problems.append(f"{name} is a switch sentinel and a tradable symbol: the flag can never share the book")
    for holder in reg["hours"]:
        if not (0 <= int(reg["hours"][holder][:2]) < 24 and 0 <= int(reg["hours"][holder][3:]) < 60):
            problems.append(f"lease_hours_{holder} is not an HH:MM Eastern time")
    if not reg["ring"]:
        problems.append("the lease day ring is empty, so no lease can carry an expiry")
    return problems


# ----------------------------------------------------------------- reading the flag
def read_payload(ops: dict) -> dict:
    return {"tool": READ_TOOL, "payload": {"group_name": registry(ops)["group"]}}


def codes_in(raw) -> set[str]:
    """The symbol codes a raw `quote_user_security` response carries, or raise if it is not one."""
    if not isinstance(raw, dict):
        raise SwitchError(f"the switch read is {type(raw).__name__}, not an MCP response object")
    if raw.get("ret_code") not in (0, "0", None):
        raise SwitchError(f"{READ_TOOL} failed: ret_code {raw.get('ret_code')} {raw.get('ret_msg') or ''}".strip())
    rows = (raw.get("data") or {}).get("security_list")
    if rows is None:
        raise SwitchError(f"{READ_TOOL} returned no security_list: the response is not the switch group")
    return {_bare(row.get("code") or row.get("symbol")) for row in rows} - {""}


def _expiry(now: datetime, reg: dict, holder: str, index: int) -> tuple[str, datetime]:
    """When the lease written on the day whose ring token is `index` ends, searching back within the ring."""
    hours = reg["hours"].get(holder)
    if not hours:
        return "unknown", None
    day, span = now.astimezone(timezone.utc), len(reg["ring"])
    for back in range(span):
        candidate = day - timedelta(days=back)
        if _ring_index(reg, candidate) == index:
            et = candidate.astimezone(_ET())
            closed = et.replace(hour=int(hours[:2]), minute=int(hours[3:]), second=0, microsecond=0)
            return ("expired" if closed <= now else "leased"), closed
    return "unknown", None


def _ET():
    from rules.trading_calendar import ET  # imported lazily so the skill can ship this file standalone
    return ET


def _ring_index(reg: dict, when: datetime) -> int:
    return when.astimezone(_ET()).timetuple().tm_yday % len(reg["ring"])


def ring_token(reg: dict, when: datetime) -> str:
    return reg["ring"][_ring_index(reg, when)]


def switch_state(raw, ops: dict, now: datetime, captured_at: str | None = None,
                 max_age_seconds: float = 300.0) -> dict:
    """What the shared flag says right now, with every ambiguity resolved to `unknown`."""
    reg = registry(ops)
    out = {"mechanism": ops["pause_switch"]["mechanism"], "group": reg["group"], "available": False,
           "state": UNKNOWN, "codes": [], "lease_holder": None, "lease_status": ABSENT,
           "lease_expires_at": None, "lease_ring_token": None, "reasons": [], "read_at": captured_at}
    try:
        codes = codes_in(raw)
    except SwitchError as exc:
        out["reasons"].append(str(exc))
        return out
    out["available"], out["codes"] = True, sorted(codes)
    if captured_at:
        age = (now - timestamp(captured_at)).total_seconds()
        if age > max_age_seconds:
            # A read that has aged out is not evidence about the present, so it is reported as unavailable:
            # `may_trade` then blocks an entry with the reason, while a reduction still passes.
            out["available"] = False
            out["reasons"].append(f"the flag read is {age:.0f}s old, over the {max_age_seconds:.0f}s window: "
                                  f"re-read it, a pause sent since then would be missed")
            return out
    hits = [name for name, token in reg["state"].items() if token in codes]
    out["state"] = hits[0] if len(hits) == 1 else UNKNOWN
    if not hits:
        out["reasons"].append(f"neither state sentinel is in {reg['group']}: the switch has never been written")
    elif len(hits) > 1:
        out["reasons"].append(f"both state sentinels are in {reg['group']}: the switch is contradictory")
    holders = [name for name, token in reg["lease"].items() if token in codes]
    ring = [i for i, token in enumerate(reg["ring"]) if token in codes]
    if len(holders) > 1 or len(ring) > 1:
        out["lease_status"] = AMBIGUOUS
        out["reasons"].append("more than one lease token is set"
                             if len(holders) > 1 else "more than one lease day token is set")
    elif not holders and not ring:
        out["lease_status"] = FREE
    elif len(holders) == 1:
        out["lease_holder"] = holders[0]
        out["lease_status"], closed = _expiry(now, reg, holders[0], ring[0] if ring else _ring_index(reg, now))
        out["lease_expires_at"] = None if closed is None else closed.isoformat()
        out["lease_ring_token"] = reg["ring"][ring[0]] if ring else None
        if ring and len(ring) == 1:
            out["lease_taken_et_day"] = _taken_day(now, reg, ring[0]).isoformat()
        if out["lease_status"] == "expired":
            out["reasons"].append(f"the {holders[0]} lease was taken for the ring day that ended "
                                  f"{out['lease_expires_at']}: it has expired")
    else:
        out["lease_status"] = AMBIGUOUS
        out["reasons"].append("a lease day token is set with no lease holder")
    return out


def _taken_day(now: datetime, reg: dict, index: int) -> datetime:
    day = now.astimezone(timezone.utc)
    for back in range(len(reg["ring"])):
        candidate = day - timedelta(days=back)
        if _ring_index(reg, candidate) == index:
            return candidate.astimezone(_ET()).date()
    return now.astimezone(_ET()).date()


def may_trade(state: dict, ops: dict, intent: str, holder: str | None = None) -> dict:
    """The decision for one submit round: reductions never wait for the switch, entries always read it."""
    name = str(intent or "ENTRY").upper()
    reg, reasons = registry(ops), list(state.get("reasons") or ())
    if name in reg["never_blocks"]:
        return {"allow": True, "intent": name, "switch": state["state"], "reasons":
                     [f"{name} is a reduction: it passes whatever the switch says"] + reasons}
    if not state.get("available"):
        return {"allow": False, "intent": name, "switch": UNKNOWN,
                "reasons": [f"the shared switch could not be read: {reasons[0] if reasons else 'no read given'}"]}
    if state["state"] == PAUSED:
        return {"allow": False, "intent": name, "switch": PAUSED,
                "reasons": [f"the switch is {PAUSED}: send no new order"
                            + (f" (source: {state.get('read_at')})" if state.get("read_at") else "")]}
    if state["state"] != RUNNING:
        return {"allow": not reg.get("block_on_unknown", True), "intent": name, "switch": UNKNOWN,
                "reasons": [f"the switch is {UNKNOWN}: " + (reasons[0] if reasons else "contradictory read")]}
    if holder:
        held = state.get("lease_holder")
        if held == holder and state.get("lease_status") == LEASED:
            return {"allow": True, "intent": name, "switch": RUNNING,
                    "reasons": [f"the {holder} lease is held and runs to {state.get('lease_expires_at')}"]}
        if held and held != holder and state.get("lease_status") == LEASED:
            return {"allow": False, "intent": name, "switch": RUNNING,
                    "reasons": [f"the live lease belongs to {held} until {state.get('lease_expires_at')}: "
                                f"{holder} may not trade the same book twice"]}
        if state.get("lease_status") in (AMBIGUOUS,):
            return {"allow": False, "intent": name, "switch": RUNNING,
                    "reasons": ["the lease is ambiguous: " + (reasons[0] if reasons else "two holders")]}
        return {"allow": True, "intent": name, "switch": RUNNING,
                "reasons": [f"no unexpired lease is held, so {holder} may take it"]}
    return {"allow": True, "intent": name, "switch": RUNNING,
            "reasons": ["the switch says running"
                        + ("" if state.get("lease_status") != ABSENT else "; no lease token is set")]}


# ----------------------------------------------------------------- writing the flag
def write_plan(desired: dict, ops: dict, now: datetime, current: set[str] | None = None) -> dict:
    """The exact `quote_modify_user_security` payloads that move the flag to `desired`.

    `desired` is `{"state": "paused"|"running", "lease": "local-entry"|"cloud-executor"|None}`. Removal is
    always MOVE_OUT, scoped to the switch group, because DEL would strip the sentinel out of every watchlist
    the user has, including their own.
    """
    reg = registry(ops)
    current = set(current or ())
    want_state = desired.get("state")
    want_lease = desired.get("lease")
    if want_state not in (RUNNING, PAUSED):
        raise SwitchError(f"the switch state must be {RUNNING} or {PAUSED}, not {want_state!r}")
    if want_lease not in (None, *reg["lease"]):
        raise SwitchError(f"unknown lease holder {want_lease!r}: the registry knows {sorted(reg['lease'])}")
    wanted = {reg["state"][want_state]}
    if want_lease:
        wanted |= {reg["lease"][want_lease], ring_token(reg, now)}
    additions = sorted(wanted - current)
    removals = sorted(({*reg["state"].values(), *reg["lease"].values(), *reg["ring"]} - wanted) & current)
    def broker(bare):
        return [reg["full"].get(b, b) for b in bare]

    payloads = []
    if additions:
        payloads.append({"tool": MODIFY_TOOL,
                         "payload": {"op": "ADD", "group_name": reg["group"], "code_list": broker(additions)}})
    if removals:
        payloads.append({"tool": MODIFY_TOOL,
                         "payload": {"op": "MOVE_OUT", "group_name": reg["group"],
                                     "code_list": broker(removals)}})
    return {"group": reg["group"], "state": want_state, "lease": want_lease,
            "changed": bool(payloads),
            "lease_expires_at": (None if not want_lease else _expiry(
                now, reg, want_lease, _ring_index(reg, now))[1].isoformat()),
            "et_day": now.astimezone(_ET()).date().isoformat(),
            "expected_codes": broker(sorted(wanted)), "writes": payloads,
            "note": ("the flag already reads as intended: no write is needed" if not payloads else
                     "send every write here, then re-read the group with "
                     f"{READ_TOOL} group_name={reg['group']} and confirm it returns exactly expected_codes: "
                     "the flag is only shared once the read agrees with the write"),
            "remove_from_all_groups": "never use op=DEL on a sentinel: it clears the symbol from every "
                                      "watchlist, not just the switch group"}


def slack_text(state: dict, source: str) -> str:
    """The one-line confirmation the Coordinator posts back in the thread, from the flag it just read."""
    lease = (f", lease {state['lease_holder']} until {state['lease_expires_at']}"
             if state.get("lease_holder") and state.get("lease_status") == LEASED else
             f", lease {state.get('lease_status')}")
    return (f"switch {state['group']}: {state['state']} (codes {' '.join(state['codes']) or 'none'}"
            f"{lease}) - from {source}")
