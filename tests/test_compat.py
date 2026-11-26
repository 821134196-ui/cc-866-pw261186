"""Compatibility engine tests.

Run with: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from event_contract_registry import compat
from event_contract_registry.contracts import (
    ContractError,
    normalize_consumer,
    normalize_contract,
)


def contract(version: int, fields: list[dict], description: str = "") -> dict:
    normalized = normalize_contract({"fields": fields, "description": description})
    return {
        "version": version,
        "fields": normalized["fields"],
        "description": description,
        "status": "published",
        "notes": "",
        "created_at": "",
    }


def consumer(
    name: str,
    version: int,
    mode: str = "new_only",
    *,
    strict_unknown: bool = False,
    follow_renames: bool = False,
    overrides: list[dict] | None = None,
) -> dict:
    normalized = normalize_consumer(
        {
            "event_type": "t.event",
            "name": name,
            "contract_version": version,
            "mode": mode,
            "strict_unknown": strict_unknown,
            "follow_renames": follow_renames,
            "overrides": overrides or [],
        }
    )
    normalized["notes"] = ""
    return normalized


def string_field(name: str, **kwargs) -> dict:
    return {"name": name, "type": "string", "required": True, **kwargs}


class RenameTests(unittest.TestCase):
    def setUp(self):
        self.v1 = contract(1, [string_field("customer_id"), string_field("order_id")])
        self.v2 = contract(
            2,
            [
                string_field("buyer_id", renamed_from="customer_id"),
                string_field("order_id"),
            ],
        )
        self.contracts = [self.v1, self.v2]
        self.index = compat.lineage_index(self.contracts)

    def test_rename_is_breaking_for_frozen_consumer(self):
        old = consumer("old-reader", 1)
        findings = compat.compare_contracts(self.v2, self.v1, old, self.index)
        rename = [f for f in findings if f["code"] == "renamed_field"][0]
        self.assertEqual(rename["severity"], "breaking")
        self.assertEqual(rename["producer_field"], "buyer_id")
        self.assertEqual(rename["field"], "customer_id")

    def test_dual_write_makes_rename_consumable(self):
        old = consumer("old-reader", 1)
        messages = compat.simulate_messages(
            self.contracts, [2], dual_write=True
        )
        results = [compat.decode_message(m, self.v1, old, self.index) for m in messages]
        self.assertTrue(all(r["ok"] for r in results))

    def test_rename_without_dual_write_fails(self):
        old = consumer("old-reader", 1)
        messages = compat.simulate_messages(self.contracts, [2])
        results = [compat.decode_message(m, self.v1, old, self.index) for m in messages]
        self.assertTrue(all(not r["ok"] for r in results))

    def test_follow_renames_resolves_alias(self):
        tracking = consumer("tracking", 1, follow_renames=True)
        findings = compat.compare_contracts(self.v2, self.v1, tracking, self.index)
        self.assertFalse([f for f in findings if f["severity"] == "breaking"])
        messages = compat.simulate_messages(self.contracts, [2])
        self.assertTrue(
            all(compat.decode_message(m, self.v1, tracking, self.index)["ok"] for m in messages)
        )

    def test_release_verdict_is_dual_write_for_rename_only(self):
        old = consumer("old-reader", 1, "new_only")
        report = compat.release_check(self.contracts, [old], [], 2, base_time=_fixed_time())
        entry = report["consumers"][0]
        self.assertEqual(entry["verdict"], "dual_write")
        self.assertEqual(entry["stage"], "frozen_new")
        self.assertTrue(entry["dual_write_preview"]["ok"])


class DefaultFillTests(unittest.TestCase):
    def test_optional_becomes_required_with_consumer_default(self):
        producer = contract(1, [{"name": "currency", "type": "string", "required": False}])
        upgraded_view = contract(2, [{"name": "currency", "type": "string", "required": True}])
        view = consumer(
            "with-default",
            2,
            overrides=[{"field": "currency", "default": "USD"}],
        )
        index = compat.lineage_index([producer, upgraded_view])
        findings = compat.compare_contracts(producer, upgraded_view, view, index)
        codes = {f["code"] for f in findings}
        self.assertIn("consumer_default_fill", codes)
        self.assertNotIn("required_field_may_be_absent", codes)

    def test_optional_becomes_required_without_default_breaks(self):
        producer = contract(1, [{"name": "currency", "type": "string", "required": False}])
        upgraded_view = contract(2, [{"name": "currency", "type": "string", "required": True}])
        view = consumer("no-default", 2)
        index = compat.lineage_index([producer, upgraded_view])
        findings = compat.compare_contracts(producer, upgraded_view, view, index)
        breaking = [f for f in findings if f["code"] == "required_field_may_be_absent"]
        self.assertEqual(len(breaking), 1)
        self.assertEqual(breaking[0]["severity"], "breaking")

    def test_minimal_serialization_exposes_absent_default(self):
        contract_v = contract(
            1,
            [
                {
                    "name": "channel",
                    "type": "string",
                    "required": False,
                    "default": "web",
                }
            ],
        )
        scenarios = compat.scenario_payloads(contract_v)
        variants = {name for name, _ in scenarios}
        self.assertEqual(variants, {"full", "minimal"})
        minimal = dict(scenarios)["minimal"]
        self.assertNotIn("channel", minimal)

    def test_default_divergence_is_degraded(self):
        v1 = contract(
            1,
            [{"name": "currency", "type": "string", "required": True, "default": "USD"}],
        )
        view = consumer(
            "diff-default",
            1,
            overrides=[{"field": "currency", "default": "CNY"}],
        )
        index = compat.lineage_index([v1])
        findings = compat.compare_contracts(v1, v1, view, index)
        divergence = [f for f in findings if f["code"] == "default_divergence"]
        self.assertEqual(divergence[0]["severity"], "degraded")


class RequiredTests(unittest.TestCase):
    def setUp(self):
        self.v1 = contract(
            1,
            [
                string_field("order_id"),
                {"name": "coupon", "type": "string", "required": False},
            ],
        )
        self.v2 = contract(
            2,
            [
                string_field("order_id"),
                {"name": "coupon", "type": "string", "required": True},
                string_field("shipping_address", type="object", required=True),
            ],
        )

    def test_required_addition_is_ignored_by_old_consumer(self):
        # A brand-new required field does not break the *old* reader; it only
        # matters to whoever requires it.
        old = consumer("old", 1)
        index = compat.lineage_index([self.v1, self.v2])
        findings = compat.compare_contracts(self.v2, self.v1, old, index)
        self.assertIn(
            "shipping_address",
            {f["field"] for f in findings if f["code"] == "added_field_ignored"},
        )
        # `coupon` exists on both sides; the producer merely made it required.
        required = [f for f in findings if f["code"] == "producer_required_consumer_optional"]
        self.assertEqual({f["field"] for f in required}, {"coupon"})

    def test_new_consumer_needs_old_producer_to_fill_field(self):
        new_consumer = consumer("new", 2)
        index = compat.lineage_index([self.v1, self.v2])
        findings = compat.compare_contracts(self.v1, self.v2, new_consumer, index)
        missing = [f for f in findings if f["code"] == "missing_required_field"]
        self.assertEqual({f["field"] for f in missing}, {"shipping_address"})
        # The optional producer field must be reported as a possible absence.
        absent = [f for f in findings if f["code"] == "required_field_may_be_absent"]
        self.assertEqual({f["field"] for f in absent}, {"coupon"})

    def test_null_required_field_is_rejected(self):
        old = consumer("old", 1)
        index = compat.lineage_index([self.v1])
        message = {"producer_version": 1, "payload": {"order_id": None}, "label": "x"}
        result = compat.decode_message(message, self.v1, old, index)
        self.assertIn("null_required", {e["code"] for e in result["errors"]})


class ConsumerModeTests(unittest.TestCase):
    def setUp(self):
        self.v1 = contract(1, [string_field("order_id"), string_field("legacy_note", required=False)])
        self.v2 = contract(
            2,
            [
                string_field("order_id"),
                string_field("note", rename_from=None) if False else
                {"name": "note", "type": "string", "required": False},
            ],
        )

    def test_new_only_consumer_ignores_backlog(self):
        v1 = contract(
            1,
            [
                string_field("order_id"),
                string_field("customer_id"),
            ],
        )
        v2 = contract(
            2,
            [
                string_field("order_id"),
                string_field("buyer_id", renamed_from="customer_id"),
            ],
        )
        new_only = consumer("fresh", 2, "new_only")
        report = compat.release_check([v1, v2], [new_only], [], 2, base_time=_fixed_time())
        self.assertEqual(report["consumers"][0]["verdict"], "direct")

    def test_backlog_consumer_must_wait_for_drain(self):
        # v1 may legitimately omit `note` (it has a default), so its messages
        # can lack the field a v2 consumer treats as required.
        v1 = contract(
            1,
            [
                string_field("order_id"),
                {"name": "note", "type": "string", "required": False, "default": "n/a"},
            ],
        )
        v2 = contract(
            2,
            [
                string_field("order_id"),
                {"name": "note", "type": "string", "required": True},
            ],
        )
        backlog = consumer("replayer", 2, "backlog")
        report = compat.release_check([v1, v2], [backlog], [], 2, base_time=_fixed_time())
        entry = report["consumers"][0]
        # New messages are fine; replaying v1 messages is where it fails.
        self.assertEqual(entry["stage"], "upgraded_backlog")
        self.assertEqual(entry["verdict"], "wait_drain")
        self.assertGreater(entry["backlog"]["failed"], 0)
        self.assertEqual(entry["frozen"]["failed"], 0)


class TypeAndEnumTests(unittest.TestCase):
    def test_timestamp_format_change_is_breaking(self):
        v1 = contract(
            1,
            [
                {
                    "name": "at",
                    "type": "timestamp",
                    "required": True,
                    "timestamp_format": "epoch_s",
                }
            ],
        )
        v2 = contract(
            2,
            [
                {
                    "name": "at",
                    "type": "timestamp",
                    "required": True,
                    "timestamp_format": "iso8601",
                }
            ],
        )
        index = compat.lineage_index([v1, v2])
        old = consumer("old", 1)
        findings = compat.compare_contracts(v2, v1, old, index)
        codes = {f["code"] for f in findings}
        self.assertIn("timestamp_format_mismatch", codes)

    def test_number_to_integer_is_degraded(self):
        producing = contract(1, [{"name": "qty", "type": "number", "required": True}])
        expecting = contract(2, [{"name": "qty", "type": "integer", "required": True}])
        index = compat.lineage_index([producing, expecting])
        old = consumer("old", 2)
        findings = compat.compare_contracts(producing, expecting, old, index)
        narrow = [f for f in findings if f["code"] == "type_narrowing"]
        self.assertEqual(narrow[0]["severity"], "degraded")

    def test_enum_violation_is_caught_in_decode(self):
        contract_v = contract(
            1,
            [
                {
                    "name": "mood",
                    "type": "string",
                    "required": True,
                    "enum": ["happy", "sad"],
                }
            ],
        )
        old = consumer("old", 1)
        index = compat.lineage_index([contract_v])
        message = {
            "producer_version": 1,
            "payload": {"mood": "angry"},
            "label": "x",
        }
        result = compat.decode_message(message, contract_v, old, index)
        self.assertEqual(result["errors"][0]["code"], "enum_violation")

    def test_new_enum_member_breaks_pinned_consumer(self):
        old_contract = contract(
            1,
            [{"name": "method", "type": "string", "required": True, "enum": ["card", "cash"]}],
        )
        new_contract = contract(
            2,
            [
                {
                    "name": "method",
                    "type": "string",
                    "required": True,
                    "enum": ["card", "cash", "crypto"],
                }
            ],
        )
        pinned = consumer("pinned", 1)
        index = compat.lineage_index([old_contract, new_contract])
        messages = compat.simulate_messages([old_contract, new_contract], [2])
        variants = {m["variant"] for m in messages}
        self.assertIn("edge", variants)
        failed = [
            compat.decode_message(m, old_contract, pinned, index) for m in messages
        ]
        codes = {e["code"] for r in failed for e in r["errors"]}
        self.assertIn("enum_violation", codes)

    def test_strict_unknown_rejects_added_field(self):
        v1 = contract(1, [string_field("order_id")])
        v2 = contract(2, [string_field("order_id"), string_field("extra")])
        index = compat.lineage_index([v1, v2])
        strict = consumer("strict", 1, strict_unknown=True)
        findings = compat.compare_contracts(v2, v1, strict, index)
        self.assertIn("unknown_field", {f["code"] for f in findings})
        lenient = consumer("lenient", 1)
        findings = compat.compare_contracts(v2, v1, lenient, index)
        self.assertIn("added_field_ignored", {f["code"] for f in findings})


class ValidationTests(unittest.TestCase):
    def test_duplicate_field_names_rejected(self):
        with self.assertRaises(ContractError):
            normalize_contract(
                {"fields": [string_field("a"), string_field("a")]}
            )

    def test_renamed_from_conflicting_with_existing_field_rejected(self):
        with self.assertRaises(ContractError):
            normalize_contract(
                {
                    "fields": [
                        string_field("a"),
                        string_field("b", renamed_from="a"),
                    ]
                }
            )

    def test_bad_default_type_rejected(self):
        with self.assertRaises(ContractError):
            normalize_contract(
                {
                    "fields": [
                        {"name": "n", "type": "integer", "required": True, "default": "x"}
                    ]
                }
            )

    def test_consumer_requires_positive_version(self):
        with self.assertRaises(ContractError):
            normalize_consumer(
                {
                    "event_type": "t",
                    "name": "c",
                    "contract_version": 0,
                }
            )


class DiffTests(unittest.TestCase):
    def test_diff_reports_rename_required_and_default(self):
        v1 = contract(
            1,
            [
                string_field("customer_id"),
                {"name": "currency", "type": "string", "required": True, "default": "USD"},
            ],
        )
        v2 = contract(
            2,
            [
                string_field("buyer_id", renamed_from="customer_id"),
                {"name": "currency", "type": "string", "required": True, "default": "CNY"},
                string_field("extra", required=False),
            ],
        )
        index = compat.lineage_index([v1, v2])
        changes = compat.diff_contracts(v1, v2, index)
        kinds = {(c["kind"], c["field"]) for c in changes}
        self.assertIn(("renamed", "buyer_id"), kinds)
        self.assertIn(("default_changed", "currency"), kinds)
        self.assertIn(("added", "extra"), kinds)


def _fixed_time():
    from datetime import datetime, timezone

    return datetime(2026, 11, 20, tzinfo=timezone.utc)


if __name__ == "__main__":
    unittest.main()
