const FACETS = {
  status: [
    "ACTIVE_NOT_RECRUITING", "COMPLETED", "ENROLLING_BY_INVITATION",
    "NOT_YET_RECRUITING", "RECRUITING", "SUSPENDED", "TERMINATED",
    "WITHDRAWN", "AVAILABLE", "NO_LONGER_AVAILABLE",
    "TEMPORARILY_NOT_AVAILABLE", "APPROVED_FOR_MARKETING", "WITHHELD", "UNKNOWN",
  ],
  study_type: ["INTERVENTIONAL", "OBSERVATIONAL", "EXPANDED_ACCESS"],
  phase: ["EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4", "NA"],
  sponsor_class: [
    "NIH", "FED", "INDIV", "INDUSTRY", "NETWORK", "AMBIG", "OTHER", "OTHER_GOV", "UNKNOWN",
  ],
  sex: ["ALL", "MALE", "FEMALE"],
};

const SEARCH_COLUMNS = [
  { key: "nct_id", label: "NCT ID" },
  { key: "brief_title", label: "Title" },
  { key: "overall_status", label: "Status", format: (v) => `<span class="badge">${escapeHtml(v)}</span>`, raw: true },
  { key: "phases", label: "Phase", format: (v) => (v || []).join(", ") },
  { key: "study_type", label: "Study type" },
  { key: "start_date", label: "Start date" },
  { key: "lead_sponsor", label: "Sponsor" },
  { key: "lead_sponsor_class", label: "Sponsor class" },
  { key: "enrollment_count", label: "Enrollment" },
  { key: "sex", label: "Sex" },
  { key: "has_results", label: "Has results", format: (v) => (v ? "Yes" : "No") },
];

// Prepended to SEARCH_COLUMNS once AI results exist, so eligibility shows
// as the leftmost column of the existing results table instead of a
// separate table.
const ELIGIBILITY_COLUMN = {
  key: "eligibility",
  label: "Eligibility",
  raw: true,
  format: (v) => {
    if (!v) return "";
    const badgeClass = `fuzzy-badge-${(v.overall || "").toLowerCase()}`;
    return `<span class="badge ${badgeClass}">${escapeHtml(v.overall)}</span>`;
  },
};

let state = {
  page: 1,
  pageSize: 25,
  approvedSignature: null,
  originalSql: "",
  customMode: false,
  lastError: "",
  lastSearchRows: [],
  fuzzyCandidates: null,
  fuzzyResults: {},
  fuzzyPromptNctIds: [],
  resultsReminderTimer: null,
};

const LABEL_OVERRIDES = {
  NA: "N/A",
  NIH: "NIH",
  FED: "Federal",
  INDIV: "Individual",
  AMBIG: "Ambiguous",
  OTHER_GOV: "Other Gov't",
};

function labelize(value) {
  if (LABEL_OVERRIDES[value]) return LABEL_OVERRIDES[value];
  return value
    .toLowerCase()
    .split("_")
    .map((w) => w[0].toUpperCase() + w.slice(1))
    .join(" ");
}

function updateTriggerText(id) {
  const count = document.querySelectorAll(`#${id}-panel .ms-option.selected`).length;
  document.getElementById(`${id}-text`).textContent = count === 0 ? "Any" : `${count} selected`;
}

function populateFacetSelects() {
  for (const [id, values] of Object.entries(FACETS)) {
    const panel = document.getElementById(`${id}-panel`);
    for (const v of values) {
      const opt = document.createElement("div");
      opt.className = "ms-option";
      opt.dataset.value = v;
      opt.setAttribute("role", "option");
      opt.setAttribute("aria-selected", "false");
      opt.innerHTML = `<span class="ms-check" aria-hidden="true">&#10003;</span><span>${escapeHtml(labelize(v))}</span>`;
      opt.addEventListener("click", () => {
        const selected = opt.classList.toggle("selected");
        opt.setAttribute("aria-selected", String(selected));
        updateTriggerText(id);
      });
      panel.appendChild(opt);
    }
  }
}

