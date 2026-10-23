import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from agent import activities

RETRY = RetryPolicy(initial_interval=timedelta(seconds=5), backoff_coefficient=2.0, maximum_attempts=3)


@workflow.defn
class IncidentInvestigationWorkflow:
    """Started by the alert webhook. Collects evidence, analyzes it, stores the RCA and notifies."""

    @workflow.run
    async def run(self, alert: dict) -> dict:
        # give metrics and logs a moment to catch up with the alert
        await asyncio.sleep(20)  # durable timer, deterministic inside a workflow
        ctx = await workflow.execute_activity(activities.collect_evidence, alert, start_to_close_timeout=timedelta(minutes=3), retry_policy=RETRY)
        report = await workflow.execute_activity(activities.analyze, args=[alert, ctx], start_to_close_timeout=timedelta(minutes=5), retry_policy=RETRY)
        report_id = await workflow.execute_activity(activities.store_report, report, start_to_close_timeout=timedelta(seconds=30), retry_policy=RETRY)
        await workflow.execute_activity(activities.notify, report, start_to_close_timeout=timedelta(seconds=30), retry_policy=RETRY)
        return {"report_id": report_id, "root_cause": report["root_cause"], "mode": report["mode"]}
