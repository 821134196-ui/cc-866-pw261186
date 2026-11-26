"""Demo dataset: two event types with a realistic, messy evolution.

`order.placed` walks through field renames, default changes, optional fields
that later become required, and finally a genuinely breaking addition.
`inventory.adjusted` changes a timestamp format and widens a number type.
"""

from __future__ import annotations

from typing import Any

from .compat import release_check
from .contracts import normalize_contract, normalize_consumer
from .db import Database

ORDER_PLACED = "order.placed"
INVENTORY_ADJUSTED = "inventory.adjusted"


def _field(**kwargs: Any) -> dict[str, Any]:
    return kwargs


ORDER_V1 = {
    "description": "订单创建事件，结算与风控的原始输入",
    "fields": [
        _field(name="order_id", type="string", required=True, description="订单号"),
        _field(name="customer_id", type="string", required=True, description="下单客户"),
        _field(
            name="total_amount",
            type="number",
            required=True,
            description="订单总额（含税）",
        ),
        _field(
            name="currency",
            type="string",
            required=True,
            has_default=True,
            default="USD",
            enum=["USD", "EUR", "CNY", "JPY"],
            description="结算币种",
        ),
        _field(
            name="placed_at",
            type="timestamp",
            required=True,
            timestamp_format="iso8601",
            description="下单时间",
        ),
        _field(
            name="channel",
            type="string",
            required=False,
            has_default=True,
            default="web",
            enum=["web", "mobile", "store"],
            description="下单渠道",
        ),
    ],
}

ORDER_V2 = {
    "description": "金额字段更名为 amount，补充税额与优惠券",
    "fields": [
        _field(name="order_id", type="string", required=True, description="订单号"),
        _field(name="customer_id", type="string", required=True, description="下单客户"),
        _field(
            name="amount",
            type="number",
            required=True,
            renamed_from="total_amount",
            description="订单总额（含税）",
        ),
        _field(
            name="currency",
            type="string",
            required=True,
            has_default=True,
            default="USD",
            enum=["USD", "EUR", "CNY", "JPY"],
            description="结算币种",
        ),
        _field(
            name="placed_at",
            type="timestamp",
            required=True,
            timestamp_format="iso8601",
            description="下单时间",
        ),
        _field(
            name="channel",
            type="string",
            required=False,
            has_default=True,
            default="web",
            enum=["web", "mobile", "store"],
            description="下单渠道",
        ),
        _field(name="tax_amount", type="number", required=False, description="税额"),
        _field(name="coupon_code", type="string", required=False, description="优惠券"),
    ],
}

ORDER_V3 = {
    "description": "客户字段更名为 buyer_id，新增支付方式",
    "fields": [
        _field(name="order_id", type="string", required=True, description="订单号"),
        _field(
            name="buyer_id",
            type="string",
            required=True,
            renamed_from="customer_id",
            description="下单客户",
        ),
        _field(
            name="amount",
            type="number",
            required=True,
            renamed_from="total_amount",
            description="订单总额（含税）",
        ),
        _field(
            name="currency",
            type="string",
            required=True,
            has_default=True,
            default="USD",
            enum=["USD", "EUR", "CNY", "JPY"],
            description="结算币种",
        ),
        _field(
            name="placed_at",
            type="timestamp",
            required=True,
            timestamp_format="iso8601",
            description="下单时间",
        ),
        _field(
            name="channel",
            type="string",
            required=False,
            has_default=True,
            default="web",
            enum=["web", "mobile", "store"],
            description="下单渠道",
        ),
        _field(name="tax_amount", type="number", required=False, description="税额"),
        _field(name="coupon_code", type="string", required=False, description="优惠券"),
        _field(
            name="payment_method",
            type="string",
            required=True,
            has_default=True,
            default="card",
            enum=["card", "wallet", "invoice"],
            description="支付方式",
        ),
    ],
}