function selectedValues(id) {
  return Array.from(document.querySelectorAll(`#${id}-panel .ms-option.selected`)).map((o) => o.dataset.value);
}

function clearFacetGroup(id) {
  document.querySelectorAll(`#${id}-panel .ms-option.selected`).forEach((o) => {
    o.classList.remove("selected");
    o.setAttribute("aria-selected", "false");
  });
  updateTriggerText(id);
}

function closeAllDropdowns() {
  document.querySelectorAll(".ms-panel").forEach((p) => p.classList.add("hidden"));
  document.querySelectorAll(".ms-trigger").forEach((t) => t.setAttribute("aria-expanded", "false"));
}

function initDropdowns() {
  for (const id of Object.keys(FACETS)) {
    const trigger = document.getElementById(`${id}-trigger`);
    const panel = document.getElementById(`${id}-panel`);
    trigger.addEventListener("click", (e) => {
      e.stopPropagation();
      const isOpen = !panel.classList.contains("hidden");
      closeAllDropdowns();
      if (!isOpen) {
        panel.classList.remove("hidden");
        trigger.setAttribute("aria-expanded", "true");
      }
    });
    panel.addEventListener("click", (e) => e.stopPropagation());
  }
  document.addEventListener("click", () => closeAllDropdowns());
}

function buildParams(page) {
  const params = new URLSearchParams();
  const keyword = document.getElementById("keyword").value.trim();
  const condition = document.getElementById("condition").value.trim();
  const sponsor = document.getElementById("sponsor").value.trim();
  if (keyword) params.set("keyword", keyword);
  if (condition) params.set("condition", condition);
  if (sponsor) params.set("sponsor", sponsor);
  const age = document.getElementById("age").value.trim();
  if (age) params.set("age", age);

  for (const id of ["status", "study_type", "phase", "sponsor_class", "sex"]) {
    for (const v of selectedValues(id)) params.append(id, v);
  }

  const hv = document.getElementById("healthy_volunteers").value;
  if (hv) params.set("healthy_volunteers", hv);
  const hr = document.getElementById("has_results").value;
  if (hr) params.set("has_results", hr);

  for (const id of ["enrollment_min", "enrollment_max", "start_year_min", "start_year_max"]) {
    const v = document.getElementById(id).value;
    if (v) params.set(id, v);
  }

  params.set("sort", document.getElementById("sort").value);
  params.set("dir", document.getElementById("dir").value);
  params.set("page", page);
  params.set("page_size", state.pageSize);
  return params;
}

function filterSignature() {
  const params = buildParams(1);
  params.delete("page");
  return params.toString();
}

function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function formatCell(value) {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value) || typeof value === "object") return JSON.stringify(value);
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return String(value);
}

// Renders both the fixed search-result schema and arbitrary custom-query
// column sets, since an edited query can select whatever columns it likes.
function renderTable(columns, rows, onRowClick) {
  const headRow = document.querySelector("#results-table thead tr");
  const body = document.getElementById("results-body");
  const emptyState = document.getElementById("empty-state");
  headRow.innerHTML = columns.map((c) => `<th>${escapeHtml(c.label)}</th>`).join("");
  body.innerHTML = "";
  emptyState.classList.toggle("hidden", rows.length > 0);
  if (rows.length === 0) {
    emptyState.querySelector("p").textContent = "No trials matched these filters.";
  }

  for (const row of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = columns
      .map((c) => {
        const raw = row[c.key];
        if (c.format && c.raw) return `<td>${c.format(raw)}</td>`;
        const display = c.format ? c.format(raw) : formatCell(raw);
        return `<td title="${escapeHtml(display)}">${escapeHtml(display)}</td>`;
      })
      .join("");
    if (onRowClick) {
      tr.style.cursor = "pointer";
      tr.addEventListener("click", () => onRowClick(row));
    }
    body.appendChild(tr);
  }
}

