"""SQLite publish log. Separate from the public inspect database."""

from __future__ import annotations

import os
import sqlite3
import threading

from release_console.errors import Conflict, NotFound

SCHEMA = """
CREATE TABLE IF NOT EXISTS publishes (
  id TEXT PRIMARY KEY,
  service TEXT NOT NULL CHECK (service IN ('frontend','backend')),
  action TEXT NOT NULL CHECK (action IN ('deploy','rollback')),
  actor TEXT NOT NULL,
  commit_sha TEXT NOT NULL DEFAULT '',
  version TEXT NOT NULL DEFAULT '',
  artifact TEXT NOT NULL DEFAULT '',
  host TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('deploying','succeeded','failed','rolled_back')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  finished_at TEXT NOT NULL DEFAULT '',
  jenkins_job TEXT NOT NULL DEFAULT '',
  jenkins_queue_url TEXT NOT NULL DEFAULT '',
  jenkins_build_url TEXT NOT NULL DEFAULT '',
  result_message TEXT NOT NULL DEFAULT '',
  rollback_of TEXT NOT NULL DEFAULT '',
  restores_id TEXT NOT NULL DEFAULT '',
  inspect_run_id TEXT NOT NULL DEFAULT '',
  inspect_status TEXT NOT NULL DEFAULT '',
  inspect_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS publishes_service_created ON publishes(service, created_at DESC);
"""

COLUMNS = (
    "id",
    "service",
    "action",
    "actor",
    "commit_sha",
    "version",
    "artifact",
    "host",
    "state",
    "created_at",
    "updated_at",
    "finished_at",
    "jenkins_job",
    "jenkins_queue_url",
    "jenkins_build_url",
    "result_message",
    "rollback_of",
    "restores_id",
    "inspect_run_id",
    "inspect_status",
    "inspect_error",
)

MUTABLE = {
    "state",
    "updated_at",
    "finished_at",
    "jenkins_queue_url",
    "jenkins_build_url",
    "result_message",
    "inspect_run_id",
    "inspect_status",
    "inspect_error",
}


class Store:
    def __init__(self, path: str):
        self.path = path
        self._thread = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, mode=0o700, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def init(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _as_dict(row: sqlite3.Row | None) -> dict[str, str] | None:
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}

    def get(self, record_id: str) -> dict[str, str] | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM publishes WHERE id=?", (record_id,)).fetchone()
            return self._as_dict(row)
        finally:
            conn.close()

    def list_all(self) -> list[dict[str, str]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM publishes ORDER BY created_at DESC, id DESC"
            ).fetchall()
            return [self._as_dict(row) or {} for row in rows]
        finally:
            conn.close()

    def list_service(self, service: str) -> list[dict[str, str]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM publishes WHERE service=? ORDER BY created_at DESC, id DESC",
                (service,),
            ).fetchall()
            return [self._as_dict(row) or {} for row in rows]
        finally:
            conn.close()

    def insert_deploying(self, row: dict[str, str]) -> dict[str, str]:
        missing = [key for key in COLUMNS if key not in row]
        if missing:
            raise ValueError("incomplete publish row")
        with self._thread:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                busy = conn.execute(
                    "SELECT id FROM publishes WHERE service=? AND state='deploying'",
                    (row["service"],),
                ).fetchone()
                if busy:
                    conn.execute("ROLLBACK")
                    raise Conflict("该服务已有发布正在进行，不能同时发布")
                conn.execute(
                    "INSERT INTO publishes (%s) VALUES (%s)"
                    % (",".join(COLUMNS), ",".join("?" for _ in COLUMNS)),
                    tuple(row[key] for key in COLUMNS),
                )
                conn.execute("COMMIT")
            except Conflict:
                raise
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()
        saved = self.get(row["id"])
        if saved is None:
            raise RuntimeError("publish row was not stored")
        return saved

    def _require_mutable(self, fields: dict[str, str]) -> None:
        unknown = set(fields) - MUTABLE
        if unknown:
            raise ValueError("field cannot be updated")

    def update(self, record_id: str, **fields: str) -> None:
        self._require_mutable(fields)
        if not fields:
            return
        keys = list(fields)
        sql = "UPDATE publishes SET " + ", ".join(key + "=?" for key in keys) + " WHERE id=?"
        values = tuple(fields[key] for key in keys) + (record_id,)
        with self._thread:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(sql, values)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def finish(self, record_id: str, fields: dict[str, str], supersede_id: str = "") -> bool:
        """Move a deploying row to a terminal state. False if it already left deploying."""
        self._require_mutable(fields)
        keys = list(fields)
        sql = (
            "UPDATE publishes SET "
            + ", ".join(key + "=?" for key in keys)
            + " WHERE id=? AND state='deploying'"
        )
        values = tuple(fields[key] for key in keys) + (record_id,)
        with self._thread:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute(
                    "SELECT state FROM publishes WHERE id=?", (record_id,)
                ).fetchone()
                if current is None:
                    conn.execute("ROLLBACK")
                    raise NotFound("发布记录不存在")
                if current["state"] != "deploying":
                    conn.execute("ROLLBACK")
                    return False
                conn.execute(sql, values)
                if supersede_id and fields.get("state") == "rolled_back":
                    conn.execute(
                        "UPDATE publishes SET state='rolled_back', updated_at=?, result_message=? "
                        "WHERE id=? AND action='deploy' AND state='succeeded'",
                        (
                            fields.get("updated_at", ""),
                            "该次发布已回滚。",
                            supersede_id,
                        ),
                    )
                conn.execute("COMMIT")
                return True
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()
