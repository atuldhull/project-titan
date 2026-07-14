"""The chat-notifications connector entrypoint (Vol 7 §6.3, TITAN-INT-042/011).

ONE connector, two backends (Slack + Discord) over ONE shared notification pipeline
(:mod:`chat_notify`) — Discord does not build a second pipeline. It contributes ``slack.*``
+ ``discord.*`` tools and reaches core only through capabilities. Construct it directly with
fakes to unit-test the whole thing in-process.

Approval resolution uses an injected ``resolve`` (default: the core tool-approval REST
endpoint via the connector's own HTTP + a service token) so the connector needs no
privileged approval capability — zero core changes.
"""

import json
from typing import Any

import chat_discord as discord
import chat_slack as slack
from chat_http import HttpTransport, UrllibTransport
from chat_notify import ChannelRouter, DeliveryResult, TemplateRenderer, send_with_retry

_CONFIG_KEYS = (
    "SLACK_BOT_TOKEN",
    "SLACK_SIGNING_SECRET",
    "SLACK_CHANNEL_MAP",
    "SLACK_DEFAULT_CHANNEL",
    "SLACK_TEMPLATES",
    "SLACK_USER_MAP",
    "DISCORD_BOT_TOKEN",
    "DISCORD_PUBLIC_KEY",
    "DISCORD_CHANNEL_MAP",
    "DISCORD_DEFAULT_CHANNEL",
    "TITAN_API_URL",
    "TITAN_SERVICE_TOKEN",
)


def _read_secret(capabilities: Any, name: str) -> str:
    if capabilities is None:
        return ""
    try:
        return str(capabilities.secret(name))
    except Exception:  # noqa: BLE001 — an ungranted/unset optional config secret is fine
        return ""


def _load_json(raw: str) -> dict[str, str]:
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


