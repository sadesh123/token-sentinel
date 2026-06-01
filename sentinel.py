#!/usr/bin/env python3
import click
from rich.console import Console
from rich.table import Table
from rich import box
from collections import defaultdict
from pathlib import Path
import json
import os
import sys
from sentinel.et_calc import fmt_usd
from sentinel.parser import slug_to_name

console = Console()


@click.group()
def cli():
    """Token Sentinel — Claude Code observability."""
    pass


@cli.command()
@click.option("--raw", is_flag=True, help="Show raw token counts.")
@click.option("--et", "show_et", is_flag=True, help="Show ET breakdown per session.")
@click.option("--session", default=None, help="Show stats for a single session ID.")
@click.option("--today", is_flag=True, help="Filter to sessions from today only.")
@click.option("--et-only", is_flag=True, help="Print today's total cost as a single value (for prompt injection).")
@click.option("--export", "export_path", default=None, help="Export snapshot to JSON file for use with 'compare'.")
def stats(raw, show_et, session, today, et_only, export_path):
    """Parse all sessions and display token/ET statistics."""
    from sentinel.parser import iter_all_sessions, slug_to_name, PROJECTS_DIR
    from sentinel.et_calc import get_multiplier
    from datetime import date

    today_str = date.today().isoformat()

    # Aggregate per project
    projects: dict[str, dict] = defaultdict(lambda: {
        "name": "",
        "sessions": 0,
        "total_cost_usd": 0.0,
        "llm_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read": 0,
        "cache_write": 0,
        "models": set(),
    })

    target_session = None

    for rec in iter_all_sessions():
        if session and rec.session_id != session:
            continue
        if today and not rec.started_at.startswith(today_str):
            continue

        slug = rec.project_slug
        p = projects[slug]
        p["name"] = slug_to_name(slug)
        p["sessions"] += 1
        p["total_cost_usd"] += rec.total_cost_usd
        p["llm_calls"] += rec.llm_call_count
        p["input_tokens"] += rec.total_input_tokens
        p["output_tokens"] += rec.total_output_tokens
        p["cache_read"] += rec.total_cache_read
        p["cache_write"] += rec.total_cache_write
        if rec.model:
            p["models"].add(rec.model)

        if session and rec.session_id == session:
            target_session = rec

    if et_only:
        total = sum(p["total_cost_usd"] for p in projects.values())
        click.echo(_fmt_usd(total))
        return

    if session and target_session:
        _print_session_detail(target_session, show_et)
        return

    if not projects:
        console.print("[yellow]No sessions found.[/yellow]")
        return

    if export_path:
        from datetime import datetime
        from sentinel.auditor import run_audit
        audit = run_audit()
        snapshot = {
            "captured_at": datetime.now().isoformat(),
            "total_cost_usd": sum(p["total_cost_usd"] for p in projects.values()),
            "total_llm_calls": sum(p.get("llm_calls", 0) for p in projects.values()),
            "projects": {
                slug: {
                    "name": p["name"],
                    "sessions": p["sessions"],
                    "avg_cost_usd": p["total_cost_usd"] / max(p["sessions"], 1),
                    "never_used_tools": len(audit.get(slug, {}).get("never_used_annotated", [])),
                }
                for slug, p in projects.items()
            },
        }
        Path(export_path).write_text(json.dumps(snapshot, indent=2))
        console.print(f"[green]✓[/green] Snapshot saved to [cyan]{export_path}[/cyan]")
        return

    if raw:
        _print_raw_table(projects)
    else:
        _print_et_table(projects)


def _print_et_table(projects: dict):
    table = Table(title="Token Sentinel — Cost by Project", box=box.SIMPLE_HEAVY)
    table.add_column("Project", style="cyan", no_wrap=True)
    table.add_column("Sessions", justify="right")
    table.add_column("Cost (USD)", justify="right", style="green")
    table.add_column("Cache Ratio", justify="right")
    table.add_column("Models")

    total_cost = 0.0
    for slug, p in sorted(projects.items(), key=lambda x: -x[1]["total_cost_usd"]):
        cache_ratio = (
            f"{p['cache_read'] / max(p['input_tokens'], 1):.0f}:1"
            if p["input_tokens"] > 0
            else "n/a"
        )
        models = ", ".join(sorted(p["models"])) or "unknown"
        cost = p["total_cost_usd"]
        total_cost += cost
        table.add_row(
            p["name"] or slug,
            str(p["sessions"]),
            _fmt_usd(cost),
            cache_ratio,
            models,
        )

    table.add_section()
    table.add_row("TOTAL", "", _fmt_usd(total_cost), "", "", style="bold")
    console.print(table)


