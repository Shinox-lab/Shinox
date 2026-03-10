"""
Standalone A2A Server — serves the accounting brain over HTTP.

This is the "external agent" side. It wraps brain.py in an A2A-compatible
HTTP server so that the Shinox mesh can reach it via A2ABridge.

Usage:
    python a2a_server.py [--host localhost] [--port 10002]
"""

import logging
import sys

import click
import uvicorn
from a2a.server.apps import A2AStarletteApplication
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    InternalError,
    Part,
    TaskState,
    TextPart,
    UnsupportedOperationError,
)
from a2a.utils import new_agent_text_message, new_task
from a2a.utils.errors import ServerError

from brain import brain
from agent import agent_card

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class AccountingAgentExecutor(AgentExecutor):
    """Wraps the LangGraph brain as an A2A AgentExecutor."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        query = context.get_user_input()
        task = context.current_task
        if not task:
            task = new_task(context.message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue, task.id, task.context_id)

        try:
            inputs = {"messages": [("user", query)]}
            config = {"configurable": {"thread_id": task.context_id}}

            final_response = None
            async for item in brain.astream(inputs, config, stream_mode="values"):
                message = item["messages"][-1]
                msg_type = message.__class__.__name__
                tool_calls = getattr(message, "tool_calls", None)

                if msg_type == "AIMessage" and tool_calls:
                    await updater.update_status(
                        TaskState.working,
                        new_agent_text_message(
                            "Looking up the exchange rates...",
                            task.context_id,
                            task.id,
                        ),
                    )
                elif msg_type == "ToolMessage":
                    await updater.update_status(
                        TaskState.working,
                        new_agent_text_message(
                            "Processing the exchange rates...",
                            task.context_id,
                            task.id,
                        ),
                    )
                elif msg_type == "AIMessage" and not tool_calls:
                    final_response = message.content

            if final_response:
                await updater.add_artifact(
                    [Part(root=TextPart(text=final_response))],
                    name="conversion_result",
                )
                await updater.complete()
            else:
                await updater.update_status(
                    TaskState.input_required,
                    new_agent_text_message(
                        "Unable to process the request. Please provide more details.",
                        task.context_id,
                        task.id,
                    ),
                    final=True,
                )

        except Exception as e:
            logger.error(f"Error during execution: {e}")
            raise ServerError(error=InternalError()) from e

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise ServerError(error=UnsupportedOperationError())


@click.command()
@click.option("--host", default="localhost")
@click.option("--port", default=10002)
def main(host, port):
    """Start the accounting agent as a standalone A2A server."""
    # Update the agent card URL to match the actual host/port
    agent_card.url = f"http://{host}:{port}/"

    request_handler = DefaultRequestHandler(
        agent_executor=AccountingAgentExecutor(),
        task_store=InMemoryTaskStore(),
    )
    server = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    )

    logger.info(f"Starting A2A server at http://{host}:{port}")
    uvicorn.run(server.build(), host=host, port=port)


if __name__ == "__main__":
    main()
