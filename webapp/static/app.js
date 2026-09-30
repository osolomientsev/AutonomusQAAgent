const form = document.getElementById("run-form");
const runButton = document.getElementById("run-button");
const stopButton = document.getElementById("stop-button");
const approveButton = document.getElementById("approve-button");
const saveSuiteReviewButton = document.getElementById("save-suite-review");
const saveSuiteResultsButton = document.getElementById("save-suite-results");
const logEl = document.getElementById("log");
const resultsEl = document.getElementById("results");
const runsListEl = document.getElementById("runs-list");
const suitesListEl = document.getElementById("suites-list");
const compareButton = document.getElementById("compare-button");
const diffPanelEl = document.getElementById("diff-panel");
const diffResultsEl = document.getElementById("diff-results");
const suitePanelEl = document.getElementById("suite-panel");
const suitePanelNameEl = document.getElementById("suite-panel-name");
const suitePanelUrlEl = document.getElementById("suite-panel-url");
const suiteCasesEl = document.getElementById("suite-cases");
const suiteRunsListEl = document.getElementById("suite-runs-list");
const suiteRunsEmptyEl = document.getElementById("suite-runs-empty");
const addSuiteCaseButton = document.getElementById("add-suite-case");
const saveSuiteChangesButton = document.getElementById("save-suite-changes");
const modelSelectField = document.getElementById("model-select-field");
const modelTextField = document.getElementById("model-text-field");
const modelSelect = form.elements["model_select"];
const modelText = form.elements["model_text"];
const providerHiddenInput = form.elements["provider"];
const cloudToggle = document.getElementById("cloud-toggle");
const cloudProviderSelect = document.getElementById("cloud-provider-select");
const cloudProviderField = document.getElementById("cloud-provider-field");
const claudeFieldsEl = document.getElementById("claude-fields");
const openaiFieldsEl = document.getElementById("openai-fields");
const numCtxFieldEl = document.getElementById("num-ctx-field");
const claudeKeyStatusEl = document.getElementById("claude-key-status");
const openaiKeyStatusEl = document.getElementById("openai-key-status");

let eventSource = null;
let currentRunId = null;
let currentSuiteId = null;
let reviewMode = false;
let defaultPrompts = { planner: "", executor: "" };
const caseCards = new Map();
const selectedForDiff = new Set();

function buildUrl(fd) {
  const rest = (fd.get("url_rest") || "").trim();
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(rest)) {
    return rest; // already has an explicit scheme (e.g. pasted a full URL)
  }
  return fd.get("url_scheme") + rest;
}

function applyUrlToForm(url) {
  const match = /^([a-zA-Z][a-zA-Z0-9+.-]*:\/\/)(.*)$/.exec(url);
  if (match) {
    const scheme = match[1];
    if (Array.from(form.elements["url_scheme"].options).some((o) => o.value === scheme)) {
      form.elements["url_scheme"].value = scheme;
    }
    form.elements["url_rest"].value = match[2];
  } else {
    form.elements["url_rest"].value = url;
  }
}

function logLine(text, cls) {
  const line = document.createElement("div");
  line.className = "log-line" + (cls ? " " + cls : "");
  line.textContent = text;
  logEl.appendChild(line);
  logEl.scrollTop = logEl.scrollHeight;
}

function caseCard(caseId, title, goal, steps, resetBefore) {
  const card = document.createElement("div");
  card.className = "case-card status-planned";
  card.innerHTML = `
    <div class="case-header">
      <span class="case-id">${caseId}</span>
      <span class="case-title"></span>
      <span class="case-status">planned</span>
    </div>
    <div class="case-goal"></div>
    <div class="case-reset"></div>
    <ol class="case-plan-list"></ol>
    <div class="case-steps"></div>
    <div class="case-summary"></div>
    <div class="case-findings"></div>
    <img class="case-screenshot" hidden />
  `;
  card.querySelector(".case-title").textContent = title;
  card.querySelector(".case-goal").textContent = goal || "";
  card.dataset.goal = goal || "";
  card.dataset.steps = JSON.stringify(steps || []);
  card.dataset.resetBefore = resetBefore === false ? "false" : "true";
  card.querySelector(".case-reset").textContent =
    resetBefore === false ? "▷ continues from the previous test's state" : "↺ resets to the initial state before this test";
  const planListEl = card.querySelector(".case-plan-list");
  (steps || []).forEach((s) => {
    const li = document.createElement("li");
    li.textContent = s;
    planListEl.appendChild(li);
  });
  resultsEl.appendChild(card);
  caseCards.set(caseId, card);
  return card;
}