def _print_raw_table(projects: dict):
    table = Table(title="Token Sentinel — Raw Counts by Project", box=box.SIMPLE_HEAVY)
    table.add_column("Project", style="cyan", no_wrap=True)
    table.add_column("Sessions", justify="right")
    table.add_column("Input", justify="right")
    table.add_column("Cache Read", justify="right")
    table.add_column("Cache Write", justify="right")
    table.add_column("Output", justify="right")

    for slug, p in sorted(projects.items(), key=lambda x: -x[1]["total_et"]):
        table.add_row(
            p["name"] or slug,
            str(p["sessions"]),
            f"{p['input_tokens']:,}",
            f"{p['cache_read']:,}",
            f"{p['cache_write']:,}",
            f"{p['output_tokens']:,}",
        )
    console.print(table)


def _print_session_detail(rec, show_et: bool):
    console.print(f"\n[bold cyan]Session:[/bold cyan] {rec.session_id}")
    console.print(f"[bold]Project:[/bold] {rec.project_slug}")
    console.print(f"[bold]Model:[/bold]   {rec.model}")
    console.print(f"[bold]CWD:[/bold]     {rec.cwd}")
    console.print(f"[bold]Started:[/bold] {rec.started_at}")
    console.print(f"[bold]Ended:[/bold]   {rec.ended_at}")
    console.print(f"[bold]Turns:[/bold]   {rec.turn_count}")
    console.print(f"\n[bold green]Cost (USD):[/bold green]  {_fmt_usd(rec.total_cost_usd)}")
    console.print(f"[bold]Input:[/bold]       {rec.total_input_tokens:,}")
    console.print(f"[bold]Cache read:[/bold]  {rec.total_cache_read:,}")
    console.print(f"[bold]Cache write:[/bold] {rec.total_cache_write:,}")
    console.print(f"[bold]Output:[/bold]      {rec.total_output_tokens:,}")

    if rec.total_input_tokens > 0:
        ratio = rec.total_cache_read / rec.total_input_tokens
        console.print(f"[bold]Cache ratio:[/bold] {ratio:.0f}:1")

    console.print(f"\n[bold]Registered tools:[/bold] {len(rec.registered_tools)}")
    used = {tc.tool_name for tc in rec.tool_calls}
    console.print(f"[bold]Used tools:[/bold]       {len(used)} — {', '.join(sorted(used)) or 'none'}")
    never = [t for t in rec.registered_tools if t not in used]
    if never:
        console.print(f"[bold yellow]Never used:[/bold yellow]      {len(never)}")


def _fmt_usd(v: float) -> str:
    if v >= 10:
        return f"${v:.2f}"
    if v >= 0.01:
        return f"${v:.3f}"
    return f"${v:.5f}"


@cli.command()
@click.option("--project", default=None, help="Audit a single project by name (e.g. VibeCodingWorkshop).")
@click.option("--all", "all_projects", is_flag=True, help="Audit all projects.")
def audit(project, all_projects):
    """Identify never-used MCP tools and estimate wasted ET."""
    from sentinel.auditor import run_audit
    from sentinel.parser import PROJECTS_DIR

    # Resolve project name → slug
    filter_slug = None
    if project:
        for d in PROJECTS_DIR.iterdir():
            if d.is_dir() and project.lower() in d.name.lower():
                filter_slug = d.name
                break
        if not filter_slug:
            console.print(f"[red]Project '{project}' not found.[/red]")
            return

    results = run_audit(filter_slug)

    if not results:
        console.print("[yellow]No sessions found.[/yellow]")
        return

    for slug, r in sorted(results.items(), key=lambda x: -x[1]["wasted_usd_total"]):
        _print_audit_result(slug, r)
        console.print()


