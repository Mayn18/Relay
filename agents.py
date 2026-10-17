import os, json
from dotenv import load_dotenv
from openai import OpenAI
import memory as m

load_dotenv()
client = OpenAI(api_key=os.environ["LLM_API_KEY"], base_url=os.environ["LLM_BASE_URL"])
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
BILLING_WORDS = ("charged", "refund", "invoice", "billing", "payment", "billed")

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
    "resolution = one sentence on what was done.",
    {"resolution": S},
    ["resolution"],
)


def run_agent(system, tools, tenant, cust, history, user_msg):
    """Returns (reply, handed_off). tenant and cust come from the session, never the LLM."""
    msgs = (
        [{"role": "system", "content": system}]
        + history
        + [{"role": "user", "content": user_msg}]
    )
    for _ in range(4):
        msg = (
            client.chat.completions.create(model=MODEL, messages=msgs, tools=tools)
            .choices[0]
            .message
        )
        if not msg.tool_calls:
            return msg.content or "", False
        msgs.append(msg)
        handed = False
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments)
            name = tc.function.name
            if name == "remember_fact":
                m.remember_fact(tenant, cust, args["key"], args["value"])
            elif name == "handoff_to_billing":
                m.handoff(
                    tenant, cust, "billing", args["summary"], args["reason"], "Billing"
                )
                handed = True
            elif name == "resolve_case":
                case = m.active_case(
                    tenant, cust
                )  # case id from the graph, not the LLM
                if case:
                    m.resolve_case(tenant, cust, case["id"], args["resolution"])
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": "ok"})
        if handed:
            return "", True
    return "", False


def billing_turn(tenant, cust, case, history, user_msg):
    profile = m.recall_profile(tenant, cust)
    similar = m.similar_cases(tenant, cust, case["topic"])
    system = (
        "You are the Billing agent of a support desk. Be brief and friendly.\n"
        f"Customer facts (from the graph): {json.dumps(profile)}\n"
        f"Open case handed to you by the Concierge: {json.dumps(case)}\n"
        f"Resolved cases of OTHER customers on the same plan (from the graph): {json.dumps(similar)}\n"
        "Do not ask the customer to repeat what is in the case. If a similar case has a fix "
        "that fits, tell the customer you are arranging the same fix, then call resolve_case. "
        "Never promise anything that is not in the similar cases."
    )
    reply, _ = run_agent(system, [REMEMBER, RESOLVE], tenant, cust, history, user_msg)
    still_open = m.active_case(tenant, cust)
    if still_open and similar:  # safety net: close with a fix the graph already proves
        m.resolve_case(tenant, cust, still_open["id"], similar[0]["fix"])
    return reply


def chat(tenant, cust, user_msg, history):
    """Returns (reply, agent_name). Memory is read from the graph on EVERY turn."""
    profile = m.recall_profile(tenant, cust)
    events = m.recent_events(tenant, cust)
    m.log_event(tenant, cust, "customer", user_msg)

    case = m.active_case(tenant, cust)  # routing is a graph query
    if case and case["agent"] == "Billing":
        agent, reply = "Billing", billing_turn(tenant, cust, case, history, user_msg)
    else:
        system = (
            "You are the Concierge agent of a support desk. Be brief and friendly.\n"
            f"Known facts about this customer (from the graph): {json.dumps(profile)}\n"
            f"Recent conversation (from the graph): {json.dumps(events)}\n"
            "Use these facts instead of asking again. If the customer states or changes a "
            "fact, call remember_fact. For anything about charges, refunds, invoices or "
            "payments, call handoff_to_billing; do not try to solve billing yourself."
        )
        agent = "Concierge"
        reply, handed = run_agent(
            system, [REMEMBER, HANDOFF], tenant, cust, history, user_msg
        )
        if (
            not handed
            and not m.active_case(tenant, cust)
            and any(w in user_msg.lower() for w in BILLING_WORDS)
        ):
            m.handoff(
                tenant,
                cust,
                "billing",
                user_msg[:120],
                "Billing keywords in the message (fallback routing)",
                "Billing",
            )
            handed = True
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
        reply, agent = chat("tenant_acme", "maya", text, history)
        history += [
            {"role": "user", "content": text},
            {"role": "assistant", "content": reply},
        ]
        print(f"[{agent}]", reply)
