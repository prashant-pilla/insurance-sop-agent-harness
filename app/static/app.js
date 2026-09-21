"use strict";

// Single mutable state object for the whole UI.
const state = {
  sessionId: null,
  transcript: [],   // [{ role: "user"|"agent", text }]
  sop: null,        // last State object from the API
  debug: null,      // last Debug object (or null)
  busy: false,
};

const PHASES = ["VERIFY_ID", "RESOLVE_INTENT", "PROCESS_CASE", "POST_PROCESS", "CLOSED"];

const FACTORS = [
  ["name", "Full name"],
  ["dob", "Date of birth"],
  ["phone", "Phone"],
  ["email", "Email"],
  ["id_last4", "ID last 4"],
];

// Keys from apps/insurance_claims/fixtures/consent_scenarios.json; sent as
// consent_scenario when a new session is created.
const CONSENT_SCENARIOS = ["default", "timeout"];
const CONSENT_STORAGE_KEY = "sop_consent_scenario";

const CONSENT_STATUS_LABELS = {
  pending: "pending",
  approved: "approved",
  timed_out: "timed out",
};

const PRESETS = [
  ["Demo caller", "I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my denied healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472."],
  ["Representative caller", "Hi, this is David Chen. I'm calling on behalf of my mother, Margaret Chen, about her denied healthcare claim from January."],
  ["Angry caller", "I already told you who I am. This is ridiculous. Just tell me why my claim was denied."],
  ["Partial ID", "Hi, this is Margaret Chen, I need help with a claim."],
  ["Off-topic", "What is reinforcement learning?"],
  ["Wrap up", "That's all I needed, thanks."],
  ["Email yes", "Yes, please send me the summary."],
  ["Email no", "No thanks, no email."],
];

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const err = new Error(`${method} ${path} failed: HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

async function loadHealth() {
  try {
    const h = await api("GET", "/healthz");
    $("health").textContent = `provider: ${h.provider} · model: ${h.model}`;
  } catch (e) {
    $("health").textContent = "backend unreachable";
  }
}

// Consent scenario selection (applies to the next new session) -------------

function consentScenario() {
  const v = $("consent-scenario").value;
  return CONSENT_SCENARIOS.includes(v) ? v : "default";
}

function restoreConsentScenario() {
  const saved = sessionStorage.getItem(CONSENT_STORAGE_KEY);
  if (saved && CONSENT_SCENARIOS.includes(saved)) $("consent-scenario").value = saved;
}

async function createSession() {
  const data = await api("POST", "/api/session", { consent_scenario: consentScenario() });
  state.sessionId = data.session_id;
  state.transcript = [{ role: "agent", text: data.reply }];
  state.sop = data.state;
  state.debug = data.debug === undefined ? null : data.debug;
  sessionStorage.setItem("sop_session_id", state.sessionId);
  renderAll();
}

async function restoreSession(id) {
  const data = await api("GET", `/api/session/${encodeURIComponent(id)}`);
  state.sessionId = data.session_id;
  state.transcript = data.transcript || [];
  state.sop = data.state;
  state.debug = data.last_debug === undefined ? null : data.last_debug;
  renderAll();
}

async function sendMessage(text) {
  const message = text.trim();
  if (!message || state.busy || !state.sessionId) return;

  setBusy(true);
  state.transcript.push({ role: "user", text: message });
  renderTranscript();
  $("input").value = "";

  try {
    const data = await api("POST", `/api/session/${encodeURIComponent(state.sessionId)}/message`, { message });
    state.transcript.push({ role: "agent", text: data.reply });
    state.sop = data.state;
    state.debug = data.debug === undefined ? null : data.debug;
    renderAll();
  } catch (e) {
    // Roll back the optimistic user bubble and keep the text for retry.
    state.transcript.pop();
    renderTranscript();
    $("input").value = message;
    showError(`Message failed (${e.message}). Your text is still in the box; try again.`);
  } finally {
    setBusy(false);
    $("input").focus();
  }
}

async function startNewSession() {
  setBusy(true);
  hideError();
  try {
    await createSession();
  } catch (e) {
    showError(`Could not create a session (${e.message}).`);
  } finally {
    setBusy(false);
  }
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function renderAll() {
  renderTranscript();
  renderState(state.sop);
  renderDebug(state.debug);
}

function renderTranscript() {
  const box = $("messages");
  box.replaceChildren(
    ...state.transcript.map((m) => {
      const row = el("div", `msg ${m.role === "user" ? "user" : "agent"}`);
      const bubble = el("div", `bubble ${m.role === "user" ? "user" : "agent"}`, m.text);
      row.appendChild(bubble);
      return row;
    })
  );
  box.scrollTop = box.scrollHeight;
}

function renderState(s) {
  const phase = s ? s.phase : null;

  // Phase stepper
  const activeIdx = PHASES.indexOf(phase);
  $("stepper").replaceChildren(
    ...PHASES.map((p, i) => {
      let cls = "step";
      if (activeIdx >= 0 && i < activeIdx) cls += " done";
      if (p === phase) cls += " active";
      const li = el("li", cls);
      li.appendChild(el("span", "step-dot"));
      li.appendChild(el("span", "step-label", p));
      return li;
    })
  );
  $("handoff-badge").hidden = phase !== "HUMAN_HANDOFF";

  // Identity verification
  const vf = (s && s.verified_factors) || {};
  $("factors").replaceChildren(
    ...FACTORS.map(([key, label]) => {
      const ok = vf[key] === true;
      const li = el("li", `factor ${ok ? "ok" : ""}`);
      li.appendChild(el("span", "factor-mark", ok ? "\u2713" : "\u2013"));
      li.appendChild(el("span", "factor-label", label));
      return li;
    })
  );
  const isRep = !!s && s.caller_role === "representative";
  const repName = (s && s.representative_name) || "representative";
  const vl = $("verified-line");
  if (s && s.verified) {
    vl.textContent = isRep
      ? `Verified as ${s.policyholder_name || "policyholder"} via representative ${repName} (consent approved)`
      : `Verified as ${s.policyholder_name || "policyholder"}`;
    vl.className = "verified-line ok";
  } else {
    vl.textContent = "Not verified";
    vl.className = "verified-line";
  }

  // Representative caller line (hidden for normal callers and older responses).
  const cl = $("caller-line");
  if (isRep) {
    const rel = s.representative_relationship ? ` (${s.representative_relationship})` : "";
    const status = CONSENT_STATUS_LABELS[s.consent_status] || s.consent_status || "none";
    cl.textContent = `Caller: representative ${repName}${rel} - consent: ${status}`;
    cl.className = `caller-line consent-${s.consent_status || "none"}`;
    cl.hidden = false;
  } else {
    cl.textContent = "";
    cl.className = "caller-line";
    cl.hidden = true;
  }
  $("attempts").textContent = `attempts: ${s ? s.verify_attempts : 0}`;

  // Memory
  const mem = (s && s.memory) || {};
  renderChips($("intent-hints"), mem.intent_hints || []);
  renderKv($("case-hints"), mem.case_hints || {});
  const notes = mem.notes || [];
  $("notes").replaceChildren(...notes.map((n) => el("li", "", n)));
  if (!notes.length) $("notes").appendChild(el("li", "muted", "none"));

  // Case
  renderKv($("case"), { active_case_id: s && s.active_case_id, intent_path: s && s.intent_path });

  // Signals
  renderKv($("signals"), {
    last_emotion: s && s.last_emotion,
    off_topic_streak: s ? s.off_topic_streak : 0,
    frustration_streak: s ? s.frustration_streak : 0,
  }, /* keepZeros */ true);

  // Email outbox
  const outbox = (s && s.outbox) || [];
  const ob = $("outbox");
  ob.replaceChildren();
  if (!outbox.length) {
    ob.appendChild(el("div", "muted small", "no emails sent"));
  } else {
    outbox.forEach((m) => {
      const d = document.createElement("details");
      d.className = "mail";
      const sum = document.createElement("summary");
      sum.appendChild(el("span", "mail-subject", m.subject || "(no subject)"));
      sum.appendChild(el("span", "mail-meta", `to ${m.to_masked || "?"} · ${formatTime(m.sent_at)}`));
      d.appendChild(sum);
      d.appendChild(el("pre", "mail-body", m.body || ""));
      ob.appendChild(d);
    });
  }

  // Handoff note
  const note = s && s.handoff_note;
  $("handoff-card").hidden = !note;
  $("handoff-note").textContent = note || "";
}

function renderDebug(d) {
  const box = $("debug");
  box.replaceChildren();
  if (!d) {
    box.appendChild(el("div", "muted small", "tracing disabled"));
    return;
  }

  // Decisions
  box.appendChild(el("h4", "", "Decisions"));
  const decisions = d.decisions || [];
  const ul = el("ul", "debug-list");
  decisions.forEach((x) => ul.appendChild(el("li", "", x)));
  if (!decisions.length) ul.appendChild(el("li", "muted", "none"));
  box.appendChild(ul);

  // Guard flags
  box.appendChild(el("h4", "", "Guard"));
  const g = d.guard || {};
  const flags = el("div", "chips");
  ["checked", "violation", "regenerated", "fallback_used"].forEach((k) => {
    flags.appendChild(el("span", `chip ${g[k] ? "chip-on" : "chip-off"}`, `${k}: ${g[k] ? "yes" : "no"}`));
  });
  box.appendChild(flags);

  // LLM calls table
  box.appendChild(el("h4", "", "LLM calls"));
  const calls = d.llm_calls || [];
  if (!calls.length) {
    box.appendChild(el("div", "muted small", "none"));
  } else {
    const table = document.createElement("table");
    table.className = "debug-table";
    const thead = document.createElement("thead");
    const hr = document.createElement("tr");
    ["role", "latency_ms", "model"].forEach((h) => hr.appendChild(el("th", "", h)));
    thead.appendChild(hr);
    table.appendChild(thead);
    const tbody = document.createElement("tbody");
    calls.forEach((c) => {
      const tr = document.createElement("tr");
      tr.appendChild(el("td", "", str(c.role)));
      tr.appendChild(el("td", "num", str(c.latency_ms)));
      tr.appendChild(el("td", "", str(c.model)));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    box.appendChild(table);
  }

  // Raw JSON blocks
  box.appendChild(el("h4", "", "Extraction"));
  box.appendChild(el("pre", "json", JSON.stringify(d.extraction ?? null, null, 2)));
  box.appendChild(el("h4", "", "Directive"));
  box.appendChild(el("pre", "json", JSON.stringify(d.directive ?? null, null, 2)));
}

// Small rendering helpers ----------------------------------------------------

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function str(v) {
  return v === null || v === undefined ? "–" : String(v);
}

function renderChips(container, items) {
  container.replaceChildren(...items.map((x) => el("span", "chip", x)));
  if (!items.length) container.appendChild(el("span", "muted small", "none"));
}

// Renders key/value pairs, skipping null/undefined (and 0 unless keepZeros).
function renderKv(dl, obj, keepZeros) {
  dl.replaceChildren();
  let count = 0;
  Object.entries(obj).forEach(([k, v]) => {
    if (v === null || v === undefined || v === "") return;
    if (v === 0 && !keepZeros) return;
    dl.appendChild(el("dt", "", k));
    dl.appendChild(el("dd", "", String(v)));
    count++;
  });
  if (!count) dl.appendChild(el("dd", "muted small", "none"));
}

function formatTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? String(iso) : d.toLocaleTimeString();
}

// ---------------------------------------------------------------------------
// UI plumbing
// ---------------------------------------------------------------------------

function setBusy(busy) {
  state.busy = busy;
  $("input").disabled = busy;
  $("send").disabled = busy;
  $("new-session").disabled = busy;
  $("typing").hidden = !busy || !state.sessionId;
  if (busy) $("messages").scrollTop = $("messages").scrollHeight;
}

function showError(text) {
  $("error-text").textContent = text;
  $("error-banner").hidden = false;
}

function hideError() {
  $("error-banner").hidden = true;
}

function bindEvents() {
  $("composer").addEventListener("submit", (e) => {
    e.preventDefault();
    hideError();
    sendMessage($("input").value);
  });

  $("input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!state.busy) $("composer").requestSubmit();
    }
  });

  $("new-session").addEventListener("click", startNewSession);
  $("error-dismiss").addEventListener("click", hideError);

  $("consent-scenario").addEventListener("change", () => {
    sessionStorage.setItem(CONSENT_STORAGE_KEY, consentScenario());
  });

  const presets = $("presets");
  PRESETS.forEach(([label, text]) => {
    const b = el("button", "btn btn-chip", label);
    b.type = "button";
    b.title = text;
    b.addEventListener("click", () => {
      $("input").value = text;
      $("input").focus();
    });
    presets.appendChild(b);
  });
}

async function init() {
  bindEvents();
  restoreConsentScenario();
  renderState(null);
  renderDebug(null);
  loadHealth();

  const saved = sessionStorage.getItem("sop_session_id");
  setBusy(true);
  try {
    if (saved) {
      try {
        await restoreSession(saved);
      } catch (e) {
        if (e.status === 404) {
          sessionStorage.removeItem("sop_session_id");
          await createSession();
        } else {
          throw e;
        }
      }
    } else {
      await createSession();
    }
  } catch (e) {
    showError(`Could not start a session (${e.message}). Click "New session" to retry.`);
  } finally {
    setBusy(false);
    $("input").focus();
  }
}

document.addEventListener("DOMContentLoaded", init);
