from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class AuditStore:
    """SQLite audit ledger for the local single-user paper terminal."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS accounts (
            market_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            currency TEXT NOT NULL,
            starting_balance REAL NOT NULL,
            cash REAL NOT NULL,
            equity REAL NOT NULL,
            available REAL NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS positions (
            market_id TEXT NOT NULL,
            instrument_id TEXT NOT NULL,
            raw_symbol TEXT NOT NULL,
            quantity REAL NOT NULL,
            sellable_quantity REAL NOT NULL DEFAULT 0,
            average_price REAL NOT NULL,
            last_price REAL NOT NULL,
            realized_pnl REAL NOT NULL DEFAULT 0,
            unrealized_pnl REAL NOT NULL DEFAULT 0,
            last_buy_date TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (market_id, instrument_id)
        );
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            market_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            instrument_id TEXT NOT NULL,
            raw_symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            quantity REAL NOT NULL,
            price REAL NOT NULL,
            fee REAL NOT NULL,
            status TEXT NOT NULL,
            source TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            signal_id TEXT,
            note TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS order_events (
            event_id TEXT PRIMARY KEY,
            order_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fills (
            fill_id TEXT PRIMARY KEY,
            order_id TEXT NOT NULL,
            market_id TEXT NOT NULL,
            instrument_id TEXT NOT NULL,
            side TEXT NOT NULL,
            quantity REAL NOT NULL,
            price REAL NOT NULL,
            fee REAL NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS signal_rules (
            market_id TEXT NOT NULL,
            raw_symbol TEXT NOT NULL,
            version INTEGER NOT NULL,
            config_json TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            PRIMARY KEY (market_id, raw_symbol, version)
        );
        CREATE TABLE IF NOT EXISTS signal_events (
            signal_id TEXT PRIMARY KEY,
            market_id TEXT NOT NULL,
            instrument_id TEXT NOT NULL,
            raw_symbol TEXT NOT NULL,
            bar_timestamp TEXT NOT NULL,
            price REAL NOT NULL,
            level TEXT NOT NULL,
            entry_score REAL NOT NULL,
            exit_score REAL NOT NULL,
            reasons_json TEXT NOT NULL,
            indicators_json TEXT NOT NULL,
            rule_version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (market_id, instrument_id, bar_timestamp, level, rule_version)
        );
        CREATE TABLE IF NOT EXISTS annotations (
            annotation_id TEXT PRIMARY KEY,
            market_id TEXT NOT NULL,
            instrument_id TEXT NOT NULL,
            raw_symbol TEXT NOT NULL,
            bar_timestamp TEXT NOT NULL,
            price REAL NOT NULL,
            annotation_type TEXT NOT NULL,
            note TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            stop_loss REAL,
            take_profit REAL,
            signal_id TEXT,
            indicator_snapshot_json TEXT NOT NULL,
            revision INTEGER NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS annotation_revisions (
            revision_id TEXT PRIMARY KEY,
            annotation_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS risk_flags (
            market_id TEXT PRIMARY KEY,
            kill_switch INTEGER NOT NULL DEFAULT 0,
            close_only INTEGER NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS risk_decisions (
            decision_id TEXT PRIMARY KEY,
            market_id TEXT NOT NULL,
            instrument_id TEXT NOT NULL,
            approved INTEGER NOT NULL,
            reason TEXT NOT NULL,
            request_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_events (
            audit_id TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            action TEXT NOT NULL,
            market_id TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
        with self._lock, self.connection() as connection:
            connection.executescript(schema)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self._lock, self.connection() as connection:
            connection.execute(sql, params)

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock, self.connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def audit(self, category: str, action: str, payload: dict[str, Any], market_id: str | None = None) -> None:
        self.execute(
            "INSERT INTO audit_events VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), category, action, market_id, json.dumps(payload, ensure_ascii=False), utc_now()),
        )

    def ensure_account(self, market_id: str, account_id: str, currency: str, balance: float) -> None:
        now = utc_now()
        self.execute(
            """
            INSERT OR IGNORE INTO accounts
            (market_id, account_id, currency, starting_balance, cash, equity, available, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (market_id, account_id, currency, balance, balance, balance, balance, now),
        )
        self.execute(
            "INSERT OR IGNORE INTO risk_flags VALUES (?, 0, 0, '', ?)",
            (market_id, now),
        )

    def set_rule(self, market_id: str, raw_symbol: str, config: dict[str, Any]) -> int:
        existing = self.query(
            "SELECT COALESCE(MAX(version), 0) AS version FROM signal_rules WHERE market_id=? AND raw_symbol=?",
            (market_id, raw_symbol),
        )
        version = int(existing[0]["version"]) + 1
        self.execute(
            "UPDATE signal_rules SET active=0 WHERE market_id=? AND raw_symbol=?",
            (market_id, raw_symbol),
        )
        self.execute(
            "INSERT INTO signal_rules VALUES (?, ?, ?, ?, 1, ?)",
            (market_id, raw_symbol, version, json.dumps(config, ensure_ascii=False), utc_now()),
        )
        self.audit("CONFIG", "SIGNAL_RULE_UPDATED", {"raw_symbol": raw_symbol, "version": version}, market_id)
        return version

    def get_rule(self, market_id: str, raw_symbol: str) -> tuple[int, dict[str, Any]] | None:
        rows = self.query(
            """
            SELECT version, config_json FROM signal_rules
            WHERE market_id=? AND raw_symbol=? AND active=1
            ORDER BY version DESC LIMIT 1
            """,
            (market_id, raw_symbol),
        )
        if not rows:
            return None
        return int(rows[0]["version"]), json.loads(rows[0]["config_json"])

    def record_signal(
        self,
        *,
        market_id: str,
        instrument_id: str,
        raw_symbol: str,
        point: dict[str, Any],
        rule_version: int,
    ) -> str | None:
        signal_id = str(uuid.uuid4())
        try:
            self.execute(
                """
                INSERT INTO signal_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal_id,
                    market_id,
                    instrument_id,
                    raw_symbol,
                    point["timestamp"],
                    point["price"],
                    point["level"],
                    point["entry_score"],
                    point["exit_score"],
                    json.dumps(point["reasons"], ensure_ascii=False),
                    json.dumps(point["indicators"], ensure_ascii=False),
                    rule_version,
                    utc_now(),
                ),
            )
        except sqlite3.IntegrityError:
            return None
        return signal_id

    def create_annotation(self, payload: dict[str, Any]) -> dict[str, Any]:
        annotation_id = str(uuid.uuid4())
        now = utc_now()
        values = (
            annotation_id,
            payload["market_id"],
            payload["instrument_id"],
            payload["raw_symbol"],
            payload["bar_timestamp"],
            float(payload["price"]),
            payload["annotation_type"],
            payload.get("note", ""),
            json.dumps(payload.get("tags", []), ensure_ascii=False),
            payload.get("stop_loss"),
            payload.get("take_profit"),
            payload.get("signal_id"),
            json.dumps(payload.get("indicator_snapshot", {}), ensure_ascii=False),
            1,
            0,
            now,
            now,
        )
        self.execute(
            "INSERT INTO annotations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        revision_payload = {**payload, "annotation_id": annotation_id, "revision": 1}
        self.execute(
            "INSERT INTO annotation_revisions VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), annotation_id, 1, json.dumps(revision_payload, ensure_ascii=False), now),
        )
        self.audit("ANNOTATION", "CREATED", revision_payload, payload["market_id"])
        return self.get_annotation(annotation_id)

    def get_annotation(self, annotation_id: str) -> dict[str, Any]:
        rows = self.query("SELECT * FROM annotations WHERE annotation_id=?", (annotation_id,))
        if not rows:
            raise KeyError(annotation_id)
        return self._decode_annotation(rows[0])

    def list_annotations(self, market_id: str, instrument_id: str) -> list[dict[str, Any]]:
        rows = self.query(
            """
            SELECT * FROM annotations
            WHERE market_id=? AND instrument_id=? AND deleted=0
            ORDER BY bar_timestamp DESC, created_at DESC
            """,
            (market_id, instrument_id),
        )
        return [self._decode_annotation(row) for row in rows]

    def update_annotation(self, annotation_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        current = self.get_annotation(annotation_id)
        merged = {**current, **changes}
        revision = int(current["revision"]) + 1
        now = utc_now()
        self.execute(
            """
            UPDATE annotations SET annotation_type=?, note=?, tags_json=?, stop_loss=?, take_profit=?,
              revision=?, deleted=?, updated_at=? WHERE annotation_id=?
            """,
            (
                merged["annotation_type"], merged.get("note", ""),
                json.dumps(merged.get("tags", []), ensure_ascii=False),
                merged.get("stop_loss"), merged.get("take_profit"), revision,
                int(bool(merged.get("deleted", False))), now, annotation_id,
            ),
        )
        revision_payload = {**merged, "revision": revision, "updated_at": now}
        self.execute(
            "INSERT INTO annotation_revisions VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), annotation_id, revision, json.dumps(revision_payload, ensure_ascii=False), now),
        )
        self.audit("ANNOTATION", "UPDATED", revision_payload, str(current["market_id"]))
        return self.get_annotation(annotation_id)

    @staticmethod
    def _decode_annotation(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["tags"] = json.loads(result.pop("tags_json"))
        result["indicator_snapshot"] = json.loads(result.pop("indicator_snapshot_json"))
        result["deleted"] = bool(result["deleted"])
        return result

    def set_risk_flags(self, market_id: str, kill_switch: bool, close_only: bool, reason: str) -> None:
        self.execute(
            """
            INSERT INTO risk_flags VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(market_id) DO UPDATE SET
              kill_switch=excluded.kill_switch,
              close_only=excluded.close_only,
              reason=excluded.reason,
              updated_at=excluded.updated_at
            """,
            (market_id, int(kill_switch), int(close_only), reason, utc_now()),
        )
        self.audit(
            "RISK", "FLAGS_UPDATED",
            {"kill_switch": kill_switch, "close_only": close_only, "reason": reason},
            market_id,
        )

    def reset_paper_market(self, market_id: str) -> None:
        with self._lock, self.connection() as connection:
            account = connection.execute(
                "SELECT starting_balance FROM accounts WHERE market_id=?", (market_id,),
            ).fetchone()
            if account is None:
                raise KeyError(market_id)
            for table in ("positions", "fills", "order_events", "orders", "risk_decisions"):
                if table == "order_events":
                    connection.execute(
                        "DELETE FROM order_events WHERE order_id IN (SELECT order_id FROM orders WHERE market_id=?)",
                        (market_id,),
                    )
                else:
                    connection.execute(f"DELETE FROM {table} WHERE market_id=?", (market_id,))
            balance = float(account["starting_balance"])
            connection.execute(
                "UPDATE accounts SET cash=?, equity=?, available=?, updated_at=? WHERE market_id=?",
                (balance, balance, balance, utc_now(), market_id),
            )
        self.audit("PAPER", "MARKET_RESET", {}, market_id)
