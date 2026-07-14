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

let state = { page: 1, pageSize: 25, approvedSignature: null };

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

function renderRows(rows) {
  const body = document.getElementById("results-body");
  body.innerHTML = "";
  for (const row of rows) {
    const tr = document.createElement("tr");
    tr.dataset.nctId = row.nct_id;
    tr.innerHTML = `
      <td>${escapeHtml(row.nct_id)}</td>
      <td title="${escapeHtml(row.brief_title)}">${escapeHtml(row.brief_title)}</td>
      <td><span class="badge">${escapeHtml(row.overall_status)}</span></td>
      <td>${escapeHtml((row.phases || []).join(", "))}</td>
      <td>${escapeHtml(row.study_type)}</td>
      <td>${escapeHtml(row.start_date)}</td>
      <td title="${escapeHtml(row.lead_sponsor)}">${escapeHtml(row.lead_sponsor)}</td>
      <td>${escapeHtml(row.lead_sponsor_class)}</td>
      <td>${row.enrollment_count ?? ""}</td>
      <td>${escapeHtml(row.sex)}</td>
      <td>${row.has_results ? "Yes" : "No"}</td>
    `;
    tr.addEventListener("click", () => openDetail(row.nct_id));
    body.appendChild(tr);
  }
}

async function runSearch(page) {
  state.page = page;
  const statusBar = document.getElementById("status-bar");
  statusBar.textContent = "Loading...";
  const params = buildParams(page);

  try {
    const resp = await fetch(`/api/search?${params.toString()}`);
    const data = await resp.json();
    if (!resp.ok) {
      statusBar.textContent = `Error: ${data.error || resp.statusText}`;
      document.getElementById("results-body").innerHTML = "";
      return;
    }
    renderRows(data.rows);
    const totalPages = Math.max(Math.ceil(data.total / data.page_size), 1);
    document.getElementById("pageInfo").textContent =
      `Page ${data.page} of ${totalPages} (${data.total.toLocaleString()} results)`;
    statusBar.textContent = `${data.total.toLocaleString()} matching trials`;
    document.getElementById("prevPage").disabled = data.page <= 1;
    document.getElementById("nextPage").disabled = data.page >= totalPages;
  } catch (err) {
    statusBar.textContent = `Error: ${err}`;
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
  const pre = document.getElementById("sql-preview");
  errorBox.classList.add("hidden");
  errorBox.textContent = "";
  pre.textContent = "Building query...";
  showSqlModal();

  try {
    const resp = await fetch(`/api/search/sql?${params.toString()}`);
    const data = await resp.json();
    if (!resp.ok) {
      pre.textContent = "";
      errorBox.textContent = data.error || resp.statusText;
      errorBox.classList.remove("hidden");
      document.getElementById("sql-run-btn").dataset.armed = "false";
      return;
    }
    pre.textContent = data.sql;
    document.getElementById("sql-run-btn").dataset.armed = "true";
    document.getElementById("sql-run-btn").dataset.pendingPage = page;
  } catch (err) {
    pre.textContent = "";
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
    document.getElementById("status-bar").textContent =
      "Set your filters and click Search to preview the query.";
    state.approvedSignature = null;
  });

  document.getElementById("prevPage").addEventListener("click", () => {
    if (state.page > 1) goToPage(state.page - 1);
  });
  document.getElementById("nextPage").addEventListener("click", () => goToPage(state.page + 1));

  document.getElementById("modal-close").addEventListener("click", closeModal);
  document.getElementById("modal-overlay").addEventListener("click", (e) => {
    if (e.target.id === "modal-overlay") closeModal();
  });

  document.getElementById("sql-run-btn").addEventListener("click", (e) => {
    if (e.target.dataset.armed !== "true") return;
    const page = parseInt(e.target.dataset.pendingPage, 10) || 1;
    state.approvedSignature = filterSignature();
    hideSqlModal();
    runSearch(page);
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