def _print_audit_result(slug: str, r: dict):
    name = r["name"] or slug
    n_reg = len(r["registered"])
    n_used = len(r["used_registered"])
    annotated = r["never_used_annotated"]
    excluded = r["excluded_tools"]
    n_never = len(annotated)

    console.print(f"\n[bold cyan]MCP AUDIT — {name}[/bold cyan]")
    console.rule()
    console.print(f"  Sessions analysed  : [bold]{r['sessions']}[/bold]")
    console.print(f"  Avg LLM calls/sess : [bold]{r['avg_llm_calls']:.1f}[/bold]")
    console.print(f"  Model              : {r['model']} (×{r['multiplier']})")
    console.print()
    console.print(f"  Registered tools   : [bold]{n_reg}[/bold]")
    console.print(
        f"  Actually used      : [bold green]{n_used}[/bold green]"
        + (f"  — {', '.join(r['used_registered'])}" if r["used_registered"] else "  (none from registered list)")
    )
    console.print(f"  Never used         : [bold red]{n_never}[/bold red]")
    if excluded:
        console.print(f"  Excluded by config : [dim]{len(excluded)} tool(s) hidden[/dim]")
    console.print()

    if r["wasted_usd_per_session"] > 0:
        console.print(
            f"  [yellow bold]Estimated cost waste / session : {_fmt_usd(r['wasted_usd_per_session'])}[/yellow bold]"
        )
        console.print(
            f"  [yellow]Estimated cost waste total     : {_fmt_usd(r['wasted_usd_total'])}[/yellow]"
        )
        console.print()

    if not annotated:
        console.print("  [green]No unexcluded waste.[/green]")
        return

    # ── EXCLUDE ───────────────────────────────────────────────────────────────
    to_exclude = [a for a in annotated if a["action"] == "exclude"]
    if to_exclude:
        console.print("  [bold red]EXCLUDE — safe to remove from this project's context:[/bold red]")
        for a in to_exclude:
            console.print(f"    [red]✗[/red] [bold]{a['tool']}[/bold]")
            console.print(f"      [dim]{a['reason']}[/dim]")
            console.print(
                f"      [cyan]sentinel exclude add {a['tool']} --project {name}[/cyan]"
            )
        console.print()

    # ── CONSIDER ──────────────────────────────────────────────────────────────
    to_consider = [a for a in annotated if a["action"] == "consider"]
    if to_consider:
        console.print("  [bold yellow]CONSIDER — project-dependent, review before excluding:[/bold yellow]")
        for a in to_consider:
            console.print(f"    [yellow]~[/yellow] [bold]{a['tool']}[/bold]")
            console.print(f"      [dim]{a['reason']}[/dim]")
            console.print(
                f"      [cyan]sentinel exclude add {a['tool']} --project {name}[/cyan]"
            )
        console.print()

    # ── KEEP ──────────────────────────────────────────────────────────────────
    to_keep = [a for a in annotated if a["action"] == "keep"]
    if to_keep:
        console.print("  [bold green]KEEP — Claude uses these internally, do not prune:[/bold green]")
        for a in to_keep:
            console.print(f"    [green]✓[/green] [bold]{a['tool']}[/bold]")
            console.print(f"      [dim]{a['reason']}[/dim]")
        console.print()

    console.print("  [bold]To prune from Claude's context:[/bold]")
    console.print(
        "  Edit [cyan]~/.claude/settings.json[/cyan] → remove unused servers from [cyan]\"mcpServers\"[/cyan]."
    )
    console.print(
        "  Or add [cyan].claude/settings.json[/cyan] in the project root to override per-project."
    )


# ── sentinel exclude ──────────────────────────────────────────────────────────

@cli.group()
def exclude():
    """Manage audit exclusions — tools to suppress from the never-used list."""
    pass


@exclude.command("add")
@click.argument("tool")
@click.option("--project", default=None, help="Scope to a specific project name.")
def exclude_add(tool, project):
    """Add TOOL to the exclusion list."""
    from sentinel import config as cfg
    from sentinel.parser import PROJECTS_DIR

    slug = None
    if project:
        for d in PROJECTS_DIR.iterdir():
            if d.is_dir() and project.lower() in d.name.lower():
                slug = d.name
                break
        if not slug:
            console.print(f"[red]Project '{project}' not found.[/red]")
            return

    added = cfg.add_exclusion(tool, slug)
    scope = f"project '{project}'" if slug else "global"
    if added:
        console.print(f"[green]✓[/green] Excluded [bold]{tool}[/bold] ({scope}).")
    else:
        console.print(f"[yellow]{tool}[/yellow] already excluded ({scope}).")


