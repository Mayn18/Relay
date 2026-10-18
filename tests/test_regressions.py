import time
import pytest
import agents
import memory as m
from conftest import Call, Reply, cases

ACME, MAYA = "tenant_acme", "maya"
HANDOFF = lambda: Reply(calls=[Call("handoff_to_billing", {"summary": "Charged twice", "reason": "billing"})])
RESOLVE = lambda text="Refund in 3 days": Reply(calls=[Call("resolve_case", {"resolution": text})])


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
