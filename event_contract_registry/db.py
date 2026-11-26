"""SQLite-backed storage for the registry."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .contracts import iso_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_types (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contracts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type_id INTEGER NOT NULL REFERENCES event_types(id) ON DELETE CASCADE,
    version       INTEGER NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    fields        TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'published',
    notes         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    UNIQUE (event_type_id, version)
);

CREATE TABLE IF NOT EXISTS consumers (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type_id    INTEGER NOT NULL REFERENCES event_types(id) ON DELETE CASCADE,
    name             TEXT NOT NULL,
    contract_version INTEGER NOT NULL,
    mode             TEXT NOT NULL DEFAULT 'new_only',
    strict_unknown   INTEGER NOT NULL DEFAULT 0,
    follow_renames   INTEGER NOT NULL DEFAULT 0,
    overrides        TEXT NOT NULL DEFAULT '[]',
    notes            TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    UNIQUE (event_type_id, name)
);

CREATE TABLE IF NOT EXISTS messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type_id    INTEGER NOT NULL REFERENCES event_types(id) ON DELETE CASCADE,
    producer_version INTEGER NOT NULL,
    payload          TEXT NOT NULL,
    produced_at      TEXT NOT NULL,
    source           TEXT NOT NULL DEFAULT 'recorded',
    label            TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS releases (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type_id INTEGER NOT NULL REFERENCES event_types(id) ON DELETE CASCADE,
    from_version  INTEGER NOT NULL,
    to_version    INTEGER NOT NULL,
    verdicts      TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_contracts_event ON contracts(event_type_id, version);
CREATE INDEX IF NOT EXISTS idx_consumers_event ON consumers(event_type_id);
CREATE INDEX IF NOT EXISTS idx_messages_event ON messages(event_type_id, producer_version);
CREATE INDEX IF NOT EXISTS idx_releases_event ON releases(event_type_id);
"""


class NotFound(LookupError):
    pass


class Conflict(ValueError):
    pass


