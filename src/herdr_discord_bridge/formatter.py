from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from .models import Binding, HerdrTarget


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
    return code_block(clean, max_chars=max_chars)


def format_bindings(bindings: Iterable[Binding], *, max_chars: int = 1800) -> str:
    lines = []
    for binding in bindings:
        place = binding.thread_id or binding.channel_id
        label = f" ({binding.label})" if binding.label else ""
        lines.append(f"{place} -> {binding.herdr_target}{label}")
    if not lines:
        return "No bindings."
    return code_block("\n".join(lines), max_chars=max_chars)


def format_dashboard(targets: Iterable[HerdrTarget], *, max_chars: int = 1900) -> str:
    target_list = list(targets)
    counts: dict[str, int] = {}
    for target in target_list:
        status = target.status or "unknown"
        counts[status] = counts.get(status, 0) + 1

    summary = "  ".join(
        f"{status}:{counts.get(status, 0)}"
        for status in ("blocked", "working", "idle", "done", "unknown")
    )
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "Herdr dashboard",
        f"Updated: {timestamp}",
        summary,
        "",
    ]
    if target_list:
        lines.extend(_dashboard_rows(target_list))
    else:
        lines.append("No Herdr agents or panes found.")
    return code_block("\n".join(lines), max_chars=max_chars)


def truncate(text: str, *, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    suffix = "\n... truncated"
    return text[: max_chars - len(suffix)] + suffix


def code_block(text: str, *, max_chars: int) -> str:
    prefix = "```text\n"
    suffix = "\n```"
    budget = max_chars - len(prefix) - len(suffix)
    if budget < 20:
        return truncate(prefix + text + suffix, max_chars=max_chars)
    content = text
    marker = "\n... truncated"
    if len(content) > budget:
        content = content[: budget - len(marker)] + marker
    return prefix + content + suffix


def _shorten_cwd(cwd: str | None) -> str:
    if not cwd:
        return "-"
    parts = cwd.rstrip("/").split("/")
    if len(parts) >= 3 and parts[0] == "":
        return "~/" + "/".join(parts[-2:])
    return cwd


def _dashboard_rows(targets: list[HerdrTarget]) -> list[str]:
    rows = []
    for target in sorted(targets, key=_dashboard_sort_key):
        status = target.status or "unknown"
        agent = target.agent_name or target.kind
        label = target.label or target.workspace_label or "-"
        cwd = _shorten_cwd(target.cwd)
        rows.append(f"{status:<8} {target.target:<8} {agent:<8} {label:<18} {cwd}")
    return rows


def _dashboard_sort_key(target: HerdrTarget) -> tuple[int, str]:
    order = {"blocked": 0, "working": 1, "done": 2, "idle": 3, "unknown": 4}
    status = target.status or "unknown"
    return (order.get(status, 5), target.target)
