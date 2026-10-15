from flask import Flask, request, jsonify, send_from_directory
import memory as m
import agents

app = Flask(__name__)
TENANTS = {"tenant_acme": "maya", "tenant_bank": "raj"}   # tenant -> demo customer
histories = {}

@app.get("/")
def index():
    return send_from_directory(".\web", "index.html")

@app.get("/state")
def state():
    tenant = request.args.get("tenant", "")
    if tenant not in TENANTS:
        return jsonify(error="unknown tenant"), 400
    facts = m.fact_history(tenant, TENANTS[tenant])
    m.TRACE.clear()
    return jsonify(facts=facts)

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
        reply = agents.chat(tenant, cust, d["message"], h)
    except Exception as e:                     # e.g. LLM rate limit
        return jsonify(error=str(e)), 500
    h += [{"role": "user", "content": d["message"]},
          {"role": "assistant", "content": reply}]
    trace = list(m.TRACE)                      # copy BEFORE fact_history adds its own query
    facts = m.fact_history(tenant, cust)
    m.TRACE.clear()
    return jsonify(reply=reply, trace=trace, facts=facts)

if __name__ == "__main__":
    m.seed()
    app.run(port=5000)