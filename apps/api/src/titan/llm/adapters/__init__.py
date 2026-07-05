"""Provider adapters — the ONLY code permitted to speak provider wire formats.

Each adapter translates the canonical schema (``titan.llm.models``) to and from
one provider's HTTP API. Provider quirks (Anthropic's system-prompt hoisting,
tool-results-in-user-turns; OpenAI/Ollama's ``role:'tool'`` messages) stay
inside the adapter, behind the canonical contract (TITAN-INT-061).

Adapters hit provider HTTP endpoints directly via ``httpx`` rather than vendor
SDKs (ADR-INT-03: a thin in-house gateway, zero extra runtime dependencies) —
which also makes wire-fixture testing a plain ``httpx.MockTransport`` away.
"""

from titan.llm.adapters.anthropic import AnthropicAdapter
from titan.llm.adapters.base import ChatAdapter, ChatResult, EmbeddingsAdapter, EmbeddingsResult
from titan.llm.adapters.google import GoogleAdapter
from titan.llm.adapters.ollama import OllamaAdapter
from titan.llm.adapters.openai import OpenAIAdapter

__all__ = [
    "AnthropicAdapter",
    "ChatAdapter",
    "ChatResult",
    "EmbeddingsAdapter",
    "EmbeddingsResult",
    "GoogleAdapter",
    "OllamaAdapter",
    "OpenAIAdapter",
]
