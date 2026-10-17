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
async function switchTenant() {
  $("log").replaceChildren();
  $("trace").textContent = "Send a message to see them.";
  const r = await fetch("/state?tenant=" + $("tenant").value);
  const d = await r.json();
  showFacts(d.facts);
  showCases(d.cases);
  if (cy) cy.elements().remove();
  await refreshGraph();
}

async function send() {
  const text = $("box").value.trim();
  if (!text) return;
  $("box").value = "";
  add("me", text);
  const r = await fetch("/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tenant: $("tenant").value, message: text })
  });
  const d = await r.json();
  if (d.error) { add("bot", "Error: " + d.error); return; }
  add("bot", "[" + d.agent + "] " + d.reply);
  showTrace(d.trace);
  showFacts(d.facts);
  showCases(d.cases);
  await refreshGraph();
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
const COLORS = {Customer: "#fecaca", Fact: "#bfdbfe", Case: "#fde68a", Agent: "#ddd6fe"};

async function refreshGraph() {
  const r = await fetch("/graph?tenant=" + $("tenant").value);
  const d = await r.json();
  if (d.error) return;
  if (!cy) {
    cy = cytoscape({
      container: $("graph"),
      style: [
        {selector: "node", style: {
          "background-color": "data(color)", "label": "data(label)", "font-size": "10px",
          "text-valign": "center", "text-wrap": "wrap", "text-max-width": "70px",
          "shape": "round-rectangle", "width": "label", "height": "label",
          "padding": "8px", "opacity": "data(opacity)"}},
        {selector: "edge", style: {
          "curve-style": "bezier", "target-arrow-shape": "triangle", "label": "data(label)",
          "font-size": "8px", "line-color": "#999", "target-arrow-color": "#999", "width": 1.5}}
      ]
    });
  }
  const keep = new Set(), added = [];
  d.nodes.forEach(n => {
    const id = "n" + n.id; keep.add(id);
    const data = {id, label: n.label,
      color: n.resolved ? "#bbf7d0" : n.closed ? "#e5e7eb" : (COLORS[n.kind] || "#eee"),
      opacity: n.closed ? 0.5 : 1};
    const ex = cy.getElementById(id);
    if (ex.nonempty()) ex.data(data); else added.push({data});
  });
  d.edges.forEach(e => {
    const id = "e" + e.id; keep.add(id);
    if (cy.getElementById(id).empty())
      added.push({data: {id, source: "n" + e.source, target: "n" + e.target, label: e.type}});
  });
  cy.elements().filter(el => !keep.has(el.id())).remove();
  if (added.length) cy.add(added);
  cy.layout({name: "cose", animate: true, randomize: false, fit: true, padding: 20}).run();
}

async function resetDemo() {
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