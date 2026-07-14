"""Deterministic, spec-compliant MCP reference servers used as conformance targets for
the Phase-53 MCP client (Vol 7 §7, R3). ``reference_stdio`` is stdlib-only so it can be
spawned as a subprocess by ``command``; ``http_app`` reuses its handler over Streamable
HTTP. They stand in for @modelcontextprotocol/server-filesystem in CI, where pulling an
npm package over the network would be non-hermetic."""
