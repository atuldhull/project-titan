"""TITAN backend package — modular monolith (Vol 3 §2.1, TITAN-BCK-001).

One FastAPI application (``titan.main``) plus a worker entrypoint
(``titan.worker``) sharing this codebase. Domain modules live in their own
packages and communicate only through each module's public ``service.py``
interface (TITAN-BCK-004).
"""
