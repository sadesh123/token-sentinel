import re
from pathlib import Path

# ── scoring signals ───────────────────────────────────────────────────────────

# High-weight: strong indicator of complexity tier
SONNET_HIGH = [
    "refactor", "migrate", "architect", "redesign", "integrate",
    "implement", "authentication", "authorisation", "authorization",
    "oauth", "jwt", "webhook", "pipeline", "infrastructure",
]
HAIKU_HIGH = [
    "fix", "typo", "rename", "remove", "delete", "format",
    "update text", "update copy", "update label", "update readme",
    "bump version", "correct", "spelling",
]

# Medium-weight: external service / data signals → sonnet
EXTERNAL_SIGNALS = [
    "api", "database", "db", "sql", "redis", "postgres", "mysql",
    "mongodb", "http", "rest", "graphql", "stripe", "sendgrid",
    "s3", "aws", "azure", "gcp", "queue", "pubsub", "kafka",
]

# Medium-weight: multi-component scope → sonnet
MULTI_COMPONENT = [
    "service", "middleware", "handler", "controller", "repository",
    "module", "layer", "interface", "schema", "migration",
]

# Regex to match an existing sentinel annotation line (for stripping)
_ANNOTATION_RE = re.compile(r'^`sentinel:[^`]*`.*$', re.MULTILINE)

# Headings that are task entries (## or ###, not the file title #)
_HEADING_RE = re.compile(r'^(#{2,3})\s+(.+)$', re.MULTILINE)

# File extension references — proxy for "files in scope"
_FILE_REF_RE = re.compile(r'\b\w[\w\-/]*\.\w{2,5}\b')


# ── scorer ────────────────────────────────────────────────────────────────────

def score_task(heading: str, body: str) -> tuple[str, list[str]]:
    """
    Score a task block and return (tier, reasons).
    tier : "haiku" | "sonnet"
    reasons : short list of signals that drove the decision
    """
    text = (heading + " " + body).lower()
    words = text.split()
    word_count = len(words)
    files_referenced = len(_FILE_REF_RE.findall(heading + " " + body))

    score = 0
    reasons: list[str] = []

    # ── word count ────────────────────────────────────────────────────────────
    if word_count > 250:
        score += 2
        reasons.append(f"{word_count} words")
    elif word_count < 60:
        score -= 1

    # ── high-weight sonnet keywords ───────────────────────────────────────────
    sonnet_hits = [k for k in SONNET_HIGH if k in text]
    if sonnet_hits:
        score += len(sonnet_hits) * 3
        reasons.append(f"{'/ '.join(sonnet_hits[:2])}")

    # ── high-weight haiku keywords ────────────────────────────────────────────
    haiku_hits = [k for k in HAIKU_HIGH if k in text]
    if haiku_hits:
        score -= len(haiku_hits) * 3
        reasons.append(f"{'/ '.join(haiku_hits[:2])}")

    # ── external services ─────────────────────────────────────────────────────
    ext_hits = [k for k in EXTERNAL_SIGNALS if k in text]
    if ext_hits:
        score += 3
        reasons.append(f"external: {ext_hits[0]}")

    # ── multi-component scope ─────────────────────────────────────────────────
    comp_hits = [k for k in MULTI_COMPONENT if k in text]
    if len(comp_hits) >= 2:
        score += 2
        reasons.append("multi-component scope")

    # ── files in scope ────────────────────────────────────────────────────────
    if files_referenced > 8:
        score += 2
        reasons.append(f"{files_referenced} files")
    elif files_referenced == 1:
        score -= 1
        reasons.append("single file")

    tier = "sonnet" if score > 0 else "haiku"
    # Keep reasons concise — max 3
    return tier, reasons[:3]


# ── annotator ─────────────────────────────────────────────────────────────────

def annotate_file(file_path: Path) -> list[dict]:
    """
    Score every ## / ### heading in a spec file and insert an annotation
    on the line immediately after. Idempotent — strips old annotations first.

    Returns list of {heading, tier, reasons} for each annotated task.
    """
    try:
        raw = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    # Strip existing sentinel annotation lines
    cleaned = _ANNOTATION_RE.sub("", raw)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    lines = cleaned.splitlines()
    output: list[str] = []
    annotated: list[dict] = []

    i = 0
    while i < len(lines):
        line = lines[i]
        m = _HEADING_RE.match(line)
        if m:
            heading_text = m.group(2)
            # Gather body: next lines until next heading or EOF (max 30 lines)
            body_lines = []
            j = i + 1
            while j < len(lines) and j < i + 30:
                if _HEADING_RE.match(lines[j]):
                    break
                body_lines.append(lines[j])
                j += 1
            body = " ".join(body_lines)

            tier, reasons = score_task(heading_text, body)
            reason_str = ", ".join(reasons) if reasons else "general task"
            annotation = f"`sentinel: {tier}` — {reason_str}"

            output.append(line)
            output.append(annotation)
            annotated.append({"heading": heading_text, "tier": tier, "reasons": reasons})
        else:
            output.append(line)
        i += 1

    file_path.write_text("\n".join(output) + "\n", encoding="utf-8")
    return annotated


def annotate_specs_dir(specs_dir: Path) -> dict[str, list[dict]]:
    """
    Annotate all .md files found under a .kiro/specs/ directory.
    Returns {relative_path: [annotated_tasks]}.
    """
    results: dict[str, list[dict]] = {}
    if not specs_dir.exists():
        return results

    for md_file in sorted(specs_dir.rglob("*.md")):
        tasks = annotate_file(md_file)
        if tasks:
            rel = str(md_file.relative_to(specs_dir.parent.parent))
            results[rel] = tasks

    return results


def find_kiro_dirs(project_roots: list[Path]) -> list[Path]:
    """Return .kiro/specs/ directories that exist under the given roots."""
    found = []
    for root in project_roots:
        specs = root / ".kiro" / "specs"
        if specs.exists() and specs.is_dir():
            found.append(specs)
    return found


def summary_toast_body(project_name: str, results: dict) -> str:
    """Build the toast notification body for a Kiro annotation event."""
    all_tasks = [t for tasks in results.values() for t in tasks]
    total = len(all_tasks)
    haiku = sum(1 for t in all_tasks if t["tier"] == "haiku")
    sonnet = total - haiku

    parts = [f"{project_name}: {total} task(s) scored."]
    if haiku:
        parts.append(f"{haiku}× haiku")
    if sonnet:
        parts.append(f"{sonnet}× sonnet")
    if haiku:
        pct = int(haiku / total * 100)
        parts.append(f"~{pct}% could run cheaper.")
    parts.append("Tasks annotated inline.")
    return " ".join(parts)
