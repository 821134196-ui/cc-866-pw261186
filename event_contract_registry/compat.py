"""Compatibility engine.

Three questions, answered per (producer version, consumer) pair:

* schema: what does the producer emit that the consumer cannot read, field by
  field (rename / default fill / optional-to-required / type / enum);
* messages: what happens when a simulated stretch of history is replayed;
* rollout: can this consumer upgrade directly, does it need dual-writes, or must
  it drain the backlog first.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .contracts import (
    TYPE_INTEGER,
    TYPE_NUMBER,
    UNSET,
    compute_lineages,
    type_relation,
    value_matches_type,
)

SEVERITY_RANK = {"info": 0, "degraded": 1, "breaking": 2}
VERDICT_RANK = {"direct": 0, "wait_drain": 1, "dual_write": 2, "blocked": 3}
RENAME_CODES = {"renamed_field", "renamed_field_missing"}

SEVERITY_LABELS = {"info": "兼容", "degraded": "降级", "breaking": "不兼容"}
VERDICT_LABELS = {
    "direct": "可直接升级",
    "dual_write": "需双写",
    "wait_drain": "必须等旧消息处理完",
    "blocked": "不可升级",
    "degraded": "可升级但语义有损",
    "compatible": "兼容",
}
CHANGE_KIND_LABELS = {
    "added": "新增字段",
    "removed": "删除字段",
    "renamed": "重命名字段",
    "type_changed": "类型变更",
    "required_changed": "必填性变更",
    "default_changed": "默认值变更",
    "enum_changed": "枚举变更",
    "timestamp_format_changed": "时间格式变更",
}


# ---------------------------------------------------------------------------
# lineage: a field keeps its identity across renames
# ---------------------------------------------------------------------------


def lineage_index(contracts: list[dict[str, Any]]) -> dict[str, Any]:
    per_version, history = compute_lineages(contracts)
    name_to_lineage: dict[str, str] = {}
    for mapping in per_version.values():
        name_to_lineage.update(mapping)
    return {
        "per_version": per_version,
        "history": history,
        "name_to_lineage": name_to_lineage,
    }


def _lineage_of(name: str, index: dict[str, Any]) -> str:
    return index["name_to_lineage"].get(name, name)


def _rename_aliases(
    field: dict[str, Any], consumer: dict[str, Any], index: dict[str, Any]
) -> list[str]:
    """Names this consumer will accept in place of *field*'s pinned name."""
    aliases = [a for a in field.get("accepts", []) if a != field["name"]]
    if consumer.get("follow_renames"):
        lineage = _lineage_of(field["name"], index)
        aliases.extend(
            n for n in index["history"].get(lineage, []) if n not in aliases and n != field["name"]
        )
    return aliases


def consumer_view(
    contract: dict[str, Any], consumer: dict[str, Any]
) -> list[dict[str, Any]]:
    """The consumer's expectation: its pinned contract plus local overrides."""
    overrides = {o["field"]: o for o in consumer.get("overrides", [])}
    view: list[dict[str, Any]] = []
    for field in contract["fields"]:
        override = overrides.get(field["name"], {})
        entry = copy.deepcopy(field)
        if "required" in override:
            entry["required"] = override["required"]
        if "default" in override:
            entry["has_default"] = True
            entry["default"] = override["default"]
        entry["accepts"] = list(override.get("accepts", []))
        view.append(entry)
    return view


# ---------------------------------------------------------------------------
# schema comparison
# ---------------------------------------------------------------------------


def _finding(
    code: str,
    severity: str,
    message: str,
    *,
    field: str,
    producer_field: str | None,
    consumer_field: str | None = None,
    producer_version: int,
    consumer_version: int,
    consumer_name: str,
    remediation: str = "",
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "field": field,
        "producer_field": producer_field,
        "consumer_field": consumer_field or field,
        "producer_version": producer_version,
        "consumer_version": consumer_version,
        "consumer_name": consumer_name,
        "message": message,
        "remediation": remediation,
    }


def _type_finding(
    pf: dict[str, Any],
    cf: dict[str, Any],
    *,
    producer_version: int,
    consumer_version: int,
    consumer_name: str,
) -> dict[str, Any] | None:
    common = dict(
        field=cf["name"],
        producer_field=pf["name"],
        consumer_field=cf["name"],
        producer_version=producer_version,
        consumer_version=consumer_version,
        consumer_name=consumer_name,
    )
    if (
        pf["type"] == cf["type"]
        and pf.get("timestamp_format") != cf.get("timestamp_format")
    ):
        return _finding(
            "timestamp_format_mismatch",
            "breaking",
            f"以 {pf.get('timestamp_format')} 编码，消费者按 {cf.get('timestamp_format')} 解析",
            remediation="统一 timestamp_format，或过渡期同时写两种格式",
            **common,
        )
    severity, code = type_relation(pf["type"], cf["type"])
    if severity == "info" and code == "same_type":
        return None
    messages = {
        "type_widening": ("info", "生产者 integer 进入消费者 number 视图，安全拓宽"),
        "type_narrowing": (
            "degraded",
            "生产者 number 进入消费者 integer 视图，小数会被截断或拒绝",
        ),
        "type_serialized": ("info", "timestamp 以字符串进入消费者 string 视图"),
        "type_parse_risk": (
            "degraded",
            "生产者 string 进入消费者 timestamp 视图，仅当字符串可解析时可用",
        ),
        "type_mismatch": ("breaking", f"类型 {pf['type']} -> {cf['type']}，消费者无法解码"),
    }
    label, message = messages.get(code, ("breaking", "类型不兼容"))
    severity = {"info": "info", "degraded": "degraded", "breaking": "breaking"}[
        {"info": "info", "degraded": "degraded", "breaking": "breaking"}.get(label, "breaking")
    ] if label in ("info", "degraded", "breaking") else "breaking"
    severity = {"type_widening": "info", "type_serialized": "info", "type_narrowing": "degraded",
                "type_parse_risk": "degraded"}.get(code, "breaking")
    remediation = {
        "type_narrowing": "消费者改用 number，或边界显式取整并记录精度损失",
        "type_parse_risk": "消费者先按字符串接收再显式解析，并保留失败分支",
        "type_mismatch": "保持类型不变，或新增字段并双写迁移",
    }.get(code, "")
    return _finding(code, severity, message, remediation=remediation, **common)


