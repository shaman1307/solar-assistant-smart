"""SQLite persistence for plan snapshots, monthly history, and EV charging state."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import BASE_DIR
from .grid_config import BILLING_MODEL_VERSION
from .influxdb import now_warsaw

log = logging.getLogger(__name__)

_DB_PATH = BASE_DIR / "data" / "solar_smart.db"
_LEGACY_EV_JSON = BASE_DIR / "data" / "ev_charging.json"
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

_SCHEMA_VERSION = 7

# Monthly history totals mirrored from payload JSON (schema v2+).
_MONTH_HISTORY_TOTAL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("billing_model_version", "TEXT"),
    ("export_revenue", "REAL"),
    ("import_energy_cost", "REAL"),
    ("service_cost", "REAL"),
    ("service_fee", "REAL"),
    ("energy_cost_total", "REAL"),
    ("import_cost_total", "REAL"),
    ("energy_cost", "REAL"),
)

_MONTH_HISTORY_ROW_KEYS: tuple[str, ...] = (
    "export_revenue",
    "import_energy_cost",
    "energy_cost_total",
    "import_cost_total",
)


def db_path() -> Path:
    return _DB_PATH


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _init_schema(_conn)
    return _conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS plan_latest (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS month_history (
            month TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            billing_model_version TEXT,
            export_revenue REAL,
            import_energy_cost REAL,
            service_cost REAL,
            service_fee REAL,
            energy_cost_total REAL,
            import_cost_total REAL,
            energy_cost REAL
        );

        CREATE TABLE IF NOT EXISTS ev_store (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS deposits (
            month_id TEXT PRIMARY KEY,
            deposit_initial REAL NOT NULL,
            deposit_current REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS config_templates (
            name TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS config_template_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS plan_day_archive (
            day TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
            (str(_SCHEMA_VERSION),),
        )
        conn.commit()
        from .config_templates import seed_templates_from_yaml

        seed_templates_from_yaml(conn)
        return
    stored_version = int(row["value"])
    if stored_version < _SCHEMA_VERSION:
        _apply_schema_migrations(conn, stored_version)


def _month_history_table_columns(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(month_history)")}


def _month_history_payload_current(payload: dict[str, Any]) -> bool:
    """True when payload includes split energy cost columns for totals and each day."""
    if payload.get("billing_model_version") != BILLING_MODEL_VERSION:
        return False
    totals = payload.get("totals") or {}
    for key in _MONTH_HISTORY_ROW_KEYS:
        if key not in totals:
            return False
    for row in payload.get("rows") or []:
        for key in _MONTH_HISTORY_ROW_KEYS:
            if key not in row:
                return False
    return True


def _month_history_totals_values(payload: dict[str, Any]) -> tuple[Any, ...]:
    totals = payload.get("totals") or {}
    return (
        payload.get("billing_model_version"),
        totals.get("export_revenue"),
        totals.get("import_energy_cost"),
        totals.get("service_cost"),
        totals.get("service_fee"),
        totals.get("energy_cost_total"),
        totals.get("import_cost_total"),
        totals.get("energy_cost"),
    )


def _backfill_month_history_columns(conn: sqlite3.Connection) -> None:
    """Populate v2 total columns from existing JSON payloads."""
    for row in conn.execute("SELECT month, payload_json FROM month_history"):
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            continue
        if not _month_history_payload_current(payload):
            continue
        conn.execute(
            """
            UPDATE month_history SET
                billing_model_version = ?,
                export_revenue = ?,
                import_energy_cost = ?,
                service_cost = ?,
                service_fee = ?,
                energy_cost_total = ?,
                import_cost_total = ?,
                energy_cost = ?
            WHERE month = ?
            """,
            (*_month_history_totals_values(payload), row["month"]),
        )


def _migrate_to_v5(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS config_templates (
            name TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS config_template_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
    )
    from .config_templates import seed_templates_from_yaml

    seed_templates_from_yaml(conn)
    log.info("SQLite schema migrated to v5 (config templates)")


def _migrate_to_v4(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS deposits (
            month_id TEXT PRIMARY KEY,
            deposit_initial REAL NOT NULL,
            deposit_current REAL NOT NULL
        )
        """,
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO deposits(month_id, deposit_initial, deposit_current)
        VALUES('2026-05', 174.0, 174.0)
        """,
    )
    log.info("SQLite schema migrated to v4 (deposits table, May 2026 seed)")


def _migrate_to_v6(conn: sqlite3.Connection) -> None:
    """Backfill June 2026 closed-month deposit and rewrite incomplete month_history stubs."""
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'deposits_june_backfill_v1'",
    ).fetchone()
    if row is not None:
        return

    june_month = "2026-06"
    june_deposit = 502.4514
    may_current_after_june = round(DEPOSIT_SEED_AMOUNT - 101.9208, 4)

    conn.execute(
        "DELETE FROM month_history WHERE month IN ('2026-05', '2026-06', '2026-07')",
    )
    conn.execute("DELETE FROM deposits WHERE month_id = '2026-07'")
    conn.execute(
        """
        INSERT OR IGNORE INTO deposits(month_id, deposit_initial, deposit_current)
        VALUES(?, ?, ?)
        """,
        (DEPOSIT_SEED_MONTH, DEPOSIT_SEED_AMOUNT, DEPOSIT_SEED_AMOUNT),
    )
    conn.execute(
        """
        INSERT INTO deposits(month_id, deposit_initial, deposit_current)
        VALUES(?, ?, ?)
        ON CONFLICT(month_id) DO UPDATE SET
            deposit_initial = excluded.deposit_initial,
            deposit_current = excluded.deposit_current
        """,
        (june_month, june_deposit, june_deposit),
    )
    conn.execute(
        "UPDATE deposits SET deposit_current = ? WHERE month_id = ?",
        (may_current_after_june, DEPOSIT_SEED_MONTH),
    )
    conn.execute(
        """
        INSERT INTO meta(key, value) VALUES('deposits_june_backfill_v1', '1')
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
    )
    log.info(
        "SQLite schema migrated to v6 (June 2026 deposit backfill: %s=%s, May current=%s)",
        june_month,
        june_deposit,
        may_current_after_june,
    )


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    existing = _month_history_table_columns(conn)
    if "import_cost_total" not in existing:
        conn.execute("ALTER TABLE month_history ADD COLUMN import_cost_total REAL")
    _backfill_month_history_columns(conn)
    log.info("SQLite schema migrated to v3 (import_cost_total column)")


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    existing = _month_history_table_columns(conn)
    for name, col_type in _MONTH_HISTORY_TOTAL_COLUMNS:
        if name not in existing:
            conn.execute(
                f"ALTER TABLE month_history ADD COLUMN {name} {col_type}",
            )
    _backfill_month_history_columns(conn)
    log.info("SQLite schema migrated to v2 (month_history billing columns)")


