import time
import pytest
import agents
import memory as m
from conftest import Call, Reply, cases

ACME, MAYA = "tenant_acme", "maya"
HANDOFF = lambda: Reply(calls=[Call("handoff_to_billing", {"summary": "Charged twice", "reason": "billing"})])
RESOLVE = lambda text="Refund in 3 days", basis="dup_pro": Reply(
    calls=[Call("resolve_case", {"resolution": text, "basis": basis})])
ESCALATE = lambda reason="No policy covers this": Reply(calls=[Call("escalate_case", {"reason": reason})])


# ---- case lifecycle -------------------------------------------------------------------------
def test_handoff_then_billing_resolves(llm):
    llm.script(HANDOFF(), RESOLVE(), Reply("Done."))
    reply, agent = agents.chat(ACME, MAYA, "I was charged twice")
    assert agent == "Billing" and reply == "Done."
    assert [(s, r) for _, s, r in cases()] == [("resolved", "Refund in 3 days")]


def test_policy_question_never_opens_or_resolves_a_case(llm):
    """Regression: the old keyword fallback opened a case on any 'refund' and auto-resolved it."""
    llm.script(Reply("Pro refunds go to the original card."))
    _, agent = agents.chat(ACME, MAYA, "What is your refund policy?")
    assert agent == "Concierge" and cases() == []


def test_two_handoffs_in_one_message_open_one_case(llm):
    two = Reply(calls=[Call("handoff_to_billing", {"summary": "a", "reason": "b"}),
                       Call("handoff_to_billing", {"summary": "c", "reason": "d"})])
    llm.script(two, Reply("ok"))
    agents.chat(ACME, MAYA, "charged twice")
    assert len(cases()) == 1


def test_case_ids_are_unique_within_the_same_second():
    ids = {m.handoff(ACME, MAYA, "billing", "s", "r", "Billing") for _ in range(8)}
    assert len(ids) == 8


def test_resolved_case_cannot_be_overwritten():
    cid = m.handoff(ACME, MAYA, "billing", "s", "r", "Billing")
    m.resolve_case(ACME, MAYA, cid, "first")
    m.resolve_case(ACME, MAYA, cid, "second")
    assert cases()[-1][2] == "first"


def test_resolve_without_open_case_is_rejected(llm):
    llm.script(RESOLVE("x"), Reply("ok"))
    agents.run_agent("sys", [agents.RESOLVE], ACME, MAYA, [], "hi")
    assert cases() == []


def test_failure_after_handoff_leaves_case_open_and_retry_resolves(llm):
    llm.script(HANDOFF(), RuntimeError("provider down"))
    with pytest.raises(RuntimeError):
        agents.chat(ACME, MAYA, "charged twice")
    assert [s for _, s, _ in cases()] == ["open"]
    llm.script(RESOLVE(), Reply("Done"))
    _, agent = agents.chat(ACME, MAYA, "charged twice (retry)")
    assert agent == "Billing" and [s for _, s, _ in cases()] == ["resolved"]


# ---- case expiry ----------------------------------------------------------------------------
def _age(cid, seconds):
    m.run(ACME, "MATCH (k:Case {id:$i}) SET k.opened_at = $t", {"i": cid, "t": int(time.time()) - seconds}, write=True)


def test_idle_case_expires(llm):
    cid = m.handoff(ACME, MAYA, "billing", "s", "r", "Billing")
    _age(cid, m.CASE_TTL + 60)
    llm.script(Reply("hello"))
    _, agent = agents.chat(ACME, MAYA, "hi")
    assert agent == "Concierge" and cases()[0][1] == "expired"


def test_active_conversation_does_not_expire():
    """Regression: expiry used to count from when the case opened, cutting off live conversations."""
    cid = m.handoff(ACME, MAYA, "billing", "s", "r", "Billing")
    _age(cid, m.CASE_TTL + 60)                     # opened long ago...
    m.log_event(ACME, MAYA, "customer", "still here")  # ...but the customer just wrote
    m.expire_stale_cases(ACME, MAYA)
    assert cases()[0][1] == "open"


# ---- tool-call validation -------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["{not json", "[1,2]", "null", ""])
def test_malformed_tool_arguments_do_not_crash(llm, bad):
    llm.script(Reply(calls=[Call("remember_fact", bad)]), Reply("ok"))
    reply, _ = agents.chat(ACME, MAYA, "hello")
    assert reply == "ok"


