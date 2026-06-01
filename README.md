# Token Sentinel

> FinOps observability for agentic coding tools. Tracks real dollar costs across Claude Code and Kiro sessions, surfaces waste from bloated tool contexts, alerts you before spend gets out of hand, and guides model selection inside your IDE workflow.

---

## The Problem

Agentic coding tools like Claude Code and Kiro make dozens of LLM API calls per session. There's no built-in cost visibility, no budget alerts, and no feedback loop telling you when your setup is wasteful. Most devs have no idea what a session actually costs until they check their bill at the end of the month.

Token Sentinel fixes that. It runs as a background daemon, reads the session transcripts your tools already write to disk, and gives you the observability layer.

---

## What It Does

**Real-time cost tracking**: Claude Code and Kiro session is parsed the moment it completes. Costs are computed from the actual Anthropic API pricing table, not estimates. You see spend per session, per project, and as a daily total with a 7-day rolling average.

**MCP tool audit**: agentic tools load every configured MCP tool schema into the context window of every API call, whether the tool gets used or not. Each unused tool schema costs ~300 tokens per call. On a 60-call session with 30 unused tools, that's 54,000 wasted tokens. Token Sentinel identifies exactly which tools are dead weight and gives you copy-paste commands to prune them; with clear advice on what's safe to remove and what Claude uses internally for reasoning.

**Budget alerts**: Windows toast notifications fire when a session costs 2× your project average, when a new session starts with excessive unused tools loaded, when your daily budget hits 80% and 100%, and when your cache hit ratio collapses (a signal that something changed in your context structure).

**Ambient cost display**: shell prompt shows today's spend, session count, and budget percentage every time you open a terminal or launch `claude`. No dashboard to remember to check.

**Kiro spec annotator**: between spec generation and task execution, Kiro gives you a window to make model tier decisions. Token Sentinel scores each task in `.kiro/specs/tasks.md` for complexity and writes an inline recommendation (`haiku` or `sonnet`) directly under the task heading. Haiku tasks are ~4× cheaper. The recommendations are *advisory*.

**Before/after comparison**: capture a cost snapshot, make a change (prune tools, adjust config), run more sessions, compare. The output tells you whether your change was a genuine efficiency gain or just less work done.

---

## Requirements

- Windows with WSL2 (Ubuntu)
- Python 3.10+
- Claude Code installed in WSL and/or Kiro
- `powershell.exe` accessible from WSL (standard on any WSL2 setup)

---

## Installation

Open a WSL terminal.

```bash
git clone <repo-url> ~/token-sentinel
cd ~/token-sentinel
pip install -r requirements.txt --break-system-packages
```

Start the daemon:

```bash
python3 sentinel.py daemon start
```

The daemon ingests all existing sessions immediately, then watches for new ones in real time.

Wire the shell prompt by appending to `~/.bashrc`:

```bash
# Token Sentinel — cost in prompt and pre-claude display
__ts_cost() {
  local f="$HOME/.token-sentinel/prompt-status.txt"
  [[ -f "$f" ]] || return
  local age=$(( $(date +%s) - $(date -r "$f" +%s 2>/dev/null || echo 0) ))
  if (( age > 600 )); then
    echo -n "[sentinel: offline]"
  else
    echo -n "[$(cat "$f")]"
  fi
}
PS1='\[\033[0;36m\]$(__ts_cost)\[\033[0m\] '"$PS1"

claude() {
  local f="$HOME/.token-sentinel/prompt-status.txt"
  if [[ -f "$f" ]]; then
    local age=$(( $(date +%s) - $(date -r "$f" +%s 2>/dev/null || echo 0) ))
    (( age <= 600 )) && echo "Token Sentinel: $(cat "$f")"
  fi
  command claude "$@"
}
```

Then reload:

```bash
source ~/.bashrc
```

Configure your daily budget in `~/.token-sentinel/config.json` (created automatically on first run):

```json
{
  "daily_budget_usd": 5.00,
  "exclude_tools": {
    "global": [],
    "per_project": {}
  }
}
```

---

## Commands

### `sentinel stats` — cost by project

```bash
python3 sentinel.py stats                          # all time
python3 sentinel.py stats --today                  # today only
python3 sentinel.py stats --session <id>           # single session detail
python3 sentinel.py stats --export baseline.json   # snapshot for compare
```

### `sentinel audit` — MCP tool waste

```bash
python3 sentinel.py audit --project MyProject   # one project
python3 sentinel.py audit --all                 # all projects
```

Each never-used tool is categorised:

- **EXCLUDE** — safe to prune, copy-paste command included
- **CONSIDER** — project-dependent, review before removing
- **KEEP** — Claude's internal reasoning tools, do not touch

