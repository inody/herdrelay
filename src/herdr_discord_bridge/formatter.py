from __future__ import annotations

from pathlib import Path

from .models import HerdrTarget


def target_alias(target: HerdrTarget) -> str:
    """Human-readable project/session label, hiding raw Herdr IDs."""
    label = target.workspace_label
    if not label or label in ("~", "-"):
        label = _cwd_basename(target.cwd)
    if not label or label in ("~", "-"):
        label = target.target
    return label


def _cwd_basename(cwd: str | None) -> str | None:
    if not cwd:
        return None
    name = Path(cwd).name
    return name or None


def format_tail(output: str, *, max_chars: int = 1800) -> str:
    clean = output.strip("\n")
    if not clean:
        clean = "(no output)"
    clean = clean.replace("```", "`\u200b``")
    return code_block(clean, max_chars=max_chars, keep="end")


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
        if len(line) > budget:
            if current:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            while len(line) > budget:
                chunks.append(line[:budget])
                line = line[budget:]
        line_len = len(line) + (1 if current else 0)
        if current and current_len + line_len > budget:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
            line_len = len(line)
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