def _apply_schema_migrations(conn: sqlite3.Connection, from_version: int) -> None:
    if from_version < 2:
        _migrate_to_v2(conn)
    if from_version < 3:
        _migrate_to_v3(conn)
    if from_version < 4:
        _migrate_to_v4(conn)
    if from_version < 5:
        _migrate_to_v5(conn)
    if from_version < 6:
        _migrate_to_v6(conn)
    if from_version < 7:
        _migrate_to_v7(conn)
    conn.execute(
        "UPDATE meta SET value = ? WHERE key = 'schema_version'",
        (str(_SCHEMA_VERSION),),
    )
    conn.commit()


def _migrate_to_v7(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS plan_day_archive (
            day TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    log.info("SQLite schema migrated to v7 (plan_day_archive)")


def _now_iso() -> str:
    return now_warsaw().strftime("%Y-%m-%d %H:%M:%S")


def ensure_month_history_billing_model() -> bool:
    """Clear month_history when billing model version changes. Returns True if cleared."""
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'billing_model_version'",
        ).fetchone()
        stored = row["value"] if row else None
        if stored == BILLING_MODEL_VERSION:
            return False
        conn.execute("DELETE FROM month_history")
        conn.execute(
            """
            INSERT INTO meta(key, value) VALUES('billing_model_version', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (BILLING_MODEL_VERSION,),
        )
        conn.commit()
        log.info(
            "Billing model %s — month_history cache cleared (was %s)",
            BILLING_MODEL_VERSION,
            stored,
        )
        return True


def read_plan() -> dict[str, Any] | None:
    """Energy arbitrage plan — sole store (SQLite plan_latest); no in-memory layer."""
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT payload_json FROM plan_latest WHERE id = 1",
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload_json"])
    except json.JSONDecodeError as exc:
        log.warning("plan_latest JSON corrupt: %s", exc)
        return None


def replace_plan_latest_payload(plan: dict[str, Any]) -> None:
    """Replace plan_latest JSON as-is. No freeze-guard. Datapatch only."""
    payload = json.dumps(plan, ensure_ascii=False, separators=(",", ":"))
    with _lock:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO plan_latest(id, payload_json, updated_at)
            VALUES(1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (payload, _now_iso()),
        )
        conn.commit()


def write_plan(plan: dict[str, Any], *, now: datetime | None = None) -> None:
    """Sole writer for Energy arbitrage plan (plan_latest).

    Never deletes the plan. On the same Warsaw day the JSON blob is replaced,
    but ``guard_future_quarters_on_write`` keeps frozen past hours / from_actual
    q15 from SQLite; only the just-completed tick (newly frozen) and open
    future quarters come from *plan*. Explicit archive datapatch scripts write
    via ``save_plan_day_archive`` / direct SQL — not this path.

    When the calendar day rolls, the previous day's rows (with Timer Schedule)
    are archived to ``plan_day_archive`` for history browsing.
    """
    from .plan_cache_merge import guard_future_quarters_on_write
    from .plan_cost import compute_plan_totals

    now = now or now_warsaw()
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT payload_json FROM plan_latest WHERE id = 1",
        ).fetchone()
        existing: dict[str, Any] | None = None
        if row:
            try:
                existing = json.loads(row["payload_json"])
            except json.JSONDecodeError as exc:
                log.warning("plan_latest JSON corrupt before write: %s", exc)
                existing = None
        guarded = guard_future_quarters_on_write(plan, existing, now=now)
        new_day = str(guarded.get("today_date") or now.strftime("%Y-%m-%d"))
        if existing:
            old_day = str(existing.get("today_date") or "")
            if old_day and old_day < new_day:
                _archive_plan_day_locked(conn, existing, old_day, compute_plan_totals)
        payload = json.dumps(guarded, ensure_ascii=False, separators=(",", ":"))
        conn.execute(
            """
            INSERT INTO plan_latest(id, payload_json, updated_at)
            VALUES(1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (payload, _now_iso()),
        )
        conn.commit()


def _archive_plan_day_locked(
    conn: sqlite3.Connection,
    plan: dict[str, Any],
    day: str,
    compute_plan_totals,
) -> None:
    """Persist one completed EA day (must hold ``_lock`` / open ``conn``)."""
    hist = [
        r for r in (plan.get("history_rows") or [])
        if str(r.get("plan_date") or "") == day
    ]
    live = [
        r for r in (plan.get("rows") or [])
        if str(r.get("plan_date") or "") == day
    ]
    day_rows = hist + live
    day_rows.sort(key=lambda r: (int(r.get("hour", 0)), str(r.get("start") or "")))
    if not day_rows:
        log.info("plan_day_archive skip %s — no rows", day)
        return
    archive = {
        "today_date": day,
        "history_rows": day_rows,
        "rows": [],
        "totals": compute_plan_totals(day_rows),
        "g12_tariff_name": plan.get("g12_tariff_name"),
        "proposed_schedule": plan.get("proposed_schedule"),
        "computed_at": plan.get("computed_at"),
        "plan_from_hour": 24,
        "has_history_rows": True,
        "history_view": True,
        "history_source": "archive",
    }
    payload = json.dumps(archive, ensure_ascii=False, separators=(",", ":"))
    conn.execute(
        """
        INSERT INTO plan_day_archive(day, payload_json, updated_at)
        VALUES(?, ?, ?)
        ON CONFLICT(day) DO UPDATE SET
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        (day, payload, _now_iso()),
    )
    log.info("plan_day_archive saved %s (%d hours)", day, len(day_rows))


def save_plan_day_archive(day: str, payload: dict[str, Any]) -> None:
    """Upsert a completed EA day archive (YYYY-MM-DD)."""
    body = dict(payload)
    body["today_date"] = day
    body["history_view"] = True
    body.setdefault("history_source", "archive")
    body.setdefault("rows", [])
    body.setdefault("plan_from_hour", 24)
    text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    with _lock:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO plan_day_archive(day, payload_json, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(day) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (day, text, _now_iso()),
        )
        conn.commit()


def load_plan_day_archive(day: str) -> dict[str, Any] | None:
    """Load archived EA day (YYYY-MM-DD), or None."""
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT payload_json FROM plan_day_archive WHERE day = ?",
            (day,),
        ).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row["payload_json"])
    except json.JSONDecodeError as exc:
        log.warning("plan_day_archive JSON corrupt for %s: %s", day, exc)
        return None
    data["history_view"] = True
    data.setdefault("history_source", "archive")
    return data


def list_plan_day_archives(limit: int | None = None) -> list[str]:
    """Archived EA days, newest first. ``limit`` None = all days."""
    with _lock:
        conn = _connect()
        if limit is None:
            rows = conn.execute(
                "SELECT day FROM plan_day_archive ORDER BY day DESC",
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT day FROM plan_day_archive
                ORDER BY day DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
    return [str(r["day"]) for r in rows]


def delete_plan() -> None:
    """Test-only wipe of plan_latest. Production paths must never call this."""
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM plan_latest WHERE id = 1")
        conn.commit()


def read_plan_forecast() -> dict[str, Any] | None:
    plan = read_plan()
    if plan is None:
        return None
    return plan.get("forecast")


def read_plan_rce() -> dict[str, Any] | None:
    plan = read_plan()
    if plan is None:
        return None
    return plan.get("rce")


def read_plan_buy_tariff() -> dict[str, Any] | None:
    plan = read_plan()
    if plan is None:
        return None
    return plan.get("buy_tariff")


def save_plan_snapshot(plan: dict[str, Any], *, now: datetime | None = None) -> None:
    """Delegate to write_plan (single persistence entry point)."""
    write_plan(plan, now=now)


def load_plan_snapshot() -> dict[str, Any] | None:
    return read_plan()


def save_month_history(month: str, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    totals = _month_history_totals_values(payload)
    with _lock:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO month_history(
                month, payload_json, updated_at,
                billing_model_version, export_revenue, import_energy_cost,
                service_cost, service_fee, energy_cost_total, import_cost_total,
                energy_cost
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(month) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at,
                billing_model_version = excluded.billing_model_version,
                export_revenue = excluded.export_revenue,
                import_energy_cost = excluded.import_energy_cost,
                service_cost = excluded.service_cost,
                service_fee = excluded.service_fee,
                energy_cost_total = excluded.energy_cost_total,
                import_cost_total = excluded.import_cost_total,
                energy_cost = excluded.energy_cost
            """,
            (month, body, _now_iso(), *totals),
        )
        conn.commit()


def load_month_history(month: str) -> dict[str, Any] | None:
    with _lock:
        conn = _connect()
        row = conn.execute(
            """
            SELECT payload_json, updated_at, billing_model_version,
                   export_revenue, import_energy_cost, service_cost,
                   service_fee, energy_cost_total, import_cost_total, energy_cost
            FROM month_history WHERE month = ?
            """,
            (month,),
        ).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row["payload_json"])
    except json.JSONDecodeError as exc:
        log.warning("month_history JSON corrupt for %s: %s", month, exc)
        return None
    if not _month_history_payload_current(data):
        log.info("month_history cache stale for %s (missing split cost fields)", month)
        return None
    if row["billing_model_version"] != BILLING_MODEL_VERSION:
        log.info(
            "month_history cache stale for %s (billing model %s != %s)",
            month,
            row["billing_model_version"],
            BILLING_MODEL_VERSION,
        )
        return None
    data.setdefault("_cached_at", row["updated_at"])
    return data


def invalidate_month_history(month: str | None = None) -> None:
    with _lock:
        conn = _connect()
        if month:
            conn.execute("DELETE FROM month_history WHERE month = ?", (month,))
        else:
            conn.execute("DELETE FROM month_history")
        conn.commit()


def load_ev_store_json() -> dict[str, Any] | None:
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT payload_json FROM ev_store WHERE id = 1",
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload_json"])
    except json.JSONDecodeError as exc:
        log.warning("ev_store JSON corrupt: %s", exc)
        return None


def save_ev_store_json(data: dict[str, Any]) -> None:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    with _lock:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO ev_store(id, payload_json, updated_at)
            VALUES(1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (payload, _now_iso()),
        )
        conn.commit()


def migrate_ev_json_to_sqlite() -> None:
    """One-time import of data/ev_charging.json into SQLite."""
    if load_ev_store_json() is not None:
        return
    if not _LEGACY_EV_JSON.is_file():
        return
    try:
        raw = json.loads(_LEGACY_EV_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("EV JSON migration skipped: %s", exc)
        return
    save_ev_store_json(raw)
    log.info("Migrated ev_charging.json into SQLite")


def reset_connection_for_tests() -> None:
    """Close DB handle (tests only)."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


DEPOSIT_SEED_MONTH = "2026-05"
DEPOSIT_SEED_AMOUNT = 174.0


def ensure_deposit_seed() -> None:
    with _lock:
        conn = _connect()
        conn.execute(
            """
            INSERT OR IGNORE INTO deposits(month_id, deposit_initial, deposit_current)
            VALUES(?, ?, ?)
            """,
            (DEPOSIT_SEED_MONTH, DEPOSIT_SEED_AMOUNT, DEPOSIT_SEED_AMOUNT),
        )
        conn.commit()


def load_all_deposits() -> dict[str, dict[str, float]]:
    ensure_deposit_seed()
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT month_id, deposit_initial, deposit_current FROM deposits ORDER BY month_id",
        ).fetchall()
    return {
        row["month_id"]: {
            "initial": float(row["deposit_initial"]),
            "current": float(row["deposit_current"]),
        }
        for row in rows
    }


def reset_deposit_current_to_initial() -> None:
    with _lock:
        conn = _connect()
        conn.execute("UPDATE deposits SET deposit_current = deposit_initial")
        conn.commit()


def upsert_open_month_deposit(month_id: str, value: float) -> None:
    amount = round(float(value), 4)
    with _lock:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO deposits(month_id, deposit_initial, deposit_current)
            VALUES(?, ?, ?)
            ON CONFLICT(month_id) DO UPDATE SET
                deposit_initial = excluded.deposit_initial,
                deposit_current = excluded.deposit_current
            """,
            (month_id, amount, amount),
        )
        conn.commit()


def save_all_deposits(deposits: dict[str, dict[str, float]]) -> None:
    with _lock:
        conn = _connect()
        for month_id, row in deposits.items():
            conn.execute(
                "UPDATE deposits SET deposit_current = ? WHERE month_id = ?",
                (round(float(row["current"]), 4), month_id),
            )
        conn.commit()


def sum_deposit_current(deposits: dict[str, dict[str, float]] | None = None) -> float:
    rows = deposits if deposits is not None else load_all_deposits()
    return round(sum(float(r["current"]) for r in rows.values()), 4)


def read_meta(key: str) -> str | None:
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else None


def write_meta(key: str, value: str) -> None:
    with _lock:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO meta(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        conn.commit()


def read_cached_deposit_total() -> dict[str, Any] | None:
    """Last cascade result stored in meta (not a naive sum of month rows)."""
    total_s = read_meta("deposit_total")
    if total_s is None:
        return None
    try:
        total = float(total_s)
    except (TypeError, ValueError):
        return None
    return {
        "deposit_total": round(total, 4),
        "as_of_month": read_meta("deposit_total_as_of_month"),
        "updated_at": read_meta("deposit_total_updated_at"),
    }


def write_cached_deposit_total(total: float, as_of_month: str) -> None:
    write_meta("deposit_total", f"{round(float(total), 4):.4f}")
    write_meta("deposit_total_as_of_month", as_of_month)
    write_meta("deposit_total_updated_at", _now_iso())


def read_month_history_daily_date() -> str | None:
    return read_meta("month_history_daily_date")


def write_month_history_daily_date(date_str: str) -> None:
    write_meta("month_history_daily_date", date_str)


def list_month_history_months() -> list[str]:
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT month FROM month_history ORDER BY month",
        ).fetchall()
    return [str(r["month"]) for r in rows]


def _template_now_iso() -> str:
    return now_warsaw().strftime("%Y-%m-%d %H:%M:%S")


def get_installed_default_template() -> str:
    from .config_templates import INSTALLED_DEFAULT_TEMPLATE

    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT value FROM config_template_meta WHERE key = 'installed_default'",
        ).fetchone()
    if row:
        return str(row["value"])
    return INSTALLED_DEFAULT_TEMPLATE


def list_config_template_names() -> list[str]:
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT name FROM config_templates ORDER BY name COLLATE NOCASE",
        ).fetchall()
    return [str(r["name"]) for r in rows]


def load_config_template(name: str) -> dict[str, Any] | None:
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT payload_json FROM config_templates WHERE name = ?",
            (name,),
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload_json"])
    except json.JSONDecodeError as exc:
        log.warning("config template JSON corrupt for %s: %s", name, exc)
        return None


def load_config_templates_store() -> dict[str, dict[str, Any]]:
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT name, payload_json FROM config_templates ORDER BY name COLLATE NOCASE",
        ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            out[str(row["name"])] = json.loads(row["payload_json"])
        except json.JSONDecodeError as exc:
            log.warning("config template JSON corrupt for %s: %s", row["name"], exc)
    return out


def save_config_template(name: str, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with _lock:
        conn = _connect()
        conn.execute(
            """
            INSERT INTO config_templates(name, payload_json, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (name, body, _template_now_iso()),
        )
        conn.commit()


def delete_config_template(name: str) -> bool:
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "DELETE FROM config_templates WHERE name = ?",
            (name,),
        )
        conn.commit()
        return cur.rowcount > 0
