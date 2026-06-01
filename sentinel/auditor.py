from collections import defaultdict
from .parser import iter_all_sessions, slug_to_name
from .et_calc import get_multiplier, get_pricing
from . import config as cfg

TOKENS_PER_TOOL_SCHEMA = 300

# (action, scope, reason)
# action : "exclude" | "keep" | "consider"
# scope  : "per-project" | "global"
TOOL_ADVICE: dict[str, tuple[str, str, str]] = {
    # ── MCP: Google Workspace ─────────────────────────────────────────────────
    "mcp__claude_ai_Gmail__authenticate": (
        "exclude", "per-project",
        "Loads OAuth flow for Gmail. Dead weight in every non-email project.",
    ),
    "mcp__claude_ai_Gmail__complete_authentication": (
        "exclude", "per-project",
        "Loads OAuth flow for Gmail. Dead weight in every non-email project.",
    ),
    "mcp__claude_ai_Google_Calendar__authenticate": (
        "exclude", "per-project",
        "Calendar OAuth. Only useful in projects that read or write schedules.",
    ),
    "mcp__claude_ai_Google_Calendar__complete_authentication": (
        "exclude", "per-project",
        "Calendar OAuth. Only useful in projects that read or write schedules.",
    ),
    "mcp__claude_ai_Google_Drive__authenticate": (
        "exclude", "per-project",
        "Drive OAuth. Only useful in projects that read or write Drive files.",
    ),
    "mcp__claude_ai_Google_Drive__complete_authentication": (
        "exclude", "per-project",
        "Drive OAuth. Only useful in projects that read or write Drive files.",
    ),

    # ── MCP: Cloud / FinOps ───────────────────────────────────────────────────
    "mcp__claude_ai_cloud_cost_optimiser__cost-analyze-csv-url": (
        "exclude", "per-project",
        "Cloud cost analysis tool. Irrelevant outside FinOps / infra projects.",
    ),
    "mcp__claude_ai_cloud_cost_optimiser__cost-hello": (
        "exclude", "per-project",
        "Cloud cost analysis tool. Irrelevant outside FinOps / infra projects.",
    ),

    # ── MCP: Security / Secrets ───────────────────────────────────────────────
    "mcp__claude_ai_cyberarkgw__authenticate": (
        "exclude", "per-project",
        "CyberArk secrets vault. Only useful when retrieving managed credentials.",
    ),
    "mcp__claude_ai_cyberarkgw__complete_authentication": (
        "exclude", "per-project",
        "CyberArk secrets vault. Only useful when retrieving managed credentials.",
    ),

    # ── Built-in: Jupyter ─────────────────────────────────────────────────────
    "NotebookEdit": (
        "exclude", "per-project",
        "Jupyter notebook editor. Exclude for any non-data-science project.",
    ),

    # ── Built-in: Scheduling ──────────────────────────────────────────────────
    "CronCreate": (
        "exclude", "per-project",
        "Cron job scheduler. Only needed if you use /schedule or /loop in this project.",
    ),
    "CronDelete": (
        "exclude", "per-project",
        "Cron job scheduler. Only needed if you use /schedule or /loop in this project.",
    ),
    "CronList": (
        "exclude", "per-project",
        "Cron job scheduler. Only needed if you use /schedule or /loop in this project.",
    ),

    # ── Built-in: Background monitoring ──────────────────────────────────────
    "Monitor": (
        "exclude", "per-project",
        "Background process monitor. Exclude unless sessions spawn long-running processes.",
    ),

    # ── Built-in: Git worktrees ───────────────────────────────────────────────
    "EnterWorktree": (
        "exclude", "per-project",
        "Isolated worktree workflow. Exclude unless you explicitly use worktree-mode agents.",
    ),
    "ExitWorktree": (
        "exclude", "per-project",
        "Isolated worktree workflow. Exclude unless you explicitly use worktree-mode agents.",
    ),

    # ── Built-in: Remote / scheduled agents ──────────────────────────────────
    "RemoteTrigger": (
        "exclude", "per-project",
        "Fires a remote scheduled agent. Exclude unless you use /schedule with remote execution.",
    ),

    # ── Built-in: Push notifications ─────────────────────────────────────────
    "PushNotification": (
        "exclude", "per-project",
        "Sends push notifications. Exclude unless this project uses notification-driven workflows.",
    ),

    # ── Built-in: MCP resource browser ───────────────────────────────────────
    "ListMcpResourcesTool": (
        "exclude", "per-project",
        "Browses MCP server resource trees. Exclude unless you explicitly navigate MCP resources.",
    ),
    "ReadMcpResourceTool": (
        "exclude", "per-project",
        "Reads MCP server resources by URI. Exclude unless you explicitly read MCP resources.",
    ),

    # ── Built-in: Web access ──────────────────────────────────────────────────
    "WebFetch": (
        "consider", "per-project",
        "Fetches external URLs. Keep for projects that reference docs or APIs. "
        "Exclude only if this project is fully self-contained.",
    ),
    "WebSearch": (
        "consider", "per-project",
        "Web search. Keep for research-heavy projects. "
        "Exclude only if this project never needs external information.",
    ),

    # ── Built-in: Plan mode ───────────────────────────────────────────────────
    # Claude uses these internally — removing them degrades multi-step reasoning.
    "EnterPlanMode": (
        "keep", "global",
        "Claude uses this internally when breaking down complex tasks. "
        "Removing it will degrade planning quality even if you never invoke /plan.",
    ),
    "ExitPlanMode": (
        "keep", "global",
        "Pair of EnterPlanMode. Must be kept together or Claude can get stuck mid-plan.",
    ),

    # ── Built-in: Task tracking ───────────────────────────────────────────────
    # Claude uses these internally for multi-step progress tracking.
    "TaskCreate": (
        "keep", "global",
        "Claude uses this internally to break long tasks into trackable steps. "
        "Pruning it silently degrades multi-step work quality.",
    ),
    "TaskGet": (
        "keep", "global",
        "Internal task tracking. Keep — Claude reads task state to stay coherent across turns.",
    ),
    "TaskList": (
        "keep", "global",
        "Internal task tracking. Keep — Claude reads task state to stay coherent across turns.",
    ),
    "TaskUpdate": (
        "keep", "global",
        "Internal task tracking. Keep — Claude writes task state to stay coherent across turns.",
    ),
    "TaskOutput": (
        "keep", "global",
        "Internal task tracking. Keep — Claude streams task output for long operations.",
    ),
    "TaskStop": (
        "keep", "global",
        "Internal task tracking. Keep — Claude uses this to cancel runaway task chains.",
    ),
}