// Wraps renderTable() for the main search results specifically: prepends
// the Eligibility column (and merges each row's AI result onto it) once AI
// matching has produced results for the current search, so eligibility
// shows inline instead of in a separate table.
function renderResultsTable(rows) {
  const hasFuzzy = Object.keys(state.fuzzyResults).length > 0;
  const columns = hasFuzzy ? [ELIGIBILITY_COLUMN, ...SEARCH_COLUMNS] : SEARCH_COLUMNS;
  const renderRows = hasFuzzy
    ? rows.map((r) => ({ ...r, eligibility: state.fuzzyResults[r.nct_id] }))
    : rows;
  renderTable(columns, renderRows, (row) => openDetail(row.nct_id));
}

async function runSearch(page) {
  state.page = page;
  state.customMode = false;
  const statusBar = document.getElementById("status-bar");
  statusBar.textContent = "Loading...";
  const params = buildParams(page);

  try {
    const resp = await fetch(`/api/search?${params.toString()}`);
    const data = await resp.json();
    if (!resp.ok) {
      state.lastError = data.error || resp.statusText;
      statusBar.textContent = `Error: ${state.lastError}`;
      document.getElementById("results-body").innerHTML = "";
      return false;
    }
    state.lastSearchRows = data.rows;
    renderResultsTable(data.rows);
    const totalPages = Math.max(Math.ceil(data.total / data.page_size), 1);
    document.getElementById("pageInfo").textContent =
      `Page ${data.page} of ${totalPages} (${data.total.toLocaleString()} results)`;
    statusBar.textContent = `${data.total.toLocaleString()} matching trials`;
    document.getElementById("prevPage").disabled = data.page <= 1;
    document.getElementById("nextPage").disabled = data.page >= totalPages;
    return true;
  } catch (err) {
    state.lastError = String(err);
    statusBar.textContent = `Error: ${state.lastError}`;
    return false;
  }
}

async function runCustomQuery(sql) {
  state.customMode = true;
  const statusBar = document.getElementById("status-bar");
  statusBar.textContent = "Running custom query...";

  try {
    const resp = await fetch("/api/execute-sql", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sql }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      state.lastError = data.error || resp.statusText;
      statusBar.textContent = `Error: ${state.lastError}`;
      document.getElementById("results-body").innerHTML = "";
      return false;
    }
    const columns = data.columns.map((c) => ({ key: c, label: c }));
    const hasNctId = data.columns.includes("nct_id");
    renderTable(columns, data.rows, hasNctId ? (row) => openDetail(row.nct_id) : null);
    document.getElementById("pageInfo").textContent = "";
    document.getElementById("prevPage").disabled = true;
    document.getElementById("nextPage").disabled = true;
    const cappedNote = data.rows.length >= 500 ? " (capped at 500 rows)" : "";
    statusBar.textContent = `Custom query returned ${data.rows.length} row(s)${cappedNote}.`;
    return true;
  } catch (err) {
    state.lastError = String(err);
    statusBar.textContent = `Error: ${state.lastError}`;
    return false;
  }
}

function showSqlModal() {
  document.getElementById("sql-modal-overlay").classList.remove("hidden");
}

function hideSqlModal() {
  document.getElementById("sql-modal-overlay").classList.add("hidden");
}

// Every new filter combination is previewed before it touches BigQuery.
// Paging within an already-approved search re-runs immediately (same
// query, just a different OFFSET) rather than re-prompting every click.
async function requestSearchApproval(page) {
  const params = buildParams(page);
  const errorBox = document.getElementById("sql-modal-error");
  const textarea = document.getElementById("sql-preview");
  errorBox.classList.add("hidden");
  errorBox.textContent = "";
  textarea.value = "Building query...";
  textarea.disabled = true;
  showSqlModal();

  try {
    const resp = await fetch(`/api/search/sql?${params.toString()}`);
    const data = await resp.json();
    textarea.disabled = false;
    if (!resp.ok) {
      textarea.value = "";
      errorBox.textContent = data.error || resp.statusText;
      errorBox.classList.remove("hidden");
      document.getElementById("sql-run-btn").dataset.armed = "false";
      return;
    }
    textarea.value = data.sql;
    state.originalSql = data.sql;
    document.getElementById("sql-run-btn").dataset.armed = "true";
    document.getElementById("sql-run-btn").dataset.pendingPage = page;
  } catch (err) {
    textarea.disabled = false;
    textarea.value = "";
    errorBox.textContent = String(err);
    errorBox.classList.remove("hidden");
    document.getElementById("sql-run-btn").dataset.armed = "false";
  }
}

