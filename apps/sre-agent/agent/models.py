"""The structured report every investigation ends with. It is validated before it is stored."""
from pydantic import BaseModel, Field


class Evidence(BaseModel):
    source: str = Field(description="mimir, loki, tempo, kubernetes or argocd")
    query: str
    finding: str


class TimelineEntry(BaseModel):
    time: str
    event: str


class RootCause(BaseModel):
    category: str = Field(description="one of: dependency_latency, dependency_errors, pod_crash, deploy_regression, resource_exhaustion, unknown")
    description: str
    confidence: str = Field(description="low, medium or high")


class Impact(BaseModel):
    slo: str
    bad_request_ratio: float | None = None
    p99_latency_seconds: float | None = None
    availability_replicas: float | None = None
    summary: str


class RcaReport(BaseModel):
    id: str
    alert: str
    service: str
    severity: str
    started_at: str
    generated_at: str
    mode: str = Field(description="llm or rule_based")
    summary: str
    impact: Impact
    timeline: list[TimelineEntry]
    root_cause: RootCause
    evidence: list[Evidence]
    contributing_factors: list[str]
    recommended_actions: list[str]
    postmortem_markdown: str
