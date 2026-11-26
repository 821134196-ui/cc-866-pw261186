"""HTTP API tests against a live in-process server on an ephemeral port."""

from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from event_contract_registry.server import create_app


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.server, self.db = create_app(":memory:", seed=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    # -- basic -----------------------------------------------------------
    def test_event_types_seeded(self):
        status, data = self.request("GET", "/api/event-types")
        self.assertEqual(status, 200)
        names = {e["name"] for e in data["event_types"]}
        self.assertIn("order.placed", names)

    def test_event_detail_has_matrix(self):
        status, data = self.request("GET", "/api/event-types/order.placed")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["matrix"]["rows"]), 4)
        self.assertEqual(len(data["matrix"]["consumers"]), 5)

    def test_unknown_event_404(self):
        status, data = self.request("GET", "/api/event-types/nope.nope")
        self.assertEqual(status, 404)

    def test_bad_json_400(self):
        req = urllib.request.Request(
            self.base + "/api/event-types/order.placed/simulate",
            data=b"{not json",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 400)

    # -- key scenarios ---------------------------------------------------
    def test_release_check_verdicts_distinct(self):
        status, data = self.request(
            "POST", "/api/event-types/order.placed/release-check", {"candidate_version": 4}
        )
        self.assertEqual(status, 200)
        verdicts = {c["consumer_name"]: c["verdict"] for c in data["consumers"]}
        self.assertEqual(verdicts["billing-legacy"], "dual_write")
        self.assertEqual(verdicts["audit-archive"], "direct")
        self.assertEqual(verdicts["warehouse-sync"], "wait_drain")
        self.assertEqual(verdicts["fraud-screening"], "blocked")
        # every non-direct verdict must name a field
        for entry in data["consumers"]:
            if entry["verdict"] != "direct":
                self.assertTrue(entry["breaking_fields"], entry["consumer_name"])

    def test_simulate_names_field_and_version(self):
        status, data = self.request(
            "POST",
            "/api/event-types/order.placed/simulate",
            {"consumer_name": "billing-legacy", "producer_versions": [1, 2, 4]},
        )
        self.assertEqual(status, 200)
        pairs = {p["producer_version"]: p for p in data["pairs"]}
        self.assertEqual(pairs[1]["failed"], 0)
        self.assertGreater(pairs[2]["failed"], 0)
        names = {
            f.get("producer_field") or f["field"] for f in pairs[2]["fields"]
        } | {f["field"] for f in pairs[2]["fields"]}
        self.assertTrue({"amount", "total_amount"} & names, names)

    def test_simulate_dual_write_repairs_rename(self):
        status, data = self.request(
            "POST",
            "/api/event-types/order.placed/simulate",
            {
                "consumer_name": "billing-legacy",
                "producer_versions": [2],
                "dual_write": True,
                "include_recorded": False,
            },
        )
        self.assertEqual(status, 200)
        pair = data["pairs"][0]
        self.assertEqual(pair["failed"], 0)

    def test_add_message_changes_backlog_verdict(self):
        # A v4 message that a v3 strict consumer cannot read.
        self.request(
            "POST",
            "/api/event-types/order.placed/messages",
            {
                "producer_version": 4,
                "payload": {
                    "order_id": "ORD-X",
                    "buyer_id": "C-1",
                    "amount": 10,
                    "currency": "CNY",
                    "placed_at": "2026-11-25T00:00:00Z",
                    "tax_amount": 1,
                    "coupon_code": "",
                    "payment_method": "card",
                    "shipping_address": {"city": "SH"},
                },
            },
        )
        status, data = self.request(
            "POST", "/api/event-types/order.placed/release-check", {"candidate_version": 4}
        )
        fraud = next(c for c in data["consumers"] if c["consumer_name"] == "fraud-screening")
        self.assertEqual(fraud["verdict"], "blocked")

    def test_create_contract_validates(self):
        status, data = self.request(
            "POST",
            "/api/event-types/order.placed/contracts",
            {"fields": [{"name": "bad name", "type": "string"}]},
        )
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "invalid_contract")

    def test_create_contract_appends_version_with_diff(self):
        status, data = self.request(
            "POST",
            "/api/event-types/inventory.adjusted/contracts",
            {
                "description": "增加批次号",
                "fields": [
                    {"name": "sku", "type": "string", "required": True},
                    {"name": "warehouse", "type": "string", "required": True},
                    {"name": "delta", "type": "number", "required": True},
                    {
                        "name": "at",
                        "type": "timestamp",
                        "required": True,
                        "timestamp_format": "iso8601",
                    },
                    {"name": "reason", "type": "string", "required": True, "default": "manual"},
                    {"name": "actor_id", "type": "uuid", "required": False},
                    {"name": "batch_no", "type": "string", "required": False},
                ],
            },
        )
        self.assertEqual(status, 200, data)
        self.assertEqual(data["contract"]["version"], 3)
        self.assertIn("added", {c["kind"] for c in data["diff"]["changes"]})

    def test_impacts_endpoint_marks_rename_dual_write(self):
        status, data = self.request("GET", "/api/event-types/order.placed/impacts?to=2")
        self.assertEqual(status, 200)
        rename = next(c for c in data["changes"] if c["kind"] == "renamed")
        self.assertEqual(rename["field"], "amount")
        self.assertTrue(rename["dual_write_resolves"])
        self.assertEqual(rename["verdict"], "dual_write")
        pairs = {p["consumer_name"]: p for p in rename["affected_pairs"]}
        self.assertEqual(pairs["billing-legacy"]["verdict"], "dual_write")
        self.assertEqual(pairs["fraud-screening"]["verdict"], "wait_drain")

    def test_consumer_upsert_and_delete(self):
        status, data = self.request(
            "PUT",
            "/api/event-types/inventory.adjusted/consumers",
            {
                "name": "temp-reader",
                "contract_version": 2,
                "mode": "backlog",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["contract_version"], 2)
        status, _ = self.request(
            "DELETE", "/api/event-types/inventory.adjusted/consumers/temp-reader"
        )
        self.assertEqual(status, 200)

    def test_consumer_unknown_version_rejected(self):
        status, data = self.request(
            "PUT",
            "/api/event-types/inventory.adjusted/consumers",
            {"name": "bad", "contract_version": 99},
        )
        self.assertEqual(status, 400)

    def test_static_index_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as res:
            body = res.read().decode()
        self.assertIn("事件契约注册", body)


if __name__ == "__main__":
    unittest.main()
