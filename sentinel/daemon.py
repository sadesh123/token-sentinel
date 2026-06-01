import logging
import os
import signal
import time
from datetime import datetime
from pathlib import Path

from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler

from . import config as cfg, db, notifier
from .auditor import TOOL_ADVICE
from .et_calc import fmt_usd
from .kiro import annotate_specs_dir, find_kiro_dirs, summary_toast_body
from .parser import PROJECTS_DIR, parse_jsonl, slug_to_name

LOG_DIR = Path.home() / ".token-sentinel" / "logs"
PID_FILE = Path.home() / ".token-sentinel" / "daemon.pid"
POLL_INTERVAL = 30  # seconds — PollingObserver for WSL /mnt/c/ compatibility

logger = logging.getLogger("sentinel.daemon")
_POLL_INTERVAL_ALIAS = POLL_INTERVAL  # re-export for tests


def setup_logging() -> None:
    from logging.handlers import TimedRotatingFileHandler
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = TimedRotatingFileHandler(
        LOG_DIR / "daemon.log", when="D", interval=1, backupCount=7
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


# ── Ingester ──────────────────────────────────────────────────────────────────

class Ingester:
    """Parses JSONL files into SQLite and dispatches alerts."""

    def ingest_all(self) -> None:
        """Initial load — parse every existing .jsonl, fire no alerts."""
        known = db.get_all_session_ids()
        count = 0
        for jsonl_file in sorted(PROJECTS_DIR.glob("**/*.jsonl")):
            try:
                self._ingest_file(jsonl_file, fire_alerts=False)
                count += 1
            except Exception as e:
                logger.error(f"Initial load error {jsonl_file}: {e}")
        logger.info(f"Initial load: {count} files processed.")
        self._refresh_prompt_status()

    def ingest(self, file_path: Path) -> None:
        """Incremental ingest for a file change event — alerts enabled."""
        try:
            self._ingest_file(file_path, fire_alerts=True)
        except Exception as e:
            logger.error(f"Ingest error {file_path}: {e}")

    def _ingest_file(self, file_path: Path, fire_alerts: bool) -> None:
        last_offset, _ = db.get_file_state(file_path)

        try:
            file_size = file_path.stat().st_size
        except OSError:
            return

        if file_size <= last_offset:
            return  # Nothing new

        is_new_file = last_offset == 0

        # Re-parse the full file for correctness (streaming dedup, accumulation).
        # ON CONFLICT DO UPDATE in db makes every write idempotent.
        rec = parse_jsonl(file_path)
        if not rec:
            return

        db.upsert_session(rec)
        db.upsert_registered_tools(rec.session_id, rec.registered_tools)
        db.upsert_tool_calls(rec.session_id, rec.tool_calls)

        if rec.started_at:
            day = rec.started_at[:10]
            db.upsert_daily_et(
                day, rec.project_slug, rec.model or "unknown",
                rec.total_et, rec.total_cost_usd, rec.llm_call_count,
            )

        db.set_file_state(file_path, file_size, datetime.utcnow().isoformat())

        logger.info(
            f"Ingested {rec.session_id[:8]}  project={slug_to_name(rec.project_slug)}"
            f"  cost={fmt_usd(rec.total_cost_usd)}  tools={len(rec.registered_tools)}"
            f"  llm_calls={rec.llm_call_count}"
        )

        if fire_alerts:
            self._dispatch_alerts(rec, is_new_file=is_new_file)
        self._refresh_prompt_status()

    # ── alert dispatch ────────────────────────────────────────────────────────

    def _dispatch_alerts(self, rec, is_new_file: bool) -> None:
        if is_new_file:
            self._alert_mcp_waste(rec)
        self._alert_et_spike(rec)
        self._alert_cache_collapse(rec)
        self._alert_budget()

    def _alert_mcp_waste(self, rec) -> None:
        """Fire if a new session loaded more than 10 tools it will never use."""
        used = {tc.tool_name for tc in rec.tool_calls}
        excl = cfg.get_exclusions(rec.project_slug)

        waste_tools = [
            t for t in rec.registered_tools
            if t not in used
            and t not in excl
            and TOOL_ADVICE.get(t, ("exclude",))[0] == "exclude"
        ]
        n = len(waste_tools)
        if n <= 10:
            return

        alert_key = rec.session_id
        if not db.should_fire("mcp_waste", alert_key, cooldown_hours=0):
            return

        name = slug_to_name(rec.project_slug)
        notifier.send_toast(
            "Token Sentinel — MCP Waste Detected",
            f"{name}: {n} unused tools loaded into context. "
            f"Run: sentinel audit --project {name}",
        )
        db.record_alert("mcp_waste", alert_key)
        logger.info(f"Alert: mcp_waste  session={rec.session_id[:8]}  unused={n}")

    def _alert_et_spike(self, rec) -> None:
        """Fire if this session's cost is more than 2× the project rolling average."""
        if rec.total_cost_usd == 0:
            return

        avg = db.get_project_rolling_avg_cost(
            rec.project_slug, exclude_session_id=rec.session_id
        )
        if avg == 0 or rec.total_cost_usd <= 2 * avg:
            return

        if not db.should_fire("et_spike", rec.session_id, cooldown_hours=0):
            return

        name = slug_to_name(rec.project_slug)
        ratio = rec.total_cost_usd / avg
        notifier.send_toast(
            "Token Sentinel — Session Spike",
            f"{name}: {fmt_usd(rec.total_cost_usd)} ({ratio:.1f}× avg {fmt_usd(avg)}). "
            f"Run: sentinel stats --session {rec.session_id}",
        )
        db.record_alert("et_spike", rec.session_id)
        logger.info(
            f"Alert: cost_spike  session={rec.session_id[:8]}"
            f"  cost={fmt_usd(rec.total_cost_usd)}  avg={fmt_usd(avg)}  ratio={ratio:.1f}x"
        )

    def _alert_cache_collapse(self, rec) -> None:
        """Fire if cache ratio falls below 10:1 for a project that normally runs hot."""
        if rec.total_input_tokens < 500:
            return  # too small to be meaningful
        ratio = rec.total_cache_read / rec.total_input_tokens

        if ratio >= 10:
            return

        # Only alert if the project has prior sessions (baseline exists)
        avg = db.get_project_rolling_avg_cost(
            rec.project_slug, exclude_session_id=rec.session_id
        )
        if avg == 0:
            return

        if not db.should_fire("cache_collapse", rec.session_id, cooldown_hours=0):
            return

        name = slug_to_name(rec.project_slug)
        notifier.send_toast(
            "Token Sentinel — Cache Collapsed",
            f"{name}: cache ratio {ratio:.0f}:1 (expected >50:1). "
            "Context structure may have changed.",
        )
        db.record_alert("cache_collapse", rec.session_id)
        logger.info(
            f"Alert: cache_collapse  session={rec.session_id[:8]}  ratio={ratio:.1f}:1"
        )

    def _alert_budget(self) -> None:
        """Fire at 80% and 100% of daily dollar budget (once per hour per threshold)."""
        config = cfg.load()
        budget = config.get("daily_budget_usd", 0)
        if budget <= 0:
            return

        today_cost = db.get_today_total_cost()
        pct = today_cost / budget
        from datetime import date
        today = date.today().isoformat()

        if pct >= 1.0 and db.should_fire("budget_100", today, cooldown_hours=1):
            notifier.send_toast(
                "Token Sentinel — Daily Budget Exceeded",
                f"Spent {fmt_usd(today_cost)} of {fmt_usd(budget)} daily budget "
                f"({int(pct * 100)}%). Consider pausing.",
            )
            db.record_alert("budget_100", today)
            logger.info(f"Alert: budget_100  spent={fmt_usd(today_cost)}  budget={fmt_usd(budget)}")

        elif 0.8 <= pct < 1.0 and db.should_fire("budget_80", today, cooldown_hours=1):
            notifier.send_toast(
                "Token Sentinel — 80% Budget Used",
                f"Spent {fmt_usd(today_cost)} of {fmt_usd(budget)} today "
                f"({int(pct * 100)}%).",
            )
            db.record_alert("budget_80", today)
            logger.info(f"Alert: budget_80  spent={fmt_usd(today_cost)}  budget={fmt_usd(budget)}")

    def _refresh_prompt_status(self) -> None:
        try:
            config = cfg.load()
            budget = config.get("daily_budget_usd", 5.00)
            notifier.update_prompt_status(
                db.get_today_total_cost(),
                db.get_today_session_count(),
                budget,
            )
        except Exception as e:
            logger.warning(f"Prompt status update failed: {e}")


# ── JSONL file watcher ────────────────────────────────────────────────────────

class _JsonlHandler(FileSystemEventHandler):
    def __init__(self, ingester: Ingester):
        self._ingester = ingester
        self._pending: set[str] = set()

    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith(".jsonl"):
            self._pending.add(event.src_path)

    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(".jsonl"):
            self._pending.add(event.src_path)

    def flush(self) -> int:
        paths, self._pending = list(self._pending), set()
        for p in paths:
            self._ingester.ingest(Path(p))
        return len(paths)


# ── Kiro spec watcher ─────────────────────────────────────────────────────────

class _KiroHandler(FileSystemEventHandler):
    """Watches .kiro/specs/ directories and annotates changed spec files."""

    def __init__(self, specs_dir: Path, project_name: str):
        self._specs_dir = specs_dir
        self._project_name = project_name
        self._pending: set[str] = set()

    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            # Ignore lines we just wrote ourselves (annotation lines)
            self._pending.add(event.src_path)

    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            self._pending.add(event.src_path)

    def flush(self) -> int:
        if not self._pending:
            return 0
        self._pending.clear()

        # Re-score the whole specs dir (any file change may affect context)
        try:
            results = annotate_specs_dir(self._specs_dir)
        except Exception as e:
            logger.error(f"Kiro annotation error {self._specs_dir}: {e}")
            return 0

        if not results:
            return 0

        all_tasks = [t for tasks in results.values() for t in tasks]
        logger.info(
            f"Kiro: annotated {len(all_tasks)} task(s) in {self._project_name}"
        )

        # Toast — only fire if this specific dir hasn't alerted in the last 5 min
        alert_key = f"kiro:{self._specs_dir}"
        if db.should_fire("kiro_annotated", alert_key, cooldown_hours=0.083):
            body = summary_toast_body(self._project_name, results)
            notifier.send_toast("Token Sentinel — Kiro Specs Scored", body)
            db.record_alert("kiro_annotated", alert_key)

        return len(all_tasks)


def _discover_kiro_dirs() -> list[tuple[Path, str]]:
    """
    Query session CWDs from SQLite and return (specs_dir, project_name)
    for every project that has a .kiro/specs/ directory.
    """
    found = []
    try:
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT cwd FROM sessions WHERE cwd IS NOT NULL AND cwd != ''"
            ).fetchall()
        for row in rows:
            root = Path(row["cwd"])
            specs = root / ".kiro" / "specs"
            if specs.exists() and specs.is_dir():
                name = root.name
                found.append((specs, name))
                logger.info(f"Kiro: found specs dir at {specs}")
    except Exception as e:
        logger.warning(f"Kiro discovery error: {e}")
    return found


