from datetime import date, timedelta
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.rule import Rule
from rich import box

from . import db
from .parser import slug_to_name
from .et_calc import fmt_usd

REPORTS_DIR = Path.home() / ".token-sentinel" / "reports"


# ── data assembly ─────────────────────────────────────────────────────────────

def _sessions(conn, today_only: bool) -> list:
    if today_only:
        return conn.execute(
            "SELECT * FROM sessions WHERE started_at LIKE ? ORDER BY cost_usd DESC",
            (f"{date.today().isoformat()}%",),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM sessions ORDER BY cost_usd DESC"
    ).fetchall()


def _daily_totals(conn, days: int = 7) -> list:
    since = (date.today() - timedelta(days=days)).isoformat()
    return conn.execute(
        "SELECT date, SUM(cost_usd) as cost, SUM(call_count) as calls "
        "FROM daily_et WHERE date >= ? GROUP BY date ORDER BY date",
        (since,),
    ).fetchall()


def build(today_only: bool = False) -> dict:
    with db.get_conn() as conn:
        sessions = _sessions(conn, today_only)
        daily = _daily_totals(conn, 7)

        # per-project rollup
        projects: dict[str, dict] = {}
        for s in sessions:
            slug = s["project_slug"]
            if slug not in projects:
                projects[slug] = {
                    "name": slug_to_name(slug),
                    "sessions": 0,
                    "cost_usd": 0.0,
                    "llm_calls": 0,
                    "cache_read": 0,
                    "input_tokens": 0,
                    "models": set(),
                }
            p = projects[slug]
            p["sessions"] += 1
            p["cost_usd"] += s["cost_usd"]
            p["llm_calls"] += s["llm_call_count"]
            p["cache_read"] += s["cache_read"]
            p["input_tokens"] += s["input_tokens"]
            if s["model"]:
                p["models"].add(s["model"])

        # model distribution (across all sessions)
        all_sessions_full = conn.execute(
            "SELECT model, llm_call_count FROM sessions"
        ).fetchall()
        model_calls: dict[str, int] = {}
        for s in all_sessions_full:
            m = s["model"] or "unknown"
            family = _model_family(m)
            model_calls[family] = model_calls.get(family, 0) + s["llm_call_count"]

        # top 5 sessions by cost
        top5 = conn.execute(
            "SELECT session_id, project_slug, started_at, cost_usd, llm_call_count, cwd "
            "FROM sessions ORDER BY cost_usd DESC LIMIT 5"
        ).fetchall()

        # MCP waste summary — one row per project
        waste = conn.execute("""
            SELECT s.project_slug,
                   COUNT(DISTINCT r.tool_name) as registered,
                   COUNT(DISTINCT tc.tool_name) as used
            FROM sessions s
            LEFT JOIN registered_tools r ON r.session_id = s.session_id
            LEFT JOIN tool_calls tc ON tc.session_id = s.session_id
            GROUP BY s.project_slug
        """).fetchall()

    total_cost = sum(s["cost_usd"] for s in sessions)
    today_str = date.today().isoformat()

    # 7-day average (exclude today)
    past_days = [r for r in daily if r["date"] != today_str]
    avg_7d = sum(r["cost"] for r in past_days) / len(past_days) if past_days else 0.0
    today_cost = next((r["cost"] for r in daily if r["date"] == today_str), 0.0)

    return {
        "today_str": today_str,
        "today_only": today_only,
        "total_cost": total_cost,
        "today_cost": today_cost,
        "avg_7d": avg_7d,
        "daily": [dict(r) for r in daily],
        "projects": projects,
        "model_calls": model_calls,
        "top5": [dict(r) for r in top5],
        "waste": [dict(r) for r in waste],
    }


def _model_family(model: str) -> str:
    for key in ("haiku", "sonnet", "opus"):
        if key in model.lower():
            return f"claude-{key}"
    return model


# ── rich renderer ─────────────────────────────────────────────────────────────