function setCaseStatus(caseId, status) {
  const card = caseCards.get(caseId);
  if (!card) return;
  card.className = "case-card status-" + status;
  card.querySelector(".case-status").textContent = status;
}

function basename(path) {
  if (!path) return "";
  return path.split(/[\\/]/).pop();
}

function addStepLine(caseId, event, runId) {
  const card = caseCards.get(caseId);
  if (!card) return;
  const wrap = document.createElement("div");
  wrap.className = "step-entry";
  const line = document.createElement("div");
  line.className = "step-line" + (event.guardrail_blocked ? " blocked" : "");
  line.textContent = `#${event.step} ${event.tool_name}(${JSON.stringify(event.tool_args)}) -> ${event.result_summary}`;
  wrap.appendChild(line);
  const effectiveRunId = runId || currentRunId;
  const screenshotUrl = event.screenshot_url || (effectiveRunId && event.screenshot_path ? `/reports/${effectiveRunId}/screenshots/${basename(event.screenshot_path)}` : null);
  if (screenshotUrl) {
    const img = document.createElement("img");
    img.className = "step-screenshot";
    img.src = screenshotUrl + "?t=" + Date.now();
    img.loading = "lazy";
    img.addEventListener("click", () => window.open(screenshotUrl, "_blank"));
    wrap.appendChild(img);
  }
  card.querySelector(".case-steps").appendChild(wrap);
}

function finishCase(event) {
  const card = caseCards.get(event.case_id);
  if (!card) return;
  setCaseStatus(event.case_id, event.status);
  card.querySelector(".case-summary").textContent = event.summary || "";
  const findingsEl = card.querySelector(".case-findings");
  findingsEl.innerHTML = "";
  (event.findings || []).forEach((f) => {
    const li = document.createElement("div");
    li.className = "finding finding-" + f.severity;
    li.textContent = `[${f.severity}] ${f.message}`;
    findingsEl.appendChild(li);
  });
  if (event.screenshot_url) {
    const img = card.querySelector(".case-screenshot");
    img.src = event.screenshot_url + "?t=" + Date.now();
    img.hidden = false;
  }
}

function makeStepRow(text) {
  const row = document.createElement("div");
  row.className = "review-step-row";
  row.innerHTML = `
    <input type="text" class="review-step" placeholder="Step instruction" />
    <button type="button" class="insert-step" title="Insert step below">+</button>
    <button type="button" class="remove-step" title="Remove step">✕</button>
  `;
  row.querySelector(".review-step").value = text || "";
  row.querySelector(".insert-step").addEventListener("click", () => {
    row.after(makeStepRow(""));
  });
  row.querySelector(".remove-step").addEventListener("click", () => row.remove());
  return row;
}

function makeReviewCard(title, goal, steps, resetBefore) {
  const card = document.createElement("div");
  card.className = "case-card review-card";
  card.innerHTML = `
    <div class="case-header">
      <input class="review-title" placeholder="Case title" />
      <button type="button" class="remove-case" title="Remove case">✕</button>
    </div>
    <textarea class="review-goal" rows="2" placeholder="What to check / comment for the agent"></textarea>
    <div class="review-steps"></div>
    <button type="button" class="add-step">+ Add step</button>
    <label class="checkbox review-reset-label">
      <input type="checkbox" class="review-reset" />
      Reset page state (return to the initial URL) before this test
    </label>
  `;
  card.querySelector(".review-title").value = title || "";
  card.querySelector(".review-goal").value = goal || "";
  card.querySelector(".review-reset").checked = resetBefore !== false;
  card.querySelector(".remove-case").addEventListener("click", () => card.remove());
  const stepsEl = card.querySelector(".review-steps");
  (steps && steps.length ? steps : [""]).forEach((s) => stepsEl.appendChild(makeStepRow(s)));
  card.querySelector(".add-step").addEventListener("click", () => stepsEl.appendChild(makeStepRow("")));
  return card;
}