@exclude.command("remove")
@click.argument("tool")
@click.option("--project", default=None, help="Scope to a specific project name.")
def exclude_remove(tool, project):
    """Remove TOOL from the exclusion list."""
    from sentinel import config as cfg
    from sentinel.parser import PROJECTS_DIR

    slug = None
    if project:
        for d in PROJECTS_DIR.iterdir():
            if d.is_dir() and project.lower() in d.name.lower():
                slug = d.name
                break

    removed = cfg.remove_exclusion(tool, slug)
    scope = f"project '{project}'" if slug else "global"
    if removed:
        console.print(f"[green]✓[/green] Removed exclusion for [bold]{tool}[/bold] ({scope}).")
    else:
        console.print(f"[yellow]{tool}[/yellow] was not in the exclusion list ({scope}).")


@exclude.command("list")
def exclude_list():
    """Show all current exclusions."""
    from sentinel import config as cfg

    excl = cfg.list_exclusions()
    g = excl.get("global", [])
    pp = excl.get("per_project", {})

    if not g and not any(pp.values()):
        console.print("[dim]No exclusions configured.[/dim]")
        return

    if g:
        console.print("[bold]Global exclusions:[/bold]")
        for t in sorted(g):
            console.print(f"  [dim]•[/dim] {t}")
        console.print()

    if pp:
        console.print("[bold]Per-project exclusions:[/bold]")
        for proj, tools in sorted(pp.items()):
            if tools:
                console.print(f"  [cyan]{proj}[/cyan]")
                for t in sorted(tools):
                    console.print(f"    [dim]•[/dim] {t}")


# ── sentinel daemon ───────────────────────────────────────────────────────────

_PID_FILE = Path.home() / ".token-sentinel" / "daemon.pid"
_LOG_FILE = Path.home() / ".token-sentinel" / "logs" / "daemon.log"


@cli.group()
def daemon():
    """Manage the background watcher daemon."""
    pass


@daemon.command("start")
def daemon_start():
    """Start the daemon in the background."""
    if _PID_FILE.exists():
        try:
            pid = int(_PID_FILE.read_text().strip())
            os.kill(pid, 0)
            console.print(f"[yellow]Daemon already running (PID {pid}).[/yellow]")
            return
        except (ProcessLookupError, ValueError, OSError):
            _PID_FILE.unlink(missing_ok=True)

    sentinel_script = Path(__file__).resolve()
    import subprocess
    err_log = Path.home() / ".token-sentinel" / "logs" / "daemon-stderr.log"
    err_log.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, str(sentinel_script), "daemon", "_run"],
        stdout=subprocess.DEVNULL,
        stderr=open(err_log, "a"),
        start_new_session=True,
    )
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(proc.pid))
    console.print(f"[green]✓[/green] Daemon started (PID {proc.pid}).")
    console.print(f"  Logs : [cyan]{_LOG_FILE}[/cyan]")
    console.print(f"  DB   : [cyan]{Path.home() / '.token-sentinel' / 'sentinel.db'}[/cyan]")


@daemon.command("stop")
def daemon_stop():
    """Stop the running daemon."""
    if not _PID_FILE.exists():
        console.print("[yellow]Daemon is not running.[/yellow]")
        return
    try:
        pid = int(_PID_FILE.read_text().strip())
        os.kill(pid, 15)  # SIGTERM
        _PID_FILE.unlink(missing_ok=True)
        console.print(f"[green]✓[/green] Daemon stopped (PID {pid}).")
    except (ProcessLookupError, ValueError):
        _PID_FILE.unlink(missing_ok=True)
        console.print("[yellow]Daemon PID was stale — cleaned up.[/yellow]")
    except PermissionError:
        console.print("[red]Permission denied sending signal to daemon.[/red]")


@daemon.command("status")
def daemon_status():
    """Show daemon status and recent log lines."""
    if not _PID_FILE.exists():
        console.print("[bold red]Daemon: NOT RUNNING[/bold red]")
        return

    try:
        pid = int(_PID_FILE.read_text().strip())
        os.kill(pid, 0)
        console.print(f"[bold green]Daemon: RUNNING[/bold green]  PID {pid}")
    except (ProcessLookupError, ValueError):
        console.print("[bold yellow]Daemon: STALE PID (not running)[/bold yellow]")
        _PID_FILE.unlink(missing_ok=True)
        return

    # Show last 10 log lines
    if _LOG_FILE.exists():
        lines = _LOG_FILE.read_text().splitlines()
        console.print(f"\n[dim]Last {min(10, len(lines))} log lines:[/dim]")
        for line in lines[-10:]:
            console.print(f"  [dim]{line}[/dim]")

    # Show prompt status
    status_file = Path.home() / ".token-sentinel" / "prompt-status.txt"
    if status_file.exists():
        console.print(f"\n[bold]Prompt status:[/bold] {status_file.read_text().strip()}")


