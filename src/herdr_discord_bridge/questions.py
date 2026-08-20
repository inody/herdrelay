from __future__ import annotations

import re
from collections.abc import Iterable

from .models import PendingQuestion


def parse_question_answer(text: str, question: PendingQuestion) -> tuple[int, ...] | None:
    """Parse a 1-based Discord numeric answer for the pending question."""
    parts = [part for part in re.split(r"[\s,]+", text.strip()) if part]
    if not parts or any(not part.isdecimal() for part in parts):
        return None
    selected = tuple(sorted({int(part) - 1 for part in parts}))
    if not selected or any(index < 0 or index >= len(question.options) for index in selected):
        return None
    if not question.multi_select and len(selected) != 1:
        return None
    return selected


def selection_keys(question: PendingQuestion, selected: Iterable[int]) -> tuple[str, ...]:
    """Return TUI keys that select indexes in a fresh Claude question dialog."""
    indexes = tuple(sorted(set(selected)))
    if not indexes or any(index < 0 or index >= len(question.options) for index in indexes):
        raise ValueError("selection is outside the available question options")
    if not question.multi_select and len(indexes) != 1:
        raise ValueError("a single-choice question needs exactly one selection")

    keys: list[str] = ["Home"]
    position = 0
    for index in indexes:
        keys.extend("Down" for _ in range(index - position))
        if question.multi_select:
            keys.append("Space")
        position = index
    keys.append("Enter")
    return tuple(keys)


def selected_labels(question: PendingQuestion, indexes: Iterable[int]) -> str:
    return ", ".join(question.options[index].label for index in indexes)
