"""Contract model + schema validation.

A contract is an ordered list of fields. The model stays close to JSON: the
whole point is to compare what one version *emits* with what another version
*reads*.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

TYPE_STRING = "string"
TYPE_INTEGER = "integer"
TYPE_NUMBER = "number"
TYPE_BOOLEAN = "boolean"
TYPE_TIMESTAMP = "timestamp"
TYPE_UUID = "uuid"
TYPE_OBJECT = "object"
TYPE_ARRAY = "array"

ALLOWED_TYPES = (
    TYPE_STRING,
    TYPE_INTEGER,
    TYPE_NUMBER,
    TYPE_BOOLEAN,
    TYPE_TIMESTAMP,
    TYPE_UUID,
    TYPE_OBJECT,
    TYPE_ARRAY,
)
ENUM_TYPES = (TYPE_STRING, TYPE_INTEGER, TYPE_NUMBER)
TIMESTAMP_FORMATS = ("iso8601", "epoch_s", "epoch_ms")
CONSUMER_MODES = ("backlog", "new_only")
COMPAT_POLICIES = ("backward", "forward", "full", "none")
CONTRACT_STATUSES = ("draft", "published", "deprecated")

NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class _Unset:
    """Sentinel for 'no default declared' (JSON null is a real default)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET = _Unset()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat(timespec="seconds").replace("+00:00", "Z")


def json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


class ContractError(ValueError):
    """Raised when a contract or consumer registration is malformed."""

    def __init__(self, errors: list[dict[str, str]]):
        self.errors = errors
        super().__init__("; ".join(f"{e['field']}: {e['message']}" for e in errors))


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_timestamp(value: Any, fmt: str = "iso8601") -> bool:
    """Return True when *value* is a timestamp in the declared wire format."""
    if fmt == "epoch_s":
        return _is_number(value) and value >= 0
    if fmt == "epoch_ms":
        return _is_number(value) and value >= 0
    if not isinstance(value, str):
        return False
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return False
    return True


def value_matches_type(value: Any, field_type: str, fmt: str = "iso8601") -> bool:
    """Depth-one JSON type check used for defaults, enum values and payloads."""
    if value is None:
        return True
    if field_type == TYPE_STRING:
        return isinstance(value, str)
    if field_type == TYPE_INTEGER:
        return _is_integer(value)
    if field_type == TYPE_NUMBER:
        return _is_number(value)
    if field_type == TYPE_BOOLEAN:
        return isinstance(value, bool)
    if field_type == TYPE_TIMESTAMP:
        return parse_timestamp(value, fmt)
    if field_type == TYPE_UUID:
        return isinstance(value, str) and bool(UUID_RE.match(value))
    if field_type == TYPE_OBJECT:
        return isinstance(value, dict)
    if field_type == TYPE_ARRAY:
        return isinstance(value, list)
    return False


