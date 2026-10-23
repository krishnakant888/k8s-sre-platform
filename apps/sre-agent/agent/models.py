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


class FaultDomain(BaseModel):
    layer: str = Field(description="application, infrastructure or unknown")
    component: str = Field(description="the specific part at fault, for example 'downstream dependency inventory.lookup' or 'memory limit of argocd-application-controller (768Mi)'")
    reasoning: list[str] = Field(description="the signals from logs, metrics, traces and events that put the problem in this layer")
    confidence: str = Field(description="low, medium or high")


class Priority(BaseModel):
    level: str = Field(description="P1 (outage, respond now), P2 (serious, within 1 hour), P3 (next business day) or P4 (backlog)")
    respond_within: str
    ongoing: bool = Field(description="whether the impact is still happening when the report is written")
    rationale: list[str] = Field(description="the severity and impact facts that decided the level")


class HowToFix(BaseModel):
    immediate: list[str] = Field(description="steps to stop the damage now, with exact commands, names and file paths")
    permanent: list[str] = Field(description="changes that stop it happening again, naming the exact component and setting")
    verify: list[str] = Field(description="how to confirm it is fixed: a query or command and the value to expect")


class RcaReport(BaseModel):
    id: str
    alert: str
    service: str
    severity: str
    started_at: str
    generated_at: str
    mode: str = Field(description="llm or rule_based")
    summary: str
    what_happened: str = Field(description="3-5 plain sentences: what broke, when, how bad, and where the time or errors come from, with numbers")
    priority: Priority
    fault_domain: FaultDomain
    impact: Impact
    timeline: list[TimelineEntry]
    root_cause: RootCause
    evidence: list[Evidence]
    contributing_factors: list[str]
    how_to_fix: HowToFix
    observability_gaps: list[str] = Field(description="signals that were missing or too weak for this investigation, each with the exact metric, log field, trace attribute or alert to add")
    rca_markdown: str
