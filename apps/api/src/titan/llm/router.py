"""Internal LLM gateway HTTP endpoint (Vol 7 §8.1). Private to the module;
main.py attaches it via mount().

The primary interface is in-process (agents call :class:`LlmGateway` directly);
this HTTP surface is the same gateway for out-of-process callers (the plugin
runtime today, an extracted service later). Guarded by ``llm:invoke`` — the
gateway is the single, audited chokepoint for every model call (TITAN-INT-002).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field

from titan.gateway.problems import ProblemException, problem_type
from titan.gateway.rbac import Requires
from titan.identity.service import Principal
from titan.llm import settings_service
from titan.llm.config import ConfigLoader
from titan.llm.gateway import LlmGateway
from titan.llm.models import ChatRequest, ChatResponse, EmbeddingsRequest, EmbeddingsResponse

router = APIRouter(prefix="/api/v1/llm", tags=["llm"])


class ProviderTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1, max_length=200)


_Invoker = Annotated[Principal, Depends(Requires("llm:invoke"))]


def _gateway(request: Request) -> LlmGateway:
    gateway: LlmGateway = request.app.state.llm_gateway
    return gateway


@router.post("/chat", response_model=ChatResponse)
async def chat(request: Request, body: ChatRequest, principal: _Invoker) -> ChatResponse:
    return await _gateway(request).chat(body)


@router.post("/embeddings", response_model=EmbeddingsResponse)
async def embeddings(
    request: Request, body: EmbeddingsRequest, principal: _Invoker
) -> EmbeddingsResponse:
    return await _gateway(request).embed(body)


# --- Settings → LLM Providers + Budgets (Vol 4 §6.13, Phase 64) --------------
#
# A separate router because these are org-settings resources, not the gateway's
# invocation surface: they must NOT be behind `llm:invoke` (which no seeded
# bundle holds) and they are org-administration, so `settings:*` is the gate.

settings_router = APIRouter(prefix="/api/v1", tags=["settings"])

_SettingsReader = Annotated[Principal, Depends(Requires("settings:read"))]
_SettingsAdmin = Annotated[Principal, Depends(Requires("settings:write"))]


def _loader(request: Request) -> ConfigLoader:
    return _gateway(request).config_loader


@settings_router.get("/settings/llm", response_model=settings_service.LlmSettingsOut)
async def get_llm_settings(
    request: Request, principal: _SettingsReader
) -> settings_service.LlmSettingsOut:
    """The EFFECTIVE gateway config — models.yaml with any overrides applied.
    Provider API keys are never included; only whether one resolved, plus a
    four-character hint."""
    loader = _loader(request)
    return settings_service.describe(
        loader.get(), overridden=settings_service.overrides_exist(loader)
    )


@settings_router.put("/settings/llm", response_model=settings_service.LlmSettingsOut)
async def update_llm_settings(
    request: Request, body: settings_service.LlmSettingsUpdate, principal: _SettingsAdmin
) -> settings_service.LlmSettingsOut:
    """Persist routing/budget overrides. Takes effect on the next gateway call
    in every process — no restart (TITAN-FRT-036)."""
    try:
        return settings_service.apply_update(_loader(request), body)
    except ValueError as exc:
        raise ProblemException(
            status=422,
            title="Invalid gateway settings",
            detail=str(exc),
            problem_type=problem_type("validation"),
        ) from exc


@settings_router.delete("/settings/llm", response_model=settings_service.LlmSettingsOut)
async def clear_llm_settings(
    request: Request, principal: _SettingsAdmin
) -> settings_service.LlmSettingsOut:
    """Discard every override and fall back to the operator's models.yaml."""
    try:
        return settings_service.clear_overrides(_loader(request))
    except (ValueError, OSError) as exc:
        raise ProblemException(
            status=422,
            title="Invalid gateway settings",
            detail=str(exc),
            problem_type=problem_type("validation"),
        ) from exc


@settings_router.post("/settings/llm:test", response_model=settings_service.ProviderTestResult)
async def test_llm_provider(
    request: Request, body: ProviderTestRequest, principal: _SettingsAdmin
) -> settings_service.ProviderTestResult:
    """Live round-trip against one alias, reporting latency and what served it.

    `settings:write`, not `settings:read`: this spends real money and writes a
    row to the usage ledger, so it is a mutation however much it reads like one.
    """
    try:
        return await settings_service.test_alias(_loader(request), _gateway(request), body.alias)
    except ValueError as exc:
        raise ProblemException(
            status=422,
            title="Unknown alias",
            detail=str(exc),
            problem_type=problem_type("validation"),
        ) from exc


class GlobalBudgetResumeResult(BaseModel):
    resumed: bool = True


@settings_router.post("/settings/llm/budget:resume-global", response_model=GlobalBudgetResumeResult)
async def resume_global_budget(
    request: Request, principal: _SettingsAdmin
) -> GlobalBudgetResumeResult:
    """Lift the deployment-global monthly budget hold (TITAN-SEC-082): clear the
    current month's ``org_month`` hold AND reset its counter, so every task paused
    on the global ceiling resumes. Raising the config value alone does NOT clear an
    existing hold, so this is the supported operator resume action for the global
    tier (the project/task tiers have their own :resume-budget routes).
    ``settings:write`` — a deployment-admin action."""
    from titan.llm.service import raise_global_month_budget

    async with request.app.state.engine.begin() as conn:
        await raise_global_month_budget(conn, redis=request.app.state.redis)
    return GlobalBudgetResumeResult()


def mount(app: FastAPI) -> None:
    app.include_router(router)
    app.include_router(settings_router)
