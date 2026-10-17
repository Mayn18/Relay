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

switchTenant();