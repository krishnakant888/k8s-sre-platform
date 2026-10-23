import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def setup_tracing(app, service: str) -> None:
    """Exports spans over OTLP/http when OTEL_EXPORTER_OTLP_ENDPOINT is set, otherwise tracing stays local."""
    resource = Resource.create(
        {
            "service.name": service,
            "service.version": os.getenv("APP_VERSION", "dev"),
            "k8s.namespace.name": os.getenv("POD_NAMESPACE", ""),
            "k8s.pod.name": os.getenv("POD_NAME", ""),
        }
    )
    provider = TracerProvider(resource=resource)
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics")