### `sentinel exclude` — suppress audit noise

After reviewing, silence tools you've consciously decided to keep:

```bash
python3 sentinel.py exclude add mcp__claude_ai_Gmail__authenticate --project MyProject
python3 sentinel.py exclude add NotebookEdit          # global
python3 sentinel.py exclude list
python3 sentinel.py exclude remove <tool> --project MyProject
```

### `sentinel report` — full daily summary

```bash
python3 sentinel.py report             # terminal output
python3 sentinel.py report --today     # today's sessions only
python3 sentinel.py report --save      # write to ~/.token-sentinel/reports/
```

Sections: spend summary vs 7-day average, cost by project, MCP waste table, model distribution, top 5 most expensive sessions, cache efficiency grades.

### `sentinel compare` — measure the impact of a change

```bash
# Before
python3 sentinel.py stats --export baseline.json

# Make a change — prune tools, update config, etc.
# Run a few sessions to build new history

# After
python3 sentinel.py stats --export after.json
python3 sentinel.py compare baseline.json after.json
```

```
VERDICT: Genuine efficiency gain. Cost down, call count stable — same quality, lower spend.
```

Cost down + call count stable = real improvement. Cost down + call count down = ambiguous (you may just be doing less work).

### `sentinel kiro` — model tier recommendations for Kiro specs

```bash
python3 sentinel.py kiro /path/to/project --dry-run   # preview scores
python3 sentinel.py kiro /path/to/project             # write inline
```

Run this after Kiro generates specs and before you kick off tasks. Each task heading in `tasks.md` gets an annotation:

```markdown
## Task 1: Fix typo in README
`sentinel: haiku` — fix/typo, single file

## Task 2: Implement OAuth2 authentication
`sentinel: sonnet` — OAuth keyword, external service, multi-component scope
```

The daemon also watches `.kiro/specs/` directories automatically and re-scores when files change, firing a toast with the summary.

### `sentinel daemon` — background watcher

```bash
python3 sentinel.py daemon start
python3 sentinel.py daemon stop
python3 sentinel.py daemon status    # PID + recent log lines
```

---

## Alerts

| Alert | Trigger |
|---|---|
| **Session spike** | Session cost > 2× your project's rolling average |
| **MCP waste** | New session starts with >10 never-used tools loaded |
| **Cache collapse** | Cache hit ratio drops below 10:1 on a project that normally runs hot |
| **Budget 80%** | Today's spend crosses 80% of `daily_budget_usd` |
| **Budget exceeded** | Today's spend crosses 100% of `daily_budget_usd` |
| **Kiro specs scored** | New or modified spec files detected, tasks annotated |

Alerts have cooldowns to prevent flooding — session-level alerts fire once per session, budget alerts fire at most once per hour per threshold.

---

## Pricing Reference

Stored in `sentinel/et_calc.py`. Update when Anthropic changes rates.

| Model | Input | Output | Cache Read | Cache Write |
|---|---|---|---|---|
| claude-haiku | $0.80/M | $4.00/M | $0.08/M | $1.00/M |
| claude-sonnet | $3.00/M | $15.00/M | $0.30/M | $3.75/M |
| claude-opus | $15.00/M | $75.00/M | $1.50/M | $18.75/M |

---

## Runtime Data

All runtime data lives in the WSL home directory regardless of where the source is cloned.

| Path | Contents |
|---|---|
| `~/.token-sentinel/sentinel.db` | SQLite — sessions, tool calls, daily totals, alerts |
| `~/.token-sentinel/config.json` | Budget and exclusion config |
| `~/.token-sentinel/prompt-status.txt` | Live cost line for the shell prompt |
| `~/.token-sentinel/reports/` | Daily markdown reports + `latest.md` symlink |
| `~/.token-sentinel/logs/daemon.log` | Daemon log, 7-day rotation |

---

## Technical Notes

**WSL only.** Toast notifications use `powershell.exe` called from WSL. The file watcher uses `PollingObserver`.

**Cost is computed independently.** The `costUSD` field in Claude Code's `stats-cache.json` is always `0`. Token Sentinel computes it directly from per-call token counts and the pricing table.

**Streaming deduplication.** Claude Code streams LLM responses in chunks. Multiple JSONL entries share the same `message.id` but only the final chunk carries the complete token usage. Token Sentinel deduplicates by message ID and counts only the last entry, preventing 2–4× token inflation.

**Kiro recommendations are advisory.** Token Sentinel scores and annotates but does not interact with Kiro's model selection. Use the scores to decide which tasks to batch on Haiku before running the heavier Sonnet tasks.