ORDER_V4 = {
    "description": "币种默认值切换为 CNY，税额与优惠券转必填，新增收货地址为必填",
    "fields": [
        _field(name="order_id", type="string", required=True, description="订单号"),
        _field(
            name="buyer_id",
            type="string",
            required=True,
            renamed_from="customer_id",
            description="下单客户",
        ),
        _field(
            name="amount",
            type="number",
            required=True,
            renamed_from="total_amount",
            description="订单总额（含税）",
        ),
        _field(
            name="currency",
            type="string",
            required=True,
            has_default=True,
            default="CNY",
            enum=["USD", "EUR", "CNY", "JPY"],
            description="结算币种",
        ),
        _field(
            name="placed_at",
            type="timestamp",
            required=True,
            timestamp_format="iso8601",
            description="下单时间",
        ),
        _field(
            name="channel",
            type="string",
            required=False,
            has_default=True,
            default="web",
            enum=["web", "mobile", "store"],
            description="下单渠道",
        ),
        _field(
            name="tax_amount",
            type="number",
            required=True,
            has_default=True,
            default=0,
            description="税额",
        ),
        _field(
            name="coupon_code",
            type="string",
            required=True,
            has_default=True,
            default="",
            description="优惠券",
        ),
        _field(
            name="payment_method",
            type="string",
            required=True,
            has_default=True,
            default="card",
            enum=["card", "wallet", "invoice", "crypto"],
            description="支付方式",
        ),
        _field(
            name="shipping_address",
            type="object",
            required=True,
            description="收货地址（v4 起必填）",
        ),
        _field(
            name="loyalty_tier",
            type="string",
            required=False,
            has_default=True,
            default="none",
            enum=["none", "silver", "gold"],
            description="会员等级",
        ),
    ],
}

INVENTORY_V1 = {
    "description": "库存调整流水",
    "fields": [
        _field(name="sku", type="string", required=True, description="SKU"),
        _field(name="warehouse", type="string", required=True, description="仓号"),
        _field(name="delta", type="integer", required=True, description="调整量"),
        _field(
            name="at",
            type="timestamp",
            required=True,
            timestamp_format="epoch_s",
            description="调整时间（Unix 秒）",
        ),
        _field(name="reason", type="string", required=False, description="原因"),
    ],
}

INVENTORY_V2 = {
    "description": "时间改为 ISO8601，调整量拓宽为 number，原因为必填",
    "fields": [
        _field(name="sku", type="string", required=True, description="SKU"),
        _field(name="warehouse", type="string", required=True, description="仓号"),
        _field(name="delta", type="number", required=True, description="调整量"),
        _field(
            name="at",
            type="timestamp",
            required=True,
            timestamp_format="iso8601",
            description="调整时间（ISO8601）",
        ),
        _field(
            name="reason",
            type="string",
            required=True,
            has_default=True,
            default="manual",
            description="原因",
        ),
        _field(name="actor_id", type="uuid", required=False, description="操作人"),
    ],
}

CONSUMERS = [
    {
        "event_type": ORDER_PLACED,
        "name": "billing-legacy",
        "contract_version": 1,
        "mode": "new_only",
        "strict_unknown": False,
        "follow_renames": False,
        "notes": "老旧结算服务，只消费上线后的新消息",
    },
    {
        "event_type": ORDER_PLACED,
        "name": "analytics-etl",
        "contract_version": 2,
        "mode": "backlog",
        "strict_unknown": False,
        "follow_renames": True,
        "notes": "离线数仓，会重放历史分区",
    },
    {
        "event_type": ORDER_PLACED,
        "name": "fraud-screening",
        "contract_version": 3,
        "mode": "backlog",
        "strict_unknown": True,
        "follow_renames": False,
        "notes": "风控实时链路，拒绝未知字段，必须消化积压",
    },
    {
        "event_type": ORDER_PLACED,
        "name": "warehouse-sync",
        "contract_version": 4,
        "mode": "backlog",
        "strict_unknown": False,
        "follow_renames": True,
        "notes": "履约系统，需回放近 30 天订单",
    },
    {
        "event_type": ORDER_PLACED,
        "name": "audit-archive",
        "contract_version": 4,
        "mode": "new_only",
        "strict_unknown": False,
        "follow_renames": False,
        "notes": "审计归档，只写新分区",
    },
    {
        "event_type": INVENTORY_ADJUSTED,
        "name": "wms-core",
        "contract_version": 1,
        "mode": "backlog",
        "strict_unknown": False,
        "follow_renames": False,
        "notes": "仓储核心，时间按 Unix 秒解析",
    },
    {
        "event_type": INVENTORY_ADJUSTED,
        "name": "stock-analytics",
        "contract_version": 2,
        "mode": "new_only",
        "strict_unknown": False,
        "follow_renames": True,
        "notes": "库存分析，只消费新流",
    },
]

