# Relay: a support desk where AI agents share one graph memory

Built for Graph Hacks (Track 2: Agent Memory and Coordination).

Relay is a customer-support demo with two AI agents. A **Concierge** talks to the
customer and a **Billing** agent takes over for money issues. Everything they know
lives in a FalkorDB graph, not in Python variables or the chat window. Restart the
server and the memory, the open cases and the handoff trail are all still there.

> All demo data is fictional. Do not enter real personal information.

## Why a graph?
- **Memory with time:** a fact is never overwritten. The old value gets an `until`
  date and the new one is added, so you can see "Pro until 4:55 PM, then Basic".
- **Coordination:** a handoff is an edge (`Case -[HANDED_TO]-> Agent`) with a reason.
  Routing is a query: if the customer has an open case handed to Billing, Billing answers.
- **Multi-hop reasoning:** Billing finds fixes from *other* customers on the same plan
  by walking customer -> plan <- other customer -> their resolved case.
- **Isolation:** each company has its own graph (`tenant_acme`, `tenant_bank`). The
  tenant and customer come from the session, never from the LLM.

## Graph model
Nodes: `Customer`, `Fact {key, value, since, until}`, `Event {who, text, at}`,
`Case {id, topic, summary, status, resolution}`, `Agent {name}`

Relationships: `(Customer)-[:HAS_FACT]->(Fact)`, `(Customer)-[:HAD_EVENT]->(Event)`,
`(Customer)-[:HAS_CASE]->(Case)`, `(Case)-[:OPENED_BY]->(Agent)`,
`(Case)-[:HANDED_TO {reason, at}]->(Agent)`

## Key queries (all in `memory.py`)
- `remember_fact`: close the old fact, then create the new one
- `recall_profile`: current facts only (`WHERE f.until IS NULL`)
- `active_case`: the customer's open case and which agent holds it (routing)
- `similar_cases`: 3-hop query over same-plan customers' resolved cases
- `case_trail`: the handoff history shown in the UI

The UI's **Evidence panel** shows the exact Cypher behind every reply.

## Run it
1. Create a FalkorDB Cloud instance and get an LLM API key.
2. `cp .env.example .env` and fill in the values.
3. `pip install -r requirements.txt`
4. `python app.py`, then open http://localhost:5000

The free Cloud tier has no persistence, so `seed()` recreates the demo data on start.
`python -c "import memory as m; m.reset()"` wipes both graphs and reseeds.

## Demo script
1. As Maya: "I just moved to the Basic plan." The Memory panel closes Pro and opens Basic.
2. "I was charged twice this month." Billing answers using a similar case, and the Case
   trail shows the handoff.
3. "Thanks." Concierge takes over again.
4. Switch to Northbank. Raj has no knowledge of Maya.

## Limitations
- Single-user demo: the query trace is a shared global.
- Fictional seed data and a small model, kept simple to fit the time available.

### Memory Guardrails

Relay restricts memory writes to an allowlist of expected facts (`name` and `plan`), validates their lengths, and checks plan values against a known set. Rejected writes are recorded as guardrail events in the graph for inspection in the Evidence panel.

Incoming messages are trimmed, limited to 500 characters, and rejected when empty. Tenant and customer identity are derived from server-side session/application data rather than model-generated tool arguments.

These safeguards reduce accidental or malicious memory poisoning, but they do not prevent all prompt injection or guarantee that every model response is safe. The guardrails should be tested alongside tenant isolation, authentication, and authorization controls.


## AI Use Disclosure

Claude (Anthropic) was used as a coding assistant for project planning and drafting parts of the codebase (`memory.py`, `agents.py`, `app.py`, and the web UI). I ran and tested the application and can explain its graph model and queries. At runtime, the agents use the Gemini API (Google) to access a hosted LLM.