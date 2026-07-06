"""Support for the ``web.*`` research tools (Phase 23).

The tools run in the trusted Tool Host and NEVER open direct connections — they
call the caching fetch-gateway (``titan.tools.egress``) via a per-call
:class:`ResearchSession`. ``extract`` turns fetched HTML into readability-extracted
markdown; ``search`` normalizes the gateway's search results."""