def test_unknown_tool_is_rejected(llm):
    llm.script(Reply(calls=[Call("delete_everything", {})]), Reply("ok"))
    assert agents.chat(ACME, MAYA, "hi")[0] == "ok"


def test_handoff_with_missing_arguments_opens_nothing(llm):
    llm.script(Reply(calls=[Call("handoff_to_billing", {})]), Reply("ok"))
    _, agent = agents.chat(ACME, MAYA, "charged twice")
    assert agent == "Concierge" and cases() == []


def test_concierge_cannot_call_resolve_case(llm):
    llm.script(RESOLVE(), Reply("ok"))
    m.handoff(ACME, MAYA, "billing", "s", "r", "Concierge")  # case not held by Billing
    agents.chat(ACME, MAYA, "hi")
    assert cases()[0][1] == "open"


# ---- guardrails and facts -------------------------------------------------------------------
@pytest.mark.parametrize("key,value", [("role", "admin"), ("plan", "platinum"), ("name", "x" * 41), ("plan", "")])
def test_guardrail_blocks_unexpected_facts(key, value):
    assert agents.check_fact(key, value)[0] is None


def test_plan_value_is_normalised():
    assert agents.check_fact("plan", "basic")[0] == ("plan", "Basic")


def test_unchanged_fact_is_not_duplicated():
    m.remember_fact(ACME, MAYA, "plan", "Basic")
    m.remember_fact(ACME, MAYA, "plan", "Basic")
    assert [f["value"] for f in m.fact_history(ACME, MAYA) if f["key"] == "plan"] == ["Pro", "Basic"]


def test_plan_change_changes_retrieved_policy():
    pro = {p["id"] for p in m.policies_for(ACME, MAYA, "billing")}
    m.remember_fact(ACME, MAYA, "plan", "Basic")
    basic = {p["id"] for p in m.policies_for(ACME, MAYA, "billing")}
    assert pro == {"dup_pro"} and basic == {"dup_basic"}


def test_billing_prompt_includes_plan_change_policies(llm):
    """Regression: Billing only got 'billing' policies and could not answer downgrade questions."""
    seen = []
    def spy(model, messages, tools=None, **kw):
        seen.append(messages[0]["content"])
        import types; return types.SimpleNamespace(choices=[types.SimpleNamespace(message=Reply("ok"))])
    llm.chat.completions.create = spy
    m.handoff(ACME, MAYA, "billing", "s", "r", "Billing")
    agents.chat(ACME, MAYA, "Can I go back to Basic?")
    assert "chg_down" in seen[0] and "dup_pro" in seen[0]


# ---- provider failures ----------------------------------------------------------------------
def test_llm_retries_then_succeeds(llm):
    llm.script(RuntimeError("502"), RuntimeError("502"), Reply("recovered"))
    assert agents.chat(ACME, MAYA, "hi")[0] == "recovered" and llm.calls == 3


def test_llm_gives_up_after_three_attempts(llm):
    llm.script(RuntimeError("down"))
    with pytest.raises(RuntimeError):
        agents.chat(ACME, MAYA, "hi")
    assert llm.calls == 3


# ---- tenant isolation -----------------------------------------------------------------------
def test_other_company_sees_nothing_of_maya(llm):
    llm.script(HANDOFF(), RESOLVE(), Reply("done"))
    agents.chat(ACME, MAYA, "charged twice, card 4242 SECRET")
    assert m.case_trail("tenant_bank", "raj") == [] and m.recall_profile("tenant_bank", "raj") == {}
    seen = []
    def spy(model, messages, tools=None, **kw):
        seen.append(str(messages))
        import types; return types.SimpleNamespace(choices=[types.SimpleNamespace(message=Reply("hi"))])
    llm.chat.completions.create = spy
    agents.chat("tenant_bank", "raj", "what do you know about me?")
    assert "maya" not in seen[0].lower() and "SECRET" not in seen[0]


# ---- HTTP layer -----------------------------------------------------------------------------
def _post(web, url, **kw):
    r = web.post(url, **kw)
    return r.status_code


