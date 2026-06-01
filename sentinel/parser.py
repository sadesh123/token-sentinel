import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Iterator
from .et_calc import calculate_et, calculate_cache_write_et, calculate_cost_usd

PROJECTS_DIR = Path.home() / ".claude" / "projects"


@dataclass
class ToolCall:
    tool_name: str
    called_at: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    et: float = 0.0


@dataclass
class SessionRecord:
    session_id: str
    project_slug: str
    cwd: str = ""
    started_at: str = ""
    ended_at: str = ""
    model: str = ""
    turn_count: int = 0
    total_et: float = 0.0
    cache_write_et: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read: int = 0
    total_cache_write: int = 0
    llm_call_count: int = 0       # unique LLM API calls (deduplicated message IDs)
    total_cost_usd: float = 0.0
    registered_tools: list = field(default_factory=list)
    tool_calls: list = field(default_factory=list)
    ai_title: str = ""


def slug_to_name(slug: str) -> str:
    """Convert '-mnt-c-Users-Admin-Desktop-VibeCodingWorkshop' → 'VibeCodingWorkshop'."""
    parts = slug.lstrip("-").split("-")
    # Reconstruct path segments — heuristic: last non-empty component after 'Desktop' or 'home'
    for marker in ("Desktop", "home", "projects"):
        if marker in parts:
            idx = parts.index(marker)
            remainder = parts[idx + 1:]
            if remainder:
                return "-".join(remainder)
    return parts[-1] if parts else slug


def parse_jsonl(file_path: Path) -> SessionRecord | None:
    """
    Parse a single .jsonl transcript file.
    Deduplicates assistant messages by message.id — only the last entry
    per message.id is counted (streaming chunks share the same id and the
    final chunk carries the complete token usage).
    """
    project_slug = file_path.parent.name
    session_id = file_path.stem

    session = SessionRecord(session_id=session_id, project_slug=project_slug)

    # message.id → last seen usage block (dedup streaming chunks)
    usage_by_msg_id: dict[str, tuple[dict, str, str]] = {}  # id → (usage, model, timestamp)
    # tool_use_id → tool_name (to correlate tool calls with their tokens)
    tool_calls_raw: list[tuple[str, str]] = []  # (tool_name, timestamp)
    seen_msg_ids_for_tools: set[str] = set()

    try:
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        obj_type = obj.get("type", "")
        ts = obj.get("timestamp", "")

        # Session cwd from first attachment line
        if obj_type == "attachment":
            if not session.cwd:
                session.cwd = obj.get("cwd", "")
            att = obj.get("attachment", {})
            if att.get("type") == "deferred_tools_delta":
                names = att.get("addedNames", [])
                session.registered_tools.extend(names)

        elif obj_type == "ai-title":
            session.ai_title = obj.get("aiTitle", "")

        elif obj_type == "permission-mode":
            if not session.started_at:
                session.started_at = ts

        elif obj_type == "assistant":
            msg = obj.get("message", {})
            msg_id = msg.get("id", "")
            model = msg.get("model", "")
            usage = msg.get("usage")
            content = msg.get("content", [])

            if model and not session.model:
                session.model = model

            if ts:
                session.ended_at = ts
                if not session.started_at:
                    session.started_at = ts

            # Collect tool calls (only once per message id to avoid dup)
            if msg_id not in seen_msg_ids_for_tools:
                for item in content:
                    if item.get("type") == "tool_use":
                        tool_calls_raw.append((item["name"], ts))
                seen_msg_ids_for_tools.add(msg_id)

            # Always overwrite — last streaming chunk wins
            if usage and msg_id:
                usage_by_msg_id[msg_id] = (usage, model or session.model, ts)
            elif usage and not msg_id:
                # No message id — count directly (shouldn't happen but be safe)
                et = calculate_et(usage, model or session.model)
                session.total_et += et
                session.total_input_tokens += usage.get("input_tokens", 0)
                session.total_output_tokens += usage.get("output_tokens", 0)
                session.total_cache_read += usage.get("cache_read_input_tokens", 0)
                session.total_cache_write += usage.get("cache_creation_input_tokens", 0)

        elif obj_type == "system":
            if obj.get("subtype") == "turn_duration":
                session.turn_count += 1

    # Each unique message ID = one LLM API call
    session.llm_call_count = len(usage_by_msg_id)

    # Aggregate deduplicated usage
    for msg_id, (usage, model, ts) in usage_by_msg_id.items():
        et = calculate_et(usage, model)
        cw_et = calculate_cache_write_et(usage, model)
        session.total_et += et
        session.cache_write_et += cw_et
        session.total_cost_usd += calculate_cost_usd(usage, model)
        session.total_input_tokens += usage.get("input_tokens", 0)
        session.total_output_tokens += usage.get("output_tokens", 0)
        session.total_cache_read += usage.get("cache_read_input_tokens", 0)
        session.total_cache_write += usage.get("cache_creation_input_tokens", 0)

    # Attach raw tool calls
    for tool_name, ts in tool_calls_raw:
        session.tool_calls.append(ToolCall(tool_name=tool_name, called_at=ts))

    # Deduplicate registered tools (addedNames can appear in multiple attachment lines)
    session.registered_tools = list(dict.fromkeys(session.registered_tools))

    return session


def iter_all_sessions() -> Iterator[SessionRecord]:
    """Yield SessionRecord for every .jsonl file across all projects."""
    for jsonl_file in sorted(PROJECTS_DIR.glob("**/*.jsonl")):
        record = parse_jsonl(jsonl_file)
        if record:
            yield record