function goToPage(page) {
  if (state.approvedSignature === filterSignature()) {
    runSearch(page);
  } else {
    requestSearchApproval(page);
  }
}

// ---------- Fuzzy match ----------

// Age and sex are now hard search filters (see buildParams / the "Sex"
// facet) rather than separate patient-panel inputs -- the patient profile
// pulls them from there instead of asking for them twice.
function getPatientProfile() {
  const sexValues = selectedValues("sex").filter((v) => v === "MALE" || v === "FEMALE");
  return {
    age: document.getElementById("age").value,
    sex: sexValues.length === 1 ? sexValues[0] : "",
    notes: document.getElementById("patient-notes").value.trim(),
  };
}

function patientProfileValid(patient) {
  return Boolean(patient.sex) && patient.age !== "" && !isNaN(Number(patient.age));
}

function resetFuzzyPanel(message) {
  state.fuzzyCandidates = null;
  state.fuzzyResults = {};
  document.getElementById("fuzzy-status").textContent = message;
  document.getElementById("fuzzy-warning").classList.add("hidden");
  document.getElementById("fuzzy-run-btn").disabled = true;
  document.getElementById("results-eligibility-summary").classList.add("hidden");
}

// The main search's filters ARE the hard criteria -- whatever trials the
// search matches (across all pages, not just the visible one) become the
// candidate pool for AI matching. Called right after a search is approved
// and run, using the same filter params (minus paging/sort).
async function refreshFuzzyCandidates() {
  const statusEl = document.getElementById("fuzzy-status");
  const warningEl = document.getElementById("fuzzy-warning");
  const runBtn = document.getElementById("fuzzy-run-btn");
  warningEl.classList.add("hidden");
  state.fuzzyCandidates = null;
  runBtn.disabled = true;

  // A fresh hard search invalidates any previous AI assessment -- the
  // candidate pool (and possibly the patient profile) may have changed.
  state.fuzzyResults = {};
  document.getElementById("results-eligibility-summary").classList.add("hidden");
  renderResultsTable(state.lastSearchRows);

  statusEl.textContent = "Checking how many searched trials qualify for AI matching...";

  const params = buildParams(1);
  params.delete("page");
  params.delete("page_size");
  params.delete("sort");
  params.delete("dir");

  try {
    const resp = await fetch(`/api/fuzzy-match/candidates?${params.toString()}`);
    const data = await resp.json();
    if (!resp.ok) {
      statusEl.textContent = `Error: ${data.error || resp.statusText}`;
      return;
    }
    state.fuzzyCandidates = data;
    if (data.total > 100) {
      statusEl.textContent = `Your search matched ${data.total.toLocaleString()} trials.`;
      warningEl.textContent = `Too many matching trials (${data.total.toLocaleString()}) to run AI matching -- narrow your search filters to 100 or fewer trials.`;
      warningEl.classList.remove("hidden");
      runBtn.disabled = true;
    } else {
      statusEl.textContent = `Your search matched ${data.total} trial${data.total === 1 ? "" : "s"}${data.total > 0 ? " -- ready to run the AI match." : "."}`;
      runBtn.disabled = data.total === 0;
    }
  } catch (err) {
    statusEl.textContent = `Error: ${err}`;
  }
}

// ---------- Generic Yes/Cancel confirm modal ----------

let activeConfirmCancel = null;

