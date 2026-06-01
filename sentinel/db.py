import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

DB_PATH = Path.home() / ".token-sentinel" / "sentinel.db"


@contextmanager
def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id      TEXT PRIMARY KEY,
            project_slug    TEXT NOT NULL,
            cwd             TEXT,
            started_at      TEXT,
            ended_at        TEXT,
            model           TEXT,
            turn_count      INTEGER DEFAULT 0,
            total_et        REAL    DEFAULT 0,
            cost_usd        REAL    DEFAULT 0,
            input_tokens    INTEGER DEFAULT 0,
            output_tokens   INTEGER DEFAULT 0,
            cache_read      INTEGER DEFAULT 0,
            cache_write     INTEGER DEFAULT 0,
            llm_call_count  INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS tool_calls (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT NOT NULL,
            tool_name   TEXT NOT NULL,
            called_at   TEXT,
            et          REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS registered_tools (
            session_id  TEXT NOT NULL,
            tool_name   TEXT NOT NULL,
            PRIMARY KEY (session_id, tool_name)
        );

        CREATE TABLE IF NOT EXISTS daily_et (
            date         TEXT NOT NULL,
            project_slug TEXT NOT NULL,
            model        TEXT NOT NULL,
            total_et     REAL    DEFAULT 0,
            cost_usd     REAL    DEFAULT 0,
            call_count   INTEGER DEFAULT 0,
            PRIMARY KEY (date, project_slug, model)
        );

        CREATE TABLE IF NOT EXISTS alert_log (
            alert_type  TEXT NOT NULL,
            target      TEXT NOT NULL,
            fired_at    TEXT NOT NULL,
            PRIMARY KEY (alert_type, target)
        );

        CREATE TABLE IF NOT EXISTS file_state (
            file_path   TEXT PRIMARY KEY,
            last_offset INTEGER DEFAULT 0,
            last_parsed TEXT
        );
        """)
        # Migrate existing DB — add columns that weren't in the initial schema
        _add_column_if_missing(conn, "sessions", "cost_usd", "REAL DEFAULT 0")
        _add_column_if_missing(conn, "daily_et", "cost_usd", "REAL DEFAULT 0")


def _add_column_if_missing(conn, table: str, column: str, col_def: str) -> None:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")


# ── session ───────────────────────────────────────────────────────────────────

def upsert_session(rec) -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO sessions
                (session_id, project_slug, cwd, started_at, ended_at, model,
                 turn_count, total_et, cost_usd, input_tokens, output_tokens,
                 cache_read, cache_write, llm_call_count)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(session_id) DO UPDATE SET
                ended_at       = excluded.ended_at,
                model          = COALESCE(excluded.model, model),
                turn_count     = excluded.turn_count,
                total_et       = excluded.total_et,
                cost_usd       = excluded.cost_usd,
                input_tokens   = excluded.input_tokens,
                output_tokens  = excluded.output_tokens,
                cache_read     = excluded.cache_read,
                cache_write    = excluded.cache_write,
                llm_call_count = excluded.llm_call_count
        """, (
            rec.session_id, rec.project_slug, rec.cwd,
            rec.started_at, rec.ended_at, rec.model,
            rec.turn_count, rec.total_et, rec.total_cost_usd,
            rec.total_input_tokens, rec.total_output_tokens,
            rec.total_cache_read, rec.total_cache_write,
            rec.llm_call_count,
        ))


def get_all_session_ids() -> set[str]:
    with get_conn() as conn:
        return {r["session_id"] for r in conn.execute("SELECT session_id FROM sessions")}


def get_project_rolling_avg_cost(
    project_slug: str,
    exclude_session_id: str | None = None,
    limit: int = 10,
) -> float:
    """Rolling average cost_usd for a project's last N sessions."""
    with get_conn() as conn:
        q = "SELECT cost_usd FROM sessions WHERE project_slug=?"
        params: list = [project_slug]
        if exclude_session_id:
            q += " AND session_id!=?"
            params.append(exclude_session_id)
        q += f" ORDER BY started_at DESC LIMIT {limit}"
        rows = conn.execute(q, params).fetchall()
        if not rows:
            return 0.0
        return sum(r["cost_usd"] for r in rows) / len(rows)


# ── tool calls / registered tools ─────────────────────────────────────────────

def upsert_tool_calls(session_id: str, tool_calls: list) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM tool_calls WHERE session_id=?", (session_id,))
        conn.executemany(
            "INSERT INTO tool_calls (session_id, tool_name, called_at, et) VALUES (?,?,?,?)",
            [(session_id, tc.tool_name, tc.called_at, tc.et) for tc in tool_calls],
        )


def upsert_registered_tools(session_id: str, tools: list[str]) -> None:
    with get_conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO registered_tools (session_id, tool_name) VALUES (?,?)",
            [(session_id, t) for t in tools],
        )


# ── daily ET ──────────────────────────────────────────────────────────────────

def upsert_daily_et(
    date_str: str, project_slug: str, model: str,
    total_et: float, cost_usd: float, call_count: int,
) -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO daily_et (date, project_slug, model, total_et, cost_usd, call_count)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(date, project_slug, model) DO UPDATE SET
                total_et   = excluded.total_et,
                cost_usd   = excluded.cost_usd,
                call_count = excluded.call_count
        """, (date_str, project_slug, model, total_et, cost_usd, call_count))


def get_today_total_cost() -> float:
    today = date.today().isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT SUM(cost_usd) FROM daily_et WHERE date=?", (today,)
        ).fetchone()
        return row[0] or 0.0


def get_today_session_count() -> int:
    today = date.today().isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE started_at LIKE ?",
            (f"{today}%",),
        ).fetchone()
        return row[0] or 0


# ── file state ────────────────────────────────────────────────────────────────

def get_file_state(file_path: Path) -> tuple[int, str | None]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT last_offset, last_parsed FROM file_state WHERE file_path=?",
            (str(file_path),),
        ).fetchone()
        return (row["last_offset"], row["last_parsed"]) if row else (0, None)


def set_file_state(file_path: Path, offset: int, parsed_at: str) -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO file_state (file_path, last_offset, last_parsed)
            VALUES (?,?,?)
            ON CONFLICT(file_path) DO UPDATE SET
                last_offset = excluded.last_offset,
                last_parsed = excluded.last_parsed
        """, (str(file_path), offset, parsed_at))


# ── alerts ────────────────────────────────────────────────────────────────────

def should_fire(alert_type: str, target: str, cooldown_hours: float = 1.0) -> bool:
    """True if the alert has never fired for this target, or cooldown has expired."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT fired_at FROM alert_log WHERE alert_type=? AND target=?",
            (alert_type, target),
        ).fetchone()
        if not row:
            return True
        fired_at = datetime.fromisoformat(row["fired_at"])
        return datetime.utcnow() - fired_at > timedelta(hours=cooldown_hours)


def record_alert(alert_type: str, target: str) -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO alert_log (alert_type, target, fired_at) VALUES (?,?,?)
            ON CONFLICT(alert_type, target) DO UPDATE SET fired_at=excluded.fired_at
        """, (alert_type, target, datetime.utcnow().isoformat()))
