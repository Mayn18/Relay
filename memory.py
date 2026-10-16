import os, time
from dotenv import load_dotenv
from falkordb import FalkorDB

load_dotenv()
db = FalkorDB(
    host=os.environ["FALKOR_HOST"],
    username=os.environ["FALKOR_USERNAME"],
    port=int(os.environ["FALKOR_PORT"]),
    password=os.environ["FALKOR_PASSWORD"],
)

TRACE = []  # every query gets logged here -> becomes the Evidence panel later


def run(tenant, cypher, params=None, write=False):
    params = params or {}
    g = db.select_graph(tenant)  # one graph per company = isolation
    TRACE.append(
        {"tenant": tenant, "cypher": " ".join(cypher.split()), "params": params}
    )
    return (g.query if write else g.ro_query)(cypher, params)


def remember_fact(tenant, cust, key, value):
    now = int(time.time())
    run(
        tenant,
        """MATCH (:Customer {id:$cust})-[:HAS_FACT]->(f:Fact {key:$key})
                   WHERE f.until IS NULL SET f.until = $now""",
        {"cust": cust, "key": key, "now": now},
        write=True,
    )
    run(
        tenant,
        """MERGE (c:Customer {id:$cust})
                   CREATE (c)-[:HAS_FACT]->(:Fact {key:$key, value:$value, since:$now})""",
        {"cust": cust, "key": key, "value": value, "now": now},
        write=True,
    )


def recall_profile(tenant, cust):
    r = run(
        tenant,
        """MATCH (:Customer {id:$cust})-[:HAS_FACT]->(f:Fact)
                       WHERE f.until IS NULL RETURN f.key, f.value""",
        {"cust": cust},
    )
    return {k: v for k, v in r.result_set}


def log_event(tenant, cust, who, text):
    run(
        tenant,
        """MERGE (c:Customer {id:$cust})
                   CREATE (c)-[:HAD_EVENT]->(:Event {who:$who, text:$text, at:$at})""",
        {"cust": cust, "who": who, "text": text, "at": int(time.time() * 1000)},
        write=True,
    )


def recent_events(tenant, cust, n=6):
    r = run(
        tenant,
        f"""MATCH (:Customer {{id:$cust}})-[:HAD_EVENT]->(e:Event)
                        RETURN e.who, e.text ORDER BY e.at DESC LIMIT {int(n)}""",
        {"cust": cust},
    )
    return list(reversed(r.result_set))  # oldest first


def seed():
    """Free Cloud has no persistence, so recreate demo data on every start."""
    for tenant, cust in [("tenant_acme", "maya"), ("tenant_bank", "raj")]:
        run(tenant, "MERGE (:Customer {id:$cust})", {"cust": cust}, write=True)
    if not recall_profile("tenant_acme", "maya"):
        remember_fact("tenant_acme", "maya", "name", "Maya")
        remember_fact("tenant_acme", "maya", "plan", "Pro")
    if not recall_profile(
        "tenant_acme", "dev"
    ):  # past customers the Billing agent can learn from
        for cust, summary, res in [
            (
                "dev",
                "Charged twice for the monthly plan",
                "Refunded the duplicate charge; credit appears on the next invoice",
            ),
            (
                "lena",
                "Duplicate payment after a card update",
                "Duplicate payment refunded within 3 business days",
            ),
        ]:
            remember_fact("tenant_acme", cust, "plan", "Basic")
            run(
                "tenant_acme",
                """MATCH (c:Customer {id:$cust})
                CREATE (c)-[:HAS_CASE]->(:Case {id:$cid, topic:'billing', summary:$s,
                       status:'resolved', resolution:$r, opened_at:$now, resolved_at:$now})""",
                {
                    "cust": cust,
                    "cid": "seed_" + cust,
                    "s": summary,
                    "r": res,
                    "now": int(time.time()),
                },
                write=True,
            )
    TRACE.clear()


def reset():
    """Wipe both graphs and reseed. Handy before recording the demo."""
    for t in ("tenant_acme", "tenant_bank"):
        try:
            db.select_graph(t).delete()
        except Exception:
            pass
    seed()


def handoff(tenant, cust, topic, summary, reason, to_agent):
    now = int(time.time())
    cid = f"case_{cust}_{now}"
    run(
        tenant,
        """MERGE (c:Customer {id:$cust})
                   CREATE (c)-[:HAS_CASE]->(:Case {id:$cid, topic:$topic, summary:$summary,
                          status:'open', opened_at:$now})""",
        {"cust": cust, "cid": cid, "topic": topic, "summary": summary, "now": now},
        write=True,
    )
    run(
        tenant,
        """MATCH (k:Case {id:$cid})
                   MERGE (a:Agent {name:'Concierge'})
                   MERGE (b:Agent {name:$to})
                   CREATE (k)-[:OPENED_BY {at:$now}]->(a)
                   CREATE (k)-[:HANDED_TO {reason:$reason, at:$now}]->(b)""",
        {"cid": cid, "to": to_agent, "reason": reason, "now": now},
        write=True,
    )
    return cid


def active_case(tenant, cust):
    r = run(
        tenant,
        """MATCH (:Customer {id:$cust})-[:HAS_CASE]->(k:Case {status:'open'})-[h:HANDED_TO]->(a:Agent)
                       RETURN k.id, k.topic, k.summary, a.name ORDER BY h.at DESC LIMIT 1""",
        {"cust": cust},
    )
    if not r.result_set:
        return None
    i, t, s, a = r.result_set[0]
    return {"id": i, "topic": t, "summary": s, "agent": a}


def resolve_case(tenant, cust, case_id, resolution):
    run(
        tenant,
        """MATCH (:Customer {id:$cust})-[:HAS_CASE]->(k:Case {id:$cid})
                   SET k.status = 'resolved', k.resolution = $res, k.resolved_at = $now""",
        {"cust": cust, "cid": case_id, "res": resolution, "now": int(time.time())},
        write=True,
    )


def similar_cases(tenant, cust, topic):
    """3 hops: me -> my plan <- other customer -> their resolved case."""
    r = run(
        tenant,
        """MATCH (:Customer {id:$cust})-[:HAS_FACT]->(f:Fact {key:'plan'})
                       WHERE f.until IS NULL
                       MATCH (o:Customer)-[:HAS_FACT]->(g:Fact {key:'plan'})
                       WHERE g.until IS NULL AND g.value = f.value AND o.id <> $cust
                       MATCH (o)-[:HAS_CASE]->(k:Case {topic:$topic, status:'resolved'})
                       RETURN k.summary, k.resolution LIMIT 3""",
        {"cust": cust, "topic": topic},
    )
    return [{"problem": p, "fix": x} for p, x in r.result_set]


def case_trail(tenant, cust):
    r = run(
        tenant,
        """MATCH (:Customer {id:$cust})-[:HAS_CASE]->(k:Case)-[r:OPENED_BY|HANDED_TO]->(a:Agent)
                       RETURN k.id, k.topic, k.status, type(r), a.name, r.reason
                       ORDER BY k.opened_at, r.at""",
        {"cust": cust},
    )
    return [
        {"id": i, "topic": t, "status": s, "step": st, "agent": a, "reason": rs or ""}
        for i, t, s, st, a, rs in r.result_set
    ]


def fact_history(tenant, cust):
    r = run(
        tenant,
        """MATCH (:Customer {id:$cust})-[:HAS_FACT]->(f:Fact)
                       RETURN f.key, f.value, f.since, f.until ORDER BY f.since""",
        {"cust": cust},
    )
    return [
        {"key": k, "value": v, "since": s, "until": u} for k, v, s, u in r.result_set
    ]
