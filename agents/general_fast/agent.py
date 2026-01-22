from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
)

skill = AgentSkill(
    id="simple_logic_skill",
    name="Simple Logic Skill",
    description="Handles simple logic tasks quickly and efficiently.",
    tags=["logic", "simple", "quick", "efficient"],
    examples=[
        "What is the price of a burger in New York City?",
        "What is the name of the capital of France?",
        "How many hours are there in a day?",
    ],
    input_modes=["text"],
    output_modes=["text"],
)

agent_card = AgentCard(
    name="Generalist Simple Logic Agent",
    description="An agent that can perform simple general logic tasks quickly and efficiently.",
    url="http://localhost:10002/",
    version="1.0.0",
    capabilities=AgentCapabilities(
        streaming=True,
        push_notifications=True,
    ),
    skills=[skill],
    default_input_modes=["text"],
    default_output_modes=["text"],
)

SELF_INTRODUCTION = """
id: "agent-generalist-01"
role: "Generalist Simple Logic Agent"
description: "I am a Generalist Simple Logic Agent. I handle a variety of simple logic tasks quickly and efficiently."
capabilities: ["simple_logic", "quick_response", "efficient_processing"]
triggers: ["logic", "simple task", "quick", "efficient"]
"""

