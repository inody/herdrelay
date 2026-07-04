from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .models import Binding, HerdrTarget


STATUS_EMOJI: dict[str, str] = {
    "blocked": "🔴",
    "working": "🟡",
    "idle": "🟢",
    "done": "🔵",
    "unknown": "⚪",
}


def status_emoji(status: str | None) -> str:
    return STATUS_EMOJI.get((status or "unknown").casefold(), "⚪")


def target_alias(target: HerdrTarget) -> str:
    """Human-readable project/session label, hiding raw Herdr IDs."""
    label = target.workspace_label
    if not label or label in ("~", "-"):
        label = _cwd_basename(target.cwd)
    if not label or label in ("~", "-"):
        label = target.target
    return label


def format_target_card(
    target: HerdrTarget, *, tail_preview: str = "", max_chars: int = 1800
) -> str:
    alias = target_alias(target)
    agent = target.agent_name or target.kind or "unknown"
    lines = [
        f"### {status_emoji(target.status)} {alias}/{agent}",
        f"target: `{target.target}`",
    ]
    if target.cwd:
        lines.append(f"cwd: `{target.cwd}`")
    lines.append(f"status: `{target.status or 'unknown'}`")
    header = "\n".join(lines)
    if not tail_preview:
        return header
    body = format_tail(tail_preview, max_chars=max(200, max_chars - len(header) - 2))
    return header + "\n" + body


def _cwd_basename(cwd: str | None) -> str | None:
    if not cwd:
        return None
    name = Path(cwd).name
    return name or None


def format_status(targets: Iterable[HerdrTarget], *, max_chars: int = 1800) -> str:
    lines = []
    for target in targets:
        status = target.status or "unknown"
        agent = target.agent_name or target.kind
        label = target.label or target.workspace_label or "-"
        cwd = _shorten_cwd(target.cwd)
        lines.append(f"{status:<8} {label:<18} {agent:<10} {target.target:<8} {cwd}")
    if not lines:
        return "No Herdr agents or panes found."
    return code_block("\n".join(lines), max_chars=max_chars)


def format_tail(output: str, *, max_chars: int = 1800) -> str:
    clean = output.strip("\n")
    if not clean:
        clean = "(no output)"
    clean = clean.replace("```", "`\u200b``")
    return code_block(clean, max_chars=max_chars, keep="end")


def format_bindings(bindings: Iterable[Binding], *, max_chars: int = 1800) -> str:
    lines = []
    for binding in bindings:
        place = binding.thread_id or binding.channel_id
        label = f" ({binding.label})" if binding.label else ""
        lines.append(f"{place} -> {binding.herdr_target}{label}")
    if not lines:
        return "No bindings."
    return code_block("\n".join(lines), max_chars=max_chars)


def wrap_code_block(text: str) -> str:
    """Wrap text in a code block without truncation (use after splitting)."""
    clean = text.strip("\n")
    if not clean:
        clean = "(no output)"
    clean = clean.replace("```", "`\u200b``")
    return f"```text\n{clean}\n```"


def split_tail_chunks(text: str, *, max_chars: int = 1900) -> list[str]:
    """Split text into chunks that each fit in a Discord code block message."""
    budget = max_chars - len("```text\n") - len("\n```")
    if budget < 50:
        budget = 50
    lines = text.split("\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1
        if current and current_len + line_len > budget:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


def truncate(text: str, *, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    suffix = "\n... truncated"
    return text[: max_chars - len(suffix)] + suffix


def code_block(text: str, *, max_chars: int, keep: str = "start") -> str:
    prefix = "```text\n"
    suffix = "\n```"
    budget = max_chars - len(prefix) - len(suffix)
    if budget < 20:
        return truncate(prefix + text + suffix, max_chars=max_chars)
    content = text
    marker = "... truncated\n" if keep == "end" else "\n... truncated"
    if len(content) > budget:
        if keep == "end":
            content = marker + content[-(budget - len(marker)) :]
        else:
            content = content[: budget - len(marker)] + marker
    return prefix + content + suffix


def _shorten_cwd(cwd: str | None) -> str:
    if not cwd:
        return "-"
    parts = cwd.rstrip("/").split("/")
    if len(parts) >= 3 and parts[0] == "":
        return "~/" + "/".join(parts[-2:])
    return cwd