class ChatConnector:
    id = "titan.chat"

    def __init__(
        self,
        capabilities: Any = None,
        *,
        http: HttpTransport | None = None,
        config: dict[str, str] | None = None,
        resolve: slack.ApprovalResolver | None = None,
        sleep: Any = None,
    ) -> None:
        self._caps = capabilities
        self._cfg = config or {k: _read_secret(capabilities, k) for k in _CONFIG_KEYS}
        self._http = http or UrllibTransport()
        self._sleep = sleep
        self._slack_router = ChannelRouter.from_json(
            self._cfg.get("SLACK_CHANNEL_MAP", ""),
            self._cfg.get("SLACK_DEFAULT_CHANNEL", ""),
        )
        self._discord_router = ChannelRouter.from_json(
            self._cfg.get("DISCORD_CHANNEL_MAP", ""),
            self._cfg.get("DISCORD_DEFAULT_CHANNEL", ""),
        )
        self._templates = TemplateRenderer.from_json(
            self._cfg.get("SLACK_TEMPLATES", "")
        )
        self._resolve = resolve or self._rest_resolver

    def health(self) -> bool:
        return True

    def tools(self) -> list[str]:
        return [
            "slack.post_message",
            "slack.post_approval",
            "discord.post_message",
            "discord.post_embed",
            "chat.notify",
        ]

    # --- tools --------------------------------------------------------------

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        args = dict(arguments or {})
        if name == "slack.post_message":
            channel = self._slack_router.channel_for(
                str(args.get("project_id", "")), str(args.get("channel", ""))
            )
            return self._retry(
                lambda: slack.post_message(
                    self._http,
                    self._cfg.get("SLACK_BOT_TOKEN", ""),
                    channel=channel,
                    text=str(args["text"]),
                )["ok"]
            )
        if name == "slack.post_approval":
            channel = self._slack_router.channel_for(
                str(args.get("project_id", "")), str(args.get("channel", ""))
            )
            return self._retry(
                lambda: slack.post_approval(
                    self._http,
                    self._cfg.get("SLACK_BOT_TOKEN", ""),
                    channel=channel,
                    text=str(args["text"]),
                    project_id=str(args["project_id"]),
                    approval_id=str(args["approval_id"]),
                )["ok"]
            )
        if name == "discord.post_message":
            channel = self._discord_router.channel_for(
                str(args.get("project_id", "")), str(args.get("channel_id", ""))
            )
            return self._retry(
                lambda: discord.post_message(
                    self._http,
                    self._cfg.get("DISCORD_BOT_TOKEN", ""),
                    channel_id=channel,
                    content=str(args["content"]),
                )["ok"]
            )
        if name == "discord.post_embed":
            channel = self._discord_router.channel_for(
                str(args.get("project_id", "")), str(args.get("channel_id", ""))
            )
            return self._retry(
                lambda: discord.post_embed(
                    self._http,
                    self._cfg.get("DISCORD_BOT_TOKEN", ""),
                    channel_id=channel,
                    title=str(args.get("title", "")),
                    description=str(args.get("description", "")),
                )["ok"]
            )
        if name == "chat.notify":
            return self._notify(args)
        return {"ok": False, "error_code": "UNKNOWN_TOOL", "message": name}

    def _notify(self, args: dict[str, Any]) -> dict[str, Any]:
        """Event-driven message: render the per-topic template, route to the project's
        channel, deliver with retry. ``backend`` selects slack (default) or discord."""
        topic = str(args.get("topic", ""))
        payload = dict(args.get("payload") or {})
        project_id = str(args.get("project_id", ""))
        text = self._templates.render(topic, payload)
        if str(args.get("backend", "slack")) == "discord":
            channel = self._discord_router.channel_for(project_id)
            return self._retry(
                lambda: discord.post_message(
                    self._http,
                    self._cfg.get("DISCORD_BOT_TOKEN", ""),
                    channel_id=channel,
                    content=text,
                )["ok"]
            )
        channel = self._slack_router.channel_for(project_id)
        return self._retry(
            lambda: slack.post_message(
                self._http,
                self._cfg.get("SLACK_BOT_TOKEN", ""),
                channel=channel,
                text=text,
            )["ok"]
        )

    def _retry(self, send: Any) -> dict[str, Any]:
        kwargs = {"sleep": self._sleep} if self._sleep is not None else {}
        result: DeliveryResult = send_with_retry(send, **kwargs)
        # A permanently-failed delivery is a structured result (surfaced in the dashboard),
        # never a raised exception (TITAN-INT-042).
        return {
            "ok": result.delivered,
            "attempts": result.attempts,
            "detail": result.detail,
        }

    # --- inbound webhooks ---------------------------------------------------

    def on_webhook(self, path: str, body: Any) -> dict[str, Any]:
        p = path.lstrip("/")
        if p.endswith("slack/interactivity"):
            return self._slack_interactivity(body)
        if p.endswith("slack/commands"):
            return slack.handle_slash(_form(body), status_of=self._status_of)
        if p.endswith("discord/interactions"):
            return discord.handle_interaction(
                _body_json(body), status_of=self._status_of
            )
        return {"handled": False, "reason": f"unknown webhook path {path}"}

    def _slack_interactivity(self, body: Any) -> dict[str, Any]:
        form = _form(body)
        try:
            payload = json.loads(form.get("payload", "{}"))
        except json.JSONDecodeError:
            return {"handled": False, "reason": "malformed interactivity payload"}
        return slack.handle_interactivity(
            payload,
            resolve=self._resolve,
            user_map=_load_json(self._cfg.get("SLACK_USER_MAP", "")),
        )

    def _status_of(self, project: str) -> str:
        return (
            f"TITAN project {project}: status unavailable from the connector"
            if project
            else "usage: /titan status <project>"
        )

    def _rest_resolver(
        self, project_id: str, approval_id: str, action: str, approver: str
    ) -> dict[str, Any]:
        """Production approval resolution: POST the core tool-approval endpoint with the
        service token (which MUST be scoped to exactly ``tool_approval:write``). NOTE: core
        attributes ``resolved_by`` to the token principal, not the Slack user — the connector
        returns the authorized Slack approver as ``attributed_approver`` for the operator
        trail. The endpoint requires an empty ApprovalResolution body."""
        api = self._cfg.get("TITAN_API_URL", "").rstrip("/")
        token = self._cfg.get("TITAN_SERVICE_TOKEN", "")
        verb = "approve" if action == "approved" else "deny"
        resp = self._http.request(
            "POST",
            f"{api}/api/v1/projects/{project_id}/tool-approvals/{approval_id}:{verb}",
            headers={
                "authorization": f"Bearer {token}",
                "content-type": "application/json",
            },
            body="{}",
        )
        return {"status": resp.status, "attributed_approver": approver}


def _form(body: Any) -> dict[str, str]:
    """Slack posts form-encoded bodies; core hands us either the parsed dict or ``{'raw':
    '<form>'}`` (its JSON parse failed). Normalize both to a field map."""
    from urllib.parse import parse_qsl

    if isinstance(body, dict) and "raw" in body and len(body) == 1:
        return dict(parse_qsl(str(body["raw"])))
    if isinstance(body, dict):
        return {str(k): str(v) for k, v in body.items()}
    return dict(parse_qsl(str(body)))


def _body_json(body: Any) -> dict[str, Any]:
    if isinstance(body, dict) and "raw" in body and len(body) == 1:
        try:
            return dict(json.loads(str(body["raw"])))
        except json.JSONDecodeError:
            return {}
    return dict(body) if isinstance(body, dict) else {}
