"""The reference TITAN plugin (Vol 7 §3.1: first-party plugins use the SAME API as
third-party ones — no private hooks).

Runs out-of-process in its own venv, spoken to over JSON-RPC/stdio by the host. It is
stdlib-only. Everything privileged goes through the ``capabilities`` handle the host
injects — core brokers each request against the operator-granted manifest and denies
anything undeclared (deny-by-default, TITAN-INT-015).
"""

from typing import Any


class SamplePlugin:
    id = "titan.sample"

    def __init__(self, capabilities: Any = None) -> None:
        self.caps = capabilities

    # --- lifecycle -----------------------------------------------------------

    def health(self) -> bool:
        return True

    def tools(self) -> list[dict[str, Any]]:
        return [{"name": "sample.echo"}, {"name": "sample.probe"}]

    # --- tools ---------------------------------------------------------------

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "sample.echo":
            return {"echo": arguments}
        if name == "sample.probe":
            return self._probe(str(arguments.get("capability", "")), arguments.get("argument"))
        raise ValueError(f"unknown tool '{name}'")

    def _probe(self, capability: str, argument: Any) -> dict[str, Any]:
        """Attempt one privileged capability. The conformance suite calls this once per
        permission class with an UNGRANTED argument and expects PERMISSION_DENIED every
        time — the plugin cannot enforce its own limits, so it simply asks and core says
        no."""
        if capability == "secrets":
            return {"value": self.caps.secret(str(argument))}
        if capability == "network":
            return {"response": self.caps.fetch(str(argument))}
        if capability == "events":
            self.caps.publish(str(argument), {"hello": "world"})
            return {"published": True}
        if capability == "filesystem":
            return {"content": self.caps.read_file(str(argument))}
        if capability == "scopes":
            return {"scope": self.caps._call("titan/scope.check", {"scope": str(argument)})}
        raise ValueError(f"unknown capability '{capability}'")

    # --- webhook -------------------------------------------------------------

    def on_webhook(self, path: str, body: Any) -> dict[str, Any]:
        return {"path": path, "received": body}
