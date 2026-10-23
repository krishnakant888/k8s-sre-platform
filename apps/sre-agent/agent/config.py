import os

MIMIR_URL = os.getenv("MIMIR_URL", "http://mimir.observability:8080/prometheus")
LOKI_URL = os.getenv("LOKI_URL", "http://loki.observability:3100")
TEMPO_URL = os.getenv("TEMPO_URL", "http://tempo.observability:3200")
K8S_URL = os.getenv("K8S_URL", "https://kubernetes.default.svc")
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"

WORKFLOW_NAMESPACE = os.getenv("WORKFLOW_NAMESPACE", "sre-agent")
WORKFLOW_TEMPLATE = os.getenv("WORKFLOW_TEMPLATE", "incident-investigation")

# without a key the agent runs its rule based analysis and says so in the report
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
MAX_AGENT_STEPS = int(os.getenv("MAX_AGENT_STEPS", "12"))

REPORT_NAMESPACE = os.getenv("REPORT_NAMESPACE", "sre-agent")
NOTIFY_WEBHOOK_URL = os.getenv("NOTIFY_WEBHOOK_URL", "")

# services whose failures users feel, used to decide priority
USER_FACING_SERVICES = {s.strip() for s in os.getenv("USER_FACING_SERVICES", "orders-api,frontend").split(",") if s.strip()}

# queries must stay cheap and bounded
MAX_RANGE_MINUTES = 120
MAX_LOG_LINES = 200
MAX_TOOL_OUTPUT_CHARS = 6000
