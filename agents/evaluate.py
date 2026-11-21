"""Read-only, standard-library specification regression checks; never a trading gate.

Usage: python -B D:/Python/moomoo-qoder-challenge/agents/evaluate.py --self-check
Optional: --baseline <absolute path to a previously retained stdout JSON report>
No cloud calls, orders, installation, file writes or claimed cloud evaluations.
"""
from __future__ import annotations

import argparse
import copy
import difflib
import hashlib
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROLES = ("coordinator", "scout", "guard", "risk-auditor", "advisor")
SKILLS = (
    "validate-trading-targets", "paper-trading-gate", "reconcile-paper-fills",
    "report-trading-day", "research-factor-evidence",
)
FIXTURE_IDS = (
    "normal-rebalance-and-expired-permission", "wrong-account-and-injected-news",
    "stale-corrupt-and-unknown-borrow", "drawdown-partial-fill-holiday-race",
)
SOURCE_PATHS = (
    "notes/handbook-notes.md", "docs/submission-checklist.md",
    "live/config.pstar.json", "live/config.v2.json",
    "research/r10_deploy.py",
    "research/factors.py", "research/pipeline.py", "research/pipeline_v2.py",
    "research/pipeline_v3.py", "research/lab_factors.py", "research/lab_cache.py",
    "agents/AGENTS.md", "agents/templates.json", "agents/evaluations.json",
    "agents/evaluate.py",
) + tuple(f"agents/{role}.md" for role in ROLES) + tuple(
    f"skills/{skill}/SKILL.md" for skill in SKILLS
)
PAPER_ORDER_TOOLS = {"sim_trade_input_order", "sim_trade_cancel_order"}
LIVE_TOOL_PREFIXES = ("trading_", "account_", "crypto_account_")
PSTAR_STRATEGY = "pstar"
DEPLOYED_RE = re.compile(r'^DEPLOYED\s*=\s*["\']([a-z0-9]+)["\']', re.MULTILINE)
VARIANTS_RE = re.compile(r'^VARIANTS\s*=\s*\{', re.MULTILINE)
DECISION_FIELDS = (
    "event_id timestamp mode account_id run_id session_id agent kind symbol strategy_id intent action "
    "reason rule_checks target_checksum order_fingerprint config_checksum"
).split()
FILL_FIELDS = (
    "event_id timestamp mode account_id run_id session_id order_id fill_id symbol "
    "side quantity price commission platform_fee regulatory_fee slippage_bps arrival_price"
).split()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def local_path(relative: str) -> Path:
    path = (ROOT / relative).resolve()
    require(path.is_relative_to(ROOT), f"Source escapes project: {relative}")
    return path


