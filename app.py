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

@app.get("/graph")
def graph():
    tenant = request.args.get("tenant", "")
    if tenant not in TENANTS:
        return jsonify(error="unknown tenant"), 400
    g = m.db.select_graph(tenant)
    rows = g.ro_query("""MATCH (n) WHERE NOT 'Event' IN labels(n)
                         RETURN id(n), labels(n)[0], properties(n)""").result_set
    nodes = []
    for nid, kind, p in rows:
        if kind == "Customer":
            label = p.get("id", "")
        elif kind == "Fact":
            label = f"{p.get('key')}={p.get('value')}"
        elif kind == "Case":
            label = f"{p.get('topic')} ({p.get('status')})"
        else:
            label = p.get("name", kind)
        nodes.append({"id": nid, "kind": kind, "label": label,
                      "closed": kind == "Fact" and p.get("until") is not None,
                      "resolved": kind == "Case" and p.get("status") == "resolved"})
    erows = g.ro_query("""MATCH (a)-[r]->(b) WHERE NOT 'Event' IN labels(b)
                          RETURN id(r), id(a), id(b), type(r)""").result_set
    edges = [{"id": i, "source": a, "target": b, "type": t} for i, a, b, t in erows]
    return jsonify(nodes=nodes, edges=edges)

if __name__ == "__main__":
    m.seed()
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=5000)