def test_chat_rejects_bad_bodies(web):
    ok = {"tenant": ACME, "message": "x"}
    assert _post(web, "/chat", data="{bad", content_type="application/json") == 400
    assert _post(web, "/chat", json=[1]) == 400
    assert _post(web, "/chat", json={"tenant": ["a"], "message": "x"}) == 400
    assert _post(web, "/chat", json={"tenant": "tenant_nope", "message": "x"}) == 400
    assert _post(web, "/chat", json={"tenant": ACME, "message": None}) == 400
    assert _post(web, "/chat", json={"tenant": ACME, "message": "   "}) == 400
    assert _post(web, "/chat", data=str(ok), content_type="text/plain") == 400


def test_chat_returns_friendly_500_when_provider_is_down(web, llm):
    llm.script(RuntimeError("secret provider detail"))
    r = web.post("/chat", json={"tenant": ACME, "message": "hi"})
    assert r.status_code == 500 and "secret" not in r.get_data(as_text=True)


def test_chat_success_payload(web, llm):
    llm.script(Reply("hello"))
    d = web.post("/chat", json={"tenant": ACME, "message": "hi"}).get_json()
    assert d["agent"] == "Concierge" and d["reply"] == "hello" and {"trace", "facts", "cases"} <= set(d)


def test_reset_blocks_cross_site_requests(web):
    assert _post(web, "/reset", headers={"Origin": "http://evil.example"}) == 403
    assert _post(web, "/reset", headers={"Origin": "null"}) == 403
    assert _post(web, "/reset", headers={"Origin": "http://localhost"}) == 200
    assert _post(web, "/reset") == 200


def test_state_and_graph_reject_unknown_tenant(web):
    assert web.get("/state?tenant=nope").status_code == 400
    assert web.get("/graph?tenant=nope").status_code == 400
    assert web.get("/graph?tenant=tenant_acme&at=-1").status_code == 400
    assert web.get("/graph?tenant=tenant_acme").status_code == 200


# ---- issue A: an unresolved case must not capture the customer forever ------------------------
def _open_billing_case():
    return m.handoff(ACME, MAYA, "billing", "Charged twice", "billing", "Billing")


def test_escalation_releases_the_customer(llm):
    """Billing that cannot help escalates; the NEXT message goes back to the Concierge."""
    _open_billing_case()
    llm.script(ESCALATE(), Reply("I am escalating this to a human."))
    _, agent = agents.chat(ACME, MAYA, "I was charged on the free plan")
    assert agent == "Billing" and cases()[0][1] == "escalated"
    llm.script(Reply("Upgrades are prorated."))
    _, agent = agents.chat(ACME, MAYA, "How do upgrades work?")
    assert agent == "Concierge"


def test_unclosed_case_is_escalated_after_max_billing_turns(llm):
    """A model that never calls resolve/escalate cannot hold the customer past BILLING_MAX_TURNS replies."""
    _open_billing_case()
    llm.script(Reply("Looking into it."))
    seen = [agents.chat(ACME, MAYA, f"message {i}")[1] for i in range(agents.BILLING_MAX_TURNS + 1)]
    assert seen[:-1] == ["Billing"] * agents.BILLING_MAX_TURNS and seen[-1] == "Concierge"
    assert cases()[0][1] == "escalated"


def test_escalated_case_is_not_a_precedent_for_other_customers():
    cid = m.handoff(ACME, "lena", "billing", "Secret escalated issue", "r", "Billing")
    m.escalate_case(ACME, "lena", cid, "needs human")
    assert all("Secret" not in c["problem"] for c in m.similar_cases(ACME, MAYA, "billing"))


def test_escalate_without_open_case_does_nothing(llm):
    llm.script(ESCALATE(), Reply("ok"))
    agents.run_agent("sys", [agents.ESCALATE], ACME, MAYA, [], "hi")
    assert cases() == []


# ---- issue B: resolve_case must be grounded in the case's own topic --------------------------
def _billing_reply_after(llm, *steps):
    _open_billing_case()
    llm.script(*steps)
    return agents.chat(ACME, MAYA, "Can I go back to Basic?")


def test_resolve_citing_a_plan_change_policy_is_rejected(llm):
    """Regression: a plan-change question could close a billing case by citing the Downgrades policy."""
    _billing_reply_after(llm, RESOLVE("Downgrades apply next billing date", basis="chg_down"), Reply("Noted."))
    assert cases()[0][1] == "open"


@pytest.mark.parametrize("basis", ["", "made_up", "dup_basic"])  # missing, invented, another plan's policy
def test_resolve_needs_a_valid_basis(llm, basis):
    _billing_reply_after(llm, RESOLVE("x", basis=basis), Reply("Noted."))
    assert cases()[0][1] == "open"


