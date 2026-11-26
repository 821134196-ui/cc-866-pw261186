"""HTTP API + static UI, standard library only."""

from __future__ import annotations

import json
import mimetypes
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from . import compat
from .contracts import (
    CONTRACT_STATUSES,
    ContractError,
    iso_now,
    normalize_consumer,
    normalize_contract,
)
from .db import Conflict, Database, NotFound

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 2 * 1024 * 1024
EVENT_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9_-]*)+$")


class ApiError(Exception):
    def __init__(self, status: int, message: str, details: Any = None):
        self.status = status
        self.message = message
        self.details = details
        super().__init__(message)


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length_header = handler.headers.get("Content-Length")
    if length_header is None:
        # A bodyless DELETE/POST is legal; treat it as an empty object.
        if handler.command in ("DELETE", "POST", "PUT"):
            return {}
        raise ApiError(411, "请求缺少 Content-Length")
    try:
        length = int(length_header)
    except ValueError as exc:
        raise ApiError(400, "Content-Length 非法") from exc
    if length > MAX_BODY:
        raise ApiError(413, "请求体过大")
    raw = handler.rfile.read(length) if length else b""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError(400, f"JSON 解析失败：{exc}") from exc
    if not isinstance(parsed, dict):
        raise ApiError(400, "请求体必须是 JSON 对象")
    return parsed


def _event_context(db: Database, name: str) -> dict[str, Any]:
    event = db.get_event_type(name)
    return {
        "event": event,
        "contracts": db.list_contracts(event["id"]),
        "consumers": db.list_consumers(event["id"]),
        "messages": db.list_messages(event["id"]),
    }


