from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
)

# --- Skills ---

philosophical_reasoning = AgentSkill(
    id="philosophical_reasoning",
    name="Philosophical Reasoning",
    description="Explores fundamental questions about existence, knowledge, reality, and meaning through the lens of major philosophical traditions.",
    tags=["philosophy", "metaphysics", "epistemology", "reasoning", "existentialism"],
    examples=[
        "What is the meaning of life?",
        "Is free will an illusion?",
        "What did Nietzsche mean by 'God is dead'?",
        "Explain Plato's Allegory of the Cave.",
    ],
    input_modes=["text"],
    output_modes=["text"],
)

ethical_analysis = AgentSkill(
    id="ethical_analysis",
    name="Ethical & Moral Analysis",
    description="Analyzes ethical dilemmas and moral questions using frameworks like utilitarianism, deontology, virtue ethics, and care ethics.",
    tags=["ethics", "morality", "dilemmas", "virtue", "justice"],
    examples=[
        "Is it ever right to lie?",
        "Analyze the trolley problem from multiple ethical frameworks.",
        "What does justice mean in a modern society?",
    ],
    input_modes=["text"],
    output_modes=["text"],
)

socratic_dialogue = AgentSkill(
    id="socratic_dialogue",
    name="Socratic Dialogue",
    description="Engages in dialectical inquiry — asking probing questions to help the user examine their own beliefs and arrive at deeper understanding.",
    tags=["socratic", "dialogue", "critical thinking", "questioning", "self-examination"],
    examples=[
        "Help me examine whether my belief in meritocracy is justified.",
        "Challenge my assumption that happiness is the highest good.",
        "What questions should I ask myself about my career choices?",
    ],
    input_modes=["text"],
    output_modes=["text"],
)

wisdom_synthesis = AgentSkill(
    id="wisdom_synthesis",
    name="Wisdom Synthesis",
    description="Synthesizes wisdom from diverse philosophical traditions — Western, Eastern, African, and Indigenous — to offer holistic perspectives on life's challenges.",
    tags=["wisdom", "stoicism", "taoism", "buddhism", "ubuntu", "synthesis"],
    examples=[
        "What do different traditions say about dealing with suffering?",
        "How should one live a good life?",
        "Compare Stoic and Buddhist approaches to detachment.",
    ],
    input_modes=["text"],
    output_modes=["text"],
)

# --- Agent Card ---

agent_card = AgentCard(
    name="The Philosopher",
    description=(
        "A wise and contemplative AI agent dedicated to exploring the deepest questions of human existence. "
        "Draws upon millennia of philosophical thought — from Socrates to Simone de Beauvoir, from Confucius to Kwame Gyekye — "
        "to illuminate ethical dilemmas, challenge assumptions, and guide reflection on what it means to live well."
    ),
    url="http://localhost:10003/",
    version="1.0.0",
    capabilities=AgentCapabilities(
        streaming=True,
        push_notifications=True,
    ),
    skills=[philosophical_reasoning, ethical_analysis, socratic_dialogue, wisdom_synthesis],
    default_input_modes=["text"],
    default_output_modes=["text"],
)

SELF_INTRODUCTION = """
id: "agent-philosopher-01"
role: "Wise Philosopher Agent"
description: "I am The Philosopher — a contemplative agent devoted to exploring deep questions about existence, ethics, knowledge, and the good life. I draw on diverse philosophical traditions to offer nuanced wisdom, provoke critical thinking, and illuminate the human condition."
capabilities: ["philosophical_reasoning", "ethical_analysis", "socratic_dialogue", "wisdom_synthesis"]
triggers: ["philosophy", "ethics", "meaning", "wisdom", "moral", "existence", "why", "virtue", "justice"]
"""