function enterReviewMode(cases) {
  reviewMode = true;
  resultsEl.innerHTML = "";
  approveButton.hidden = false;
  approveButton.disabled = false;
  saveSuiteReviewButton.hidden = false;

  const addRow = document.createElement("div");
  addRow.className = "add-case-row";
  const addBtn = document.createElement("button");
  addBtn.type = "button";
  addBtn.textContent = "+ Add case";
  addBtn.addEventListener("click", () => resultsEl.insertBefore(makeReviewCard("", "", [], true), addRow));
  addRow.appendChild(addBtn);

  cases.forEach((c) => resultsEl.appendChild(makeReviewCard(c.title, c.goal, c.steps, c.reset_before)));
  resultsEl.appendChild(addRow);
}

function exitReviewMode() {
  reviewMode = false;
  approveButton.hidden = true;
  approveButton.disabled = false;
  saveSuiteReviewButton.hidden = true;
}

function collectCasesFrom(container) {
  return Array.from(container.querySelectorAll(".review-card"))
    .map((card) => ({
      title: card.querySelector(".review-title").value.trim(),
      goal: card.querySelector(".review-goal").value.trim(),
      steps: Array.from(card.querySelectorAll(".review-step"))
        .map((i) => i.value.trim())
        .filter(Boolean),
      reset_before: card.querySelector(".review-reset").checked,
    }))
    .filter((c) => c.title && c.goal);
}

function collectReviewCases() {
  return collectCasesFrom(resultsEl);
}

function collectResultCases() {
  return Array.from(caseCards.values()).map((card) => ({
    title: card.querySelector(".case-title").textContent,
    goal: card.dataset.goal || "",
    steps: JSON.parse(card.dataset.steps || "[]"),
    reset_before: card.dataset.resetBefore !== "false",
  }));
}

async function saveCasesAsSuite(cases) {
  if (cases.length === 0) {
    logLine("Nothing to save: the case list is empty.", "error");
    return;
  }
  const name = prompt("Suite name:");
  if (!name || !name.trim()) return;
  const url = buildUrl(new FormData(form));
  try {
    const res = await fetch("/api/suites", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim(), url, cases }),
    });
    if (!res.ok) throw new Error(await res.text());
    logLine(`Saved suite "${name.trim()}" (${cases.length} case(s)).`);
    await loadSuites();
  } catch (err) {
    logLine("Failed to save suite: " + err, "error");
  }
}

saveSuiteReviewButton.addEventListener("click", () => saveCasesAsSuite(collectReviewCases()));
saveSuiteResultsButton.addEventListener("click", () => saveCasesAsSuite(collectResultCases()));

async function loadDefaultPrompts() {
  try {
    const res = await fetch("/api/prompts");
    defaultPrompts = await res.json();
  } catch (err) {
    logLine("Failed to fetch default prompts: " + err, "error");
  }
}

document.querySelectorAll(".load-default").forEach((btn) => {
  btn.addEventListener("click", () => {
    const key = btn.dataset.target === "planner_prompt" ? "planner" : "executor";
    form.elements[btn.dataset.target].value = defaultPrompts[key] || "";
  });
});

async function loadModels(provider) {
  if (provider === "openai") {
    // No curated catalog for arbitrary OpenAI-compatible endpoints - the
    // free-text field is authoritative here, nothing to populate.
    return;
  }
  try {
    const res = await fetch(`/api/models?provider=${encodeURIComponent(provider)}`);
    const data = await res.json();
    modelSelect.innerHTML = "";
    (data.models || []).forEach((name) => {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      if (name === data.default) opt.selected = true;
      modelSelect.appendChild(opt);
    });
    if (!modelSelect.value && data.default) {
      const opt = document.createElement("option");
      opt.value = data.default;
      opt.textContent = data.default;
      opt.selected = true;
      modelSelect.appendChild(opt);
    }
  } catch (err) {
    logLine("Failed to fetch the model list: " + err, "error");
  }
}

function currentModel(fd) {
  return fd.get("provider") === "openai" ? fd.get("model_text") || "" : fd.get("model_select") || "";
}

function effectiveProvider() {
  return cloudToggle.checked ? cloudProviderSelect.value : "ollama";
}

function syncProviderHidden() {
  providerHiddenInput.value = effectiveProvider();
}

function applyProviderVisibility() {
  syncProviderHidden();
  const provider = effectiveProvider();
  cloudProviderField.hidden = !cloudToggle.checked;
  claudeFieldsEl.hidden = provider !== "claude";
  openaiFieldsEl.hidden = provider !== "openai";
  numCtxFieldEl.hidden = provider !== "ollama";
  modelSelectField.hidden = provider === "openai";
  modelTextField.hidden = provider !== "openai";
}