RECORDED = [
    {
        "event_type": ORDER_PLACED,
        "producer_version": 1,
        "produced_at": "2026-10-02T09:14:00Z",
        "label": "v1/历史真实消息",
        "payload": {
            "order_id": "ORD-100231",
            "customer_id": "C-88213",
            "total_amount": 128.5,
            "currency": "USD",
            "placed_at": "2026-10-02T09:13:58Z",
            "channel": "web",
        },
    },
    {
        "event_type": ORDER_PLACED,
        "producer_version": 2,
        "produced_at": "2026-10-20T03:05:00Z",
        "label": "v2/历史真实消息",
        "payload": {
            "order_id": "ORD-100774",
            "customer_id": "C-90210",
            "amount": 64.0,
            "currency": "EUR",
            "placed_at": "2026-10-20T03:04:59Z",
            "channel": "mobile",
            "tax_amount": 10.2,
        },
    },
    {
        "event_type": ORDER_PLACED,
        "producer_version": 3,
        "produced_at": "2026-11-11T12:40:00Z",
        "label": "v3/历史真实消息",
        "payload": {
            "order_id": "ORD-101190",
            "buyer_id": "C-77120",
            "amount": 899.0,
            "currency": "CNY",
            "placed_at": "2026-11-11T12:39:51Z",
            "channel": "store",
            "tax_amount": 103.4,
            "coupon_code": "DOUBLE11",
            "payment_method": "wallet",
        },
    },
    {
        "event_type": INVENTORY_ADJUSTED,
        "producer_version": 1,
        "produced_at": "2026-10-08T07:00:00Z",
        "label": "v1/历史真实消息",
        "payload": {
            "sku": "SKU-4410",
            "warehouse": "WH-01",
            "delta": -3,
            "at": 1759906800,
            "reason": "damaged",
        },
    },
]


def seed(db: Database) -> None:
    event_types = {
        ORDER_PLACED: (ORDER_PLACED, "订单创建事件，演示字段重命名与必填收紧"),
        INVENTORY_ADJUSTED: (INVENTORY_ADJUSTED, "库存调整事件，演示时间格式迁移"),
    }
    for name, (_, description) in event_types.items():
        try:
            db.create_event_type(name, description)
        except Exception:
            pass

    order_contracts = [ORDER_V1, ORDER_V2, ORDER_V3, ORDER_V4]
    for version, raw in enumerate(order_contracts, start=1):
        event = db.get_event_type(ORDER_PLACED)
        normalized = normalize_contract(raw)
        try:
            db.insert_contract(
                event["id"], version, normalized["fields"], normalized["description"]
            )
        except Exception:
            pass

    for version, raw in enumerate([INVENTORY_V1, INVENTORY_V2], start=1):
        event = db.get_event_type(INVENTORY_ADJUSTED)
        normalized = normalize_contract(raw)
        try:
            db.insert_contract(
                event["id"], version, normalized["fields"], normalized["description"]
            )
        except Exception:
            pass

    for raw in CONSUMERS:
        event = db.get_event_type(raw["event_type"])
        normalized = normalize_consumer(raw)
        normalized["notes"] = raw.get("notes", "")
        db.upsert_consumer(event["id"], normalized)

    for raw in RECORDED:
        event = db.get_event_type(raw["event_type"])
        db.add_message(
            event["id"],
            raw["producer_version"],
            raw["payload"],
            raw["produced_at"],
            label=raw["label"],
        )

    # Record a release decision history entry for the latest published version.
    for name, candidate in ((ORDER_PLACED, 4), (INVENTORY_ADJUSTED, 2)):
        event = db.get_event_type(name)
        contracts = db.list_contracts(event["id"])
        consumers = db.list_consumers(event["id"])
        recorded = db.list_messages(event["id"])
        report = release_check(contracts, consumers, recorded, candidate)
        db.record_release(event["id"], candidate - 1, candidate, report)


def seed_if_empty(db: Database) -> bool:
    if db.list_event_types():
        return False
    seed(db)
    return True
