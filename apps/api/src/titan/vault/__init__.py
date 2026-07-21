"""The secrets vault (Vol 8 §6, ADR-SEC-03; Phase 68).

A leaf infrastructure module — like :mod:`titan.events` and
:mod:`titan.settings`, it imports NO domain module (enforced by the
import-linter "shared libraries never import domain modules" contract). That is
deliberate: the event-bus envelope signer (:mod:`titan.vault.signing`) and the
field cipher (:mod:`titan.vault.cipher`) are reused by ``titan.events`` and
``titan.identity`` respectively, and a leaf module is the only thing both can
depend on without breaking the domain-independence contracts.

Nothing heavy is re-exported from this package root on purpose: importing
``titan.vault.signing`` must not transitively pull in the router (which reaches
the gateway), so callers import the specific submodule they need.
"""
