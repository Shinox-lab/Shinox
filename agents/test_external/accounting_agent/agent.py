from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
)

skill = AgentSkill(
    id="currency_conversion_skill",
    name="Currency Conversion Skill",
    description="Handles currency conversion requests using exchange rate data.",
    tags=["currency", "conversion", "exchange rates"],
    examples=[
        "Convert 100 USD to EUR",
        "What is the exchange rate from GBP to JPY?",
        "How much is 250 CAD in AUD?",
    ],
    input_modes=["text"],
    output_modes=["text"],
)

agent_card = AgentCard(
    name="Accounting Specialist Agent",
    description="An agent specialized in accounting tasks including currency conversion.",
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
id: "agent-accounting-01"
role: "Accounting Specialist"
description: "I am an Accounting Specialist Agent. I handle tasks related to accounting, including currency conversion, invoice processing, and financial reporting."
capabilities: ["process_invoice", "reconcile_expense", "generate_financial_report", "convert_currency"]
triggers: ["invoice", "expense", "financial report", "audit", "currency"]
"""