function onProviderUIChange() {
  applyProviderVisibility();
  loadModels(effectiveProvider());
}

cloudToggle.addEventListener("change", onProviderUIChange);
cloudProviderSelect.addEventListener("change", onProviderUIChange);

function renderKeyStatus(el, status, envVarName) {
  if (!status) return;
  if (status.session_set) {
    el.textContent = "Key saved for this session.";
  } else if (status.env_set) {
    el.textContent = `No session key set — using ${envVarName} from the server's environment.`;
  } else {
    el.textContent = `No key set. Enter one above, or set ${envVarName} in the server's environment.`;
  }
  if (status.workspace_id_set) {
    el.textContent += " Workspace ID saved.";
  }
}

async function loadCredentialStatus() {
  try {
    const res = await fetch("/api/credentials");
    const data = await res.json();
    renderKeyStatus(claudeKeyStatusEl, data.claude, "ANTHROPIC_API_KEY");
    renderKeyStatus(openaiKeyStatusEl, data.openai, "OPENAI_API_KEY");
  } catch (err) {
    logLine("Failed to fetch credential status: " + err, "error");
  }
}

function currentApiKey(fd) {
  const provider = fd.get("provider");
  if (provider === "claude") return fd.get("claude_api_key") || "";
  if (provider === "openai") return fd.get("openai_api_key") || "";
  return "";
}

document.querySelectorAll(".clear-key").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const provider = btn.dataset.provider;
    try {
      const res = await fetch(`/api/credentials/${provider}`, { method: "DELETE" });
      if (!res.ok) throw new Error(await res.text());
      const fieldName = provider === "claude" ? "claude_api_key" : "openai_api_key";
      form.elements[fieldName].value = "";
      if (provider === "claude") form.elements["claude_workspace_id"].value = "";
      await loadCredentialStatus();
      logLine(`Cleared the saved ${provider} key.`);
    } catch (err) {
      logLine("Failed to clear key: " + err, "error");
    }
  });
});

