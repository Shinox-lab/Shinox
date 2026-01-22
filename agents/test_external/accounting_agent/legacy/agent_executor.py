import logging

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    InternalError,
    InvalidParamsError,
    Part,
    TaskState,
    TextPart,
    UnsupportedOperationError,
)
from a2a.utils import (
    new_agent_text_message,
    new_task,
)
from a2a.utils.errors import ServerError

from agents.test_external.currency_agent.brain import brain


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class CurrencyAgentExecutor(AgentExecutor):
    """Currency Conversion AgentExecutor Example."""

    def __init__(self):
        self.agent = brain

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        error = self._validate_request(context)
        if error:
            raise ServerError(error=InvalidParamsError())

        query = context.get_user_input()
        task = context.current_task
        if not task:
            task = new_task(context.message)  # type: ignore
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        try:
            # Prepare input and config for the graph
            inputs = {'messages': [('user', query)]}
            config = {'configurable': {'thread_id': task.context_id}}
            
            final_response = None
            async for item in self.agent.astream(inputs, config, stream_mode='values'):
                message = item['messages'][-1]
                
                # More detailed logging
                msg_type = message.__class__.__name__
                has_tool_calls_attr = hasattr(message, 'tool_calls')
                tool_calls_value = getattr(message, 'tool_calls', None) if has_tool_calls_attr else None
                tool_calls_bool = bool(tool_calls_value) if tool_calls_value is not None else False
                
                logger.info(f"Stream item - type: {msg_type}, has_attr: {has_tool_calls_attr}, tool_calls: {tool_calls_value}, bool: {tool_calls_bool}")
                
                # Check if LLM is making a tool call
                if msg_type == 'AIMessage' and tool_calls_value:
                    logger.info("Detected tool call")
                    await updater.update_status(
                        TaskState.working,
                        new_agent_text_message(
                            'Looking up the exchange rates...',
                            task.context_id,
                            task.id,
                        ),
                    )
                # Check if we got a tool response
                elif msg_type == 'ToolMessage':
                    logger.info("Detected tool message")
                    await updater.update_status(
                        TaskState.working,
                        new_agent_text_message(
                            'Processing the exchange rates...',
                            task.context_id,
                            task.id,
                        ),
                    )
                # Final AI response (when no tool calls)
                elif msg_type == 'AIMessage' and not tool_calls_value:
                    logger.info(f"Detected final AI message with content: {message.content[:200] if message.content else 'None'}")
                    final_response = message.content
            
            logger.info(f"Stream loop completed. Final response exists: {final_response is not None}")
            
            # Send final response
            if final_response:
                logger.info("Sending final response as artifact")
                await updater.add_artifact(
                    [Part(root=TextPart(text=final_response))],
                    name='conversion_result',
                )
                logger.info("Completing task")
                await updater.complete()
                logger.info("Task completed successfully")
            else:
                logger.info("No final response, sending input_required status")
                await updater.update_status(
                    TaskState.input_required,
                    new_agent_text_message(
                        'Unable to process the request. Please provide more details.',
                        task.context_id,
                        task.id,
                    ),
                    final=True,
                )
                logger.info("Input required status sent")

        except Exception as e:
            logger.error(f'An error occurred while streaming the response: {e}')
            raise ServerError(error=InternalError()) from e

    def _validate_request(self, context: RequestContext) -> bool:
        return False

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        raise ServerError(error=UnsupportedOperationError())