// okOnly renders a single acknowledgement button (no real choice to make --
// used for the empty-notes heads-up, which isn't a gate, just a notice).
function showConfirm(message, { okOnly = false } = {}) {
  return new Promise((resolve) => {
    const overlay = document.getElementById("confirm-modal-overlay");
    const yesBtn = document.getElementById("confirm-modal-yes-btn");
    const cancelBtn = document.getElementById("confirm-modal-cancel-btn");
    document.getElementById("confirm-modal-message").textContent = message;
    yesBtn.textContent = okOnly ? "OK" : "Yes";
    cancelBtn.classList.toggle("hidden", okOnly);

    function cleanup(result) {
      overlay.classList.add("hidden");
      yesBtn.removeEventListener("click", onYes);
      cancelBtn.removeEventListener("click", onCancel);
      overlay.removeEventListener("click", onOverlay);
      activeConfirmCancel = null;
      resolve(result);
    }
    function onYes() { cleanup(true); }
    function onCancel() { cleanup(false); }
    function onOverlay(e) { if (e.target.id === "confirm-modal-overlay") cleanup(okOnly); }

    yesBtn.addEventListener("click", onYes);
    cancelBtn.addEventListener("click", onCancel);
    overlay.addEventListener("click", onOverlay);
    activeConfirmCancel = okOnly ? onYes : onCancel;
    overlay.classList.remove("hidden");
  });
}

function showFuzzyModal() {
  document.getElementById("fuzzy-modal-overlay").classList.remove("hidden");
}

function hideFuzzyModal() {
  document.getElementById("fuzzy-modal-overlay").classList.add("hidden");
  document.getElementById("fuzzy-modal-run-btn").dataset.armed = "false";
}

async function fetchFuzzyPromptAndCost(patient, nctIds) {
  const resp = await fetch("/api/fuzzy-match/prompt", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ patient, nct_ids: nctIds }),
  });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.error || resp.statusText);
  return data;
}

function displayFuzzyPromptModal(data, nctIds) {
  const errorBox = document.getElementById("fuzzy-modal-error");
  errorBox.classList.add("hidden");
  errorBox.textContent = "";
  document.getElementById("fuzzy-prompt-preview").value = data.prompt;
  const costEl = document.getElementById("fuzzy-cost-estimate");
  if (data.cost) {
    const c = data.cost;
    costEl.textContent =
      `This will cost up to $${c.max_total_cost.toFixed(2)} ` +
      `($${c.input_cost.toFixed(4)} input for ${c.input_tokens.toLocaleString()} tokens + ` +
      `up to $${c.max_output_cost.toFixed(2)} output). Actual cost is usually well below the max.`;
  } else {
    costEl.textContent = "Cost estimate unavailable.";
  }
  state.fuzzyPromptNctIds = nctIds;
  document.getElementById("fuzzy-modal-run-btn").dataset.armed = "true";
  showFuzzyModal();
}

// The manual "Run AI match" button -- fetches the prompt + cost and shows
// the review modal, which is where the cost estimate actually gets shown.
async function openFuzzyPromptModal() {
  if (!state.fuzzyCandidates || !state.fuzzyCandidates.trials || !state.fuzzyCandidates.trials.length) return;
  const patient = getPatientProfile();
  if (!patientProfileValid(patient)) {
    document.getElementById("fuzzy-status").textContent =
      "Select the patient's sex and enter their age in the search filters above before running the AI match.";
    return;
  }

  const nctIds = state.fuzzyCandidates.trials.map((t) => t.nct_id);
  const textarea = document.getElementById("fuzzy-prompt-preview");
  const costEl = document.getElementById("fuzzy-cost-estimate");
  const errorBox = document.getElementById("fuzzy-modal-error");
  errorBox.classList.add("hidden");
  errorBox.textContent = "";
  textarea.value = "Building prompt...";
  costEl.textContent = "";
  document.getElementById("fuzzy-modal-run-btn").dataset.armed = "false";
  showFuzzyModal();

  try {
    const data = await fetchFuzzyPromptAndCost(patient, nctIds);
    displayFuzzyPromptModal(data, nctIds);
  } catch (err) {
    textarea.value = "";
    errorBox.textContent = String(err.message || err);
    errorBox.classList.remove("hidden");
  }
}

