"""All OpenTelemetry wiring lives here so main.py stays about the app.

Nothing in this project creates spans by hand. Every span you see in Jaeger is
produced by one of the instrumentors set up below.
"""

import logging
import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.langchain import LangchainInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

log = logging.getLogger(__name__)


def setup_telemetry(app) -> TracerProvider:
    """Wire up the tracer provider and every instrumentor.

    Must run before any LangChain object is constructed. The LangChain
    instrumentor patches langchain_core.callbacks.BaseCallbackManager.__init__,
    so a chain built before this call silently produces no spans.
    """
    # service.name is what populates Jaeger's service dropdown. Get it wrong and
    # the UI looks empty even though spans are arriving fine.
    resource = Resource.create(
        {"service.name": os.getenv("OTEL_SERVICE_NAME", "langchain-chat")}
    )

    provider = TracerProvider(resource=resource)
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
    provider.add_span_processor(
        # insecure=True: plaintext gRPC, fine inside the compose network.
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
    )
    trace.set_tracer_provider(provider)

    LangchainInstrumentor().instrument(tracer_provider=provider)
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    LoggingInstrumentor().instrument(set_logging_format=True)

    # Without this, LangChain spans arrive in Jaeger as unparented roots and the
    # waterfall view never renders.
    #
    # ASGI emits one "http send" span per chunk written, so a streamed response
    # produces a span per token -- 113 of them for a 113-character reply. Set
    # EXCLUDE_ASGI_SPANS=true to drop the send/receive spans and leave just the
    # server span plus the LangChain chain. Default is off: seeing the spam once
    # is the lesson, turning it off is the fix.
    exclude = (
        ["send", "receive"]
        if os.getenv("EXCLUDE_ASGI_SPANS", "false").lower() == "true"
        else None
    )
    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=provider, exclude_spans=exclude
    )

    log.info("telemetry ready, exporting to %s", endpoint)
    return provider
