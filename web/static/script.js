const $ = id => document.getElementById(id);
const fmt = t => t ? new Date(t * 1000).toLocaleString() : "(current)";

function add(cls, text) {
  const d = document.createElement("div");
  d.className = "msg " + cls; d.textContent = text;
  $("log").appendChild(d); $("log").scrollTop = 1e9;
}
function showFacts(facts) {
  const t = document.createElement("table");
  t.innerHTML = "<tr><th>Fact</th><th>Value</th><th>Since</th><th>Until</th></tr>";
  facts.forEach(f => {
    const r = t.insertRow();
    [f.key, f.value, fmt(f.since), fmt(f.until)].forEach(x => r.insertCell().textContent = x);
    if (f.until) r.style.color = "#999";
  });
  $("facts").replaceChildren(t);
}
function showTrace(trace) {
  $("trace").replaceChildren();
  trace.forEach(q => {
    const d = document.createElement("div"); d.className = "q";
    d.textContent = `[${q.tenant}]\n${q.cypher}\nparams: ${JSON.stringify(q.params)}`;
    $("trace").appendChild(d);
  });
}
let busy = false;  // true while a chat request is in flight

async function getJSON(url, opts) {
  // Always resolves to an object; failures become {error: "..."} instead of throwing.
  try {
    const r = await fetch(url, opts);
    try { return await r.json(); }
    catch (e) { return { error: "Server error (" + r.status + "). Please try again." }; }
  } catch (e) {
    return { error: "Could not reach the server. Please try again." };
  }
}

async function refreshPanels(tenant) {
  const d = await getJSON("/state?tenant=" + tenant);
  if (d.error || tenant !== $("tenant").value) return;  // ignore replies for a company we left
  showFacts(d.facts);
  showCases(d.cases);
  await refreshGraph();
}

async function switchTenant() {
  $("log").replaceChildren();
  $("trace").textContent = "Send a message to see them.";
  if (cy) cy.elements().remove();
  moments = [];
  $("tt").max = 0;
  $("tt").value = 0;
  await refreshPanels($("tenant").value);
}

async function send() {
  const text = $("box").value.trim();
  if (!text || busy) return;           // ignore empty input and double-submits
  busy = true;
  $("sendBtn").disabled = true;
  const tenant = $("tenant").value;
  $("box").value = "";
  add("me", text);
  try {
    const d = await getJSON("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tenant: tenant, message: text })
    });
    if ($("tenant").value !== tenant) return;  // user switched company while waiting
    if (d.error) {
      add("bot", "Error: " + d.error);
      await refreshPanels(tenant);             // the graph may have changed before the failure
      return;
    }
    add("bot", "[" + d.agent + "] " + d.reply);
    showTrace(d.trace);
    showFacts(d.facts);
    showCases(d.cases);
    $("tt").value = $("tt").max;
    await refreshGraph();
  } finally {
    busy = false;
    $("sendBtn").disabled = false;
  }
}

function showCases(cases) {
  const t = document.createElement("table");
  t.innerHTML = "<tr><th>Case</th><th>Step</th><th>Agent</th><th>Why</th></tr>";
  cases.forEach(c => {
    const r = t.insertRow();
    [`${c.topic} (${c.status})`, c.step, c.agent, c.reason].forEach(x => r.insertCell().textContent = x);
  });
  $("cases").replaceChildren(t);
}

let cy = null;
const COLORS = {
  Customer: "#fecaca",
  Fact: "#bfdbfe",
  Case: "#fde68a",
  Agent: "#ddd6fe",
  Article: "#fdba74",
  Plan: "#99f6e4",
  Topic: "#fbcfe8"
};

const KB = new Set(["Article", "Plan", "Topic"]);

let moments = [];

function atParam() {
  const t = $("tt");

  // Slider at the right end = live graph
  if (!moments.length || +t.value >= +t.max) return null;

  return moments[+t.value];
}

async function refreshGraph() {
  const at = atParam();

  const tenant = $("tenant").value;
  const d = await getJSON("/graph?tenant=" + tenant +
    (at !== null ? "&at=" + at : "")
  );
  if (d.error || tenant !== $("tenant").value) return;  // drop a reply for a company we left

  moments = Array.isArray(d.moments) ? d.moments : [];

  const t = $("tt");
  t.max = Math.max(moments.length - 1, 0);

  if (at === null) t.value = t.max;

  $("ttLabel").textContent =
    at === null
      ? "Now"
      : new Date(at * 1000).toLocaleTimeString();

  const nodes = d.nodes.filter(
    n => $("kb").checked || !KB.has(n.kind)
  );

  const ids = new Set(nodes.map(n => n.id));

  const edges = d.edges.filter(
    e => ids.has(e.source) && ids.has(e.target)
  );

  if (!cy) {
    cy = cytoscape({
      container: $("graph"),
      style: [
        {
          selector: "node", style: {
            "background-color": "data(color)", "label": "data(label)", "font-size": "10px",
            "text-valign": "center", "text-wrap": "wrap", "text-max-width": "70px",
            "shape": "round-rectangle", "width": "label", "height": "label",
            "padding": "8px", "opacity": "data(opacity)"
          }
        },
        {
          selector: "edge", style: {
            "curve-style": "bezier", "target-arrow-shape": "triangle", "label": "data(label)",
            "font-size": "8px", "line-color": "#999", "target-arrow-color": "#999", "width": 1.5
          }
        }
      ]
    });
  }
  const keep = new Set(), added = [];
  nodes.forEach(n => {
    const id = "n" + n.id; keep.add(id);
    const data = {
      id, label: n.label,
      color: n.resolved ? "#bbf7d0" : n.closed ? "#e5e7eb" : (COLORS[n.kind] || "#eee"),
      opacity: n.closed ? 0.5 : 1
    };
    const ex = cy.getElementById(id);
    if (ex.nonempty()) ex.data(data); else added.push({ data });
  });
  edges.forEach(e => {
    const id = "e" + e.id; keep.add(id);
    if (cy.getElementById(id).empty())
      added.push({ data: { id, source: "n" + e.source, target: "n" + e.target, label: e.type } });
  });
  cy.elements().filter(el => !keep.has(el.id())).remove();
  if (added.length) cy.add(added);
  cy.layout({ name: "cose", animate: true, randomize: false, fit: true, padding: 20 }).run();
}

async function resetDemo() {
  if (!confirm("Reset the demo data for both companies?")) return;
  try {
    const response = await fetch("/reset", { method: "POST" });

    if (!response.ok) {
      alert("Reset failed. Please check the server.");
      return;
    }

    $("box").value = "";
    await switchTenant();
  } catch (err) {
    console.error("Reset failed:", err);
    alert("Could not reset the demo. Check that the server is running.");
  }
}

switchTenant();