ACTION_PRIORITY = {"exclude": 0, "consider": 1, "keep": 2}


def run_audit(filter_slug: str | None = None) -> dict:
    projects: dict[str, dict] = defaultdict(lambda: {
        "name": "",
        "session_ids": [],
        "registered": set(),
        "used": set(),
        "model": "claude-sonnet-4-6",
        "total_llm_calls": 0,
    })

    for rec in iter_all_sessions():
        if filter_slug and rec.project_slug != filter_slug:
            continue
        p = projects[rec.project_slug]
        p["name"] = slug_to_name(rec.project_slug)
        p["session_ids"].append(rec.session_id)
        p["registered"].update(rec.registered_tools)
        p["used"].update(tc.tool_name for tc in rec.tool_calls)
        if rec.model:
            p["model"] = rec.model
        p["total_llm_calls"] += rec.llm_call_count

    results = {}
    for slug, p in projects.items():
        n_sessions = len(p["session_ids"])
        if n_sessions == 0:
            continue

        registered = sorted(p["registered"])
        used_all = p["used"]
        exclusions = cfg.get_exclusions(slug)

        never_used_all = [t for t in registered if t not in used_all]
        never_used = [t for t in never_used_all if t not in exclusions]
        excluded_tools = [t for t in never_used_all if t in exclusions]
        used_registered = sorted(t for t in registered if t in used_all)

        avg_llm_calls = p["total_llm_calls"] / n_sessions
        multiplier = get_multiplier(p["model"])
        pricing = get_pricing(p["model"])
        # Unused tools inflate input context — cost = unused × avg_calls × ~300 tokens × input price
        wasted_usd_per_session = (
            len(never_used) * avg_llm_calls * TOKENS_PER_TOOL_SCHEMA
            * pricing["input"] / 1_000_000
        )
        wasted_usd_total = wasted_usd_per_session * n_sessions

        # Annotate never-used tools with advice, sorted by action priority
        annotated = []
        for tool in never_used:
            action, scope, reason = TOOL_ADVICE.get(
                tool,
                ("consider", "per-project", "No specific guidance. Review whether this project ever needs it.")
            )
            # Group by MCP prefix for unknown mcp__ tools
            if tool.startswith("mcp__") and tool not in TOOL_ADVICE:
                action, scope, reason = (
                    "exclude", "per-project",
                    "Unknown MCP tool with no recorded usage. Safe to exclude per-project."
                )
            annotated.append({
                "tool": tool,
                "action": action,
                "scope": scope,
                "reason": reason,
            })

        annotated.sort(key=lambda x: (ACTION_PRIORITY[x["action"]], x["tool"]))

        results[slug] = {
            "name": p["name"],
            "sessions": n_sessions,
            "avg_llm_calls": avg_llm_calls,
            "model": p["model"],
            "multiplier": multiplier,
            "registered": registered,
            "used_registered": used_registered,
            "never_used_annotated": annotated,
            "excluded_tools": excluded_tools,
            "wasted_usd_per_session": wasted_usd_per_session,
            "wasted_usd_total": wasted_usd_total,
        }

    return results