def normalize_field(raw: Any) -> tuple[dict | None, list[dict[str, str]]]:
    """Validate one field declaration, returning (normalized, errors)."""
    errors: list[dict[str, str]] = []

    def err(message: str) -> None:
        label = str(raw.get("name", "?")) if isinstance(raw, dict) else "?"
        errors.append({"field": label, "message": message})

    if not isinstance(raw, dict):
        err("字段声明必须是对象")
        return None, errors

    name = raw.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        err("name 必须匹配 ^[A-Za-z_][A-Za-z0-9_]*$")
        name = str(name) if name is not None else "<unnamed>"

    field_type = raw.get("type", TYPE_STRING)
    if field_type not in ALLOWED_TYPES:
        err(f"未知类型 {field_type!r}，可选：{', '.join(ALLOWED_TYPES)}")
        field_type = TYPE_STRING

    required = raw.get("required", False)
    if not isinstance(required, bool):
        err("required 必须是布尔值")
        required = False

    fmt = raw.get("timestamp_format", "iso8601")
    if field_type == TYPE_TIMESTAMP:
        if fmt not in TIMESTAMP_FORMATS:
            err(f"timestamp_format 必须是 {', '.join(TIMESTAMP_FORMATS)}")
            fmt = "iso8601"
    else:
        fmt = "iso8601"

    has_default = "default" in raw
    default = raw.get("default", UNSET)
    if has_default and not value_matches_type(default, field_type, fmt):
        err(f"default 与类型 {field_type} 不匹配")

    enum = raw.get("enum")
    if enum is not None:
        if field_type not in ENUM_TYPES:
            err(f"类型 {field_type} 不支持 enum")
        elif not isinstance(enum, list) or not enum:
            err("enum 必须是非空数组")
            enum = None
        else:
            seen: list[Any] = []
            for item in enum:
                if not value_matches_type(item, field_type, fmt) or item is None:
                    err(f"enum 值 {item!r} 与类型 {field_type} 不匹配")
                elif item in seen:
                    err(f"enum 值 {item!r} 重复")
                else:
                    seen.append(item)
            enum = seen if not errors else enum

    renamed_from = raw.get("renamed_from")
    if renamed_from is not None:
        if not isinstance(renamed_from, str) or not NAME_RE.match(renamed_from):
            err("renamed_from 必须是合法字段名")
        elif renamed_from == name:
            err("renamed_from 不能与当前名称相同")

    description = raw.get("description", "")
    if not isinstance(description, str):
        err("description 必须是字符串")
        description = ""

    since = raw.get("since")
    if since is not None and (not _is_integer(since) or since < 1):
        err("since 必须是正整数版本号")

    if errors:
        return None, errors

    normalized: dict[str, Any] = {
        "name": name,
        "type": field_type,
        "required": required,
        "has_default": has_default,
        "description": description,
    }
    if has_default:
        normalized["default"] = default
    if enum is not None:
        normalized["enum"] = enum
    if renamed_from:
        normalized["renamed_from"] = renamed_from
    if field_type == TYPE_TIMESTAMP:
        normalized["timestamp_format"] = fmt
    if since is not None:
        normalized["since"] = since
    return normalized, []


def normalize_contract(raw: Any) -> dict[str, Any]:
    """Validate a contract body. Returns {'fields': [...], 'description': str}."""
    if not isinstance(raw, dict):
        raise ContractError([{"field": "<contract>", "message": "契约必须是 JSON 对象"}])

    raw_fields = raw.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise ContractError(
            [{"field": "<contract>", "message": "fields 必须是非空数组"}]
        )

    errors: list[dict[str, str]] = []
    fields: list[dict[str, Any]] = []
    for item in raw_fields:
        normalized, item_errors = normalize_field(item)
        if item_errors:
            errors.extend(item_errors)
        elif normalized is not None:
            fields.append(normalized)

    names = [f["name"] for f in fields]
    for name in set(names):
        if names.count(name) > 1:
            errors.append({"field": name, "message": "同一版本内字段名重复"})
    for f in fields:
        old = f.get("renamed_from")
        if old and old in names:
            errors.append(
                {"field": f["name"], "message": f"renamed_from 与现存字段 {old} 冲突"}
            )

    description = raw.get("description", "")
    if not isinstance(description, str):
        errors.append({"field": "<contract>", "message": "description 必须是字符串"})
        description = ""

    if errors:
        raise ContractError(errors)
    return {"fields": fields, "description": description}


