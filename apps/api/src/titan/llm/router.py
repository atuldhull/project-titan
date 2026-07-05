"""Internal LLM gateway HTTP endpoint (Vol 7 §8.1). Private to the module;
main.py attaches it via mount().

The primary interface is in-process (agents call :class:`LlmGateway` directly);
this HTTP surface is the same gateway for out-of-process callers (the plugin
runtime today, an extracted service later). Guarded by ``llm:invoke`` — the
gateway is the single, audited chokepoint for every model call (TITAN-INT-002).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Request

from titan.gateway.rbac import Requires
from titan.identity.service import Principal
from titan.llm.gateway import LlmGateway
from titan.llm.models import ChatRequest, ChatResponse, EmbeddingsRequest, EmbeddingsResponse

router = APIRouter(prefix="/api/v1/llm", tags=["llm"])

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


def mount(app: FastAPI) -> None:
    app.include_router(router)