class Application:
    def __init__(self, db: Database):
        self.db = db

    # -- event types -----------------------------------------------------
    def list_event_types(self, params: dict[str, list[str]] | None = None) -> Any:
        return {"event_types": self.db.list_event_types()}

    def create_event_type(self, body: dict[str, Any]) -> Any:
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ApiError(400, "name 必填")
        name = name.strip()
        if not EVENT_NAME_RE.match(name):
            raise ApiError(
                400, "事件类型名必须形如 domain.action，仅小写字母、数字、点、下划线、连字符"
            )
        try:
            return self.db.create_event_type(name, str(body.get("description", "")))
        except Conflict as exc:
            raise ApiError(409, str(exc)) from exc

    def get_event(self, event: str, params: dict[str, list[str]] | None = None) -> Any:
        ctx = _event_context(self.db, event)
        index = compat.lineage_index(ctx["contracts"])
        matrix = compat.build_matrix(ctx["contracts"], ctx["consumers"], ctx["messages"])
        latest = max((c["version"] for c in ctx["contracts"]), default=None)
        impacts = None
        if latest is not None and latest > 1:
            impacts = compat.change_impacts(
                ctx["contracts"], ctx["consumers"], ctx["messages"], latest
            )
        return {
            "event_type": ctx["event"],
            "contracts": [
                {
                    "version": c["version"],
                    "description": c["description"],
                    "status": c["status"],
                    "notes": c["notes"],
                    "created_at": c["created_at"],
                    "fields": c["fields"],
                }
                for c in ctx["contracts"]
            ],
            "consumers": ctx["consumers"],
            "messages": ctx["messages"],
            "field_history": index["history"],
            "latest_version": latest,
            "matrix": matrix,
            "latest_impacts": impacts,
            "releases": self.db.list_releases(ctx["event"]["id"], limit=5),
        }

    # -- contracts -------------------------------------------------------
    def create_contract(self, event: str, body: dict[str, Any]) -> Any:
        ctx = self.db.get_event_type(event)
        normalized = normalize_contract(body)
        status = body.get("status", "published")
        if status not in CONTRACT_STATUSES:
            raise ApiError(400, f"status 必须是 {', '.join(CONTRACT_STATUSES)}")
        version = self.db.next_version(ctx["id"])
        try:
            contract = self.db.insert_contract(
                ctx["id"],
                version,
                normalized["fields"],
                normalized["description"],
                status=status,
                notes=str(body.get("notes", "")),
            )
        except Conflict as exc:
            raise ApiError(409, str(exc)) from exc
        return {
            "contract": contract,
            "diff": self._diff(ctx, version - 1, version),
        }

    def update_contract(self, event: str, version: int, body: dict[str, Any]) -> Any:
        ctx = self.db.get_event_type(event)
        status = body.get("status")
        if status not in CONTRACT_STATUSES:
            raise ApiError(400, f"status 必须是 {', '.join(CONTRACT_STATUSES)}")
        try:
            return self.db.update_contract_status(ctx["id"], version, status)
        except NotFound as exc:
            raise ApiError(404, str(exc)) from exc

    def delete_contract(self, event: str, version: int) -> Any:
        ctx = self.db.get_event_type(event)
        try:
            self.db.delete_contract(ctx["id"], version)
        except NotFound as exc:
            raise ApiError(404, str(exc)) from exc
        return {"deleted": version}

    def _diff(self, event: dict[str, Any], from_version: int, to_version: int) -> dict[str, Any]:
        contracts = self.db.list_contracts(event["id"])
        index = compat.lineage_index(contracts)
        by_version = {c["version"]: c for c in contracts}
        current = by_version.get(to_version)
        if current is None:
            raise ApiError(404, f"版本 {to_version} 不存在")
        previous = by_version.get(from_version)
        return {
            "from_version": previous["version"] if previous else None,
            "to_version": current["version"],
            "changes": compat.diff_contracts(previous, current, index),
        }

    def diff(self, event: str, params: dict[str, list[str]]) -> Any:
        ctx = self.db.get_event_type(event)
        contracts = self.db.list_contracts(ctx["id"])
        if not contracts:
            raise ApiError(404, "该事件类型尚无版本")
        versions = [c["version"] for c in contracts]
        try:
            to_version = int(params.get("to", [versions[-1]])[0])
            from_version = int(params.get("from", [str(to_version - 1)])[0])
        except ValueError as exc:
            raise ApiError(400, "from/to 必须是整数版本号") from exc
        return self._diff(ctx, from_version, to_version)

    def impacts(self, event: str, params: dict[str, list[str]]) -> Any:
        ctx = _event_context(self.db, event)
        if not ctx["contracts"]:
            raise ApiError(404, "该事件类型尚无版本")
        latest = max(c["version"] for c in ctx["contracts"])
        raw = params.get("to", [str(latest)])[0]
        try:
            to_version = int(raw)
        except ValueError as exc:
            raise ApiError(400, "to 必须是整数版本号") from exc
        try:
            return compat.change_impacts(
                ctx["contracts"], ctx["consumers"], ctx["messages"], to_version
            )
        except KeyError as exc:
            raise ApiError(404, str(exc)) from exc

    # -- consumers -------------------------------------------------------
    def put_consumer(self, event: str, body: dict[str, Any]) -> Any:
        ctx = self.db.get_event_type(event)
        payload = dict(body)
        payload.setdefault("event_type", event)
        notes = payload.pop("notes", "")
        try:
            normalized = normalize_consumer(payload)
        except ContractError as exc:
            raise ApiError(400, "消费者注册不合法", exc.errors) from exc
        versions = {c["version"] for c in self.db.list_contracts(ctx["id"])}
        if normalized["contract_version"] not in versions:
            raise ApiError(400, f"消费者绑定的版本 v{normalized['contract_version']} 不存在")
        normalized["notes"] = notes
        return self.db.upsert_consumer(ctx["id"], normalized)

    def delete_consumer(self, event: str, consumer: str) -> Any:
        ctx = self.db.get_event_type(event)
        try:
            self.db.delete_consumer(ctx["id"], consumer)
        except NotFound as exc:
            raise ApiError(404, str(exc)) from exc
        return {"deleted": consumer}

    # -- messages --------------------------------------------------------
    def add_message(self, event: str, body: dict[str, Any]) -> Any:
        ctx = self.db.get_event_type(event)
        version = body.get("producer_version")
        payload = body.get("payload")
        if not isinstance(version, int) or isinstance(version, bool):
            raise ApiError(400, "producer_version 必须是整数")
        if not isinstance(payload, dict):
            raise ApiError(400, "payload 必须是 JSON 对象")
        if version not in {c["version"] for c in self.db.list_contracts(ctx["id"])}:
            raise ApiError(400, f"生产者版本 v{version} 不存在")
        return self.db.add_message(
            ctx["id"],
            version,
            payload,
            body.get("produced_at") or iso_now(),
            label=str(body.get("label", "")),
        )

    def clear_messages(self, event: str) -> Any:
        ctx = self.db.get_event_type(event)
        return {"removed": self.db.clear_messages(ctx["id"])}

    # -- analysis --------------------------------------------------------
    def matrix(self, event: str, params: dict[str, list[str]] | None = None) -> Any:
        ctx = _event_context(self.db, event)
        return compat.build_matrix(ctx["contracts"], ctx["consumers"], ctx["messages"])

    def _resolve_consumer(
        self, ctx: dict[str, Any], event: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        name = body.get("consumer_name")
        if name:
            consumer = next((c for c in ctx["consumers"] if c["name"] == name), None)
            if consumer is None:
                raise ApiError(404, f"消费者 {name!r} 不存在")
            return consumer
        raw = body.get("consumer")
        if not isinstance(raw, dict):
            raise ApiError(400, "必须提供 consumer_name 或 consumer 定义")
        payload = dict(raw)
        payload.setdefault("name", "adhoc")
        payload.setdefault("event_type", event)
        try:
            consumer = normalize_consumer(payload)
        except ContractError as exc:
            raise ApiError(400, "consumer 定义不合法", exc.errors) from exc
        consumer["name"] = payload["name"]
        consumer["id"] = -1
        consumer["created_at"] = ""
        consumer["notes"] = ""
        return consumer

    def _resolve_versions(
        self, contracts: list[dict[str, Any]], body: dict[str, Any]
    ) -> list[int]:
        by_version = {c["version"]: c for c in contracts}
        raw = body.get("producer_versions")
        if raw is None:
            return sorted(by_version)
        if not isinstance(raw, list) or not all(
            isinstance(v, int) and not isinstance(v, bool) for v in raw
        ):
            raise ApiError(400, "producer_versions 必须是整数数组")
        unknown = [v for v in raw if v not in by_version]
        if unknown:
            raise ApiError(400, f"版本不存在：{unknown}")
        return raw

    def simulate(self, event: str, body: dict[str, Any]) -> Any:
        ctx = _event_context(self.db, event)
        if not ctx["contracts"]:
            raise ApiError(404, "该事件类型尚无版本")
        versions = self._resolve_versions(ctx["contracts"], body)
        consumer = self._resolve_consumer(ctx, event, body)
        by_version = {c["version"]: c for c in ctx["contracts"]}
        consumer_contract = by_version.get(consumer["contract_version"])
        if consumer_contract is None:
            raise ApiError(400, f"消费者绑定的版本 v{consumer['contract_version']} 不存在")

        index = compat.lineage_index(ctx["contracts"])
        dual_write = bool(body.get("dual_write", False))
        include_recorded = body.get("include_recorded", True)

        messages = compat.simulate_messages(
            ctx["contracts"], versions, dual_write=dual_write
        )
        if include_recorded:
            messages += [m for m in ctx["messages"] if m["producer_version"] in set(versions)]

        decoded = [
            compat.decode_message(m, consumer_contract, consumer, index) for m in messages
        ]
        per_version: dict[int, dict[str, Any]] = {}
        for result, message in zip(decoded, messages):
            bucket = per_version.setdefault(
                message["producer_version"],
                {
                    "producer_version": message["producer_version"],
                    "consumer_name": consumer["name"],
                    "consumer_version": consumer_contract["version"],
                    "total": 0,
                    "failed": 0,
                    "fields": {},
                },
            )
            bucket["total"] += 1
            if result["ok"]:
                continue
            bucket["failed"] += 1
            for error in result["errors"]:
                entry = bucket["fields"].setdefault(
                    error["field"],
                    {
                        "field": error["field"],
                        "producer_field": error.get("producer_field"),
                        "codes": [],
                        "count": 0,
                        "sample": result["label"],
                        "message": error["message"],
                        "remediation": error.get("remediation", ""),
                    },
                )
                entry["count"] += 1
                if error["code"] not in entry["codes"]:
                    entry["codes"].append(error["code"])
        pairs = []
        for version in sorted(per_version):
            bucket = per_version[version]
            pairs.append(
                {
                    **bucket,
                    "passed": bucket["total"] - bucket["failed"],
                    "fields": sorted(
                        bucket["fields"].values(), key=lambda f: (-f["count"], f["field"])
                    ),
                }
            )
        return {
            "event_type": event,
            "consumer": {
                "name": consumer["name"],
                "contract_version": consumer_contract["version"],
                "mode": consumer["mode"],
                "strict_unknown": consumer.get("strict_unknown", False),
                "follow_renames": consumer.get("follow_renames", False),
            },
            "dual_write": dual_write,
            "producer_versions": versions,
            "pairs": pairs,
            "message_count": len(decoded),
            "sample_failures": [r for r in decoded if not r["ok"]][:5],
        }

    def release_check(self, event: str, body: dict[str, Any]) -> Any:
        ctx = _event_context(self.db, event)
        if not ctx["contracts"]:
            raise ApiError(404, "该事件类型尚无版本")
        candidate = body.get("candidate_version")
        if candidate is None:
            candidate = max(c["version"] for c in ctx["contracts"])
        if not isinstance(candidate, int) or isinstance(candidate, bool):
            raise ApiError(400, "candidate_version 必须是整数")
        try:
            report = compat.release_check(
                ctx["contracts"], ctx["consumers"], ctx["messages"], candidate
            )
        except KeyError as exc:
            raise ApiError(404, str(exc)) from exc
        if body.get("record", False):
            release = self.db.record_release(
                ctx["event"]["id"], max(candidate - 1, 0), candidate, report
            )
            report["release_id"] = release["id"]
        return report

    def releases(self, event: str, params: dict[str, list[str]] | None = None) -> Any:
        ctx = self.db.get_event_type(event)
        return {"releases": self.db.list_releases(ctx["id"], limit=20)}


# (method, path regex, handler, kind) — kind: "params" | "body" | "both"
ROUTES: list[tuple[str, re.Pattern[str], Callable[..., Any], str]] = [
    ("GET", re.compile(r"^/api/event-types$"), Application.list_event_types, "params"),
    ("POST", re.compile(r"^/api/event-types$"), Application.create_event_type, "body"),
    ("GET", re.compile(r"^/api/event-types/(?P<event>[^/]+)$"), Application.get_event, "params"),
    ("POST", re.compile(r"^/api/event-types/(?P<event>[^/]+)/contracts$"), Application.create_contract, "body"),
    ("POST", re.compile(r"^/api/event-types/(?P<event>[^/]+)/contracts/(?P<version>\d+)$"), Application.update_contract, "body"),
    ("DELETE", re.compile(r"^/api/event-types/(?P<event>[^/]+)/contracts/(?P<version>\d+)$"), Application.delete_contract, "plain"),
    ("GET", re.compile(r"^/api/event-types/(?P<event>[^/]+)/diff$"), Application.diff, "params"),
    ("GET", re.compile(r"^/api/event-types/(?P<event>[^/]+)/impacts$"), Application.impacts, "params"),
    ("PUT", re.compile(r"^/api/event-types/(?P<event>[^/]+)/consumers$"), Application.put_consumer, "body"),
    ("DELETE", re.compile(r"^/api/event-types/(?P<event>[^/]+)/consumers/(?P<consumer>[^/]+)$"), Application.delete_consumer, "plain"),
    ("POST", re.compile(r"^/api/event-types/(?P<event>[^/]+)/messages$"), Application.add_message, "body"),
    ("DELETE", re.compile(r"^/api/event-types/(?P<event>[^/]+)/messages$"), Application.clear_messages, "plain"),
    ("GET", re.compile(r"^/api/event-types/(?P<event>[^/]+)/matrix$"), Application.matrix, "params"),
    ("POST", re.compile(r"^/api/event-types/(?P<event>[^/]+)/simulate$"), Application.simulate, "body"),
    ("POST", re.compile(r"^/api/event-types/(?P<event>[^/]+)/release-check$"), Application.release_check, "body"),
    ("GET", re.compile(r"^/api/event-types/(?P<event>[^/]+)/releases$"), Application.releases, "params"),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "EventContractRegistry/1.0"
    app: Application

    def log_message(self, fmt: str, *args: Any) -> None:  # pragma: no cover
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path.startswith("/api/"):
            self._handle_api(method, path, parse_qs(parsed.query))
        elif method == "GET":
            self._serve_static(path)
        else:
            _json_response(self, 405, {"error": "method_not_allowed"})

    def _handle_api(self, method: str, path: str, params: dict[str, list[str]]) -> None:
        path_matched = False
        for route_method, pattern, func, kind in ROUTES:
            match = pattern.match(path)
            if not match:
                continue
            path_matched = True
            if route_method != method:
                continue
            try:
                kwargs = dict(match.groupdict())
                if "version" in kwargs:
                    kwargs["version"] = int(kwargs["version"])
                kwargs = {k: v for k, v in kwargs.items() if v is not None}
                if kind == "params":
                    result = func(self.app, params=params, **kwargs)
                elif kind == "body":
                    result = func(self.app, **kwargs, body=_read_json(self))
                elif kind == "plain":
                    result = func(self.app, **kwargs)
                else:  # both
                    result = func(self.app, params=params, **kwargs, body=_read_json(self))
                _json_response(self, 200, result)
                return
            except ApiError as exc:
                _json_response(
                    self,
                    exc.status,
                    {"error": "api_error", "message": exc.message, "details": exc.details},
                )
                return
            except NotFound as exc:
                _json_response(self, 404, {"error": "not_found", "message": str(exc)})
                return
            except Conflict as exc:
                _json_response(self, 409, {"error": "conflict", "message": str(exc)})
                return
            except ContractError as exc:
                _json_response(
                    self,
                    400,
                    {"error": "invalid_contract", "message": str(exc), "details": exc.errors},
                )
                return
            except Exception as exc:  # pragma: no cover - defensive
                traceback.print_exc()
                _json_response(self, 500, {"error": "internal_error", "message": str(exc)})
                return
        if path_matched:
            _json_response(self, 405, {"error": "method_not_allowed"})
        else:
            _json_response(self, 404, {"error": "not_found", "message": "未知接口"})

    def _serve_static(self, path: str) -> None:
        if path in ("/", ""):
            path = "/index.html"
        relative = Path(path.lstrip("/"))
        if ".." in relative.parts:
            _json_response(self, 400, {"error": "bad_path"})
            return
        target = STATIC_DIR / relative
        if not target.is_file():
            if "." not in relative.name:
                target = STATIC_DIR / "index.html"
            else:
                _json_response(self, 404, {"error": "not_found"})
                return
        data = target.read_bytes()
        ctype, _ = mimetypes.guess_type(str(target))
        if target.suffix == ".js":
            ctype = "text/javascript; charset=utf-8"
        elif target.suffix in (".html", ".css"):
            ctype = "text/html; charset=utf-8" if target.suffix == ".html" else "text/css; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")


def create_app(db_path: str | Path, *, seed: bool = True) -> tuple[ThreadingHTTPServer, Database]:
    db = Database(db_path)
    if seed:
        from .demo import seed_if_empty

        seed_if_empty(db)
    handler = type("BoundHandler", (Handler,), {"app": Application(db)})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    return server, db


def make_server(
    db_path: str | Path, host: str = "127.0.0.1", port: int = 8765
) -> ThreadingHTTPServer:
    db = Database(db_path)
    from .demo import seed_if_empty

    seed_if_empty(db)
    handler = type("BoundHandler", (Handler,), {"app": Application(db)})
    return ThreadingHTTPServer((host, port), handler)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="事件契约注册服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--db", default=str(Path(__file__).parent.parent / "var" / "registry.db")
    )
    parser.add_argument("--no-seed", action="store_true")
    parser.add_argument("--fresh", action="store_true", help="删除现有数据库后启动")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if args.fresh:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(db_path) + suffix)
            if candidate.exists():
                candidate.unlink()
    db = Database(db_path)
    if not args.no_seed:
        from .demo import seed_if_empty

        seed_if_empty(db)
    handler = type("BoundHandler", (Handler,), {"app": Application(db)})
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"事件契约注册服务: http://{args.host}:{args.port}  (db: {db_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