// The one automatic popup: 2 seconds after hard search results land, remind
// the user how to get AI filtering, rather than gating/chaining any further
// confirms on top of it.
function scheduleResultsReminder() {
  if (state.resultsReminderTimer) clearTimeout(state.resultsReminderTimer);
  state.resultsReminderTimer = setTimeout(() => {
    state.resultsReminderTimer = null;
    showConfirm("Fill out patient profile for further filtering and then click 'Run AI Match'.", { okOnly: true });
  }, 2000);
}

// Eligibility results land inline as the leftmost column of the existing
// results table (see renderResultsTable), plus a counts header above it --
// no separate results table.
function applyFuzzyResults(results) {
  state.fuzzyResults = {};
  for (const r of results) state.fuzzyResults[r.nct_id] = r;

  const counts = { Eligible: 0, Uncertain: 0, Ineligible: 0 };
  for (const r of results) {
    if (counts[r.overall] !== undefined) counts[r.overall]++;
  }
  const summaryEl = document.getElementById("results-eligibility-summary");
  summaryEl.textContent = `${counts.Eligible} Eligible, ${counts.Uncertain} Uncertain, ${counts.Ineligible} Ineligible`;
  summaryEl.classList.remove("hidden");

  renderResultsTable(state.lastSearchRows);
}

async function runFuzzyMatchAi() {
  const btn = document.getElementById("fuzzy-modal-run-btn");
  if (btn.dataset.armed !== "true") return;
  const errorBox = document.getElementById("fuzzy-modal-error");
  errorBox.classList.add("hidden");
  errorBox.textContent = "";
  btn.disabled = true;
  const originalLabel = btn.textContent;
  btn.textContent = "Running...";

  const patient = getPatientProfile();
  try {
    const resp = await fetch("/api/fuzzy-match/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ patient, nct_ids: state.fuzzyPromptNctIds }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      errorBox.textContent = data.error || resp.statusText;
      errorBox.classList.remove("hidden");
      return;
    }
    applyFuzzyResults(data.results);
    hideFuzzyModal();
  } catch (err) {
    errorBox.textContent = String(err);
    errorBox.classList.remove("hidden");
  } finally {
    btn.disabled = false;
    btn.textContent = originalLabel;
  }
}

function showModalTab(tab) {
  const isDetails = tab === "details";
  document.getElementById("modal-tab-details").classList.toggle("active", isDetails);
  document.getElementById("modal-tab-fuzzy").classList.toggle("active", !isDetails);
  document.getElementById("modal-body").classList.toggle("hidden", !isDetails);
  document.getElementById("modal-fuzzy-body").classList.toggle("hidden", isDetails);
}

function renderFuzzyDetail(nctId) {
  const r = state.fuzzyResults[nctId];
  const container = document.getElementById("modal-fuzzy-body");
  if (!r) {
    container.innerHTML = "";
    return;
  }
  const list = (items) =>
    items && items.length
      ? `<ul>${items.map((i) => `<li>${escapeHtml(i)}</li>`).join("")}</ul>`
      : `<p class="muted">None identified.</p>`;

  container.innerHTML = `
    <div class="field">
      <div class="field-label">Overall eligibility</div>
      <div class="field-value"><span class="badge fuzzy-badge-${(r.overall || "").toLowerCase()}">${escapeHtml(r.overall)}</span></div>
    </div>
    <div class="field">
      <div class="field-label">Criteria that make patient ineligible</div>
      ${list(r.ineligible_criteria)}
    </div>
    <div class="field">
      <div class="field-label">Uncertain criteria</div>
      ${list(r.uncertain_criteria)}
    </div>
    <div class="field">
      <div class="field-label">Criteria that make patient eligible</div>
      ${list(r.eligible_criteria)}
    </div>
  `;
}

function fieldBlock(label, value) {
  if (value === null || value === undefined || value === "") return "";
  return `<div class="field"><div class="field-label">${escapeHtml(label)}</div><div class="field-value">${escapeHtml(value)}</div></div>`;
}

