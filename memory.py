import os, time
from dotenv import load_dotenv
from falkordb import FalkorDB

load_dotenv()
db = FalkorDB(host=os.environ["FALKOR_HOST"],
              port=int(os.environ["FALKOR_PORT"]),
              password=os.environ["FALKOR_PASSWORD"])

TRACE = []   # every query gets logged here -> becomes the Evidence panel later

def run(tenant, cypher, params=None, write=False):
    params = params or {}
    g = db.select_graph(tenant)          # one graph per company = isolation
    TRACE.append({"tenant": tenant, "cypher": " ".join(cypher.split()), "params": params})
    return (g.query if write else g.ro_query)(cypher, params)

def remember_fact(tenant, cust, key, value):
    now = int(time.time())
    run(tenant, """MATCH (:Customer {id:$cust})-[:HAS_FACT]->(f:Fact {key:$key})
                   WHERE f.until IS NULL SET f.until = $now""",
        {"cust": cust, "key": key, "now": now}, write=True)
    run(tenant, """MERGE (c:Customer {id:$cust})
                   CREATE (c)-[:HAS_FACT]->(:Fact {key:$key, value:$value, since:$now})""",
        {"cust": cust, "key": key, "value": value, "now": now}, write=True)

def recall_profile(tenant, cust):
    r = run(tenant, """MATCH (:Customer {id:$cust})-[:HAS_FACT]->(f:Fact)
                       WHERE f.until IS NULL RETURN f.key, f.value""", {"cust": cust})
    return {k: v for k, v in r.result_set}

def log_event(tenant, cust, who, text):
    run(tenant, """MERGE (c:Customer {id:$cust})
                   CREATE (c)-[:HAD_EVENT]->(:Event {who:$who, text:$text, at:$at})""",
        {"cust": cust, "who": who, "text": text, "at": int(time.time() * 1000)}, write=True)

def recent_events(tenant, cust, n=6):
    r = run(tenant, f"""MATCH (:Customer {{id:$cust}})-[:HAD_EVENT]->(e:Event)
                        RETURN e.who, e.text ORDER BY e.at DESC LIMIT {int(n)}""",
            {"cust": cust})
    return list(reversed(r.result_set))   # oldest first

def seed():
    """Free Cloud has no persistence, so recreate demo data on every start."""
    for tenant, cust in [("tenant_acme", "maya"), ("tenant_bank", "raj")]:
        run(tenant, "MERGE (:Customer {id:$cust})", {"cust": cust}, write=True)
    if not recall_profile("tenant_acme", "maya"):
        remember_fact("tenant_acme", "maya", "name", "Maya")
        remember_fact("tenant_acme", "maya", "plan", "Pro")
    TRACE.clear()