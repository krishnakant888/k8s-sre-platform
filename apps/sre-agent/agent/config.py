import os

MIMIR_URL = os.getenv("MIMIR_URL", "http://mimir.observability:8080/prometheus")
LOKI_URL = os.getenv("LOKI_URL", "http://loki.observability:3100")
TEMPO_URL = os.getenv("TEMPO_URL", "http://tempo.observability:3200")
K8S_URL = os.getenv("K8S_URL", "https://kubernetes.default.svc")
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
# the cluster under investigation, when it is not the one the agent runs in: a read-only token and the API CA mounted from a Secret
PLATFORM_K8S_URL = os.getenv("PLATFORM_K8S_URL", "")
PLATFORM_DIR = os.getenv("PLATFORM_DIR", "/var/run/platform")
# the cluster that runs Argo CD, for the deploy history of applications
GITOPS_K8S_URL = os.getenv("GITOPS_K8S_URL", "")
GITOPS_DIR = os.getenv("GITOPS_DIR", "/var/run/gitops")

WORKFLOW_NAMESPACE = os.getenv("WORKFLOW_NAMESPACE", "sre-agent")
WORKFLOW_TEMPLATE = os.getenv("WORKFLOW_TEMPLATE", "incident-investigation")

# without a key the agent runs its rule based analysis and says so in the report
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
MAX_AGENT_STEPS = int(os.getenv("MAX_AGENT_STEPS", "12"))

REPORTS_DIR = os.getenv("REPORTS_DIR", "/reports")

# remediation: open a draft PR when the agent diagnoses a sizing OOM
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITOPS_REPO = os.getenv("GITOPS_REPO", "krishnakant888/k8s-sre-platform")
GRAFANA_URL = os.getenv("GRAFANA_URL", "http://grafana.observability.svc.cluster.local:3000")
GRAFANA_API_TOKEN = os.getenv("GRAFANA_API_TOKEN", "")
NOTIFY_WEBHOOK_URL = os.getenv("NOTIFY_WEBHOOK_URL", "")
# scale-out cap: escalates to CPU vertical scale-up when reached
MAX_REPLICAS = int(os.getenv("MAX_REPLICAS", "5"))
# Set to "false" to disable agent CPU limit changes when VPA manages vertical sizing
VERTICAL_REMEDIATION = os.getenv("VERTICAL_REMEDIATION", "true").lower() not in ("false", "0", "no")
# CPU vertical scale-up ceiling in millicores; above this the agent opens a PR but flags for human review
MAX_CPU_LIMIT_M = int(os.getenv("MAX_CPU_LIMIT_M", "2000"))
# Memory vertical scale-up ceiling in MiB; at the ceiling, scale out only when CPU metrics also show load pressure
MAX_MEMORY_LIMIT_MIB = int(os.getenv("MAX_MEMORY_LIMIT_MIB", "4096"))

# services whose failures users feel, used to decide priority
USER_FACING_SERVICES = {s.strip() for s in os.getenv("USER_FACING_SERVICES", "orders-api,frontend").split(",") if s.strip()}

# queries must stay cheap and bounded
MAX_RANGE_MINUTES = 120
MAX_LOG_LINES = 200
MAX_TOOL_OUTPUT_CHARS = 6000