@daemon.command("_run", hidden=True)
def daemon_run():
    """Internal: run the daemon loop (spawned by 'start')."""
    from sentinel.daemon import SentinelDaemon, PID_FILE
    PID_FILE.write_text(str(os.getpid()))
    SentinelDaemon().run()


# ── sentinel report ───────────────────────────────────────────────────────────

@cli.command()
@click.option("--today", is_flag=True, help="Filter to today's sessions only.")
@click.option("--save", "do_save", is_flag=True, help="Write markdown report to ~/.token-sentinel/reports/.")
def report(today, do_save):
    """Print a full cost and usage report."""
    from sentinel.reporter import build, render_rich, save
    from sentinel import db

    db.init_db()
    data = build(today_only=today)

    render_rich(data, console)

    if do_save:
        path = save(data)
        console.print(f"[dim]Report saved: {path}[/dim]")
    else:
        console.print(f"[dim]Tip: add --save to write a markdown report to disk.[/dim]")


# ── sentinel compare ──────────────────────────────────────────────────────────

@cli.command()
@click.argument("baseline", type=click.Path(exists=True))
@click.argument("after", type=click.Path(exists=True))
def compare(baseline, after):
    """Compare two cost snapshots exported with 'sentinel stats --export'."""
    import json
    from rich.table import Table
    from rich import box as rbox

    try:
        b = json.loads(Path(baseline).read_text())
        a = json.loads(Path(after).read_text())
    except Exception as e:
        console.print(f"[red]Could not read snapshot files: {e}[/red]")
        return

    console.print(f"\n[bold cyan]Token Sentinel — Before / After[/bold cyan]")
    console.print(f"  Baseline : [dim]{b.get('captured_at', baseline)}[/dim]")
    console.print(f"  After    : [dim]{a.get('captured_at', after)}[/dim]")
    console.print()

    # Overall metrics
    b_cost = b.get("total_cost_usd", 0)
    a_cost = a.get("total_cost_usd", 0)
    b_calls = b.get("total_llm_calls", 0)
    a_calls = a.get("total_llm_calls", 0)

    table = Table(box=rbox.SIMPLE_HEAVY, show_header=True)
    table.add_column("Metric", style="cyan")
    table.add_column("Before", justify="right")
    table.add_column("After", justify="right")
    table.add_column("Change", justify="right")

    def _row(label, bv, av, fmt_fn, lower_is_better=True):
        change = ((av - bv) / bv * 100) if bv else 0
        if abs(change) < 1:
            colour, arrow = "dim", "≈"
        elif (change < 0) == lower_is_better:
            colour, arrow = "green", "↓" if change < 0 else "↑"
        else:
            colour, arrow = "red", "↓" if change < 0 else "↑"
        change_str = f"[{colour}]{arrow} {abs(change):.0f}%[/{colour}]"
        table.add_row(label, fmt_fn(bv), fmt_fn(av), change_str)

    _row("Total cost (USD)",      b_cost,  a_cost,  fmt_usd)
    _row("Total LLM calls",       b_calls, a_calls, str, lower_is_better=False)

    # Per-project breakdown
    b_proj = b.get("projects", {})
    a_proj = a.get("projects", {})
    all_slugs = set(b_proj) | set(a_proj)

    if all_slugs:
        table.add_section()
        for slug in sorted(all_slugs):
            bp = b_proj.get(slug, {})
            ap = a_proj.get(slug, {})
            name = slug_to_name(slug) if slug else slug
            _row(f"  {name} — avg cost/sess",
                 bp.get("avg_cost_usd", 0), ap.get("avg_cost_usd", 0), fmt_usd)
            _row(f"  {name} — unused tools",
                 bp.get("never_used_tools", 0), ap.get("never_used_tools", 0), str)

    console.print(table)

    # Verdict
    console.print()
    if a_cost < b_cost * 0.9 and abs(a_calls - b_calls) / max(b_calls, 1) < 0.15:
        console.print("[bold green]VERDICT: Genuine efficiency gain.[/bold green] "
                      "Cost down, call count stable — same quality, lower spend.")
    elif a_cost < b_cost:
        console.print("[yellow]VERDICT: Cost reduced.[/yellow] "
                      "Verify call count is stable before concluding quality is unchanged.")
    elif a_cost > b_cost * 1.1:
        console.print("[red]VERDICT: Cost increased.[/red] "
                      "Check which sessions drove the rise.")
    else:
        console.print("[dim]VERDICT: No significant change.[/dim]")
    console.print()