class Database:
    """Thin SQLite wrapper with one connection per thread.

    ``:memory:`` uses a shared-cache URI so every thread sees the same
    in-memory database (a plain ``:memory:`` DSN would give each connection its
    own empty database).
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = threading.local()
        self._keeper: sqlite3.Connection | None = None
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        else:
            self._dsn = f"file:registry-{id(self)}?mode=memory&cache=shared"
            # Keep one connection open for the lifetime of the object, otherwise
            # the shared in-memory database is dropped when the last closes.
            self._keeper = self._new_connection()
        with self.connection() as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def _new_connection(self) -> sqlite3.Connection:
        dsn = getattr(self, "_dsn", self.path)
        conn = sqlite3.connect(dsn, timeout=30, uri=dsn.startswith("file:"))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # -- event types -----------------------------------------------------
    def list_event_types(self) -> list[dict[str, Any]]:
        rows = self.connection().execute(
            "SELECT * FROM event_types ORDER BY name"
        ).fetchall()
        result = []
        for row in rows:
            counts = self.connection().execute(
                "SELECT COUNT(*) AS n, MAX(version) AS latest FROM contracts"
                " WHERE event_type_id = ?",
                (row["id"],),
            ).fetchone()
            consumers = self.connection().execute(
                "SELECT COUNT(*) AS n FROM consumers WHERE event_type_id = ?",
                (row["id"],),
            ).fetchone()
            result.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "description": row["description"],
                    "created_at": row["created_at"],
                    "version_count": counts["n"] or 0,
                    "latest_version": counts["latest"],
                    "consumer_count": consumers["n"] or 0,
                }
            )
        return result

    def get_event_type(self, name: str) -> dict[str, Any]:
        row = self.connection().execute(
            "SELECT * FROM event_types WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            raise NotFound(f"事件类型 {name!r} 不存在")
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "created_at": row["created_at"],
        }

    def create_event_type(self, name: str, description: str = "") -> dict[str, Any]:
        with self.transaction() as conn:
            try:
                conn.execute(
                    "INSERT INTO event_types (name, description, created_at)"
                    " VALUES (?, ?, ?)",
                    (name, description, iso_now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict(f"事件类型 {name!r} 已存在") from exc
        return self.get_event_type(name)

    # -- contracts -------------------------------------------------------
    def list_contracts(self, event_type_id: int) -> list[dict[str, Any]]:
        rows = self.connection().execute(
            "SELECT * FROM contracts WHERE event_type_id = ? ORDER BY version",
            (event_type_id,),
        ).fetchall()
        return [self._contract_row(r) for r in rows]

    def get_contract(self, event_type_id: int, version: int) -> dict[str, Any]:
        row = self.connection().execute(
            "SELECT * FROM contracts WHERE event_type_id = ? AND version = ?",
            (event_type_id, version),
        ).fetchone()
        if row is None:
            raise NotFound(f"版本 {version} 不存在")
        return self._contract_row(row)

    def next_version(self, event_type_id: int) -> int:
        row = self.connection().execute(
            "SELECT MAX(version) AS v FROM contracts WHERE event_type_id = ?",
            (event_type_id,),
        ).fetchone()
        return (row["v"] or 0) + 1

    def insert_contract(
        self,
        event_type_id: int,
        version: int,
        fields: list[dict[str, Any]],
        description: str = "",
        status: str = "published",
        notes: str = "",
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            try:
                conn.execute(
                    "INSERT INTO contracts"
                    " (event_type_id, version, description, fields, status, notes, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        event_type_id,
                        version,
                        description,
                        json.dumps(fields, ensure_ascii=False),
                        status,
                        notes,
                        iso_now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict(f"版本 {version} 已存在") from exc
        return self.get_contract(event_type_id, version)

    def update_contract_status(
        self, event_type_id: int, version: int, status: str
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE contracts SET status = ? WHERE event_type_id = ? AND version = ?",
                (status, event_type_id, version),
            )
            if cur.rowcount == 0:
                raise NotFound(f"版本 {version} 不存在")
        return self.get_contract(event_type_id, version)

    def delete_contract(self, event_type_id: int, version: int) -> None:
        with self.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM contracts WHERE event_type_id = ? AND version = ?",
                (event_type_id, version),
            )
            if cur.rowcount == 0:
                raise NotFound(f"版本 {version} 不存在")

    def _contract_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "event_type_id": row["event_type_id"],
            "version": row["version"],
            "description": row["description"],
            "fields": json.loads(row["fields"]),
            "status": row["status"],
            "notes": row["notes"],
            "created_at": row["created_at"],
        }

    # -- consumers -------------------------------------------------------
    def list_consumers(self, event_type_id: int) -> list[dict[str, Any]]:
        rows = self.connection().execute(
            "SELECT * FROM consumers WHERE event_type_id = ? ORDER BY name",
            (event_type_id,),
        ).fetchall()
        return [self._consumer_row(r) for r in rows]

    def get_consumer(self, event_type_id: int, name: str) -> dict[str, Any]:
        row = self.connection().execute(
            "SELECT * FROM consumers WHERE event_type_id = ? AND name = ?",
            (event_type_id, name),
        ).fetchone()
        if row is None:
            raise NotFound(f"消费者 {name!r} 不存在")
        return self._consumer_row(row)

    def upsert_consumer(
        self, event_type_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO consumers"
                " (event_type_id, name, contract_version, mode, strict_unknown,"
                "  follow_renames, overrides, notes, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(event_type_id, name) DO UPDATE SET"
                "  contract_version = excluded.contract_version,"
                "  mode = excluded.mode,"
                "  strict_unknown = excluded.strict_unknown,"
                "  follow_renames = excluded.follow_renames,"
                "  overrides = excluded.overrides,"
                "  notes = excluded.notes",
                (
                    event_type_id,
                    payload["name"],
                    payload["contract_version"],
                    payload["mode"],
                    int(payload.get("strict_unknown", False)),
                    int(payload.get("follow_renames", False)),
                    json.dumps(payload.get("overrides", []), ensure_ascii=False),
                    payload.get("notes", ""),
                    iso_now(),
                ),
            )
        return self.get_consumer(event_type_id, payload["name"])

    def delete_consumer(self, event_type_id: int, name: str) -> None:
        with self.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM consumers WHERE event_type_id = ? AND name = ?",
                (event_type_id, name),
            )
            if cur.rowcount == 0:
                raise NotFound(f"消费者 {name!r} 不存在")

    def _consumer_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "event_type_id": row["event_type_id"],
            "name": row["name"],
            "contract_version": row["contract_version"],
            "mode": row["mode"],
            "strict_unknown": bool(row["strict_unknown"]),
            "follow_renames": bool(row["follow_renames"]),
            "overrides": json.loads(row["overrides"]),
            "notes": row["notes"],
            "created_at": row["created_at"],
        }

    # -- messages --------------------------------------------------------
    def add_message(
        self,
        event_type_id: int,
        producer_version: int,
        payload: dict[str, Any],
        produced_at: str,
        label: str = "",
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO messages"
                " (event_type_id, producer_version, payload, produced_at, source, label)"
                " VALUES (?, ?, ?, ?, 'recorded', ?)",
                (
                    event_type_id,
                    producer_version,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    produced_at,
                    label,
                ),
            )
        return {
            "id": cur.lastrowid,
            "producer_version": producer_version,
            "payload": payload,
            "produced_at": produced_at,
            "source": "recorded",
            "label": label,
        }

    def list_messages(
        self,
        event_type_id: int,
        producer_version: int | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        if producer_version is None:
            rows = self.connection().execute(
                "SELECT * FROM messages WHERE event_type_id = ?"
                " ORDER BY produced_at, id LIMIT ?",
                (event_type_id, limit),
            ).fetchall()
        else:
            rows = self.connection().execute(
                "SELECT * FROM messages WHERE event_type_id = ? AND producer_version = ?"
                " ORDER BY produced_at, id LIMIT ?",
                (event_type_id, producer_version, limit),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "producer_version": r["producer_version"],
                "payload": json.loads(r["payload"]),
                "produced_at": r["produced_at"],
                "source": r["source"],
                "label": r["label"],
            }
            for r in rows
        ]

    def clear_messages(self, event_type_id: int) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM messages WHERE event_type_id = ? AND source = 'recorded'",
                (event_type_id,),
            )
        return cur.rowcount

    # -- releases --------------------------------------------------------
    def record_release(
        self,
        event_type_id: int,
        from_version: int,
        to_version: int,
        verdicts: dict[str, Any],
    ) -> dict[str, Any]:
        created_at = iso_now()
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO releases"
                " (event_type_id, from_version, to_version, verdicts, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    event_type_id,
                    from_version,
                    to_version,
                    json.dumps(verdicts, ensure_ascii=False),
                    created_at,
                ),
            )
        return {
            "id": cur.lastrowid,
            "from_version": from_version,
            "to_version": to_version,
            "verdicts": verdicts,
            "created_at": created_at,
        }

    def list_releases(self, event_type_id: int, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.connection().execute(
            "SELECT * FROM releases WHERE event_type_id = ?"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (event_type_id, limit),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "from_version": r["from_version"],
                "to_version": r["to_version"],
                "verdicts": json.loads(r["verdicts"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