async function openDetail(nctId) {
  const overlay = document.getElementById("modal-overlay");
  const body = document.getElementById("modal-body");
  const tabsEl = document.getElementById("modal-tabs");
  body.innerHTML = "Loading...";
  body.classList.remove("hidden");
  tabsEl.classList.add("hidden");
  overlay.classList.remove("hidden");

  try {
    const resp = await fetch(`/api/trial/${encodeURIComponent(nctId)}`);
    const t = await resp.json();
    if (!resp.ok) {
      body.innerHTML = `<p>Error: ${escapeHtml(t.error || resp.statusText)}</p>`;
      return;
    }

    // fieldBlock() escapes its value and field-value has white-space:
    // pre-wrap, so a plain newline renders as a real line break -- an
    // embedded "<br>" would just show up as literal escaped text.
    const interventions = (t.interventions || [])
      .map((i) => `${i.type}: ${i.name}`)
      .join("\n");
    const locations = (t.locations || [])
      .slice(0, 15)
      .map((l) => `${l.facility} — ${l.city}, ${l.state}, ${l.country} (${l.status})`)
      .join("\n");
    const secondaryOutcomes = (t.secondary_outcomes || [])
      .map((o) => `${o.measure} — ${o.time_frame}`)
      .join("\n");

    body.innerHTML = `
      <h2>${escapeHtml(t.brief_title)}</h2>
      ${fieldBlock("NCT ID", t.nct_id)}
      ${fieldBlock("Official title", t.official_title)}
      ${fieldBlock("Status", t.overall_status)}
      ${fieldBlock("Study type", t.study_type)}
      ${fieldBlock("Phases", (t.phases || []).join(", "))}
      ${fieldBlock("Start date", t.start_date)}
      ${fieldBlock("Primary completion date", t.primary_completion_date)}
      ${fieldBlock("Completion date", t.completion_date)}
      ${fieldBlock("Lead sponsor", `${t.lead_sponsor || ""} (${t.lead_sponsor_class || ""})`)}
      ${fieldBlock("Conditions", (t.conditions || []).join(", "))}
      ${fieldBlock("Keywords", (t.keywords || []).join(", "))}
      ${fieldBlock("Interventions", interventions)}
      ${fieldBlock("Enrollment", t.enrollment_count)}
      ${fieldBlock("Sex", t.sex)}
      ${fieldBlock("Healthy volunteers", t.healthy_volunteers === null ? "" : (t.healthy_volunteers ? "Yes" : "No"))}
      ${fieldBlock("Min / max age (days)", `${t.minimum_age_days ?? ""} / ${t.maximum_age_days ?? ""}`)}
      ${fieldBlock("Brief summary", t.brief_summary)}
      ${fieldBlock("Inclusion criteria", t.inclusion_criteria)}
      ${fieldBlock("Exclusion criteria", t.exclusion_criteria)}
      ${fieldBlock("Locations (up to 15)", locations)}
      ${fieldBlock("Secondary outcomes", secondaryOutcomes)}
      ${fieldBlock("Has results", t.has_results ? "Yes" : "No")}
      ${fieldBlock("Retrieved at", t.retrieved_at)}
    `;

    if (state.fuzzyResults[nctId]) {
      tabsEl.classList.remove("hidden");
      renderFuzzyDetail(nctId);
      showModalTab("details");
    } else {
      document.getElementById("modal-fuzzy-body").innerHTML = "";
    }
  } catch (err) {
    body.innerHTML = `<p>Error: ${escapeHtml(String(err))}</p>`;
  }
}

function closeModal() {
  document.getElementById("modal-overlay").classList.add("hidden");
}

function closeSqlModal() {
  hideSqlModal();
  document.getElementById("sql-run-btn").dataset.armed = "false";
}

