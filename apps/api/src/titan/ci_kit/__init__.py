"""The constitution CI enforcement kit (Phase 45; Vol 6 §6.1–6.5).

Volume 6 §6 is the constitution: the rules binding every line of TITAN-generated
code. Phase 43 built the *artifact* (versioned, retrieved into agent context);
this package is its **machine-enforcement layer** — the CI kit TITAN installs
into every generated workspace so the machine-checkable subset fails the build
rather than relying on a reviewer to notice (ADR-WFL-04: standards enforced by
machines first, reviewer second).

There is exactly ONE constitution. This kit does not restate it — every gate here
cites the TITAN-WFL requirement id it enforces (see :mod:`titan.ci_kit.spec`).

**This package is deliberately self-contained**: stdlib only, and it imports
nothing from ``titan`` (machine-enforced by the import-linter contract "The CI
kit is self-contained"). That is load-bearing, not stylistic — the whole
directory is COPY'd into the Alpine sandbox image
(``infra/docker/titan-sandbox.Dockerfile``), where the DevOps agent runs
``titan-ci-init`` through the Phase-21 terminal tool. A single import of
``titan.settings`` would drag FastAPI and SQLAlchemy into the sandbox and break
the install.
"""

from titan.ci_kit.config import KitConfig
from titan.ci_kit.install import install_kit
from titan.ci_kit.render import KitFile, render_kit
from titan.ci_kit.spec import RULES, Rule

__all__ = ["RULES", "KitConfig", "KitFile", "Rule", "install_kit", "render_kit"]