# ── Daemon entry point ────────────────────────────────────────────────────────

class SentinelDaemon:
    def __init__(self):
        self._ingester = Ingester()
        self._running = False

    def run(self) -> None:
        setup_logging()
        db.init_db()

        logger.info("Token Sentinel daemon starting.")
        self._running = True

        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

        # Initial load — populates DB, no alerts
        self._ingester.ingest_all()

        observer = PollingObserver(timeout=POLL_INTERVAL)

        # Watch Claude Code JSONL transcripts
        jsonl_handler = _JsonlHandler(self._ingester)
        observer.schedule(jsonl_handler, str(PROJECTS_DIR), recursive=True)
        logger.info(f"Watching {PROJECTS_DIR} every {POLL_INTERVAL}s.")

        # Watch Kiro spec dirs discovered from session CWDs
        kiro_handlers: list[_KiroHandler] = []
        for specs_dir, project_name in _discover_kiro_dirs():
            kh = _KiroHandler(specs_dir, project_name)
            observer.schedule(kh, str(specs_dir), recursive=True)
            kiro_handlers.append(kh)
            logger.info(f"Watching Kiro specs: {specs_dir}")

        observer.start()

        try:
            while self._running:
                time.sleep(POLL_INTERVAL)
                flushed = jsonl_handler.flush()
                if flushed:
                    logger.info(f"Poll: processed {flushed} JSONL file(s).")
                for kh in kiro_handlers:
                    kh.flush()
        finally:
            observer.stop()
            observer.join()
            PID_FILE.unlink(missing_ok=True)
            logger.info("Daemon stopped.")

    def _on_signal(self, signum, frame):
        logger.info(f"Signal {signum} received — stopping.")
        self._running = False