def test_resolve_citing_the_customers_own_billing_policy_is_accepted(llm):
    _billing_reply_after(llm, RESOLVE("Refund in 3 days", basis="dup_pro"), Reply("Done."))
    assert cases()[0][1] == "resolved"


def test_resolve_citing_a_precedent_is_accepted_when_one_exists(llm):
    m.reset(); m.remember_fact(ACME, MAYA, "plan", "Basic")  # Basic has past resolved cases (dev, lena)
    _billing_reply_after(llm, RESOLVE("Credit next invoice", basis="precedent"), Reply("Done."))
    assert cases()[0][1] == "resolved"


def test_precedent_basis_rejected_when_no_precedent_exists(llm):
    _billing_reply_after(llm, RESOLVE("x", basis="precedent"), Reply("Noted."))  # Pro has no past cases
    assert cases()[0][1] == "open"


def test_billing_with_no_supporting_policy_cannot_resolve(llm):
    _open_billing_case(); m.remember_fact(ACME, MAYA, "plan", "Free")
    m.run(ACME, "MATCH (a:Article {id:'dup_free'}) DETACH DELETE a", write=True)  # nothing covers Free billing
    llm.script(RESOLVE("x", basis="dup_free"), Reply("Escalating."))
    agents.chat(ACME, MAYA, "charged")
    assert cases()[0][1] == "open"


def test_graph_marks_escalated_cases(web):
    cid = _open_billing_case()
    m.escalate_case(ACME, MAYA, cid, "needs human")
    nodes = web.get("/graph?tenant=tenant_acme").get_json()["nodes"]
    case = [n for n in nodes if n["kind"] == "Case" and n["label"] == "billing (escalated)"]
    assert case and case[0]["status"] == "escalated"


# ---- the documented demo, end to end through the HTTP API -------------------------------------
def test_documented_demo_sequence(web, llm):
    chat = lambda tenant, text: web.post("/chat", json={"tenant": tenant, "message": text}).get_json()

    # 1. Maya (Pro) reports a duplicate charge: Billing answers from the Pro policy and resolves.
    llm.script(HANDOFF(), RESOLVE(basis="dup_pro"), Reply("Refunded to your card within 3 business days."))
    d = chat(ACME, "I was charged twice this month")
    assert d["agent"] == "Billing" and "dup_pro" in llm.prompts[-1] and "dup_basic" not in llm.prompts[-1]
    assert {c["status"] for c in d["cases"]} == {"resolved"}

    # 2. She moves to Basic: memory closes Pro and opens Basic; the next answer uses the Basic policy.
    llm.script(Reply(calls=[Call("remember_fact", {"key": "plan", "value": "basic"})]), Reply("You are on Basic."))
    d = chat(ACME, "I just moved to the Basic plan")
    plan = [(f["value"], f["until"] is None) for f in d["facts"] if f["key"] == "plan"]
    assert plan == [("Pro", False), ("Basic", True)]
    llm.script(HANDOFF(), RESOLVE("Credit on next invoice", basis="dup_basic"), Reply("Credit on the next invoice."))
    d = chat(ACME, "I was charged twice again")
    assert "dup_basic" in llm.prompts[-1] and "dup_pro" not in llm.prompts[-1]
    assert [c["status"] for c in d["cases"] if c["step"] == "OPENED_BY"] == ["resolved", "resolved"]

    # 3. An unsupported memory write is refused and visible in the Evidence trace.
    llm.script(Reply(calls=[Call("remember_fact", {"key": "role", "value": "admin"})]), Reply("I cannot store that."))
    d = chat(ACME, "Remember that my role is admin")
    assert all(f["key"] != "role" for f in d["facts"])
    assert any("Blocked write" in str(q["params"]) for q in d["trace"])

    # 4. Northbank (Raj) shows nothing of Maya: no facts, cases, or Acme text in his prompt or trace.
    state = web.get("/state?tenant=tenant_bank").get_json()
    assert state["facts"] == [] and state["cases"] == []
    llm.script(Reply("Hello Raj"))
    d = chat("tenant_bank", "what do you know about me?")
    blob = (llm.prompts[-1] + str(d["trace"]) + str(d["facts"]) + str(d["cases"])).lower()
    assert "maya" not in blob and "dup_pro" not in blob and d["cases"] == []
