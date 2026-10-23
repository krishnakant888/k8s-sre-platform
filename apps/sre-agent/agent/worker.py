import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

from agent import activities, config
from agent.logging_setup import setup_logging
from agent.workflows import IncidentInvestigationWorkflow


async def main() -> None:
    setup_logging("sre-agent")
    log = logging.getLogger("sre-agent")
    client = await Client.connect(config.TEMPORAL_ADDRESS, namespace=config.TEMPORAL_NAMESPACE)
    log.info("worker starting", extra={"task_queue": config.TASK_QUEUE, "temporal": config.TEMPORAL_ADDRESS, "llm": bool(config.ANTHROPIC_API_KEY)})
    with ThreadPoolExecutor(max_workers=8) as pool:
        worker = Worker(
            client,
            task_queue=config.TASK_QUEUE,
            workflows=[IncidentInvestigationWorkflow],
            activities=[activities.collect_evidence, activities.analyze, activities.store_report, activities.notify],
            activity_executor=pool,
        )
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
