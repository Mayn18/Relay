from flask import Flask, request, jsonify, send_from_directory
import threading
import memory as m
import agents
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")

app = Flask(
    __name__, static_folder=os.path.join(WEB_DIR, "static"), static_url_path="/static"
)
TENANTS = {"tenant_acme": "maya", "tenant_bank": "raj"}  # tenant -> demo customer
locks = {}  # (tenant, customer) -> threading.Lock(): one turn at a time per customer


@app.get("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.get("/state")
def state():
    tenant = request.args.get("tenant", "")
    if tenant not in TENANTS:
        return jsonify(error="unknown tenant"), 400

    m.trace_start()
    facts = m.fact_history(tenant, TENANTS[tenant])
    cases = m.case_trail(tenant, TENANTS[tenant])

    return jsonify(facts=facts, cases=cases)


@app.post("/chat")
def chat():
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        return jsonify(error="expected a JSON object"), 400
    tenant = d.get("tenant")

    if not isinstance(tenant, str) or tenant not in TENANTS:  # allowlist, never the LLM
        return jsonify(error="unknown tenant"), 400

    cust = TENANTS[tenant]
    msg = str(d.get("message") or "").strip()[:500]

    if not msg:
        return jsonify(error="empty message"), 400

    m.trace_start()

    with locks.setdefault((tenant, cust), threading.Lock()):
        try:
            reply, agent = agents.chat(tenant, cust, msg)            
        except Exception:
            app.logger.exception("chat failed")
            return jsonify(error="The model provider had a hiccup. Please send that again."), 500
    # Snapshot before panel queries add their own trace entries.
    trace = m.trace_get()
    facts = m.fact_history(tenant, cust)
    cases = m.case_trail(tenant, cust)

    return jsonify(reply=reply, agent=agent, trace=trace, facts=facts, cases=cases)


@app.post("/reset")
def reset():
    # Block cross-site requests (a web page open in your browser could otherwise POST here).
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
        return jsonify(error="forbidden"), 403
    m.reset()
    return jsonify(ok=True)

@app.get("/graph")
def graph():
    tenant = request.args.get("tenant", "")
    if tenant not in TENANTS:
        return jsonify(error="unknown tenant"), 400
    at = request.args.get("at", type=int)
    if at is not None and at < 0:
        return jsonify(error="invalid timestamp"), 400
    g = m.db.select_graph(tenant)
    rows = g.ro_query("""MATCH (n) WHERE NOT 'Event' IN labels(n)
                         RETURN id(n), labels(n)[0], properties(n)""").result_set
    moments, nodes, visible = set(), [], set()
    for nid, kind, p in rows:
        if kind == "Fact":
            moments.update([p.get("since"), p.get("until")])
        elif kind == "Case":
            moments.update([p.get("opened_at"), p.get("resolved_at")])
        born = (
            p.get("since")
            if kind == "Fact"
            else p.get("opened_at") if kind == "Case" else None
        )
        if at is not None and born is not None and born > at:
            continue
        closed = (
            kind == "Fact"
            and p.get("until") is not None
            and (at is None or p["until"] <= at)
        )
        resolved = (
            kind == "Case"
            and p.get("resolved_at") is not None
            and (at is None or p["resolved_at"] <= at)
        )
        if kind == "Customer":
            label = p.get("id", "")
        elif kind == "Fact":
            label = f"{p.get('key')}={p.get('value')}"
        elif kind == "Case":
            label = f"{p.get('topic')} ({p.get('status') if resolved else 'open'})"
        else:
            label = p.get("name") or p.get("title") or kind
        visible.add(nid)
        nodes.append(
            {
                "id": nid,
                "kind": kind,
                "label": label,
                "closed": closed,
                "resolved": resolved,
            }
        )
    erows = g.ro_query("""MATCH (a)-[r]->(b) WHERE NOT 'Event' IN labels(b)
                          RETURN id(r), id(a), id(b), type(r)""").result_set

    edges = [
        {"id": i, "source": a, "target": b, "type": t}
        for i, a, b, t in erows
        if a in visible and b in visible
    ]
    moments.discard(None)
    return jsonify(nodes=nodes, edges=edges, moments=sorted(moments))


if __name__ == "__main__":
    m.seed()
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=5000)
