# Logging standard

Every service writes one JSON object per line to stdout. Alloy ships it to Loki, and the SRE agent reads the same fields.
The aim: a single log line should say who logged it, which request it belongs to, and what failed, without opening anything else.

## Fields

| Field | Example | Why it is there |
|---|---|---|
| `ts`, `level`, `msg`, `logger` | `2026-10-23T16:24:10.123Z`, `error` | basics |
| `service` | `orders-api` | which component |
| `version` | `sha-02c4b25...` | the release, shows if a problem is tied to one release |
| `environment`, `namespace` | `local`, `demo` | where it runs |
| `pod`, `node` | `orders-api-76f8-grp8n`, `platform-worker2` | shows if a problem is tied to one replica or one node |
| `request_id` | `9f3c1a...` | one id per request (taken from `X-Request-ID` if the caller sent one), returned in the response header |
| `trace_id`, `span_id` | OpenTelemetry ids | click from a log line to its trace in Grafana |
| `method`, `route`, `path`, `status`, `duration_ms` | `GET`, `/orders/{order_id}`, `/orders/7`, `200`, `41.2` | what the request was and how it went |
| `dependency_ms` | `1515.7` | time spent waiting on the dependency, tells a slow service from a slow dependency |
| `client_ip`, `user_agent` | | who is calling |
| `fault_injected` | `true` | a drill is active |
| `dependency`, `operation`, `outcome` | `inventory`, `lookup`, `error` / `slow` | on every failed or slow downstream call |
| `error_type`, `error_message`, `stack` | `DependencyUnavailable` | on every failure, with the stack trace for exceptions |

Labels in Loki stay few (`namespace`, `pod`, `container`, `service`) so the index stays small; everything else is a JSON field,
filtered with `| json`, for example `{service="orders-api"} | json | node="platform-worker2" | level="error"`.

## What the agent does with them

- counts error lines per `pod`, `node`, `version`, `error_type` and `dependency`: errors from one node or one replica point at infrastructure, errors from one release point at the application, a named `dependency` shows where to look
- lists the fields that are missing from the lines it read and puts them in the report's *observability gaps*, with how to add them
