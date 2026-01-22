todo: implement another api for Squad_lead agent to call for getting more agent worker to the squad.


v2 will support for 
The Hybrid Solution: "Recipes" + "Semantic Search"
We do not use just a Vector Store. We use a Tiered Discovery approach.

Tier 1: The "Playbook" (Graph/SQL)
The Director first checks a "Squad Recipe" database.
Query: incident_type = "outage"
Result: ["Role: Tech_Lead", "Role: Database_Admin", "Role: Public_Relations"]
Why: This ensures the "Post Plan" (Customer comms) is never missed because it is hard-coded in the operational playbook.
Tier 2: The "Casting Call" (Vector Store)
Now the Director knows it needs a "Public Relations" role.
Query: "Agent capable of writing empathetic customer emails and press releases."
Vector Result: Returns Agent_External_Comms_v2.
Tier 3: The Selection (LLM)
The Director creates the invite list.
Revised Architecture for Registry:
Don't think of it as just a Vector Store. Think of it as a Service Discovery API backed by SQL (for capabilities/metadata) and Vector (for fuzzy description matching).