"""Tolerant JSON scanning shared by the intake parsers.

Small local models are unreliable formatters: they wrap the JSON we asked for in
prose, fence it, or trail commentary after it. Both intake parsers (stories,
clarification questions) therefore start by carving the first *balanced* JSON
span out of the completion rather than handing the whole text to ``json.loads``.
The scanner is string-aware (a bracket inside a JSON string is not a bracket) —
subtle enough to be worth having in exactly one place.
"""


def extract_balanced(text: str, opener: str, closer: str) -> str | None:
    """The first balanced ``opener … closer`` span in ``text`` (tolerating prose or
    code fences around it), or None if there is no balanced span."""
    start = text.find(opener)
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
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


__all__ = ["extract_balanced"]
