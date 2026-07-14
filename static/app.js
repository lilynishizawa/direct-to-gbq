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

let state = {
  page: 1,
  pageSize: 25,
  approvedSignature: null,
  originalSql: "",
  customMode: false,
  lastError: "",
};

function populateFacetSelects() {
  for (const [id, values] of Object.entries(FACETS)) {
    const select = document.getElementById(id);
    for (const v of values) {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = v;
      select.appendChild(opt);
    }
  }
}

function selectedValues(id) {
  return Array.from(document.getElementById(id).selectedOptions).map((o) => o.value);
}

function buildParams(page) {
  const params = new URLSearchParams();
  const keyword = document.getElementById("keyword").value.trim();
  const condition = document.getElementById("condition").value.trim();
  const sponsor = document.getElementById("sponsor").value.trim();
  if (keyword) params.set("keyword", keyword);
  if (condition) params.set("condition", condition);
  if (sponsor) params.set("sponsor", sponsor);

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
  headRow.innerHTML = columns.map((c) => `<th>${escapeHtml(c.label)}</th>`).join("");
  body.innerHTML = "";

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
    renderTable(SEARCH_COLUMNS, data.rows, (row) => openDetail(row.nct_id));
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

function fieldBlock(label, value) {
  if (value === null || value === undefined || value === "") return "";
  return `<div class="field"><div class="field-label">${escapeHtml(label)}</div><div class="field-value">${escapeHtml(value)}</div></div>`;
}

async function openDetail(nctId) {
  const overlay = document.getElementById("modal-overlay");
  const body = document.getElementById("modal-body");
  body.innerHTML = "Loading...";
  overlay.classList.remove("hidden");

  try {
    const resp = await fetch(`/api/trial/${encodeURIComponent(nctId)}`);
    const t = await resp.json();
    if (!resp.ok) {
      body.innerHTML = `<p>Error: ${escapeHtml(t.error || resp.statusText)}</p>`;
      return;
    }

    const interventions = (t.interventions || [])
      .map((i) => `${escapeHtml(i.type)}: ${escapeHtml(i.name)}`)
      .join("<br>");
    const locations = (t.locations || [])
      .slice(0, 15)
      .map((l) => `${escapeHtml(l.facility)} — ${escapeHtml(l.city)}, ${escapeHtml(l.state)}, ${escapeHtml(l.country)} (${escapeHtml(l.status)})`)
      .join("<br>");
    const secondaryOutcomes = (t.secondary_outcomes || [])
      .map((o) => `${escapeHtml(o.measure)} — ${escapeHtml(o.time_frame)}`)
      .join("<br>");

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
  document.getElementById("status-bar").textContent =
    "Set your filters and click Search to preview the query.";

  document.getElementById("filters").addEventListener("submit", (e) => {
    e.preventDefault();
    requestSearchApproval(1);
  });

  document.getElementById("resetBtn").addEventListener("click", () => {
    document.getElementById("filters").reset();
    for (const id of ["status", "study_type", "phase", "sponsor_class", "sex"]) {
      Array.from(document.getElementById(id).options).forEach((o) => (o.selected = false));
    }
    document.getElementById("results-body").innerHTML = "";
    document.getElementById("pageInfo").textContent = "";
    document.getElementById("prevPage").disabled = true;
    document.getElementById("nextPage").disabled = true;
    document.getElementById("status-bar").textContent =
      "Set your filters and click Search to preview the query.";
    state.approvedSignature = null;
    state.customMode = false;
    state.originalSql = "";
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
      if (ok) state.approvedSignature = filterSignature();
    } else {
      ok = await runCustomQuery(currentSql);
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

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeModal();
      closeSqlModal();
    }
  });
});