def normalize_consumer(raw: Any) -> dict[str, Any]:
    """Validate a consumer registration (pinned contract view + strictness)."""
    if not isinstance(raw, dict):
        raise ContractError([{"field": "<consumer>", "message": "消费者必须是 JSON 对象"}])

    errors: list[dict[str, str]] = []

    if not isinstance(raw.get("event_type"), str) or not raw["event_type"]:
        errors.append({"field": "event_type", "message": "必须指定事件类型"})

    name = raw.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name.replace("-", "_")):
        errors.append(
            {"field": "name", "message": "消费者名称只能包含字母、数字、下划线或连字符"}
        )

    version = raw.get("contract_version")
    if not _is_integer(version) or version < 1:
        errors.append({"field": "contract_version", "message": "必须是正整数版本号"})

    mode = raw.get("mode", "new_only")
    if mode not in CONSUMER_MODES:
        errors.append(
            {"field": "mode", "message": f"mode 必须是 {', '.join(CONSUMER_MODES)}"}
        )

    strict_unknown = raw.get("strict_unknown", False)
    follow_renames = raw.get("follow_renames", False)
    for label, value in (("strict_unknown", strict_unknown), ("follow_renames", follow_renames)):
        if not isinstance(value, bool):
            errors.append({"field": label, "message": "必须是布尔值"})

    overrides_raw = raw.get("overrides", [])
    overrides: list[dict[str, Any]] = []
    if not isinstance(overrides_raw, list):
        errors.append({"field": "overrides", "message": "必须是数组"})
    else:
        for item in overrides_raw:
            if not isinstance(item, dict) or not isinstance(item.get("field"), str):
                errors.append({"field": "overrides", "message": "每项必须包含 field 名称"})
                continue
            field_name = item["field"]
            if not NAME_RE.match(field_name):
                errors.append({"field": field_name, "message": "覆盖项字段名不合法"})
                continue
            entry: dict[str, Any] = {"field": field_name}
            if "required" in item:
                if not isinstance(item["required"], bool):
                    errors.append({"field": field_name, "message": "required 覆盖必须是布尔值"})
                else:
                    entry["required"] = item["required"]
            if "default" in item:
                entry["default"] = item["default"]
            if "accepts" in item:
                accepts = item["accepts"]
                if not isinstance(accepts, list) or not all(
                    isinstance(a, str) and NAME_RE.match(a) for a in accepts
                ):
                    errors.append(
                        {"field": field_name, "message": "accepts 必须是合法字段名数组"}
                    )
                else:
                    entry["accepts"] = accepts
            overrides.append(entry)

    if errors:
        raise ContractError(errors)
    return {
        "event_type": raw["event_type"],
        "name": name,
        "contract_version": version,
        "mode": mode,
        "strict_unknown": bool(strict_unknown),
        "follow_renames": bool(follow_renames),
        "overrides": overrides,
    }


def compute_lineages(
    contracts: list[dict[str, Any]],
) -> tuple[dict[int, dict[str, str]], dict[str, list[str]]]:
    """Track field identity across versions via ``renamed_from``.

    Returns ``(per_version, history)`` where ``per_version[version]`` maps a
    field name to a stable lineage id (the earliest known name), and
    ``history[lineage]`` lists every name the lineage has used, oldest first.
    """
    name_lineage: dict[str, str] = {}
    per_version: dict[int, dict[str, str]] = {}
    history: dict[str, list[str]] = {}

    for contract in sorted(contracts, key=lambda c: c["version"]):
        mapping: dict[str, str] = {}
        for field in contract["fields"]:
            renamed_from = field.get("renamed_from")
            if renamed_from and renamed_from in name_lineage:
                lineage = name_lineage[renamed_from]
            else:
                lineage = name_lineage.get(field["name"], field["name"])
            mapping[field["name"]] = lineage
            name_lineage[field["name"]] = lineage
            names = history.setdefault(lineage, [])
            if field["name"] not in names:
                names.append(field["name"])
        per_version[contract["version"]] = mapping
    return per_version, history


def type_relation(producer_type: str, consumer_type: str) -> tuple[str, str]:
    """How a producer value of one type behaves when a consumer expects another.

    Returns ``(severity, code)`` where severity is info / degraded / breaking.
    """
    if producer_type == consumer_type:
        return "info", "same_type"
    if producer_type == TYPE_INTEGER and consumer_type == TYPE_NUMBER:
        return "info", "type_widening"
    if producer_type == TYPE_NUMBER and consumer_type == TYPE_INTEGER:
        return "degraded", "type_narrowing"
    if producer_type == TYPE_TIMESTAMP and consumer_type == TYPE_STRING:
        return "info", "type_serialized"
    if producer_type == TYPE_STRING and consumer_type == TYPE_TIMESTAMP:
        return "degraded", "type_parse_risk"
    return "breaking", "type_mismatch"
