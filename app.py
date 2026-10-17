from flask import Flask, request, jsonify, send_from_directory
import memory as m
import agents
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")

app = Flask(
     __name__,
    static_folder=os.path.join(WEB_DIR, "static"),
    static_url_path="/static"
)
TENANTS = {"tenant_acme": "maya", "tenant_bank": "raj"}   # tenant -> demo customer
histories = {}

@app.get("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")

@app.get("/state")
def state():
    tenant = request.args.get("tenant", "")
    if tenant not in TENANTS:
        return jsonify(error="unknown tenant"), 400
    facts = m.fact_history(tenant, TENANTS[tenant])
    cases = m.case_trail(tenant, TENANTS[tenant])
    m.TRACE.clear()
    return jsonify(facts=facts, cases=cases)

@app.post("/chat")
def chat():
    d = request.get_json()
    tenant = d.get("tenant", "")
    if tenant not in TENANTS:                  # tenant comes from our allowlist, never the LLM
        return jsonify(error="unknown tenant"), 400
    cust = TENANTS[tenant]
    m.TRACE.clear()
    h = histories.setdefault(tenant, [])
    try:
        reply, agent = agents.chat(tenant, cust, d["message"], h)
    except Exception as e:
        return jsonify(error=str(e)), 500
    h += [{"role": "user", "content": d["message"]},
          {"role": "assistant", "content": reply}]
    trace = list(m.TRACE)                      # copy BEFORE the panel queries add their own
    facts = m.fact_history(tenant, cust)
    cases = m.case_trail(tenant, cust)
    m.TRACE.clear()
    return jsonify(reply=reply, agent=agent, trace=trace, facts=facts, cases=cases)

if __name__ == "__main__":
    m.seed()
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=5000)