def compare_contracts(
    producer_contract: dict[str, Any],
    consumer_contract: dict[str, Any],
    consumer: dict[str, Any],
    index: dict[str, Any],
) -> list[dict[str, Any]]:
    pv, cv = producer_contract["version"], consumer_contract["version"]
    name = consumer["name"]
    findings: list[dict[str, Any]] = []
    producer_fields = producer_contract["fields"]
    producer_by_name = {f["name"]: f for f in producer_fields}
    producer_by_lineage = {_lineage_of(f["name"], index): f for f in producer_fields}
    view = consumer_view(consumer_contract, consumer)
    matched_producer: set[str] = set()
    consumer_lineages = {_lineage_of(cf["name"], index) for cf in view}

    for cf in view:
        lineage = _lineage_of(cf["name"], index)
        pf = None
        if cf["name"] in producer_by_name:
            pf = producer_by_name[cf["name"]]
        else:
            for alias in _rename_aliases(cf, consumer, index):
                if alias in producer_by_name:
                    pf = producer_by_name[alias]
                    break

        if pf is None:
            renamed = producer_by_lineage.get(lineage)
            if renamed is not None:
                findings.append(
                    _finding(
                        "renamed_field",
                        "breaking",
                        f"生产者改名为 {renamed['name']}，消费者仍读取 {cf['name']}",
                        field=cf["name"],
                        producer_field=renamed["name"],
                        producer_version=pv,
                        consumer_version=cv,
                        consumer_name=name,
                        remediation="dual_write",
                    )
                )
                matched_producer.add(renamed["name"])
            elif cf.get("has_default"):
                findings.append(
                    _finding(
                        "consumer_default_fill",
                        "info",
                        f"生产者不再发送 {cf['name']}，消费者用默认值 {cf.get('default')!r} 补齐",
                        field=cf["name"],
                        producer_field=None,
                        producer_version=pv,
                        consumer_version=cv,
                        consumer_name=name,
                    )
                )
            elif not cf["required"]:
                findings.append(
                    _finding(
                        "field_absent_optional",
                        "info",
                        f"可选字段 {cf['name']} 不再出现，按缺省处理",
                        field=cf["name"],
                        producer_field=None,
                        producer_version=pv,
                        consumer_version=cv,
                        consumer_name=name,
                    )
                )
            else:
                findings.append(
                    _finding(
                        "missing_required_field",
                        "breaking",
                        f"消费者必填字段 {cf['name']} 在生产者中不存在",
                        field=cf["name"],
                        producer_field=None,
                        producer_version=pv,
                        consumer_version=cv,
                        consumer_name=name,
                        remediation="生产者补发字段，或消费者声明默认值/改为可选",
                    )
                )
            continue

        matched_producer.add(pf["name"])
        if pf["name"] != cf["name"]:
            findings.append(
                _finding(
                    "renamed_field",
                    "info",
                    f"重命名 {pf['name']} -> {cf['name']} 已由消费者别名解析",
                    field=cf["name"],
                    producer_field=pf["name"],
                    producer_version=pv,
                    consumer_version=cv,
                    consumer_name=name,
                )
            )

        type_finding = _type_finding(
            pf, cf, producer_version=pv, consumer_version=cv, consumer_name=name
        )
        if type_finding:
            findings.append(type_finding)

        c_has_default = cf.get("has_default", False)
        p_has_default = pf.get("has_default", False)
        c_default = cf.get("default", UNSET) if c_has_default else UNSET
        p_default = pf.get("default", UNSET) if p_has_default else UNSET

        if cf["required"] and not pf["required"]:
            if c_has_default:
                divergent = p_has_default and p_default != c_default
                findings.append(
                    _finding(
                        "consumer_default_fill",
                        "degraded" if divergent else "info",
                        (
                            f"生产者字段可选，消费者用自身默认值 {c_default!r} 补齐"
                            + (f"；生产者默认值 {p_default!r}，缺省时语义不一致" if divergent else "")
                        ),
                        field=cf["name"],
                        producer_field=pf["name"],
                        producer_version=pv,
                        consumer_version=cv,
                        consumer_name=name,
                        remediation="consumer_default" if divergent else "",
                    )
                )
            elif consumer.get("follow_renames") and p_has_default:
                findings.append(
                    _finding(
                        "default_undeclared_by_consumer",
                        "degraded",
                        "生产者字段可选且默认值只在生产端声明，缺省消息可能缺必填值",
                        field=cf["name"],
                        producer_field=pf["name"],
                        producer_version=pv,
                        consumer_version=cv,
                        consumer_name=name,
                        remediation="consumer_default",
                    )
                )
            else:
                findings.append(
                    _finding(
                        "required_field_may_be_absent",
                        "breaking",
                        f"消费者要求 {cf['name']}，生产者同名字段可选且消费者无默认值",
                        field=cf["name"],
                        producer_field=pf["name"],
                        producer_version=pv,
                        consumer_version=cv,
                        consumer_name=name,
                        remediation="生产端改为必填，或消费者声明默认值/改为可选",
                    )
                )
        elif cf["required"] and pf["required"] and c_has_default and p_has_default:
            if c_default != p_default:
                findings.append(
                    _finding(
                        "default_divergence",
                        "degraded",
                        f"默认值不一致：生产者 {p_default!r}，消费者 {c_default!r}",
                        field=cf["name"],
                        producer_field=pf["name"],
                        producer_version=pv,
                        consumer_version=cv,
                        consumer_name=name,
                        remediation="对齐默认值或改为必填显式传值",
                    )
                )
        elif not cf["required"] and pf["required"]:
            findings.append(
                _finding(
                    "producer_required_consumer_optional",
                    "info",
                    f"生产者必填 {pf['name']}，消费者可选，接收端更宽松",
                    field=cf["name"],
                    producer_field=pf["name"],
                    producer_version=pv,
                    consumer_version=cv,
                    consumer_name=name,
                )
            )

        p_enum, c_enum = pf.get("enum"), cf.get("enum")
        if c_enum and p_enum:
            extra = [v for v in p_enum if v not in c_enum]
            if extra:
                findings.append(
                    _finding(
                        "enum_narrowed",
                        "breaking",
                        f"生产者可能发送枚举外的值 {extra}",
                        field=cf["name"],
                        producer_field=pf["name"],
                        producer_version=pv,
                        consumer_version=cv,
                        consumer_name=name,
                        remediation="消费者扩充枚举，或生产端做值归一",
                    )
                )
            elif any(v not in p_enum for v in c_enum):
                findings.append(
                    _finding(
                        "enum_widened",
                        "degraded",
                        "消费者枚举含生产者不会发送的值，可能落入错误分支",
                        field=cf["name"],
                        producer_field=pf["name"],
                        producer_version=pv,
                        consumer_version=cv,
                        consumer_name=name,
                    )
                )
        elif c_enum and not p_enum:
            findings.append(
                _finding(
                    "enum_unspecified",
                    "degraded",
                    "消费者按枚举校验，生产者未声明枚举，运行时值可能越界",
                    field=cf["name"],
                    producer_field=pf["name"],
                    producer_version=pv,
                    consumer_version=cv,
                    consumer_name=name,
                    remediation="生产端声明枚举，或消费者放宽为字符串校验",
                )
            )

    for pf in producer_fields:
        if pf["name"] in matched_producer:
            continue
        if _lineage_of(pf["name"], index) in consumer_lineages:
            continue
        if consumer.get("strict_unknown"):
            findings.append(
                _finding(
                    "unknown_field",
                    "breaking",
                    f"消费者 strict_unknown 拒绝未知字段 {pf['name']}",
                    field=pf["name"],
                    producer_field=pf["name"],
                    producer_version=pv,
                    consumer_version=cv,
                    consumer_name=name,
                    remediation="消费者关闭 strict_unknown，或先发版消费者",
                )
            )
        else:
            findings.append(
                _finding(
                    "added_field_ignored",
                    "info",
                    f"新增字段 {pf['name']} 被消费者忽略",
                    field=pf["name"],
                    producer_field=pf["name"],
                    producer_version=pv,
                    consumer_version=cv,
                    consumer_name=name,
                )
            )

    findings.sort(key=lambda f: (-SEVERITY_RANK[f["severity"]], f["field"], f["code"]))
    return findings


