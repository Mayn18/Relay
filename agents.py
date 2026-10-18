import os, json
from dotenv import load_dotenv
from openai import OpenAI
import time
import memory as m

load_dotenv()
client = OpenAI(
    api_key=os.environ["LLM_API_KEY"],
    base_url=os.environ["LLM_BASE_URL"],
    timeout=float(os.environ.get("LLM_TIMEOUT", "30")),  # seconds per model call; a stalled provider must not hold the customer lock for minutes
    max_retries=0,  # retries are handled, and bounded, by llm() below
)
MODEL = os.environ["LLM_MODEL"]

print(f"Using model {MODEL} at {os.environ['LLM_BASE_URL']}")


def tool(name, desc, props, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


S = {"type": "string"}

# --- Guardrails: restrict which facts the model may store
ALLOWED_FACTS = {"name": 40, "plan": 20}
PLANS = {"free", "basic", "pro", "enterprise"}
# Billing may answer this many times per case; after that the case is escalated to a human so an
# unresolved case cannot hold the customer indefinitely.
BILLING_MAX_TURNS = int(os.environ.get("BILLING_MAX_TURNS", "4"))


def check_fact(key, value):
    """Returns ((key, value), None) if allowed, else (None, reason)."""
    key = str(key or "").strip().lower()
    value = str(value or "").strip()

    if key not in ALLOWED_FACTS:
        return None, f"'{key}' is not a fact this desk may store"

    if not value or len(value) > ALLOWED_FACTS[key]:
        return None, f"invalid value for '{key}'"

    if key == "plan":
        if value.lower() not in PLANS:
            return None, f"'{value}' is not a known plan"
        value = value.capitalize()

    return (key, value), None


REMEMBER = tool(
    "remember_fact",
    "Save or update a fact about the customer (e.g. key='plan', value='Basic'). "
    "Call whenever the customer states or changes a fact about themselves.",
    {"key": S, "value": S},
    ["key", "value"],
)
HANDOFF = tool(
    "handoff_to_billing",
    "Hand the customer to the Billing agent. Call this for anything about charges, "
    "refunds, invoices or payments. summary = one sentence on the problem; "
    "reason = why Billing should take it.",
    {"summary": S, "reason": S},
    ["summary", "reason"],
)
RESOLVE = tool(
    "resolve_case",
    "Close the customer's open case once the billing issue is dealt with. "
    "resolution = one sentence on what was done. basis = the id of the policy article that covers "
    "this billing issue (from the policy list you were given), or 'precedent' if a similar case does.",
    {"resolution": S, "basis": S},
    ["resolution", "basis"],
)
ESCALATE = tool(
    "escalate_case",
    "Hand the case to a human when no policy or similar case covers the issue. "
    "reason = one sentence for the human agent.",
    {"reason": S},
    ["reason"],
)


def llm(msgs, tools):
    """Call the model, retrying twice on provider errors (free tiers fail now and then)."""
    for attempt in range(3):
        try:
            return (
                client.chat.completions.create(
                    model=MODEL,
                    messages=msgs,
                    tools=tools,
                )
                .choices[0]
                .message
            )
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))


def run_agent(system, tools, tenant, cust, history, user_msg, resolvable=()):
    """Returns (reply, handed_off). tenant and cust come from the session, never the LLM.
    resolvable = the basis values resolve_case may cite for this turn (policy ids of the case topic, 'precedent')."""
    msgs = (
        [{"role": "system", "content": system}]
        + history
        + [{"role": "user", "content": user_msg}]
    )
    allowed = {t["function"]["name"] for t in tools}
    handed = False
    for _ in range(4):
        msg = llm(msgs, tools)
        if not msg.tool_calls:
            return msg.content or "", False
        msgs.append(msg)
        for tc in msg.tool_calls:
            name = tc.function.name
            result = "ok"
            try:
                args = json.loads(tc.function.arguments or "{}")
                if not isinstance(args, dict):
                    raise ValueError("arguments must be a JSON object")
            except ValueError:
                args, result = {}, "error: arguments must be a JSON object"
            if name not in allowed:  # only tools this agent was actually given
                result = f"error: unknown tool '{name}'"
            elif result == "ok":
                if name == "remember_fact":
                    ok, err = check_fact(args.get("key"), args.get("value"))
                    if err:
                        result = "refused: " + err
                        m.log_event(tenant, cust, "guardrail", "Blocked write: " + err)
                    else:
                        m.remember_fact(tenant, cust, *ok)
                elif name == "handoff_to_billing":
                    summary = str(args.get("summary") or "").strip()[:300]
                    reason = str(args.get("reason") or "").strip()[:300]
                    if not summary or not reason:
                        result = "error: summary and reason are required"
                    elif handed or m.active_case(tenant, cust):
                        result = "A case is already open; do not hand off again."
                    else:
                        m.handoff(tenant, cust, "billing", summary, reason, "Billing")
                        handed = True
                elif name == "resolve_case":
                    case = m.active_case(tenant, cust)  # case id from the graph, not the LLM
                    basis = str(args.get("basis") or "").strip()
                    if not case:
                        result = "error: there is no open case"
                    elif basis not in resolvable:
                        result = ("error: resolve_case needs a basis that covers this case's billing issue; "
                                  f"allowed: {sorted(resolvable) or 'none (escalate instead)'}")
                    else:
                        resolution = str(args.get("resolution") or "Resolved").strip()[:300]
                        m.resolve_case(tenant, cust, case["id"], resolution)
                elif name == "escalate_case":
                    case = m.active_case(tenant, cust)
                    if case:
                        reason = str(args.get("reason") or "Needs a human").strip()[:300]
                        m.escalate_case(tenant, cust, case["id"], "Escalated: " + reason)
                    else:
                        result = "error: there is no open case"
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        if handed:
            return "", True
    return "", False


