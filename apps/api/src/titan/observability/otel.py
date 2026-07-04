"""OpenTelemetry tracing setup (TITAN-BCK-025, ADR-011).

Instruments FastAPI, SQLAlchemy, redis-py, and httpx, exporting OTLP to the
local otel-collector. Tracing is enabled only when
``TITAN_OTEL_EXPORTER_OTLP_ENDPOINT`` is set — with no collector configured
every setup call is a no-op, so tests and collector-less local runs stay
silent. Traces never contain prompt or completion text (TITAN-OPS-016).
"""

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.settings import Settings

_configured = False


def tracing_enabled(settings: Settings) -> bool:
    return bool(settings.otel_exporter_otlp_endpoint)


def setup_tracing(settings: Settings, service_name: str) -> bool:
    """Install the global tracer provider + OTLP exporter. Idempotent."""
    global _configured
    if not tracing_enabled(settings) or _configured:
        return _configured

    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
    exporter = OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    RedisInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()
    _configured = True
    return True


def instrument_app(settings: Settings, app: FastAPI) -> None:
    """Attach FastAPI auto-instrumentation (no-op when tracing is disabled)."""
    if tracing_enabled(settings):
        FastAPIInstrumentor.instrument_app(app)


def instrument_engine(settings: Settings, engine: AsyncEngine) -> None:
    """Attach SQLAlchemy auto-instrumentation to the async engine's core."""
    if tracing_enabled(settings):
        SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
