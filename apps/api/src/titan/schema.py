"""Aggregate import of every module's models so Base.metadata is complete.

Import this module wherever the full schema must be registered (tests, seed,
future tooling). Migrations stay hand-written (Vol 3 §6) — this is NOT an
autogenerate hook.
"""

from titan.agents import models as agents_models
from titan.db import Base
from titan.events import models as events_models
from titan.identity import models as identity_models
from titan.messaging import models as messaging_models
from titan.orchestrator import models as orchestrator_models
from titan.projects import models as projects_models
from titan.runs import models as runs_models

__all__ = [
    "Base",
    "agents_models",
    "events_models",
    "identity_models",
    "messaging_models",
    "orchestrator_models",
    "projects_models",
    "runs_models",
]