def render_rich(data: dict, console: Console | None = None) -> None:
    c = console or Console()
    today = data["today_str"]
    label = "Today" if data["today_only"] else "All time"

    c.print()
    c.print(f"[bold cyan]Token Sentinel — Report[/bold cyan]  [dim]{today}[/dim]")
    c.print()

    # ── Summary ───────────────────────────────────────────────────────────────
    c.print(Rule("Summary", style="cyan"))
    c.print(f"  {label} spend    : [bold green]{fmt_usd(data['today_cost'] if data['today_only'] else data['total_cost'])}[/bold green]")
    if data["avg_7d"] > 0:
        delta = ((data["today_cost"] - data["avg_7d"]) / data["avg_7d"]) * 100
        arrow = "↑" if delta > 0 else "↓"
        colour = "red" if delta > 15 else "green" if delta < -5 else "yellow"
        c.print(f"  7-day avg/day  : {fmt_usd(data['avg_7d'])}  [{colour}]{arrow} {abs(delta):.0f}%[/{colour}] vs avg")
    c.print()

    # ── By project ────────────────────────────────────────────────────────────
    c.print(Rule("By Project", style="cyan"))
    ptable = Table(box=box.SIMPLE, show_header=True, pad_edge=False)
    ptable.add_column("Project", style="cyan")
    ptable.add_column("Sessions", justify="right")
    ptable.add_column("Cost", justify="right", style="green")
    ptable.add_column("LLM Calls", justify="right")
    ptable.add_column("Cache Ratio", justify="right")

    for slug, p in sorted(data["projects"].items(), key=lambda x: -x[1]["cost_usd"]):
        ratio = (
            f"{p['cache_read'] / max(p['input_tokens'], 1):.0f}:1"
            if p["input_tokens"] > 0 else "n/a"
        )
        ptable.add_row(
            p["name"] or slug,
            str(p["sessions"]),
            fmt_usd(p["cost_usd"]),
            str(p["llm_calls"]),
            ratio,
        )
    c.print(ptable)

    # ── MCP waste summary ─────────────────────────────────────────────────────
    waste_rows = [w for w in data["waste"] if (w["registered"] - w["used"]) > 5]
    if waste_rows:
        c.print(Rule("MCP Waste", style="yellow"))
        c.print("  [dim]Projects loading unused tools into every API call.[/dim]")
        wtable = Table(box=box.SIMPLE, show_header=True, pad_edge=False)
        wtable.add_column("Project", style="cyan")
        wtable.add_column("Registered", justify="right")
        wtable.add_column("Used", justify="right")
        wtable.add_column("Wasted", justify="right", style="red")
        for w in sorted(waste_rows, key=lambda x: -(x["registered"] - x["used"])):
            wasted = w["registered"] - w["used"]
            wtable.add_row(
                slug_to_name(w["project_slug"]),
                str(w["registered"]),
                str(w["used"]),
                str(wasted),
            )
        c.print(wtable)
        c.print(f"  Run [cyan]sentinel audit --all[/cyan] for full prune recommendations.")
        c.print()

    # ── Model distribution ────────────────────────────────────────────────────
    c.print(Rule("Model Distribution", style="cyan"))
    total_calls = sum(data["model_calls"].values()) or 1
    for family, calls in sorted(data["model_calls"].items(), key=lambda x: -x[1]):
        pct = calls / total_calls * 100
        bar = "█" * int(pct / 5)
        c.print(f"  {family:<22} {bar:<20} {pct:5.1f}%  ({calls} calls)")
    c.print()

    # ── Most expensive sessions ───────────────────────────────────────────────
    c.print(Rule("Most Expensive Sessions", style="cyan"))
    stable = Table(box=box.SIMPLE, show_header=True, pad_edge=False)
    stable.add_column("Cost", justify="right", style="green")
    stable.add_column("Project", style="cyan")
    stable.add_column("Date", style="dim")
    stable.add_column("Calls", justify="right")
    stable.add_column("Session ID", style="dim")
    for s in data["top5"]:
        stable.add_row(
            fmt_usd(s["cost_usd"]),
            slug_to_name(s["project_slug"]),
            (s["started_at"] or "")[:10],
            str(s["llm_call_count"]),
            s["session_id"][:8] + "...",
        )
    c.print(stable)

    # ── Cache efficiency ──────────────────────────────────────────────────────
    c.print(Rule("Cache Efficiency", style="cyan"))
    c.print("  [dim]Higher ratio = more cache hits = lower cost.[/dim]")
    for slug, p in sorted(data["projects"].items(), key=lambda x: -x[1]["cache_read"]):
        if p["input_tokens"] == 0:
            continue
        ratio = p["cache_read"] / p["input_tokens"]
        if ratio >= 100:
            grade, colour = "excellent", "green"
        elif ratio >= 20:
            grade, colour = "good", "yellow"
        else:
            grade, colour = "poor — consider warming context", "red"
        c.print(f"  {p['name'] or slug:<25} {ratio:>8.0f}:1  [{colour}]{grade}[/{colour}]")
    c.print()


# ── markdown renderer ─────────────────────────────────────────────────────────

def render_markdown(data: dict) -> str:
    lines = [
        f"# Token Sentinel — {data['today_str']}",
        "",
        "## Summary",
        f"- Today: {fmt_usd(data['today_cost'])}",
        f"- 7-day avg: {fmt_usd(data['avg_7d'])}/day",
        "",
        "## By Project",
        "| Project | Sessions | Cost | LLM Calls | Cache Ratio |",
        "|---|---|---|---|---|",
    ]
    for slug, p in sorted(data["projects"].items(), key=lambda x: -x[1]["cost_usd"]):
        ratio = (
            f"{p['cache_read'] / max(p['input_tokens'], 1):.0f}:1"
            if p["input_tokens"] > 0 else "n/a"
        )
        lines.append(
            f"| {p['name'] or slug} | {p['sessions']} | {fmt_usd(p['cost_usd'])} "
            f"| {p['llm_calls']} | {ratio} |"
        )

    lines += ["", "## Most Expensive Sessions",
              "| Cost | Project | Date | Calls | Session |",
              "|---|---|---|---|---|"]
    for s in data["top5"]:
        lines.append(
            f"| {fmt_usd(s['cost_usd'])} | {slug_to_name(s['project_slug'])} "
            f"| {(s['started_at'] or '')[:10]} | {s['llm_call_count']} "
            f"| {s['session_id'][:8]}... |"
        )

    lines += ["", "## Cache Efficiency", "| Project | Ratio |", "|---|---|"]
    for slug, p in sorted(data["projects"].items(), key=lambda x: -x[1]["cache_read"]):
        if p["input_tokens"] > 0:
            ratio = p["cache_read"] / p["input_tokens"]
            lines.append(f"| {p['name'] or slug} | {ratio:.0f}:1 |")

    return "\n".join(lines)


# ── save to disk ──────────────────────────────────────────────────────────────

def save(data: dict) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    md = render_markdown(data)
    out = REPORTS_DIR / f"{data['today_str']}.md"
    out.write_text(md)
    latest = REPORTS_DIR / "latest.md"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(out.name)
    return out