document.addEventListener("DOMContentLoaded", () => {
  populateFacetSelects();
  initDropdowns();
  document.getElementById("status-bar").textContent =
    "Set your filters and click Search to preview the query.";

  document.getElementById("filters").addEventListener("submit", (e) => {
    e.preventDefault();
    requestSearchApproval(1);
  });

  document.getElementById("resetBtn").addEventListener("click", () => {
    if (state.resultsReminderTimer) {
      clearTimeout(state.resultsReminderTimer);
      state.resultsReminderTimer = null;
    }
    document.getElementById("filters").reset();
    for (const id of ["status", "study_type", "phase", "sponsor_class", "sex"]) {
      clearFacetGroup(id);
    }
    document.getElementById("results-body").innerHTML = "";
    document.getElementById("empty-state").classList.remove("hidden");
    document.getElementById("pageInfo").textContent = "";
    document.getElementById("prevPage").disabled = true;
    document.getElementById("nextPage").disabled = true;
    document.getElementById("status-bar").textContent =
      "Set your filters and click Search to preview the query.";
    state.approvedSignature = null;
    state.customMode = false;
    state.originalSql = "";
    state.lastSearchRows = [];

    resetFuzzyPanel("Run a search to see how many trials qualify for AI matching.");
  });

  document.getElementById("prevPage").addEventListener("click", () => {
    if (state.page > 1) goToPage(state.page - 1);
  });
  document.getElementById("nextPage").addEventListener("click", () => goToPage(state.page + 1));

  document.getElementById("modal-close").addEventListener("click", closeModal);
  document.getElementById("modal-overlay").addEventListener("click", (e) => {
    if (e.target.id === "modal-overlay") closeModal();
  });

  document.getElementById("sql-run-btn").addEventListener("click", async (e) => {
    const btn = e.target;
    if (btn.dataset.armed !== "true") return;
    const page = parseInt(btn.dataset.pendingPage, 10) || 1;
    const currentSql = document.getElementById("sql-preview").value;
    const errorBox = document.getElementById("sql-modal-error");
    errorBox.classList.add("hidden");
    errorBox.textContent = "";
    btn.disabled = true;

    let ok;
    if (currentSql.trim() === state.originalSql.trim()) {
      ok = await runSearch(page);
      if (ok) {
        state.approvedSignature = filterSignature();
        refreshFuzzyCandidates();
        scheduleResultsReminder();
      }
    } else {
      ok = await runCustomQuery(currentSql);
      if (ok) {
        resetFuzzyPanel("Fuzzy match isn't available for hand-edited SQL results -- run a normal search to use it.");
      }
    }

    btn.disabled = false;
    if (ok) {
      hideSqlModal();
    } else {
      errorBox.textContent = state.lastError || "Query failed.";
      errorBox.classList.remove("hidden");
    }
  });
  document.getElementById("sql-reset-btn").addEventListener("click", () => {
    document.getElementById("sql-preview").value = state.originalSql;
    document.getElementById("sql-modal-error").classList.add("hidden");
  });
  document.getElementById("sql-cancel-btn").addEventListener("click", closeSqlModal);
  document.getElementById("sql-modal-overlay").addEventListener("click", (e) => {
    if (e.target.id === "sql-modal-overlay") closeSqlModal();
  });

  document.getElementById("modal-tab-details").addEventListener("click", () => showModalTab("details"));
  document.getElementById("modal-tab-fuzzy").addEventListener("click", () => showModalTab("fuzzy"));

  document.getElementById("fuzzy-run-btn").addEventListener("click", openFuzzyPromptModal);
  document.getElementById("fuzzy-modal-run-btn").addEventListener("click", runFuzzyMatchAi);
  document.getElementById("fuzzy-modal-cancel-btn").addEventListener("click", hideFuzzyModal);
  document.getElementById("fuzzy-modal-overlay").addEventListener("click", (e) => {
    if (e.target.id === "fuzzy-modal-overlay") hideFuzzyModal();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeModal();
      closeSqlModal();
      hideFuzzyModal();
      closeAllDropdowns();
      if (activeConfirmCancel) activeConfirmCancel();
    }
  });
});
