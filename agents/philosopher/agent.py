from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
)

skill = AgentSkill(
    id="philosophical_reasoning",
    name="Philosophical Reasoning",
    description="Provides deep philosophical insights, ethical analysis, and wisdom.",
    tags=["philosophy", "wisdom", "ethics", "reasoning", "depth"],
    examples=[
        "What is the meaning of life?",
        "Is free will an illusion?",
        "How should one live a good life?",
    ],
    input_modes=["text"],
    output_modes=["text"],
)

agent_card = AgentCard(
    name="The Philosopher",
    description="A wise agent dedicated to exploring deep questions, ethical dilemmas, and the nature of existence.",
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