def no_duplicates(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_constant(value: str):
    raise ValueError(f"Nonfinite JSON number: {value}")


def parse_json(text: str):
    return json.loads(text, object_pairs_hook=no_duplicates, parse_constant=reject_constant)


def load(relative: str):
    return parse_json(local_path(relative).read_text(encoding="utf-8"))


def canonical_bytes(path) -> bytes:
    """A file's bytes with CRLF folded to LF, so git's text conversion is not a content change.

    `core.autocrlf=true` checks these sources out with CRLF while the repository stores LF, so hashing raw
    bytes bound the release to one machine's checkout and made a fresh clone report drift for every file that
    was last written with LF endings. Content identity is what the lock must bind.
    """
    return path.read_bytes().replace(b"\r\n", b"\n")


def sha256(relative: str) -> str:
    return hashlib.sha256(canonical_bytes(local_path(relative))).hexdigest()


def pointer(value, path: str):
    require(path == "" or path.startswith("/"), "Invalid JSON pointer")
    if not path:
        return value
    for token in path[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False)


def assert_value(actual, assertion: dict) -> None:
    op = assertion["op"]
    expected = assertion["value"]
    if op == "eq":
        # Canonical JSON keeps true distinct from 1 and null distinct from zero.
        require(canonical(actual) == canonical(expected), f"expected {expected!r}, got {actual!r}")
    elif op == "contains":
        require(isinstance(actual, str) and isinstance(expected, str), "contains needs text")
        require(expected in actual, f"missing text {expected!r}")
    else:
        raise ValueError(f"Unsupported assertion operator: {op}")


def fixture_results(evaluations: dict, overrides: dict | None = None) -> list[dict]:
    overrides = overrides or {}
    results = []
    for fixture in evaluations["fixtures"]:
        failures = []
        for index, assertion in enumerate(fixture["local_assertions"], 1):
            source = assertion["source"]
            try:
                if source in overrides:
                    value = overrides[source]
                elif source.endswith(".json"):
                    value = load(source)
                else:
                    value = local_path(source).read_text(encoding="utf-8")
                assert_value(pointer(value, assertion.get("pointer", "")), assertion)
            except (ValueError, KeyError, IndexError, TypeError, OSError) as exc:
                failures.append(f"assertion {index} {source}: {exc}")
        results.append({
            "id": fixture["id"], "local_status": "FAIL" if failures else "PASS",
            "assertion_count": len(fixture["local_assertions"]),
            "failures": failures, "cloud_status": "UNVERIFIED",
        })
    return results


def validate_skill(text: str, name: str) -> None:
    lines = text.splitlines()
    require(lines and lines[0] == "---", f"{name}: missing YAML start")
    require("---" in lines[1:], f"{name}: missing YAML end")
    end = lines.index("---", 1)
    fields = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        require(bool(separator) and key not in fields, f"{name}: invalid metadata")
        fields[key] = value.strip()
    require(set(fields) == {"name", "description"}, f"{name}: unexpected metadata")
    require(fields["name"] == name and re.fullmatch(r"[a-z0-9-]+", name), f"{name}: name mismatch")
    description = fields["description"]
    require(0 < len(description) <= 1024, f"{name}: description length")
    require(description not in {"|", ">"}, f"{name}: multiline description")
    require(len(lines[end + 1:]) < 500, f"{name}: body must be under 500 lines")
    require("instructions, not permissions" in text, f"{name}: missing permission disclaimer")
    require(not re.search(r"\bTO" + r"DO\b", text, re.I), f"{name}: unresolved placeholder")


def validate_metadata() -> None:
    for name in ("AGENTS",) + ROLES:
        text = local_path(f"agents/{name}.md").read_text(encoding="utf-8")
        for field in ("Retrieved at:", "Agent ID: null", "Cloud version: null",
                      "Local revision: 0.3.5", "Change summary:"):
            require(field in text, f"{name}: missing {field}")
        require("no cloud retrieval" in text, f"{name}: retrieval scope unclear")
        require("BLOCKED" in text, f"{name}: missing activation status")
        require(not re.search(r"\bTO" + r"DO\b", text, re.I), f"{name}: unresolved placeholder")
    actual = {path.parent.name for path in (ROOT / "skills").glob("*/SKILL.md")}
    require(actual == set(SKILLS), "Expected exactly the five requested skills")
    for skill in SKILLS:
        validate_skill(local_path(f"skills/{skill}/SKILL.md").read_text(encoding="utf-8"), skill)


def validate_source_lock(lock: dict) -> None:
    require(lock["activation"] == "BLOCKED", "Release must remain blocked")
    require(lock["local_revision"] == "0.3.5", "Unexpected local revision")
    require(set(lock["source_sha256"]) == set(SOURCE_PATHS), "Source manifest coverage mismatch")
    for path, expected in lock["source_sha256"].items():
        require(bool(re.fullmatch(r"[0-9a-f]{64}", expected)), f"Invalid source hash: {path}")
        require(sha256(path) == expected, f"Source hash mismatch: {path}")
    require(lock["cloud_verification_status"] == "UNVERIFIED", "Cloud verification cannot be inferred")
    require(set(lock["roles"]) == set(ROLES), "Role lock coverage mismatch")
    require(set(lock["skills"]) == set(SKILLS), "Skill lock coverage mismatch")
    for group in ("roles", "skills"):
        for name, binding in lock[group].items():
            for key, value in binding.items():
                if key.endswith("_id") or key.startswith("expected_"):
                    require(value is None, f"Undiscovered binding must be null: {group}/{name}/{key}")
            path = binding["local_source"]
            require(binding["local_source_sha256"] == lock["source_sha256"][path], f"Local binding hash: {name}")
    require(lock["validated_mcp_contract_sha256"] is None, "MCP contract remains unverified")


def validate_templates(t: dict, config: dict) -> None:
    require(t["artifact_type"] == "inactive_desired_specification_not_api_payload", "Not an API payload")
    require(t["activation"] == "BLOCKED" and t["execution_enabled"] is False, "Execution enabled")
    require(t["schedules_created"] is False and t["schedule_intent"]["active"] is False, "Schedules active")
    require(t["skills_are_permissions"] is False and t["mode"] == "Forward", "Incorrect authority/mode")
    policy = t["release_policy"]
    require(policy["role_sessions"] == "isolated", "Roles must be isolated")
    require(policy["template_policy"] == "release_specific_frozen_templates", "Missing frozen templates")
    require(policy["reject_missing_or_mismatched_effective_version_or_hash"] is True, "Missing version/hash rejection")
    require(policy["allow_latest_skill_version"] is False, "Unpinned skill version")
    require(policy["skill_policy"] == "exact_cloud_version_and_content_sha256", "Incomplete skill pins")
    require(policy["changes_require"] == ["offline_unit_tests", "four_fixed_prompt_regressions", "reviewed_diff", "new_cloud_session", "activation_review"], "Incomplete change protocol")
    permissions = t["permission_policy"]
    require(permissions["default"] == "deny" and permissions["unknown_payload_fields"] == "deny", "Default permission is unsafe")
    require(permissions["allow_llm_permission_answers"] is False, "LLM may not answer order permission")
    require(permissions["permission_answerer"] == "trusted_deterministic_relay", "Wrong permission answerer")
    require(t["mcp_contract"]["status"] == "UNVERIFIED", "Unverified contract claimed valid")
    require(all(value is None for value in t["mcp_contract"]["schema_hashes"].values()), "Unknown schemas must be null")
    require(set(t["roles"]) == set(ROLES), "Unexpected role set")
    outputs = []
    for name, role in t["roles"].items():
        require(role["effective_tools"] == [] and role["can_execute"] is False, f"{name}: active tools")
        require(role["strategy_creation"] is False, f"{name}: strategy authoring granted")
        require(set(role["skills"]).issubset(SKILLS), f"{name}: unknown skill")
        if name != "guard":
            require(role["desired_order_tools"] == [], f"{name}: order tools outside Guard")
            require("paper-trading-gate" not in role["skills"], f"{name}: execution workflow outside Guard")
        for tool in role["desired_read_tools"]:
            require(tool in t["mcp_contract"]["schema_hashes"], f"{name}: unknown read tool")
            require(not tool.startswith(LIVE_TOOL_PREFIXES), f"{name}: live-namespaced tool")
            require(tool not in PAPER_ORDER_TOOLS, f"{name}: mutation disguised as read")
        require(role["output_path"].startswith(f"agents/runtime/{name}/"), f"{name}: output ownership")
        outputs.append(role["output_path"])
    require(len(set(outputs)) == len(ROLES), "Shared role output path")
    guard = t["roles"]["guard"]
    require(set(guard["desired_order_tools"]) == PAPER_ORDER_TOOLS, "Guard order tools are not the paper pair")
    require(guard["desired_order_permissions"] == {tool: "always_ask" for tool in guard["desired_order_tools"]}, "Unguarded mutation tool")
    require(permissions["denied_tool_prefixes"] == list(LIVE_TOOL_PREFIXES), "Live tool namespaces not denied")
    require(t["roles"]["risk-auditor"]["session"] == "separate_isolated_forward", "Auditor not separate")
    require(all(tool.startswith("sim_trade_") and tool not in PAPER_ORDER_TOOLS for tool in t["roles"]["risk-auditor"]["desired_read_tools"]), "Auditor not paper-account-read-only")
    require(t["roles"]["advisor"]["desired_read_tools"] == [] and t["roles"]["advisor"]["relay_operations"] == [], "Advisor not tool-free")
    require(t["roles"]["advisor"]["vault_profile"] == "none", "Advisor has Vault")
    require(t["workflow"]["auditor_acceptance_bindings"] == ["plan_hash", "target_checksum", "account_snapshot_checksum", "config_checksum", "mode", "account_id", "run_id", "session_id", "fencing_token", "expires_at"], "Audit binding drift")
    require(t["workflow"]["test_every_order_explicit_approval"] is True, "Test approval missing")
    require(t["workflow"]["competition_autonomous_approval_granted"] is False, "Autonomy not granted")
    automatic = t["workflow"]["local_automatic_execution"]
    require(automatic["decided"] == "2026-11-21" and automatic["decided_by"] == "user",
            "Automatic execution decision missing")
    require("durable pause and resume commands" in automatic["human_controls"],
            "Automatic execution without the pause switch")
    require(t["workflow"]["expired_order"]["competition"] == "reconcile_cancel_then_rerun_evidence_and_plan",
            "Expired approval still routed to a human in automatic mode")
    require(t["workflow"]["exits_cancels_replacements_require_approval"]["competition"]
            == "gate_checked_no_human_approval_since_2026-11-21", "Exit approval drift")
    require(t["rules"]["required_ids"] == [f"R{i}" for i in range(0, 22)], "Missing R0-R21")
    variant = config["variant"]
    frozen = config["variants"][variant]
    mappings = {
        "ticker_cap_of_initial_equity": "ticker_cap_of_initial",
        "trim_ticker_above_of_initial_equity": "trim_ticker_above_of_initial",
        "trim_destination_of_initial_equity": "ticker_cap_of_initial",
        "margin_cap_of_equity": "margin_cap",
        "delever_margin_above": "delever_margin_above",
        "delever_to": "delever_to",
        "trade_band_of_equity": "trade_band_of_equity",
        "rebalance_anchor": "rebalance_anchor",
        "rebalance_every_sessions": "rebalance_every",
        "max_positions": "max_positions",
        "n_short": "n_short",
        "max_executed_orders_per_day": "max_orders_per_day",
        "competition_order_cap": "competition_order_cap",
        "emergency_flatten_reserve_orders": "emergency_flatten_reserve",
        "initial_equity": "initial_equity",
        "drawdown_flatten_and_latch": "drawdown_limit",
        "etf_min_price_usd": "min_price",
        "quote_max_age_seconds": "quote_max_age_seconds", "account_max_age_seconds": "account_max_age_seconds",
        "limit_band_bps": "limit_band_bps", "order_ttl_seconds": "order_ttl_seconds",
        "options_enabled": "options_enabled",
    }
    for spec_key, config_key in mappings.items():
        require(t["rules"][spec_key] == config[config_key], f"Config drift: {config_key}")
    require(t["rules"]["etf_sleeve"] == config["etf_sleeve"], "ETF sleeve drift")
    require(t["rules"]["etf_universe"] == config["etf_universe"], "ETF universe drift")
    require(t["rules"]["spy_overflow_order"] == config["spy_route"], "SPY overflow route drift")
    require(t["rules"]["etf_min_fund_size_usd"] == config["min_etf_market_value"], "ETF fund-size screen drift")
    require(t["rules"]["short_min_market_cap_usd"] == config["min_short_market_cap"], "Short size screen drift")
    require(config["strategy"] == PSTAR_STRATEGY, "Baseline drift")
    require(not config["execution_enabled"] or automatic["decided"] == "2026-11-21",
            "Local execution enabled without the 2026-11-21 user decision")
    require(t["rules"]["strategy"] == config["strategy"] and t["rules"]["variant"] == variant, "Strategy drift")
    require(t["rules"]["daily_loss_entry_halt"] is False and config["daily_loss_limit"] is None,
            "P* has no daily-loss entry halt")
    require(t["rules"]["residual_beta_limit"] is False, "P* must not claim a residual-beta rule")
    require(t["rules"]["between_rebalance_may_add_exposure"] is False, "Between-rebalance exposure is addable")
    require(t["rules"]["variant_switch"] == "one_config_line_no_code_change", "Variant switch is not a config line")
    require(sorted(t["rules"]["variants_available"]) == sorted(config["variants"]), "Frozen variant set drift")
    require(t["target_contract"]["strategy"] == config["strategy"], "Target strategy drift")
    require(t["target_contract"]["variant"] == variant, "Target variant drift")
    require(t["target_contract"]["strategy_id"] == frozen["strategy_id"], "Target strategy id drift")
    require(t["target_contract"]["factors"] == frozen["factors"], "Frozen factor set drift")
    require(t["target_contract"]["k_scale"] == frozen["k_scale"], "Frozen scale drift")
    require(t["target_contract"]["schema_version"] == config["schema_version"], "Target schema drift")
    require(t["target_contract"]["single_source_of_truth"] == config["single_source_of_truth"], "Source of truth drift")
    require(config["single_source_of_truth"] == "research/r10_deploy.py", "P* must compute in r10_deploy.py only")
    require(t["rules"]["single_stock_longs_allowed"] is False, "Single-stock longs granted")
    require(t["rules"]["new_per_name_take_profit_or_streak_overlay"] is False, "Unapproved overlay")
    require(t["workflow"]["approval_ttl_seconds"] == config["approval_ttl_seconds"], "Approval config drift")
    journal = t["journal_contract"]
    require(journal["writer"] == "trusted_relay_only", "Journal has multiple writers")
    require(journal["decision_required"] == DECISION_FIELDS and journal["fill_required"] == FILL_FIELDS, "Journal fields drift")
    require(t["release_evidence"]["all_runtime_evidence_status"] == "UNVERIFIED", "Fabricated runtime evidence")
    require(t["release_evidence"]["frozen_book_live_trading_proven"] is False, "Unproven live-trading claim")


def validate_evaluations(evaluations: dict) -> None:
    fixtures = evaluations["fixtures"]
    require(tuple(item["id"] for item in fixtures) == FIXTURE_IDS, "Exactly four fixed fixtures are required")
    require(evaluations["cloud_status"] == "UNVERIFIED", "Cloud evaluation was not performed")
    require(evaluations["cloud_execution_requires_separate_authorization"] is True, "Cloud authorization missing")
    for fixture in fixtures:
        require(fixture["prompt"].startswith("Evaluation only:"), "Fixture is not clearly synthetic")
        require(fixture["local_assertions"] and fixture["cloud_assertions"], "Missing assertions")
        for assertion in fixture["local_assertions"]:
            require(assertion["source"] in SOURCE_PATHS, "Unpinned assertion source")
            require(assertion["op"] in {"eq", "contains"}, "Unsupported assertion")
        for assertion in fixture["cloud_assertions"]:
            require(assertion["op"] == "eq" and assertion["pointer"].startswith("/"), "Invalid cloud assertion")


def validate_research_source(config: dict) -> None:
    """The locked research file must still be the single source that the configuration points at."""
    source = local_path("research/r10_deploy.py").read_text(encoding="utf-8")
    require(bool(VARIANTS_RE.search(source)), "P* variants are not defined in research/r10_deploy.py")
    deployed = DEPLOYED_RE.search(source)
    require(deployed is not None, "research/r10_deploy.py does not record DEPLOYED")
    require(deployed.group(1) == config["variant"],
            f"configured variant {config['variant']} is not the frozen DEPLOYED {deployed.group(1)}")
    require(str(config["variants"][config["variant"]]["k_scale"]) in source,
            "the configured frozen scale is not present in the research source")


def build_report() -> dict:
    templates = load("agents/templates.json")
    config = load("live/config.pstar.json")
    evaluations = load("agents/evaluations.json")
    lock = load("agents/release-lock.json")
    errors = []
    checks = (
        ("metadata_and_skills", validate_metadata),
        ("source_lock", lambda: validate_source_lock(lock)),
        ("desired_templates", lambda: validate_templates(templates, config)),
        ("fixed_fixture_contract", lambda: validate_evaluations(evaluations)),
        ("research_source_of_truth", lambda: validate_research_source(config)),
    )
    for name, check in checks:
        try:
            check()
        except (ValueError, KeyError, TypeError, OSError) as exc:
            errors.append(f"{name}: {exc}")
    fixtures = fixture_results(evaluations)
    return {
        "scope": "offline_specification_invariants_only",
        "local_revision": "0.3.5", "activation": "BLOCKED",
        "cloud_prompt_evaluation": "UNVERIFIED", "runtime_gate_tested": False,
        "status": "FAIL" if errors or any(f["failures"] for f in fixtures) else "PASS",
        "spec_errors": errors, "fixtures": fixtures,
        "source_sha256": {path: sha256(path) for path in sorted(SOURCE_PATHS)},
        "release_lock_sha256": sha256("agents/release-lock.json"),
    }


class RegressionSelfCheck(unittest.TestCase):
    def setUp(self):
        self.templates = load("agents/templates.json")
        self.evaluations = load("agents/evaluations.json")

    def assert_fixture_rejects(self, path: str, value):
        changed = copy.deepcopy(self.templates)
        parent_path, _, key = path.rpartition("/")
        pointer(changed, parent_path)[key] = value
        results = fixture_results(self.evaluations, {"agents/templates.json": changed})
        self.assertTrue(any(item["local_status"] == "FAIL" for item in results))

    def test_current_release(self):
        self.assertEqual(build_report()["status"], "PASS")

    def test_exactly_four_fixtures(self):
        validate_evaluations(self.evaluations)
        self.assertEqual(len(self.evaluations["fixtures"]), 4)

    def test_wrong_account_mutation(self):
        self.assert_fixture_rejects("/rules/account_ids", {"test": 5832812, "competition": 5832812})

    def test_permission_bypass_mutation(self):
        self.assert_fixture_rejects("/permission_policy/paper_order_mutations", "allow")

    def test_payload_inspection_mutation(self):
        self.assert_fixture_rejects("/permission_policy/verify_actual_raw_payload", False)

    def test_single_stock_long_mutation(self):
        self.assert_fixture_rejects("/rules/single_stock_longs_allowed", True)

    def test_target_approval_mutation(self):
        self.assert_fixture_rejects("/workflow/target_change_invalidates_approval", False)

    def test_early_close_mutation(self):
        self.assert_fixture_rejects("/schedule_intent/early_close/report", "16:10")

    def test_lease_mutation(self):
        self.assert_fixture_rejects("/workflow/lock", "none")

    def assert_templates_reject(self, mutate, expected: str):
        templates = copy.deepcopy(self.templates)
        mutate(templates)
        with self.assertRaisesRegex(ValueError, expected):
            validate_templates(templates, load("live/config.pstar.json"))

    def test_live_order_tool_rejected(self):
        def mutate(templates):
            role = templates["roles"]["guard"]
            role["desired_order_tools"] = ["sim_trade_input_order", "trading_order_place"]
            role["desired_order_permissions"] = {tool: "always_ask" for tool in role["desired_order_tools"]}

        self.assert_templates_reject(mutate, "paper pair")

    def test_live_account_read_rejected(self):
        def mutate(templates):
            templates["roles"]["risk-auditor"]["desired_read_tools"][0] = "account_positions"

        self.assert_templates_reject(mutate, "read tool")

    def test_missing_live_namespace_deny_rejected(self):
        self.assert_templates_reject(
            lambda templates: templates["permission_policy"].__setitem__("denied_tool_prefixes", ["crypto_account_"]),
            "namespaces not denied",
        )

    def test_hash_tampering_rejected(self):
        lock = load("agents/release-lock.json")
        lock["source_sha256"]["agents/guard.md"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "Source hash mismatch"):
            validate_source_lock(lock)

    def test_skill_name_mismatch_rejected(self):
        with self.assertRaisesRegex(ValueError, "name mismatch"):
            validate_skill("---\nname: wrong\ndescription: Check targets.\n---\ninstructions, not permissions", SKILLS[0])

    def test_strict_json_and_bool(self):
        with self.assertRaises(ValueError):
            parse_json('{"a":1,"a":2}')
        with self.assertRaises(ValueError):
            parse_json('{"a":NaN}')
        with self.assertRaises(ValueError):
            assert_value(True, {"op": "eq", "value": 1})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-check", action="store_true", help="Run offline unittest checks and in-memory negative mutations")
    parser.add_argument("--baseline", type=Path, help="Read a prior stdout JSON report and print a reviewed diff; never write it")
    args = parser.parse_args()
    if args.self_check:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(RegressionSelfCheck)
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        if not result.wasSuccessful():
            return 1
    report = build_report()
    rendered = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    print(rendered, end="")
    if args.baseline:
        require(args.baseline.is_absolute(), "Baseline path must be absolute")
        before = json.dumps(parse_json(args.baseline.read_text(encoding="utf-8")), indent=2, sort_keys=True, allow_nan=False) + "\n"
        if before != rendered:
            sys.stderr.writelines(difflib.unified_diff(before.splitlines(True), rendered.splitlines(True), fromfile=str(args.baseline), tofile="current-offline-specification"))
            return 1
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, KeyError, IndexError, TypeError, OSError) as error:
        print(f"Specification evaluation failed: {error}", file=sys.stderr)
        sys.exit(1)
