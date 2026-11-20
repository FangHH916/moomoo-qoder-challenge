"""Offline tests for the pre-trade eligibility screen: the competition's own limits on the ten sleeve
ETFs and the broker's borrow state for each short the frozen book asks for.

The screening never changes the book, so every test asserts the skipped name and its reason rather than a
replacement. Inputs are hand-built relay rows plus, for the end-to-end case, the committed research run.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from live import eligibility
from live.contracts import pstar_variant_config, timestamp
from live.test_pstar import NOW, config_for, run_file, snapshot

ETFS = config_for()["etf_universe"]


def etf_row(**updates) -> dict:
    row = {"verified_by_relay": True, "market": "US", "security_type": "ETF", "halted": False,
           "as_of": NOW.isoformat(), "market_value": 40_000_000_000.0, "market_cap": 40_000_000_000.0}
    return {**row, **updates}


def etf_quote(price: float = 100.0, **updates) -> dict:
    return {**{"verified_by_relay": True, "price": price, "as_of": NOW.isoformat()}, **updates}


def short_row(**updates) -> dict:
    row = {"verified_by_relay": True, "market": "US", "security_type": "COMMON_STOCK", "halted": False,
           "as_of": NOW.isoformat(), "market_cap": 5_000_000_000.0, "shortable": True, "easy_to_borrow": True}
    return {**row, **updates}


class EtfScreen(unittest.TestCase):
    def check(self, rows=None, quotes=None):
        return eligibility.check_etfs(config_for(), rows or {name: etf_row() for name in ETFS},
                                      quotes or {name: etf_quote() for name in ETFS}, NOW)

    def test_the_whole_sleeve_passes_when_everything_is_verified(self):
        result = self.check()
        self.assertEqual(result["tradable"], ETFS)
        self.assertEqual(result["skipped"], [])
        self.assertEqual(len(result["rows"]), len(ETFS))
        self.assertEqual(result["checked_at"], NOW.isoformat())

    def test_a_fund_smaller_than_the_limit_is_not_tradable(self):
        rows = {name: etf_row() for name in ETFS}
        rows["SLV"] = etf_row(market_value=499_999_999.0)
        result = self.check(rows)
        self.assertEqual(result["skipped"], ["SLV"])
        self.assertEqual(next(row["reasons"] for row in result["rows"] if row["symbol"] == "SLV"),
                         [f"market value $499,999,999 below the ${config_for()['min_etf_market_value']:,.0f} limit"])

    def test_the_fund_size_field_falls_back_until_one_is_found(self):
        for key in ("market_value", "market_cap", "fund_aum", "asset_netting_value"):
            with self.subTest(key=key):
                self.assertEqual(eligibility.market_value({key: 6e8}), 6e8)
        self.assertIsNone(eligibility.market_value({"market_cap": None}))
        self.assertIsNone(eligibility.market_value({}))
        rows = {name: etf_row() for name in ETFS}
        rows["IWM"] = etf_row(market_value=None, market_cap=None, fund_aum=4e8)
        self.assertEqual(next(row["reasons"] for row in self.check(rows)["rows"] if row["symbol"] == "IWM"),
                         ["market value $400,000,000 below the $500,000,000 limit"])

    def test_the_competition_price_floor_applies(self):
        quotes = {name: etf_quote() for name in ETFS}
        quotes["UUP"] = etf_quote(0.99)
        result = self.check(quotes=quotes)
        self.assertEqual(result["skipped"], ["UUP"])
        self.assertIn("price $0.99 below the $1 limit",
                      next(row["reasons"] for row in result["rows"] if row["symbol"] == "UUP"))

    def test_everything_the_relay_must_confirm_for_an_etf(self):
        cases = {
            "not relay-verified": lambda rows, quotes: rows["GLD"].update(verified_by_relay=False),
            "classified as a stock": lambda rows, quotes: rows["GLD"].update(security_type="COMMON_STOCK"),
            "not a US listing": lambda rows, quotes: rows["GLD"].update(market="HK"),
            "halted": lambda rows, quotes: rows["GLD"].update(halted=True),
            "halt state unknown": lambda rows, quotes: rows["GLD"].pop("halted"),
            "market value unknown": lambda rows, quotes: rows["GLD"].update(market_value=None, market_cap=None),
            "quote not relay-verified": lambda rows, quotes: quotes["GLD"].update(verified_by_relay=False),
            "no usable quote": lambda rows, quotes: quotes["GLD"].update(price=0),
            "stale quote": lambda rows, quotes: quotes["GLD"].update(
                as_of=(NOW - timedelta(seconds=config_for()["quote_max_age_seconds"] + 1)).isoformat()),
            "quote from the future": lambda rows, quotes: quotes["GLD"].update(as_of=(NOW + timedelta(
                seconds=10)).isoformat()),
            "no quote at all": lambda rows, quotes: quotes.pop("GLD"),
            "no broker record": lambda rows, quotes: rows.pop("GLD"),
        }
        for label, corrupt in cases.items():
            with self.subTest(case=label):
                rows = {name: etf_row() for name in ETFS}
                quotes = {name: etf_quote() for name in ETFS}
                corrupt(rows, quotes)
                result = self.check(rows, quotes)
                self.assertEqual(result["skipped"], ["GLD"])
                self.assertTrue(next(row["reasons"] for row in result["rows"] if row["symbol"] == "GLD"))

    def test_the_recorded_price_of_a_skipped_etf_stays_readable(self):
        rows = {name: etf_row() for name in ETFS}
        quotes = {name: etf_quote() for name in ETFS}
        quotes["TLT"] = etf_quote(price=None)
        rows["IEF"].update(verified_by_relay=False)
        result = self.check(rows, quotes)
        self.assertIsNone(next(row for row in result["rows"] if row["symbol"] == "TLT")["price"])
        self.assertEqual(next(row for row in result["rows"] if row["symbol"] == "IEF")["market_value"],
                         40_000_000_000.0)


class BorrowScreen(unittest.TestCase):
    def check(self, rows=None, symbols=("AAA", "BBB")):
        return eligibility.check_shorts(config_for(), rows or {name: short_row() for name in symbols},
                                        list(symbols))

    def test_confirmed_borrow_passes_for_every_requested_short(self):
        result = self.check()
        self.assertEqual(result["tradable"], ["AAA", "BBB"])
        self.assertEqual(result["skipped"], [])

    def test_borrow_and_size_are_hard_requirements(self):
        cases = {
            "not shortable": lambda rows: rows["AAA"].update(shortable=False),
            "not easy to borrow": lambda rows: rows["AAA"].update(easy_to_borrow=False),
            "borrow state unknown": lambda rows: rows["AAA"].pop("shortable"),
            "below the $1B short cap": lambda rows: rows["AAA"].update(market_cap=999_999_999.0),
            "market cap unknown": lambda rows: rows["AAA"].update(market_cap=None),
            "not a common stock": lambda rows: rows["AAA"].update(security_type="ETF"),
            "halted": lambda rows: rows["AAA"].update(halted=True),
            "no relay record": lambda rows: rows["AAA"].update(verified_by_relay=False),
            "not on the broker list at all": lambda rows: rows.pop("AAA"),
        }
        for label, corrupt in cases.items():
            with self.subTest(case=label):
                rows = {name: short_row() for name in ("AAA", "BBB")}
                corrupt(rows)
                result = self.check(rows)
                self.assertEqual([result["tradable"], result["skipped"]], [["BBB"], ["AAA"]])
                self.assertTrue(next(row["reasons"] for row in result["rows"] if row["symbol"] == "AAA"))

    def test_only_a_symbol_the_broker_can_name_is_screenable(self):
        for name in ("us.aaa", "AA AI*", "", "aapl"):
            with self.subTest(symbol=name):
                result = self.check({name: short_row(), "BBB": short_row()}, (name, "BBB"))
                self.assertEqual([result["tradable"], result["skipped"]], [["BBB"], [name]])
                self.assertIn("invalid symbol", next(row["reasons"] for row in result["rows"]
                                                     if row["symbol"] == name))

    def test_an_unborrowable_short_is_skipped_and_never_replaced(self):
        rows = {name: short_row() for name in ("AAA", "BBB")}
        rows["CCC"] = short_row()
        result = self.check(rows | {"AAA": short_row(shortable=False)}, ("AAA", "BBB"))
        self.assertEqual([result["tradable"], result["skipped"]], [["BBB"], ["AAA"]])
        self.assertEqual(len(result["rows"]), 2)
        self.assertNotIn("CCC", [row["symbol"] for row in result["rows"]])


class BookLevelPlan(unittest.TestCase):
    def setUp(self):
        self.config = config_for()
        self.targets = {"weights": {"SPY": 0.2, "GLD": 0.1, "AAA": -0.01, "BBB": -0.01}}

    def plan(self, rows=None, quotes=None):
        return eligibility.plan_eligibility(self.config, self.targets,
                                            rows if rows is not None else {
                                                **{name: etf_row() for name in ETFS},
                                                **{name: short_row() for name in ("AAA", "BBB")}},
                                            quotes if quotes is not None else
                                            {**{name: etf_quote() for name in ETFS},
                                             **{name: etf_quote() for name in ("AAA", "BBB")}}, NOW)

    def test_a_fully_tradeable_book_reports_nothing_blocked(self):
        result = self.plan()
        self.assertEqual([result["blocked"], result["blocked_detail"], result["slack_report"]],
                         [[], {}, []])
        self.assertEqual(result["shorts"]["tradable"], ["AAA", "BBB"])

    def test_only_the_names_the_targets_ask_for_are_screened(self):
        rows = {**{name: etf_row() for name in ETFS}, **{name: short_row() for name in ("AAA", "BBB")},
                "IWM": etf_row(halted=True), "QQQ": etf_row(verified_by_relay=False)}
        result = self.plan(rows)
        self.assertEqual(result["blocked"], [])
        self.assertIn("IWM", result["etfs"]["skipped"])
        self.assertEqual(result["slack_report"], [])

    def test_a_blocked_long_is_reported_name_and_reason(self):
        rows = {**{name: etf_row() for name in ETFS}, **{name: short_row() for name in ("AAA", "BBB")}}
        rows["GLD"] = etf_row(market_value=1e8)
        result = self.plan(rows)
        self.assertEqual(result["blocked"], ["GLD"])
        self.assertIn("below the", result["blocked_detail"]["GLD"][0])
        self.assertIn("ETF GLD skipped", result["slack_report"][0])
        self.assertIn("Weight not redistributed.", result["slack_report"][0])

    def test_a_blocked_short_is_reported_and_counted(self):
        rows = {**{name: etf_row() for name in ETFS}, "AAA": short_row(easy_to_borrow=False),
                "BBB": short_row()}
        result = self.plan(rows)
        self.assertEqual(result["blocked"], ["AAA"])
        self.assertIn("short AAA skipped:", result["slack_report"][0])
        self.assertTrue(result["slack_report"][-1].startswith("short book trades 1 of 2 names"))

    def test_a_missing_broker_record_blocks_a_short_too(self):
        rows = {**{name: etf_row() for name in ETFS}, "BBB": short_row()}
        result = self.plan(rows)
        self.assertEqual(result["blocked"], ["AAA"])
        self.assertIn("no verified broker record", result["blocked_detail"]["AAA"])

    def test_the_committed_run_book_is_fully_screenable(self):
        """The frozen book against relay-shaped rows: nothing is blocked, so the plan trades all 82 names."""
        run = run_file("pstar15")
        weights = {row["symbol"]: row["weight"] for row in run["targets"]}
        etf_names = set(self.config["etf_universe"])
        sleeve = etf_names & set(weights)
        rows, quotes = {name: etf_row() for name in etf_names}, {name: etf_quote(200.0) for name in etf_names}
        for name, weight in weights.items():
            if name in etf_names:
                continue
            rows[name] = short_row()
            quotes[name] = etf_quote(round(abs(weight) * 100_000, 2))
        result = eligibility.plan_eligibility(self.config, {"weights": weights}, rows, quotes, NOW)
        self.assertEqual(result["blocked"], [])
        self.assertEqual(len(result["shorts"]["tradable"]), self.config["n_short"])
        self.assertEqual(result["etfs"]["tradable"], ETFS)
        self.assertEqual(len(result["shorts"]["rows"]), self.config["n_short"])
        self.assertEqual(sorted(row["symbol"] for row in result["etfs"]["rows"] if row["symbol"] in sleeve),
                         sorted(sleeve))
        self.assertEqual(result["slack_report"], [])


class Freshness(unittest.TestCase):
    def test_the_age_window_is_closed_at_both_ends(self):
        limit = config_for()["quote_max_age_seconds"]
        for seconds, expected in ((0, True), (limit, True), (limit + 1, False), (-1, False)):
            with self.subTest(seconds=seconds):
                record = {"as_of": (NOW - timedelta(seconds=seconds)).isoformat()}
                self.assertEqual(eligibility._fresh(record, NOW, limit), expected)
        self.assertFalse(eligibility._fresh({}, NOW, limit))
        self.assertFalse(eligibility._fresh({"as_of": "not a timestamp"}, NOW, limit))

    def test_the_module_never_needs_the_wall_clock(self):
        self.assertEqual(timestamp(NOW.isoformat()), NOW)
        self.assertIsInstance(NOW, datetime)


if __name__ == "__main__":
    unittest.main()