def billing_turn(tenant, cust, case, history, user_msg):
    profile = m.recall_profile(tenant, cust)
    similar = m.similar_cases(tenant, cust, case["topic"])
    policies = m.policies_for(tenant, cust)  # all policy topics for the current plan
    # resolve_case may only cite a policy about THIS case's topic, or a real precedent
    resolvable = {p["id"] for p in m.policies_for(tenant, cust, case["topic"])}
    if similar:
        resolvable.add("precedent")
    system = (
        "You are the Billing agent of a support desk. Be brief and friendly.\n"
        f"Customer facts (from the graph): {json.dumps(profile)}\n"
        f"Open case handed to you by the Concierge: {json.dumps(case)}\n"
        f"Resolved cases of OTHER customers on the same plan (from the graph): {json.dumps(similar)}\n"
        f"Company policy for this customer's current plan (from the graph): {json.dumps(policies)}\n"
        "Base every promise on the policy or on the similar cases. If one covers the case's billing issue, "
        "tell the customer exactly what will happen, then call resolve_case with basis = that policy's id "
        "(or 'precedent'). If neither covers it, tell the customer you are escalating to a human, call "
        "escalate_case, and do NOT call resolve_case or promise anything. "
        "Do not ask the customer to repeat what is in the case.\n"
        "Only call resolve_case or escalate_case if the customer's latest message is about this case. "
        "If it is about something else (for example a plan change), answer it briefly from the policies, "
        "mention the case is still open, and do not close it.\n"
    )
    reply, _ = run_agent(system, [REMEMBER, RESOLVE, ESCALATE], tenant, cust, history, user_msg, resolvable)
    return reply


def history_from_graph(events):
    """Rebuild the chat history from Event nodes, so no state lives in Python."""
    return [
        {"role": "user" if who == "customer" else "assistant", "content": text}
        for who, text in events
        if who != "guardrail" and text
    ]


def chat(tenant, cust, user_msg):
    """Returns (reply, agent_name). Memory is read from the graph on EVERY turn."""

    m.expire_stale_cases(tenant, cust)

    profile = m.recall_profile(tenant, cust)
    events = m.recent_events(tenant, cust)
    history = history_from_graph(events)
    m.log_event(tenant, cust, "customer", user_msg)

    case = m.active_case(tenant, cust)  # routing is a graph query
    if case and case["agent"] == "Billing" and m.billing_turns(tenant, cust, case) >= BILLING_MAX_TURNS:
        m.escalate_case(tenant, cust, case["id"],
                        f"Escalated: Billing did not close the case within {BILLING_MAX_TURNS} replies")
        case = None  # the customer is released back to the Concierge
    if case and case["agent"] == "Billing":
        agent, reply = "Billing", billing_turn(tenant, cust, case, history, user_msg)
    else:
        system = (
            "You are the Concierge agent of a support desk. Be brief and friendly.\n"
            f"Known facts about this customer (from the graph): {json.dumps(profile)}\n"
            f"Company policy for this customer's plan (from the graph): {json.dumps(m.policies_for(tenant, cust))}\n"
            "Answer policy questions only from these policies; if not covered, say you will check with the team.\n"
            "Use these facts instead of asking again. If the customer states or changes a "
            "fact, call remember_fact. For anything about charges, refunds, invoices or "
            "payments, call handoff_to_billing; do not try to solve billing yourself."
        )
        agent = "Concierge"
        reply, handed = run_agent(
            system, [REMEMBER, HANDOFF], tenant, cust, history, user_msg
        )
        if handed:
            agent = "Billing"
            reply = billing_turn(
                tenant, cust, m.active_case(tenant, cust), history, user_msg
            )
    m.log_event(tenant, cust, agent.lower(), reply)
    return reply, agent


if __name__ == "__main__":
    m.seed()
    history = []
    print("Chatting as maya @ tenant_acme. Ctrl+C to quit.")
    while True:
        text = input("You: ")
        if text.lower() in {"quit", "exit"}:
            break
        if not text:
            continue
        reply, agent = chat("tenant_acme", "maya", text)
        print(f"[{agent}]", reply)