function formatTimestamp(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function runBadgesEl(statusCounts) {
  const wrap = document.createElement("span");
  wrap.className = "run-badges";
  Object.entries(statusCounts || {}).forEach(([status, count]) => {
    const badge = document.createElement("span");
    badge.className = "run-badge run-badge-" + status;
    badge.textContent = `${count} ${status}`;
    wrap.appendChild(badge);
  });
  return wrap;
}

function runMetaLine(run) {
  const meta = document.createElement("div");
  meta.className = "run-meta-line";
  const ts = document.createElement("span");
  ts.className = "run-timestamp";
  ts.textContent = formatTimestamp(run.created_at);
  meta.appendChild(ts);
  meta.appendChild(runBadgesEl(run.status_counts));
  return meta;
}

function buildRunHistoryRow(run) {
  const li = document.createElement("li");
  const a = document.createElement("a");
  a.href = "#";
  a.textContent = run.run_id;
  a.addEventListener("click", (e) => {
    e.preventDefault();
    showPastRun(run);
  });
  li.appendChild(a);
  li.appendChild(runMetaLine(run));
  return li;
}

function buildRunRow(run) {
  const li = document.createElement("li");

  const diffCheckbox = document.createElement("input");
  diffCheckbox.type = "checkbox";
  diffCheckbox.className = "diff-checkbox";
  diffCheckbox.title = "Select for comparison (only runs of the same resource can be compared)";
  diffCheckbox.dataset.runId = run.run_id;
  diffCheckbox.dataset.url = run.url || "";
  diffCheckbox.checked = selectedForDiff.has(run.run_id);
  diffCheckbox.addEventListener("change", () => {
    if (diffCheckbox.checked) {
      selectedForDiff.add(run.run_id);
    } else {
      selectedForDiff.delete(run.run_id);
    }
    compareButton.hidden = selectedForDiff.size !== 2;
    refreshDiffCheckboxStates();
  });
  li.appendChild(diffCheckbox);

  const a = document.createElement("a");
  a.href = "#";
  a.textContent = run.run_id;
  a.addEventListener("click", (e) => {
    e.preventDefault();
    showPastRun(run);
  });
  li.appendChild(a);

  const actions = document.createElement("span");
  actions.className = "run-actions";

  const rerunBtn = document.createElement("button");
  rerunBtn.type = "button";
  rerunBtn.title = "Re-run with the same settings";
  rerunBtn.textContent = "▶";
  rerunBtn.addEventListener("click", () => rerunPastRun(run));
  actions.appendChild(rerunBtn);

  const deleteBtn = document.createElement("button");
  deleteBtn.type = "button";
  deleteBtn.className = "delete";
  deleteBtn.title = "Delete";
  deleteBtn.textContent = "✕";
  deleteBtn.addEventListener("click", () => deletePastRun(run));
  actions.appendChild(deleteBtn);

  li.appendChild(actions);
  li.appendChild(runMetaLine(run));
  return li;
}

function refreshDiffCheckboxStates() {
  let selectedUrl = null;
  document.querySelectorAll(".diff-checkbox").forEach((cb) => {
    if (selectedForDiff.has(cb.dataset.runId)) selectedUrl = cb.dataset.url;
  });
  document.querySelectorAll(".diff-checkbox").forEach((cb) => {
    const isSelected = selectedForDiff.has(cb.dataset.runId);
    const blockedByCount = !isSelected && selectedForDiff.size >= 2;
    const blockedByResource = !isSelected && selectedUrl !== null && cb.dataset.url !== selectedUrl;
    cb.disabled = blockedByCount || blockedByResource;
  });
}

async function loadRuns() {
  try {
    const res = await fetch("/api/runs");
    const data = await res.json();
    runsListEl.innerHTML = "";
    const runs = data.runs || [];
    const stillSelected = new Set(runs.map((r) => r.run_id));
    selectedForDiff.forEach((id) => {
      if (!stillSelected.has(id)) selectedForDiff.delete(id);
    });

    const groups = new Map();
    runs.forEach((run) => {
      const key = run.url || "(unknown resource)";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(run);
    });

    groups.forEach((groupRuns, url) => {
      const groupLi = document.createElement("li");
      groupLi.className = "run-group";
      const header = document.createElement("div");
      header.className = "run-group-header";
      header.textContent = url;
      header.title = url;
      groupLi.appendChild(header);
      const groupList = document.createElement("ul");
      groupList.className = "run-group-list";
      groupRuns.forEach((run) => groupList.appendChild(buildRunRow(run)));
      groupLi.appendChild(groupList);
      runsListEl.appendChild(groupLi);
    });

    compareButton.hidden = selectedForDiff.size !== 2;
    refreshDiffCheckboxStates();
  } catch (err) {
    logLine("Failed to fetch past runs: " + err, "error");
  }
}

compareButton.addEventListener("click", async () => {
  if (selectedForDiff.size !== 2) return;
  const [a, b] = Array.from(selectedForDiff);
  try {
    const res = await fetch(`/api/runs/diff?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
    if (!res.ok) throw new Error(await res.text());
    const diff = await res.json();
    renderDiff(diff);
  } catch (err) {
    logLine("Failed to compare runs: " + err, "error");
  }
});

function diffSection(title, entries, cls) {
  if (entries.length === 0) return "";
  const rows = entries
    .map((e) => {
      const b = "status_b" in e ? e.status_b : e.status;
      const a = "status_a" in e ? e.status_a : e.status;
      return `<tr class="${cls}"><td>${e.title}</td><td>${a}</td><td>${b}</td></tr>`;
    })
    .join("");
  return `<h3>${title} (${entries.length})</h3><table><thead><tr><th>Case</th><th>A</th><th>B</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderDiff(diff) {
  diffPanelEl.hidden = false;
  let html = `<p>A: <code>${diff.a_run_id}</code> (${diff.a_url}) &nbsp;vs&nbsp; B: <code>${diff.b_run_id}</code> (${diff.b_url})</p>`;
  html += diffSection("Regressions (passed in A, not in B)", diff.regressions, "diff-regression");
  html += diffSection("Fixed (not passing in A, passes in B)", diff.fixed, "diff-fixed");
  html += diffSection("Changed", diff.changed, "diff-changed");
  html += diffSection("Unchanged", diff.unchanged, "");
  html += diffSection("Only in A", diff.only_in_a, "");
  html += diffSection("Only in B", diff.only_in_b, "");
  if (
    diff.regressions.length === 0 &&
    diff.fixed.length === 0 &&
    diff.changed.length === 0 &&
    diff.only_in_a.length === 0 &&
    diff.only_in_b.length === 0
  ) {
    html += `<p class="diff-empty">No differences.</p>`;
  }
  diffResultsEl.innerHTML = html;
  diffPanelEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function loadSuites() {
  try {
    const res = await fetch("/api/suites");
    const data = await res.json();
    suitesListEl.innerHTML = "";
    (data.suites || []).forEach((suite) => {
      const li = document.createElement("li");

      const nameEl = document.createElement("a");
      nameEl.href = "#";
      nameEl.className = "suite-name";
      nameEl.textContent = suite.name;
      nameEl.addEventListener("click", (e) => {
        e.preventDefault();
        showSuite(suite);
      });
      const meta = document.createElement("span");
      meta.className = "suite-meta";
      meta.textContent = `${suite.case_count} case(s)`;
      nameEl.appendChild(document.createElement("br"));
      nameEl.appendChild(meta);
      li.appendChild(nameEl);

      const actions = document.createElement("span");
      actions.className = "run-actions";

      const runBtn = document.createElement("button");
      runBtn.type = "button";
      runBtn.title = "Run this suite (skips planning)";
      runBtn.textContent = "▶";
      runBtn.addEventListener("click", () => runSuite(suite));
      actions.appendChild(runBtn);

      const deleteBtn = document.createElement("button");
      deleteBtn.type = "button";
      deleteBtn.className = "delete";
      deleteBtn.title = "Delete";
      deleteBtn.textContent = "✕";
      deleteBtn.addEventListener("click", () => deleteSuite(suite));
      actions.appendChild(deleteBtn);

      li.appendChild(actions);
      suitesListEl.appendChild(li);
    });
  } catch (err) {
    logLine("Failed to fetch suites: " + err, "error");
  }
}

async function runSuite(suite) {
  applyUrlToForm(suite.url);
  syncProviderHidden();
  const fd = new FormData(form);
  const body = {
    url: suite.url,
    provider: fd.get("provider"),
    model: currentModel(fd),
    openai_base_url: fd.get("openai_base_url") || "",
    api_key: currentApiKey(fd),
    workspace_id: fd.get("claude_workspace_id") || "",
    channel: fd.get("channel"),
    max_cases: Number(fd.get("max_cases")),
    max_steps: Number(fd.get("max_steps")),
    num_ctx: Number(fd.get("num_ctx")),
    headless: !fd.get("show_browser"),
    auto_confirm_destructive: !!fd.get("auto_confirm_destructive"),
    focus_hint: fd.get("focus_hint") || "",
    precondition_goal: fd.get("precondition") || "",
    planner_system_prompt: fd.get("planner_prompt") || "",
    executor_system_prompt: fd.get("executor_prompt") || "",
    review: !!fd.get("review"),
    fresh_browser_per_case: !!fd.get("fresh_browser_per_case"),
    suite_id: suite.id,
  };
  logLine(`Running suite "${suite.name}" (${body.url}) ...`);
  await launchRun(body);
}

async function deleteSuite(suite) {
  if (!confirm(`Delete suite "${suite.name}"?`)) return;
  try {
    const res = await fetch(`/api/suites/${suite.id}`, { method: "DELETE" });
    if (!res.ok) throw new Error(await res.text());
    await loadSuites();
  } catch (err) {
    logLine("Failed to delete suite: " + err, "error");
  }
}

async function showSuite(suite) {
  try {
    const res = await fetch(`/api/suites/${suite.id}`);
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();

    currentSuiteId = data.id;
    diffPanelEl.hidden = true;
    suitePanelEl.hidden = false;
    suitePanelNameEl.value = data.name || "";
    suitePanelUrlEl.value = data.url || "";
    suiteCasesEl.innerHTML = "";
    (data.cases || []).forEach((c) => suiteCasesEl.appendChild(makeReviewCard(c.title, c.goal, c.steps, c.reset_before)));

    const runsRes = await fetch(`/api/suites/${suite.id}/runs`);
    const runsData = await runsRes.json();
    suiteRunsListEl.innerHTML = "";
    const runs = runsData.runs || [];
    suiteRunsEmptyEl.hidden = runs.length > 0;
    runs.forEach((run) => suiteRunsListEl.appendChild(buildRunHistoryRow(run)));

    suitePanelEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (err) {
    logLine("Failed to load suite: " + err, "error");
  }
}

addSuiteCaseButton.addEventListener("click", () => {
  suiteCasesEl.appendChild(makeReviewCard("", "", [], true));
});

saveSuiteChangesButton.addEventListener("click", async () => {
  if (!currentSuiteId) return;
  const cases = collectCasesFrom(suiteCasesEl);
  if (cases.length === 0 && !confirm("The case list is empty. Save anyway? This suite will have no cases to run.")) {
    return;
  }
  const name = suitePanelNameEl.value.trim();
  const url = suitePanelUrlEl.value.trim();
  if (!name) {
    logLine("Suite name is required.", "error");
    return;
  }
  saveSuiteChangesButton.disabled = true;
  try {
    const res = await fetch(`/api/suites/${currentSuiteId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, url, cases }),
    });
    if (!res.ok) throw new Error(await res.text());
    logLine(`Suite "${name}" updated (${cases.length} case(s)).`);
    await loadSuites();
  } catch (err) {
    logLine("Failed to save suite changes: " + err, "error");
  } finally {
    saveSuiteChangesButton.disabled = false;
  }
});

async function showPastRun(run) {
  try {
    const res = await fetch(run.json_url);
    const data = await res.json();
    logEl.innerHTML = "";
    resultsEl.innerHTML = "";
    caseCards.clear();
    logLine(`Loaded past run ${run.run_id} (${data.url})`);
    saveSuiteResultsButton.hidden = !(data.results && data.results.length);
    (data.results || []).forEach((r) => {
      caseCard(r.case.id, r.case.title, r.case.goal, r.case.steps, r.case.reset_before);
      (r.steps || []).forEach((s) => addStepLine(r.case.id, s, run.run_id));
      finishCase({
        case_id: r.case.id,
        status: r.status,
        summary: r.summary,
        findings: r.findings,
        screenshot_url: `/reports/${run.run_id}/screenshots/${r.case.id}.png`,
      });
    });
  } catch (err) {
    logLine("Failed to load the report: " + err, "error");
  }
}

async function rerunPastRun(run) {
  try {
    const res = await fetch(run.json_url);
    const data = await res.json();
    const cfg = data.config || {};
    const body = {
      url: cfg.url,
      provider: cfg.provider || "ollama",
      model: cfg.model,
      openai_base_url: cfg.openai_base_url || "",
      ollama_host: cfg.ollama_host,
      headless: cfg.headless,
      channel: cfg.channel,
      max_cases: cfg.max_cases,
      max_steps: cfg.max_steps_per_case,
      num_ctx: cfg.num_ctx,
      auto_confirm_destructive: cfg.auto_confirm_destructive,
      focus_hint: cfg.focus_hint || "",
      precondition_goal: cfg.precondition_goal || "",
      planner_system_prompt: cfg.planner_system_prompt || "",
      executor_system_prompt: cfg.executor_system_prompt || "",
      fresh_browser_per_case: cfg.fresh_browser_per_case || false,
      review: false,
    };
    logLine(`Re-running ${run.run_id} (${body.url}) ...`);
    await launchRun(body);
  } catch (err) {
    logLine("Failed to re-run: " + err, "error");
  }
}

async function deletePastRun(run) {
  if (!confirm(`Delete run ${run.run_id}? This permanently removes its report and screenshots.`)) return;
  try {
    const res = await fetch(`/api/runs/${run.run_id}`, { method: "DELETE" });
    if (!res.ok) throw new Error(await res.text());
    await loadRuns();
  } catch (err) {
    logLine("Failed to delete run: " + err, "error");
  }
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  syncProviderHidden();
  const fd = new FormData(form);
  const body = {
    url: buildUrl(fd),
    provider: fd.get("provider"),
    model: currentModel(fd),
    openai_base_url: fd.get("openai_base_url") || "",
    api_key: currentApiKey(fd),
    workspace_id: fd.get("claude_workspace_id") || "",
    channel: fd.get("channel"),
    max_cases: Number(fd.get("max_cases")),
    max_steps: Number(fd.get("max_steps")),
    num_ctx: Number(fd.get("num_ctx")),
    headless: !fd.get("show_browser"),
    auto_confirm_destructive: !!fd.get("auto_confirm_destructive"),
    focus_hint: fd.get("focus_hint") || "",
    precondition_goal: fd.get("precondition") || "",
    planner_system_prompt: fd.get("planner_prompt") || "",
    executor_system_prompt: fd.get("executor_prompt") || "",
    review: !!fd.get("review"),
    fresh_browser_per_case: !!fd.get("fresh_browser_per_case"),
  };
  await launchRun(body);
});

stopButton.addEventListener("click", async () => {
  if (!currentRunId) return;
  stopButton.disabled = true;
  try {
    await fetch(`/api/run/${currentRunId}/stop`, { method: "POST" });
    logLine("Stop requested, the agent will finish its current step...");
  } catch (err) {
    logLine("Failed to stop the run: " + err, "error");
    stopButton.disabled = false;
  }
});

approveButton.addEventListener("click", async () => {
  if (!currentRunId) return;
  const cases = collectReviewCases();

  if (cases.length === 0 && !confirm("The test case list is empty. Run anyway?")) {
    return;
  }

  approveButton.disabled = true;
  try {
    const res = await fetch(`/api/run/${currentRunId}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cases }),
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    exitReviewMode();
    resultsEl.innerHTML = "";
    caseCards.clear();
    (data.cases || []).forEach((c) => caseCard(c.id, c.title, c.goal, c.steps, c.reset_before));
    logLine("Test cases approved, execution started.");
  } catch (err) {
    logLine("Failed to approve test cases: " + err, "error");
    approveButton.disabled = false;
  }
});

async function launchRun(body) {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  logEl.innerHTML = "";
  resultsEl.innerHTML = "";
  caseCards.clear();
  exitReviewMode();
  saveSuiteResultsButton.hidden = true;
  runButton.disabled = true;

  try {
    const res = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(await res.text());
    const { run_id } = await res.json();
    logLine(`Run ${run_id} started`);
    if (body.api_key || body.workspace_id) {
      // Cached server-side by now; never keep it sitting in the field.
      form.elements["claude_api_key"].value = "";
      form.elements["openai_api_key"].value = "";
      form.elements["claude_workspace_id"].value = "";
      loadCredentialStatus();
    }
    subscribe(run_id);
  } catch (err) {
    logLine("Failed to start the agent: " + err, "error");
    runButton.disabled = false;
  }
}

function subscribe(runId) {
  currentRunId = runId;
  stopButton.hidden = false;
  stopButton.disabled = false;
  eventSource = new EventSource(`/api/run/${runId}/events`);
  eventSource.onmessage = (e) => {
    const event = JSON.parse(e.data);
    handleEvent(event);
    if (event.type === "done" || event.type === "error") {
      eventSource.close();
      eventSource = null;
      endRun();
      loadRuns();
    }
  };
  eventSource.onerror = () => {
    logLine("Connection to the server was lost", "error");
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
    endRun();
  };
}

function endRun() {
  runButton.disabled = false;
  stopButton.hidden = true;
  stopButton.disabled = false;
  currentRunId = null;
  if (reviewMode) {
    exitReviewMode();
  }
}

function handleEvent(event) {
  switch (event.type) {
    case "info":
      logLine(event.message);
      break;
    case "error":
      logLine(event.message, "error");
      break;
    case "precondition_start":
      logLine(`Running precondition: ${event.goal}`);
      break;
    case "precondition_end":
      logLine(`  -> precondition ${event.status}: ${event.summary}`);
      break;
    case "cases_planned":
      logLine(`Planned ${event.cases.length} test case(s)`);
      event.cases.forEach((c) => caseCard(c.id, c.title, c.goal, c.steps, c.reset_before));
      break;
    case "awaiting_review":
      logLine(`${event.cases.length} case(s) awaiting review. Edit as needed and click "Approve & run".`);
      caseCards.clear();
      enterReviewMode(event.cases);
      break;
    case "review_approved":
      break;
    case "case_start":
      setCaseStatus(event.case_id, "running");
      break;
    case "step":
      addStepLine(event.case_id, event);
      break;
    case "case_end":
      finishCase(event);
      break;
    case "done":
      if (reviewMode) {
        exitReviewMode();
        resultsEl.innerHTML = "";
      }
      saveSuiteResultsButton.hidden = caseCards.size === 0;
      logLine(event.cancelled ? "Run stopped by the user." : `Done. Report: ${event.md_url}`);
      break;
    default:
      break;
  }
}

applyProviderVisibility();
loadModels(effectiveProvider());
loadCredentialStatus();
loadRuns();
loadSuites();
loadDefaultPrompts();
