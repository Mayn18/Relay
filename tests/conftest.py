"""Test setup: real Relay code, embedded FalkorDB (falkordblite), scripted fake LLM.
No network, API key or Docker needed."""
import json, os, sys, tempfile, types
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.update(LLM_API_KEY="test", LLM_BASE_URL="http://localhost:9", LLM_MODEL="fake",
                  FALKOR_HOST="x", FALKOR_PORT="1", FALKOR_USERNAME="", FALKOR_PASSWORD="")

import falkordb
from redislite.falkordb_client import FalkorDB as EmbeddedFalkor

_db = EmbeddedFalkor(os.path.join(tempfile.mkdtemp(), "relay-test.db"))
falkordb.FalkorDB = lambda **kw: _db  # memory.py will use the embedded engine

import memory as m  # noqa: E402
import agents  # noqa: E402
import app as webapp  # noqa: E402


class _Fn:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


class Call:
    """A model tool call. `args` may be a dict or a raw (possibly malformed) string."""
    _n = 0

    def __init__(self, name, args):
        Call._n += 1
        self.id, self.type = f"call_{Call._n}", "function"
        self.function = _Fn(name, args if isinstance(args, str) else json.dumps(args))


class Reply:
    def __init__(self, content=None, calls=None):
        self.content, self.tool_calls, self.role = content, calls or None, "assistant"


class FakeLLM:
    """Plays back a script: each step is a Reply, or an Exception to raise. The last step repeats."""
    def __init__(self):
        self.steps, self.calls = [], 0
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

    def script(self, *steps):
        self.steps, self.calls = list(steps), 0

    def _create(self, model, messages, tools=None, **kw):
        self.calls += 1
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, Exception):
            raise step
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=step)])


@pytest.fixture
def llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(agents, "client", fake)
    monkeypatch.setattr(agents.time, "sleep", lambda *_: None)  # no retry back-off in tests
    return fake


@pytest.fixture(autouse=True)
def fresh_graph():
    m.reset()
    yield


@pytest.fixture
def web():
    webapp.app.testing = True
    return webapp.app.test_client()


def cases(tenant="tenant_acme", cust="maya"):
    rs = m.run(tenant, """MATCH (:Customer {id:$c})-[:HAS_CASE]->(k:Case)
                          RETURN k.id, k.status, k.resolution ORDER BY k.opened_at""", {"c": cust})
    return rs.result_set
