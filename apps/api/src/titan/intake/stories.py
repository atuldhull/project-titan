"""Parse an LLM completion into ProductBacklog user stories (Vol 6 H3).

The CEO handler asks the model for a JSON array of user stories, but small local
models are unreliable formatters — they wrap JSON in prose, fence it, or drop out
of JSON entirely. This parser is deliberately tolerant: it extracts the first
JSON array it can find and validates each element, and if there is no usable JSON
it falls back to scraping ``As a … , I want … , so that …`` lines, so a wonky
completion still yields *some* backlog rather than none. Empty / malformed items
are dropped; the result is capped so a runaway completion can't flood the board.
"""

import json
import re
from dataclasses import dataclass
from typing import Any

MAX_STORIES = 12
# Task.title is capped at 500 chars (orchestrator TaskCreate); clamp here so one
# verbose/hostile story can never raise a validation error at persist time.
MAX_TITLE = 500
_STORY_LINE = re.compile(r"(As an? .+?, I want .+?(?:, so that .+?)?)(?:$|[\n\r])", re.IGNORECASE)


@dataclass(frozen=True)
class Story:
    """One user story destined for a Phase-11 task (title + acceptance spec)."""

    title: str
    acceptance: tuple[str, ...] = ()

    def spec_text(self) -> str:
        if not self.acceptance:
            return ""
        return "Acceptance criteria:\n" + "\n".join(f"- {a}" for a in self.acceptance)


def parse_stories(text: str) -> list[Story]:
    """Best-effort extraction of user stories from a completion."""
    stories = _from_json(text)
    if not stories:
        stories = _from_lines(text)
    return stories[:MAX_STORIES]


def _from_json(text: str) -> list[Story]:
    raw = _extract_json_array(text)
    if raw is None:
        return []
    try:
        items = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    stories: list[Story] = []
    for item in items:
        story = _story_from_item(item)
        if story is not None:
            stories.append(story)
    return stories


def _story_from_item(item: Any) -> Story | None:
    if not isinstance(item, dict):
        return None
    title = str(item.get("title") or item.get("story") or "").strip()[:MAX_TITLE]
    if not title:
        return None
    raw_ac = item.get("acceptance_criteria") or item.get("acceptance") or []
    if isinstance(raw_ac, str):
        criteria = [raw_ac.strip()] if raw_ac.strip() else []
    elif isinstance(raw_ac, list):
        criteria = [str(c).strip() for c in raw_ac if str(c).strip()]
    else:
        criteria = []
    return Story(title=title, acceptance=tuple(criteria))


def _extract_json_array(text: str) -> str | None:
    """The first balanced ``[ … ]`` in the text (tolerating prose/fences around it)."""
    start = text.find("[")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _from_lines(text: str) -> list[Story]:
    seen: set[str] = set()
    stories: list[Story] = []
    for match in _STORY_LINE.finditer(text):
        title = match.group(1).strip().rstrip(".")[:MAX_TITLE]
        if title and title.lower() not in seen:
            seen.add(title.lower())
            stories.append(Story(title=title))
    return stories


__all__ = ["MAX_STORIES", "Story", "parse_stories"]