# ---------------------------------------------------------------------------
# simulated payloads
# ---------------------------------------------------------------------------


def _sample_value(field: dict[str, Any], edge: bool = False) -> Any:
    if field.get("enum"):
        values = field["enum"]
        return copy.deepcopy(values[-1] if edge else values[0])
    if field.get("has_default") and not edge:
        return copy.deepcopy(field["default"])
    ftype = field["type"]
    if ftype == "string":
        return f"sample-{field['name']}"
    if ftype == "integer":
        return 7
    if ftype == "number":
        return 12.5
    if ftype == "boolean":
        return True
    if ftype == "timestamp":
        fmt = field.get("timestamp_format")
        if fmt == "epoch_s":
            return 1763700000
        if fmt == "epoch_ms":
            return 1763700000000
        return "2026-11-20T08:30:00Z"
    if ftype == "uuid":
        return "8b6f2c1e-3a4d-4f6b-9c2e-1d7a5b3f9e01"
    if ftype == "object":
        return {"key": "value"}
    if ftype == "array":
        return ["item"]
    return None


def scenario_payloads(contract: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Serialization habits a real producer legitimately exhibits.

    ``full`` writes every field; ``minimal`` omits optional fields that have a
    default (only this variant exposes "optional became required"); ``edge``
    sends the last value of every enum, which is where a newly added enum
    member breaks a pinned consumer.
    """
    fields = contract["fields"]
    full = {f["name"]: _sample_value(f) for f in fields}
    minimal = {
        f["name"]: _sample_value(f)
        for f in fields
        if f["required"] or not f.get("has_default")
    }
    edge = {
        f["name"]: _sample_value(f, edge=True) if f.get("enum") else _sample_value(f)
        for f in fields
    }
    scenarios = [("full", full)]
    if minimal != full:
        scenarios.append(("minimal", minimal))
    if edge != full and edge != minimal:
        scenarios.append(("edge", edge))
    return scenarios


def dual_write_payload(
    payload: dict[str, Any], contract: dict[str, Any], index: dict[str, Any]
) -> dict[str, Any]:
    """Add every historical name of a renamed field so old readers decode."""
    result = dict(payload)
    for field in contract["fields"]:
        if field["name"] not in payload:
            continue
        lineage = _lineage_of(field["name"], index)
        for old_name in index["history"].get(lineage, []):
            if old_name != field["name"]:
                result.setdefault(old_name, payload[field["name"]])
    return result


def simulate_messages(
    contracts: list[dict[str, Any]],
    producer_versions: Iterable[int],
    *,
    base_time: datetime | None = None,
    dual_write: bool = False,
) -> list[dict[str, Any]]:
    index = lineage_index(contracts)
    by_version = {c["version"]: c for c in contracts}
    base_time = base_time or datetime.now(timezone.utc)
    versions = sorted(set(producer_versions))
    messages: list[dict[str, Any]] = []
    for position, version in enumerate(versions):
        contract = by_version.get(version)
        if contract is None:
            continue
        produced = base_time - timedelta(days=len(versions) - position)
        for offset, (variant, payload) in enumerate(scenario_payloads(contract)):
            if dual_write:
                payload = dual_write_payload(payload, contract, index)
            messages.append(
                {
                    "producer_version": version,
                    "produced_at": (produced + timedelta(minutes=5 * offset)).isoformat(
                        timespec="seconds"
                    ),
                    "payload": payload,
                    "label": f"v{version}/{variant}",
                    "variant": variant,
                    "source": "simulated",
                }
            )
    return messages


# ---------------------------------------------------------------------------
# decoding
# ---------------------------------------------------------------------------


def decode_message(
    message: dict[str, Any],
    consumer_contract: dict[str, Any],
    consumer: dict[str, Any],
    index: dict[str, Any],
) -> dict[str, Any]:
    payload = message["payload"] if isinstance(message.get("payload"), dict) else {}
    view = consumer_view(consumer_contract, consumer)
    errors: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    consumed: set[str] = set()

    for cf in view:
        lineage = _lineage_of(cf["name"], index)
        candidates = [cf["name"]] + _rename_aliases(cf, consumer, index)
        found = next((n for n in candidates if n in payload), None)

        if found is None:
            renamed = next(
                (
                    n
                    for n in index["history"].get(lineage, [])
                    if n != cf["name"] and n in payload
                ),
                None,
            )
            if renamed is not None:
                consumed.add(renamed)
                errors.append(
                    {
                        "code": "renamed_field_missing",
                        "field": cf["name"],
                        "producer_field": renamed,
                        "message": f"消息携带新名 {renamed}，消费者只认 {cf['name']}",
                        "severity": "breaking",
                        "remediation": "dual_write",
                    }
                )
            elif cf.get("has_default"):
                notes.append(
                    {
                        "code": "default_filled",
                        "field": cf["name"],
                        "message": f"字段缺失，消费者填入默认值 {cf.get('default')!r}",
                        "value": cf.get("default"),
                    }
                )
            elif not cf["required"]:
                notes.append(
                    {"code": "absent_optional", "field": cf["name"], "message": "可选字段缺失"}
                )
            else:
                errors.append(
                    {
                        "code": "missing_required",
                        "field": cf["name"],
                        "producer_field": None,
                        "message": f"缺少必填字段 {cf['name']}，消费者无默认值",
                        "severity": "breaking",
                        "remediation": "生产端补发字段，或消费者声明默认值",
                    }
                )
            continue

        consumed.add(found)
        value = payload[found]
        if found != cf["name"]:
            notes.append(
                {
                    "code": "alias_resolved",
                    "field": cf["name"],
                    "producer_field": found,
                    "message": f"用别名 {found} 解出 {cf['name']}",
                }
            )

        if value is None:
            if cf["required"]:
                errors.append(
                    {
                        "code": "null_required",
                        "field": cf["name"],
                        "producer_field": found,
                        "message": f"必填字段 {cf['name']} 为 null",
                        "severity": "breaking",
                        "remediation": "生产端禁止写入 null，或消费者允许 null",
                    }
                )
            continue

        expected = cf["type"]
        ok = value_matches_type(value, expected, cf.get("timestamp_format", "iso8601"))
        if ok and expected == TYPE_INTEGER and isinstance(value, float):
            ok = False
        if ok and expected == TYPE_NUMBER and isinstance(value, bool):
            ok = False
        if not ok:
            errors.append(
                {
                    "code": "type_mismatch",
                    "field": cf["name"],
                    "producer_field": found,
                    "message": f"期望 {expected}，实际收到 {type(value).__name__}={value!r}",
                    "severity": "breaking",
                    "remediation": "保持字段类型，或消费者显式转换并处理失败",
                }
            )
            continue

        if cf.get("enum") and value not in cf["enum"]:
            errors.append(
                {
                    "code": "enum_violation",
                    "field": cf["name"],
                    "producer_field": found,
                    "message": f"值 {value!r} 不在消费者枚举 {cf['enum']} 中",
                    "severity": "breaking",
                    "remediation": "消费者扩充枚举，或生产端做值归一",
                }
            )

    if consumer.get("strict_unknown"):
        for name, _value in payload.items():
            if name in consumed:
                continue
            if _lineage_of(name, index) in {_lineage_of(cf["name"], index) for cf in view}:
                continue
            errors.append(
                {
                    "code": "unknown_field",
                    "field": name,
                    "producer_field": name,
                    "message": f"消费者 strict_unknown 拒绝未知字段 {name}",
                    "severity": "breaking",
                    "remediation": "消费者先发版兼容该字段，或生产者延后发送",
                }
            )

    return {
        "producer_version": message.get("producer_version"),
        "consumer_name": consumer["name"],
        "consumer_version": consumer_contract["version"],
        "produced_at": message.get("produced_at"),
        "label": message.get("label", ""),
        "variant": message.get("variant", ""),
        "source": message.get("source", "simulated"),
        "ok": not errors,
        "errors": errors,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# summaries
# ---------------------------------------------------------------------------


def _worst_severity(findings: list[dict[str, Any]]) -> str:
    worst = "info"
    for finding in findings:
        if SEVERITY_RANK[finding["severity"]] > SEVERITY_RANK[worst]:
            worst = finding["severity"]
    return worst


def _collect_errors(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    collected = []
    for result in results:
        for error in result["errors"]:
            collected.append(
                {
                    **error,
                    "producer_version": result["producer_version"],
                    "label": result["label"],
                    "variant": result["variant"],
                    "source": result["source"],
                }
            )
    return collected


def _dedupe_issues(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for error in errors:
        key = (error["field"], error["code"])
        if key in seen:
            seen[key]["count"] += 1
            seen[key].setdefault("versions", set()).add(error.get("producer_version"))
            continue
        seen[key] = {
            "field": error["field"],
            "producer_field": error.get("producer_field"),
            "code": error["code"],
            "message": error["message"],
            "remediation": error.get("remediation", ""),
            "sample": error.get("label", ""),
            "count": 1,
            "versions": {error.get("producer_version")},
        }
    issues = []
    for issue in seen.values():
        issue["versions"] = sorted(v for v in issue["versions"] if v is not None)
        issues.append(issue)
    issues.sort(key=lambda i: (-i["count"], i["field"], i["code"]))
    return issues


def _field_label(issue: dict[str, Any]) -> str:
    field = issue["field"]
    if issue.get("producer_field") and issue["producer_field"] != field:
        return f"{issue['producer_field']} → {field}"
    return field


def _message_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [r for r in results if not r["ok"]]
    issues = _dedupe_issues(_collect_errors(failed))
    return {
        "total": len(results),
        "failed": len(failed),
        "passed": len(results) - len(failed),
        "severity": "breaking" if failed else "info",
        "issues": issues,
        "fields": [
            {"field": i["field"], "producer_field": i["producer_field"], "codes": [i["code"]],
             "count": i["count"]}
            for i in issues
        ],
        "samples": failed[:3],
    }


def pair_result(
    producer_contract: dict[str, Any],
    consumer_contract: dict[str, Any],
    consumer: dict[str, Any],
    index: dict[str, Any],
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    findings = compare_contracts(producer_contract, consumer_contract, consumer, index)
    decoded = [decode_message(m, consumer_contract, consumer, index) for m in messages]
    message_summary = _message_summary(decoded)
    schema_severity = _worst_severity(findings)
    severity = max(schema_severity, message_summary["severity"], key=SEVERITY_RANK.get)

    breaking_fields: list[str] = []
    for finding in findings:
        if finding["severity"] == "breaking":
            label = _field_label(finding)
            if label not in breaking_fields:
                breaking_fields.append(label)
    for issue in message_summary["issues"]:
        label = _field_label(issue)
        if label not in breaking_fields:
            breaking_fields.append(label)

    return {
        "producer_version": producer_contract["version"],
        "consumer_name": consumer["name"],
        "consumer_version": consumer_contract["version"],
        "consumer_mode": consumer["mode"],
        "pair": f"v{producer_contract['version']} → {consumer['name']}@v{consumer_contract['version']}",
        "severity": severity,
        "severity_label": SEVERITY_LABELS[severity],
        "breaking_fields": breaking_fields,
        "schema": {"severity": schema_severity, "findings": findings},
        "messages": message_summary,
    }


def build_matrix(
    contracts: list[dict[str, Any]],
    consumers: list[dict[str, Any]],
    recorded: list[dict[str, Any]],
    *,
    base_time: datetime | None = None,
) -> dict[str, Any]:
    index = lineage_index(contracts)
    by_version = {c["version"]: c for c in contracts}
    rows = []
    for version in sorted(by_version):
        contract = by_version[version]
        messages = simulate_messages(contracts, [version], base_time=base_time)
        messages += [m for m in recorded if m["producer_version"] == version]
        cells = []
        for consumer in consumers:
            consumer_contract = by_version.get(consumer["contract_version"])
            if consumer_contract is None:
                cells.append(
                    {
                        "consumer_name": consumer["name"],
                        "consumer_version": consumer["contract_version"],
                        "consumer_mode": consumer["mode"],
                        "severity": "breaking",
                        "severity_label": SEVERITY_LABELS["breaking"],
                        "breaking_fields": [],
                        "error": "消费者绑定的契约版本不存在",
                    }
                )
                continue
            cells.append(pair_result(contract, consumer_contract, consumer, index, messages))
        rows.append(
            {
                "producer_version": version,
                "status": contract.get("status", "published"),
                "description": contract.get("description", ""),
                "cells": cells,
            }
        )

    summary = {"compatible": 0, "degraded": 0, "breaking": 0}
    for row in rows:
        for cell in row["cells"]:
            summary[
                {"info": "compatible", "degraded": "degraded", "breaking": "breaking"}[
                    cell["severity"]
                ]
            ] += 1
    return {
        "versions": [
            {
                "version": c["version"],
                "status": c.get("status", "published"),
                "description": c.get("description", ""),
                "field_count": len(c["fields"]),
            }
            for c in contracts
        ],
        "consumers": [
            {
                "name": c["name"],
                "version": c["contract_version"],
                "mode": c["mode"],
                "strict_unknown": c.get("strict_unknown", False),
                "follow_renames": c.get("follow_renames", False),
            }
            for c in consumers
        ],
        "rows": rows,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# release check
# ---------------------------------------------------------------------------


def _classify(
    findings: list[dict[str, Any]],
    frozen_errors: list[dict[str, Any]],
    backlog_errors: list[dict[str, Any]],
    mode: str,
) -> tuple[str, str]:
    """Pick the rollout verdict; ``stage`` names the phase that failed."""
    breaking_codes = {f["code"] for f in findings if f["severity"] == "breaking"}
    breaking_codes |= {e["code"] for e in frozen_errors}
    if breaking_codes:
        if breaking_codes <= RENAME_CODES:
            return "dual_write", "frozen_new"
        return "blocked", "frozen_new"
    if mode == "backlog" and backlog_errors:
        return "wait_drain", "upgraded_backlog"
    return "direct", ""


def _verdict_reason(verdict: str, mode: str) -> str:
    if verdict == "direct":
        return (
            "当前部署可直接读新消息，升级契约后也能回放历史积压"
            if mode == "backlog"
            else "只收新消息，当前部署可直接读新版本消息，无需过渡期"
        )
    if verdict == "dual_write":
        return "失败仅为字段重命名：过渡期同时写旧名与新名即可通过"
    if verdict == "wait_drain":
        return "当前部署能读新消息，但升级后无法回放旧消息；先用旧消费者处理完积压再切换"
    return "当前部署读新消息即失败，且不是重命名问题，双写也无法覆盖"


def release_check(
    contracts: list[dict[str, Any]],
    consumers: list[dict[str, Any]],
    recorded: list[dict[str, Any]],
    candidate_version: int,
    *,
    base_time: datetime | None = None,
    include_dual_write_preview: bool = True,
) -> dict[str, Any]:
    index = lineage_index(contracts)
    by_version = {c["version"]: c for c in contracts}
    candidate = by_version.get(candidate_version)
    if candidate is None:
        raise KeyError(f"候选版本 {candidate_version} 不存在")
    base_time = base_time or datetime.now(timezone.utc)

    backlog_versions = [v for v in sorted(by_version) if v < candidate_version]
    backlog_messages = simulate_messages(contracts, backlog_versions, base_time=base_time)
    backlog_messages += [m for m in recorded if m["producer_version"] < candidate_version]
    new_messages = simulate_messages(contracts, [candidate_version], base_time=base_time)
    new_messages += [m for m in recorded if m["producer_version"] == candidate_version]
    new_dual = simulate_messages(
        contracts, [candidate_version], base_time=base_time, dual_write=True
    ) + [m for m in recorded if m["producer_version"] == candidate_version]

    entries: list[dict[str, Any]] = []
    summary = {"direct": 0, "dual_write": 0, "wait_drain": 0, "blocked": 0}

    def entry(consumer: dict[str, Any], verdict: str, **extra: Any) -> dict[str, Any]:
        summary[verdict] += 1
        return {
            "consumer_name": consumer["name"],
            "consumer_version": consumer["contract_version"],
            "mode": consumer["mode"],
            "verdict": verdict,
            "verdict_label": VERDICT_LABELS[verdict],
            "reason": _verdict_reason(verdict, consumer["mode"]),
            "stage": extra.pop("stage", ""),
            "breaking_fields": extra.pop("breaking_fields", []),
            "schema_findings": extra.pop("schema_findings", []),
            "frozen": extra.pop("frozen", {"total": 0, "failed": 0, "errors": []}),
            "backlog": extra.pop(
                "backlog", {"total": 0, "failed": 0, "errors": [], "ignored": False}
            ),
            "dual_write_preview": extra.pop("dual_write_preview", None),
        }

    for consumer in consumers:
        consumer_contract = by_version.get(consumer["contract_version"])
        if consumer_contract is None:
            entries.append(
                entry(
                    consumer,
                    "blocked",
                    stage="registry",
                    reason="消费者绑定的契约版本不存在",
                )
            )
            continue

        findings = compare_contracts(candidate, consumer_contract, consumer, index)
        frozen_results = [
            decode_message(m, consumer_contract, consumer, index) for m in new_messages
        ]
        upgraded_backlog = [
            decode_message(m, candidate, consumer, index) for m in backlog_messages
        ]
        frozen_errors = _collect_errors(frozen_results)
        backlog_errors = _collect_errors(upgraded_backlog)
        verdict, stage = _classify(
            findings, frozen_errors, backlog_errors, consumer["mode"]
        )

        preview = None
        if include_dual_write_preview and stage == "frozen_new":
            dual_errors = _collect_errors(
                [decode_message(m, consumer_contract, consumer, index) for m in new_dual]
            )
            preview = {"ok": not dual_errors, "errors": _dedupe_issues(dual_errors)[:10]}
            if verdict == "dual_write" and dual_errors:
                verdict = "blocked"
                fields = sorted({_field_label(e) for e in dual_errors})
                findings = findings + [
                    _finding(
                        "dual_write_residual",
                        "breaking",
                        "双写后仍失败：" + "、".join(fields),
                        field=dual_errors[0]["field"],
                        producer_field=dual_errors[0].get("producer_field"),
                        producer_version=candidate_version,
                        consumer_version=consumer_contract["version"],
                        consumer_name=consumer["name"],
                        remediation="双写无法覆盖该差异，检查 strict_unknown 与字段契约",
                    )
                ]

        if stage == "frozen_new":
            relevant = frozen_errors
        elif stage == "upgraded_backlog":
            relevant = backlog_errors
        else:
            relevant = []
        issues = _dedupe_issues(relevant)
        breaking_fields: list[str] = []
        for issue in issues:
            label = _field_label(issue)
            if label not in breaking_fields:
                breaking_fields.append(label)
        for finding in findings:
            if finding["severity"] != "breaking":
                continue
            label = _field_label(finding)
            if label not in breaking_fields:
                breaking_fields.append(label)

        entries.append(
            entry(
                consumer,
                verdict,
                stage=stage,
                breaking_fields=breaking_fields,
                schema_findings=[f for f in findings if f["severity"] != "info"][:20],
                frozen={
                    "total": len(frozen_results),
                    "failed": sum(1 for r in frozen_results if not r["ok"]),
                    "errors": _dedupe_issues(frozen_errors)[:20],
                },
                backlog={
                    "total": len(upgraded_backlog),
                    "failed": sum(1 for r in upgraded_backlog if not r["ok"]),
                    "errors": _dedupe_issues(backlog_errors)[:20],
                    "ignored": consumer["mode"] == "new_only",
                    "evaluated_as": "候选契约回放旧消息",
                },
                dual_write_preview=preview,
            )
        )

    return {
        "candidate_version": candidate_version,
        "candidate_status": candidate.get("status", "published"),
        "window": {
            "backlog_producer_versions": backlog_versions,
            "backlog_messages": len(backlog_messages),
            "new_messages": len(new_messages),
            "recorded_backlog": len(
                [m for m in recorded if m["producer_version"] < candidate_version]
            ),
            "recorded_new": len(
                [m for m in recorded if m["producer_version"] == candidate_version]
            ),
        },
        "summary": summary,
        "consumers": entries,
    }


# ---------------------------------------------------------------------------
# diff + per-change impact
# ---------------------------------------------------------------------------


def diff_contracts(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    index: dict[str, Any],
) -> list[dict[str, Any]]:
    if previous is None:
        return [
            {
                "kind": "added",
                "field": f["name"],
                "detail": f"初始字段，类型 {f['type']}"
                + ("，必填" if f["required"] else "，可选"),
            }
            for f in current["fields"]
        ]

    prev_by_name = {f["name"]: f for f in previous["fields"]}
    consumed: set[str] = set()
    changes: list[dict[str, Any]] = []

    for field in current["fields"]:
        name = field["name"]
        old = None
        old_name = field.get("renamed_from")
        if old_name and old_name in prev_by_name:
            old = prev_by_name[old_name]
            consumed.add(old_name)
            changes.append(
                {
                    "kind": "renamed",
                    "field": name,
                    "previous_field": old_name,
                    "detail": f"由 {old_name} 重命名而来",
                }
            )
        elif name in prev_by_name:
            old = prev_by_name[name]
            consumed.add(name)
        else:
            lineage = _lineage_of(name, index)
            for candidate_name, candidate_field in prev_by_name.items():
                if _lineage_of(candidate_name, index) == lineage:
                    old = candidate_field
                    consumed.add(candidate_name)
                    if candidate_name != name:
                        changes.append(
                            {
                                "kind": "renamed",
                                "field": name,
                                "previous_field": candidate_name,
                                "detail": f"血缘映射：{candidate_name} -> {name}",
                            }
                        )
                    break

        if old is None:
            changes.append(
                {
                    "kind": "added",
                    "field": name,
                    "detail": f"新增字段，类型 {field['type']}"
                    + ("，必填" if field["required"] else "，可选")
                    + (f"，默认值 {field['default']!r}" if field.get("has_default") else "，无默认值"),
                }
            )
            continue

        if old["type"] != field["type"]:
            changes.append(
                {
                    "kind": "type_changed",
                    "field": name,
                    "previous": old["type"],
                    "current": field["type"],
                    "detail": f"类型 {old['type']} -> {field['type']}",
                }
            )
        if old["required"] != field["required"]:
            changes.append(
                {
                    "kind": "required_changed",
                    "field": name,
                    "previous": old["required"],
                    "current": field["required"],
                    "detail": "可选 -> 必填" if field["required"] else "必填 -> 可选",
                }
            )
        old_default = old.get("default", UNSET) if old.get("has_default") else UNSET
        new_default = field.get("default", UNSET) if field.get("has_default") else UNSET
        if old_default != new_default:
            changes.append(
                {
                    "kind": "default_changed",
                    "field": name,
                    "previous": None if old_default is UNSET else old_default,
                    "current": None if new_default is UNSET else new_default,
                    "detail": f"默认值 {old_default!r} -> {new_default!r}",
                }
            )
        if old.get("enum") != field.get("enum"):
            changes.append(
                {
                    "kind": "enum_changed",
                    "field": name,
                    "previous": old.get("enum"),
                    "current": field.get("enum"),
                    "detail": f"枚举 {old.get('enum')} -> {field.get('enum')}",
                }
            )
        if old.get("timestamp_format") != field.get("timestamp_format"):
            changes.append(
                {
                    "kind": "timestamp_format_changed",
                    "field": name,
                    "previous": old.get("timestamp_format"),
                    "current": field.get("timestamp_format"),
                    "detail": "时间格式变化",
                }
            )

    for old_name, _old in prev_by_name.items():
        if old_name not in consumed:
            changes.append(
                {"kind": "removed", "field": old_name, "detail": f"字段 {old_name} 被删除"}
            )
    return changes


def _touches(entry: dict[str, Any], names: set[str]) -> bool:
    return bool(
        {entry.get("field"), entry.get("producer_field"), entry.get("consumer_field")}
        & names
    )


def change_impacts(
    contracts: list[dict[str, Any]],
    consumers: list[dict[str, Any]],
    recorded: list[dict[str, Any]],
    to_version: int,
    *,
    base_time: datetime | None = None,
) -> dict[str, Any]:
    """Every change in a diff, with the concrete pairs and fields it breaks."""
    index = lineage_index(contracts)
    by_version = {c["version"]: c for c in contracts}
    current = by_version.get(to_version)
    if current is None:
        raise KeyError(f"版本 {to_version} 不存在")
    previous = by_version.get(to_version - 1)
    changes = diff_contracts(previous, current, index)
    base_time = base_time or datetime.now(timezone.utc)

    earlier = [v for v in sorted(by_version) if v < to_version]
    new_messages = simulate_messages(contracts, [to_version], base_time=base_time)
    new_messages += [m for m in recorded if m["producer_version"] == to_version]
    backlog_messages = simulate_messages(contracts, earlier, base_time=base_time)
    backlog_messages += [m for m in recorded if m["producer_version"] < to_version]
    # Dual-writing only affects messages produced from now on; already-published
    # messages cannot be repaired, so the feasibility probe uses simulated ones.
    dual_messages = simulate_messages(
        contracts, [to_version], base_time=base_time, dual_write=True
    )

    comparison_cache = {
        c["name"]: compare_contracts(current, by_version[c["contract_version"]], c, index)
        for c in consumers
        if c["contract_version"] in by_version
    }
    frozen_cache = {
        c["name"]: _collect_errors(
            [decode_message(m, by_version[c["contract_version"]], c, index) for m in new_messages]
        )
        for c in consumers
        if c["contract_version"] in by_version
    }
    backlog_cache = {
        c["name"]: _collect_errors(
            [decode_message(m, current, c, index) for m in backlog_messages]
        )
        for c in consumers
        if c["contract_version"] in by_version
    }
    dual_cache = {
        c["name"]: _collect_errors(
            [decode_message(m, by_version[c["contract_version"]], c, index) for m in dual_messages]
        )
        for c in consumers
        if c["contract_version"] in by_version
    }

    impacts: list[dict[str, Any]] = []
    for change in changes:
        names = {change["field"]}
        if change.get("previous_field"):
            names.add(change["previous_field"])
        affected: list[dict[str, Any]] = []
        worst = "info"

        for consumer in consumers:
            if consumer["name"] not in comparison_cache:
                continue
            findings = [f for f in comparison_cache[consumer["name"]] if _touches(f, names)]
            frozen = [e for e in frozen_cache[consumer["name"]] if _touches(e, names)]
            backlog = (
                [e for e in backlog_cache[consumer["name"]] if _touches(e, names)]
                if consumer["mode"] == "backlog"
                else []
            )
            if not findings and not frozen and not backlog:
                continue
            severity = _worst_severity(findings)
            if frozen or backlog:
                severity = "breaking"
            worst = max(worst, severity, key=SEVERITY_RANK.get)

            codes = sorted({f["code"] for f in findings} | {e["code"] for e in frozen + backlog})
            workarounds = {
                x["remediation"] for x in findings + frozen + backlog if x.get("remediation")
            }
            pair_verdict = "compatible"
            if severity == "breaking":
                # A renamed field is the only case dual-writing can repair, and
                # it only repairs consumers that have not upgraded yet.
                if frozen and set(codes) <= RENAME_CODES and consumer["mode"] == "new_only":
                    pair_verdict = "dual_write"
                elif frozen:
                    pair_verdict = "blocked"
                elif backlog and consumer["mode"] == "backlog":
                    pair_verdict = "wait_drain"
                else:
                    pair_verdict = "blocked"
            elif severity == "degraded":
                pair_verdict = "degraded"
            affected.append(
                {
                    "consumer_name": consumer["name"],
                    "consumer_version": consumer["contract_version"],
                    "mode": consumer["mode"],
                    "severity": severity,
                    "verdict": pair_verdict,
                    "verdict_label": VERDICT_LABELS[pair_verdict],
                    "codes": codes,
                    "frozen_failures": len(frozen),
                    "backlog_failures": len(backlog),
                    "message": (findings + frozen + backlog)[0]["message"]
                    if (findings or frozen or backlog)
                    else "",
                    "workarounds": sorted(workarounds),
                }
            )

        dual_resolves = None
        if worst == "breaking":
            flags = []
            for pair in affected:
                if pair["severity"] != "breaking":
                    continue
                if not set(pair["codes"]) <= RENAME_CODES:
                    flags.append(False)
                    continue
                errs = dual_cache.get(pair["consumer_name"], [])
                flags.append(not any(_touches(e, names) for e in errs))
            dual_resolves = all(flags) if flags else False

        impacts.append(
            {
                **change,
                "kind_label": CHANGE_KIND_LABELS.get(change["kind"], change["kind"]),
                "severity": worst,
                "severity_label": SEVERITY_LABELS[worst],
                "compatible": worst != "breaking",
                "affected_pairs": affected,
                "workarounds": sorted({w for a in affected for w in a["workarounds"]}),
                "dual_write_resolves": dual_resolves,
                "verdict": _impact_verdict(worst, affected, bool(dual_resolves)),
            }
        )

    impacts.sort(key=lambda i: (-SEVERITY_RANK[i["severity"]], i["field"], i["kind"]))
    return {
        "from_version": previous["version"] if previous else None,
        "to_version": to_version,
        "changes": impacts,
        "summary": {
            "total": len(impacts),
            "breaking": sum(1 for i in impacts if i["severity"] == "breaking"),
            "degraded": sum(1 for i in impacts if i["severity"] == "degraded"),
            "compatible": sum(1 for i in impacts if i["severity"] == "info"),
        },
    }


def _impact_verdict(
    severity: str, affected: list[dict[str, Any]], dual_resolves: bool
) -> str:
    if severity != "breaking":
        return "degraded" if severity == "degraded" else "compatible"
    pair_verdicts = []
    for pair in affected:
        if pair["severity"] != "breaking":
            continue
        candidate = pair["verdict"]
        if candidate == "dual_write" and not dual_resolves:
            candidate = "blocked"
        pair_verdicts.append(candidate)
    if not pair_verdicts:
        return "compatible"
    return max(pair_verdicts, key=VERDICT_RANK.get)
