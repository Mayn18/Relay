import os, json
from dotenv import load_dotenv
from openai import OpenAI
import memory as m

load_dotenv()
client = OpenAI(api_key=os.environ["LLM_API_KEY"], base_url=os.environ["LLM_BASE_URL"])
MODEL = os.environ["LLM_MODEL"]

TOOLS = [{"type": "function", "function": {
    "name": "remember_fact",
    "description": "Save or update a fact about the customer (e.g. key='plan', value='Basic'). "
                  "Call whenever the customer states or changes a fact about themselves.",
    "parameters": {"type": "object",
                   "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                   "required": ["key", "value"]}}}]

def chat(tenant, cust, user_msg, history):
    # Memory is read from the graph on EVERY turn, not kept in Python variables.
    profile = m.recall_profile(tenant, cust)
    events = m.recent_events(tenant, cust)
    system = ("You are the Concierge agent of a support desk. Be brief and friendly.\n"
              f"Known facts about this customer (from the graph): {json.dumps(profile)}\n"
              f"Recent conversation (from the graph): {json.dumps(events)}\n"
              "Use these facts instead of asking again. If the customer states a new fact "
              "or changes one, call remember_fact.")
    msgs = [{"role": "system", "content": system}] + history + \
           [{"role": "user", "content": user_msg}]
    m.log_event(tenant, cust, "customer", user_msg)

    reply = ""
    for _ in range(4):                       # max 4 LLM round-trips per message
        msg = client.chat.completions.create(model=MODEL, messages=msgs, tools=TOOLS) \
                    .choices[0].message
        if not msg.tool_calls:
            reply = msg.content or ""
            break
        msgs.append(msg)
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments)
            m.remember_fact(tenant, cust, args["key"], args["value"])  # tenant/cust come from
            msgs.append({"role": "tool", "tool_call_id": tc.id,        # the session, never the LLM
                         "content": "saved"})
    m.log_event(tenant, cust, "concierge", reply)
    return reply

if __name__ == "__main__":
    m.seed()
    history = []                              # lost on restart ON PURPOSE: the graph is the memory
    print("Chatting as maya @ tenant_acme. Ctrl+C to quit.")
    while True:
        text = input("You: ")
        reply = chat("tenant_acme", "maya", text, history)
        history += [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
        print("Bot:", reply)