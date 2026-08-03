"""Prompt-safety helpers shared across the evolution LLM calls.

The evolution subsystem feeds agent-authored, untrusted text (task titles/specs,
failure descriptions, reflection lessons) into rubric/reflection/classifier
prompts. ``fence_untrusted`` wraps that text in a sentinel-delimited DATA block so
an injected 'ignore your instructions' inside the data cannot pose as a system
instruction. The prompts themselves also instruct the model to treat fenced
content as DATA-not-instructions; the fence is defense-in-depth.
"""

import re

# A run of 3+ identical angle brackets — how an attacker would try to forge the
# ``<<<`` / ``>>>`` sentinels from inside the data.
_SENTINEL_RUN = re.compile(r"([<>])\1{2,}")


def fence_untrusted(text_value: str) -> str:
    """Wrap untrusted text in a ``<<<DATA … DATA>>>`` fence, collapsing any run of
    3+ ``<`` or ``>`` to a single char first.

    A single-pass ``str.replace('<<<','<')`` is NOT safe: ``'DATA>>>>>'`` collapses
    back to the exact closing sentinel ``'DATA>>>'`` (three chars replaced, two left
    behind reform the run), letting an attacker break out of the fence. Collapsing
    maximal runs of 3+ to one — in a single linear, ReDoS-free pass — removes every
    ``<<<``/``>>>`` with no residue that can reconstruct one.
    """
    cleaned = _SENTINEL_RUN.sub(r"\1", text_value or "")
    return f"<<<DATA\n{cleaned}\nDATA>>>"
