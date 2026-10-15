import os, time
from dotenv import load_dotenv
from falkordb import FalkorDB

load_dotenv()
db = FalkorDB(host=os.environ["FALKOR_HOST"],
              port=int(os.environ["FALKOR_PORT"]),
              username=os.environ["FALKOR_USERNAME"],
              password=os.environ["FALKOR_PASSWORD"])
g = db.select_graph("tenant_acme")   # one graph per company

def remember_fact(cust, key, value):
    now = int(time.time())
    # close the old value, if any
    g.query("""MATCH (:Customer {id:$cust})-[:HAS_FACT]->(f:Fact {key:$key})
               WHERE f.until IS NULL SET f.until = $now""",
            {"cust": cust, "key": key, "now": now})
    # add the new one
    g.query("""MERGE (c:Customer {id:$cust})
               CREATE (c)-[:HAS_FACT]->(:Fact {key:$key, value:$value, since:$now})""",
            {"cust": cust, "key": key, "value": value, "now": now})

def recall_profile(cust):
    r = g.ro_query("""MATCH (:Customer {id:$cust})-[:HAS_FACT]->(f:Fact)
                      WHERE f.until IS NULL RETURN f.key, f.value""",
                   {"cust": cust})
    return r.result_set

remember_fact("maya", "plan", "Pro")
remember_fact("maya", "plan", "Basic")   # closes Pro, adds Basic
print(recall_profile("maya"))            # expect [['plan', 'Basic']]