# ── sentinel kiro ─────────────────────────────────────────────────────────────

@cli.command()
@click.argument("path", default=".", type=click.Path(exists=True))
@click.option("--dry-run", is_flag=True, help="Score specs but do not write annotations.")
def kiro(path, dry_run):
    """Score Kiro specs and annotate tasks.md files with model tier recommendations."""
    from sentinel.kiro import annotate_specs_dir, find_kiro_dirs, score_task
    from rich.table import Table
    from rich import box as rbox

    root = Path(path).resolve()
    specs_dir = root / ".kiro" / "specs"

    if not specs_dir.exists():
        console.print(f"[yellow]No Kiro specs found.[/yellow] Expected: [cyan]{specs_dir}[/cyan]")
        return

    if dry_run:
        # Score without writing — show what would be annotated
        console.print(f"[dim]Dry run — no files will be modified.[/dim]\n")
        table = Table(box=rbox.SIMPLE, show_header=True, pad_edge=False)
        table.add_column("File", style="cyan")
        table.add_column("Task", max_width=45)
        table.add_column("Tier", justify="center")
        table.add_column("Signals")

        import re
        _HEADING_RE = re.compile(r'^(#{2,3})\s+(.+)$', re.MULTILINE)

        for md_file in sorted(specs_dir.rglob("*.md")):
            lines = md_file.read_text(errors="replace").splitlines()
            i = 0
            while i < len(lines):
                m = _HEADING_RE.match(lines[i])
                if m:
                    heading_text = m.group(2)
                    # Gather only this task's body (until next heading)
                    body_lines = []
                    j = i + 1
                    while j < len(lines) and j < i + 30:
                        if _HEADING_RE.match(lines[j]):
                            break
                        body_lines.append(lines[j])
                        j += 1
                    body = " ".join(body_lines)
                    tier, reasons = score_task(heading_text, body)
                    colour = "green" if tier == "haiku" else "yellow"
                    table.add_row(
                        str(md_file.relative_to(root)),
                        heading_text[:45],
                        f"[{colour}]{tier}[/{colour}]",
                        ", ".join(reasons[:2]) or "general task",
                    )
                i += 1
        console.print(table)
        return

    # Live run — annotate files
    results = annotate_specs_dir(specs_dir)

    if not results:
        console.print("[yellow]No task headings found in spec files.[/yellow]")
        return

    console.print(f"\n[bold cyan]Kiro Spec Annotations — {root.name}[/bold cyan]\n")

    for rel_path, tasks in results.items():
        console.print(f"  [cyan]{rel_path}[/cyan]")
        for t in tasks:
            tier = t["tier"]
            colour = "green" if tier == "haiku" else "yellow"
            reasons = ", ".join(t["reasons"][:2]) if t["reasons"] else "general task"
            console.print(
                f"    [{colour}]{'■' if tier == 'sonnet' else '□'}[/{colour}] "
                f"[bold]{t['heading'][:50]}[/bold]"
            )
            console.print(f"      [dim]{tier} — {reasons}[/dim]")
        console.print()

    all_tasks = [t for tasks in results.values() for t in tasks]
    haiku_n = sum(1 for t in all_tasks if t["tier"] == "haiku")
    sonnet_n = len(all_tasks) - haiku_n

    console.print(f"  [bold]{len(all_tasks)} task(s) annotated[/bold] — "
                  f"[green]{haiku_n}× haiku[/green], [yellow]{sonnet_n}× sonnet[/yellow]")
    if haiku_n:
        pct = int(haiku_n / len(all_tasks) * 100)
        console.print(f"  [dim]{pct}% of tasks are Haiku candidates (~4× cheaper than Sonnet).[/dim]")
    console.print()
    console.print(f"  [dim]Annotations written inline. Re-run anytime — idempotent.[/dim]")


if __name__ == "__main__":
    cli()
