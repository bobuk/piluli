(() => {
  "use strict";
  const $ = (selector) => document.querySelector(selector);
  const token = $('meta[name="piluli-token"]').content;
  const views = ["extensions", "skills", "prompts", "themes", "packages"];
  const names = { extensions: "Extensions", skills: "Skills", prompts: "Prompts", themes: "Themes", packages: "Packages" };
  const origins = { user: "User", project: "Project", builtin: "Built-in" };
  const state = { scope: $('meta[name="piluli-scope"]').content, view: "extensions", data: null, pending: new Map(), marked: new Set(), selected: null, query: "", busy: false };
  const escape = (text) => String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const effective = (row) => state.pending.has(row.id) ? state.pending.get(row.id) : row.enabled;
  const allRows = () => !state.data ? [] : state.view === "packages" ? state.data.packages : state.data.resources[state.view];
  const rows = () => allRows().filter((row) => [row.name, row.source, row.path, row.description].some((value) => String(value || "").toLocaleLowerCase().includes(state.query.toLocaleLowerCase())));
  function notice(text, error = false) {
    $("#notice").textContent = text;
    $("#notice").hidden = !text;
    $("#notice").classList.toggle("error", error);
    $("#install-notice").textContent = text;
    $("#install-notice").hidden = !text;
  }
  function controls() {
    document.querySelectorAll("button, input").forEach((control) => { control.disabled = state.busy; });
    $("#apply").disabled = state.busy || !state.pending.size;
    $("#discard").disabled = state.busy || !state.pending.size;
    if (!state.data) $("#install").disabled = true;
    $("#update-all").hidden = state.view !== "packages";
    $("#update-all").disabled = state.busy || !state.data?.packages.some((pkg) => pkg.manageable);
    document.querySelectorAll("[data-locked]").forEach((button) => { button.disabled = true; });
    $("#list-region").setAttribute("aria-busy", String(state.busy));
    $("#apply-count").textContent = state.pending.size ? `(${state.pending.size})` : "";
    $("#pending-count").textContent = state.pending.size;
    $("#pending-count").classList.toggle("changed", !!state.pending.size);
    bulkControls();
  }
  function bulkControls() {
    const visible = state.view === "packages" ? [] : rows();
    const ids = new Set(visible.map((row) => row.id));
    // Selection never includes hidden resources or resources from another view.
    for (const id of state.marked) if (!ids.has(id)) state.marked.delete(id);
    const count = state.marked.size;
    $("#bulk-actions").hidden = state.view === "packages";
    $("#selection-count").textContent = `${count} selected`;
    $("#select-all").hidden = state.view === "packages";
    $("#select-all").disabled = state.busy || !visible.length;
    $("#select-all").checked = visible.length > 0 && count === visible.length;
    $("#select-all").indeterminate = count > 0 && count < visible.length;
    for (const id of ["bulk-enable", "bulk-disable", "clear-selection"]) $("#" + id).disabled = state.busy || !count;
    document.querySelectorAll("[data-select]").forEach((input) => {
      input.checked = state.marked.has(input.dataset.select);
      input.closest("tr").classList.toggle("marked", input.checked);
    });
  }
  function render(focusList = false) {
    const visible = rows();
    if (!visible.some((row) => row.id === state.selected)) state.selected = visible[0]?.id || null;
    const isPackage = state.view === "packages";
    const user = state.scope === "user";
    $("#view-title").textContent = names[state.view];
    $("#scope-label").textContent = user ? "USER" : "CURRENT PROJECT";
    $("#scope-description").textContent = user
      ? "Applies across projects. Project overrides take precedence."
      : "Only this project. User settings stay unchanged.";
    document.querySelectorAll('[name="scope"]').forEach((input) => { input.checked = input.value === state.scope; });
    document.querySelectorAll("[data-view]").forEach((button) => {
      const active = button.dataset.view === state.view;
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
    });
    if (state.data) {
      document.querySelectorAll("[data-count]").forEach((el) => {
        el.textContent = el.dataset.count === "packages" ? state.data.packages.length : state.data.resources[el.dataset.count].length;
      });
      $("#settings-path").textContent = state.data.settingsPath;
      $("#project-path").textContent = state.data.projectDir;
      $("#project-name").textContent = state.data.projectDir.split(/[\\/]/).filter(Boolean).pop() || state.data.projectDir;
    }
    $("#total").textContent = allRows().length;
    $("#enabled-label").textContent = isPackage ? "Installed" : "Enabled";
    $("#enabled-count").textContent = allRows().filter((row) => isPackage ? row.installed : effective(row)).length;
    $("#inherited-label").textContent = user ? "From packages" : "Inherited";
    $("#inherited-count").textContent = allRows().filter((row) => user ? (isPackage || row.packageId) : row.inherited).length;
    $("#result-count").textContent = `${visible.length} of ${allRows().length} · ${user ? "user" : "project"}`;
    $("#name-column").textContent = isPackage ? "Package" : "Resource";
    $("#actions-column").textContent = isPackage ? "Actions" : user ? "For user" : "In project";
    $("#rows").innerHTML = visible.length ? visible.map((row) => {
      const changed = state.pending.has(row.id);
      const enabled = isPackage ? row.installed : effective(row);
      const status = isPackage ? (row.installed ? "Installed" : "Not installed") : changed ? (enabled ? "Enable *" : "Disable *") : (enabled ? "Enabled" : "Disabled");
      const actions = isPackage
        ? row.manageable ? `<button class="row-action" data-action="update" aria-label="Update ${escape(row.name)}" title="Reinstall in this scope">↻</button><button class="row-action" data-action="remove" aria-label="Remove ${escape(row.name)}" title="Remove package">×</button>` : '<span class="origin">Inherited</span>'
        : `<button class="switch" role="switch" aria-checked="${enabled}" data-action="toggle" aria-label="${escape(row.name)}: ${user ? "for user" : "in project"}"></button>`;
      return `<tr data-id="${row.id}" class="${row.id === state.selected ? "selected" : ""} ${changed ? "pending" : ""}" aria-selected="${row.id === state.selected}" tabindex="${row.id === state.selected ? "0" : "-1"}">
        <td><div class="item-heading">${isPackage ? "" : `<input type="checkbox" class="bulk-check" data-select="${row.id}" aria-label="Select ${escape(row.name)}">`}<span class="item-icon" aria-hidden="true">${escape(row.name.slice(0, 1).toUpperCase())}</span><div class="item-info"><strong>${escape(row.name)}</strong><small title="${escape(row.source)}">${escape(row.source)}${row.version ? ` · v${escape(row.version)}` : ""}</small></div></div></td>
        <td><span class="origin ${row.origin === "user" ? "user" : ""}">${origins[row.origin] || escape(row.origin)}</span></td>
        <td><span class="state ${changed ? "pending" : enabled ? "" : "off"}">${status}</span></td><td><div class="actions">${actions}</div></td></tr>`;
    }).join("") : '<tr><td colspan="4" class="empty">No resources found. Change your search or install a package.</td></tr>';
    renderDetails(visible.find((row) => row.id === state.selected));
    controls();
    if (focusList) focusSelected();
  }
  function renderDetails(row) {
    $("#detail-name").textContent = row?.name || "Choose a resource";
    $("#detail-description").textContent = row?.description || "";
    $("#detail-path").textContent = row?.path || "";
  }
  function focusSelected() {
    const row = $("tr.selected") || $("#resource-table");
    row.focus({ preventScroll: true });
    row.scrollIntoView({ block: "nearest", behavior: "instant" });
  }
  async function request(url, body) {
    const response = await fetch(url, { method: body ? "POST" : "GET", headers: { "X-Piluli-Token": token, "Content-Type": "application/json" }, ...(body ? { body: JSON.stringify(body) } : {}) });
    let result;
    try { result = await response.json(); } catch (_) { throw new Error(`Server returned HTTP ${response.status}`); }
    if (!response.ok || !result.ok) throw new Error(result.error || `HTTP ${response.status}`);
    return result;
  }
  async function load(scope = state.scope) {
    const result = await request(`/api/state?scope=${scope}`);
    state.scope = scope;
    state.data = result.data;
    state.marked.clear();
    render();
  }
  async function work(action) {
    if (state.busy) return;
    state.busy = true; controls();
    try { await action(); } catch (error) { notice(error.message, true); }
    finally { state.busy = false; controls(); }
  }
  function confirm(title, text, label = "Continue") {
    const dialog = $("#confirm-dialog");
    $("#confirm-title").textContent = title;
    $("#confirm-text").textContent = text;
    $("#confirm-ok").textContent = label;
    dialog.returnValue = "cancel";
    dialog.showModal();
    return new Promise((resolve) => dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true }));
  }
  async function discardFor(reason) {
    if (!state.pending.size) return true;
    if (!await confirm("Unsaved changes", `${reason}\nDiscard ${state.pending.size} changes?`, "Discard and continue")) return false;
    state.pending.clear(); render(); return true;
  }
  async function changeScope(scope) {
    if (state.busy || scope === state.scope) return;
    render();
    if (!await discardFor("Switch settings scope.")) return;
    await work(async () => { await load(scope); notice(""); });
  }
  function stage(row, enabled) {
    if (enabled === row.enabled) state.pending.delete(row.id); else state.pending.set(row.id, enabled);
  }
  function toggle(row) {
    if (state.busy || !row || state.view === "packages") return;
    stage(row, !effective(row));
    render(true);
  }
  function bulk(enabled) {
    if (state.busy || state.view === "packages") return;
    for (const row of rows()) if (state.marked.has(row.id)) stage(row, enabled);
    render();
  }
  async function apply() {
    if (!state.pending.size || !state.data || state.busy) return;
    await work(async () => {
      const result = await request("/api/apply", { scope: state.scope, revision: state.data.revision, changes: [...state.pending].map(([id, enabled]) => ({ id, enabled })) });
      state.pending.clear();
      await load();
      notice(result.message);
      focusSelected();
    });
  }
  async function packageAction(action, row, source) {
    if (!state.data || state.busy) return;
    if (state.pending.size) { notice("Apply or discard pending changes first.", true); return; }
    if (action !== "install") {
      const target = state.scope === "user" ? "User — affects all projects" : "Current project";
      const explanation = action === "remove" ? "The package will be removed from this scope." : "The configured source will be reinstalled in this scope only. This may execute code with your permissions.";
      if (!await confirm(action === "remove" ? "Remove package?" : "Update packages?", `${row?.name || "All packages in this scope"}\n${target}\n\n${explanation}`, action === "remove" ? "Remove" : "Update")) return;
    }
    await work(async () => {
      notice("Running Pi…");
      const result = await request("/api/packages/action", { scope: state.scope, action, id: row?.id, source, revision: state.data.revision });
      await load();
      notice(result.message);
      if (action === "install") { $("#source").value = ""; $("#install-dialog").close(); }
    });
  }
  function view(name, focus = false) { if (state.busy) return; state.view = name; state.selected = null; state.marked.clear(); render(focus); }
  $("#select-all").addEventListener("change", (event) => {
    if (state.busy || state.view === "packages") return;
    state.marked.clear();
    if (event.target.checked) rows().forEach((row) => state.marked.add(row.id));
    bulkControls();
  });
  $("#rows").addEventListener("change", (event) => {
    if (state.busy || !event.target.matches("[data-select]")) return;
    const id = event.target.dataset.select;
    if (event.target.checked) state.marked.add(id); else state.marked.delete(id);
    bulkControls();
  });
  $("#bulk-enable").addEventListener("click", () => bulk(true));
  $("#bulk-disable").addEventListener("click", () => bulk(false));
  $("#clear-selection").addEventListener("click", () => { state.marked.clear(); bulkControls(); });
  $("#rows").addEventListener("click", (event) => {
    const tr = event.target.closest("tr[data-id]");
    if (!tr || state.busy || event.target.matches("[data-select]")) return;
    const row = rows().find((item) => item.id === tr.dataset.id);
    state.selected = row.id;
    const action = event.target.closest("button")?.dataset.action;
    if (action === "toggle") toggle(row);
    else if (action) packageAction(action, row);
    else render(true);
  });
  $("#rows").addEventListener("focusin", (event) => {
    const row = event.target.closest("tr[data-id]");
    if (row && row.dataset.id !== state.selected) {
      state.selected = row.dataset.id;
      renderDetails(rows().find((item) => item.id === state.selected));
      // Preserve native Tab focus; only update visual selection, not DOM nodes.
      document.querySelectorAll("tr[data-id]").forEach((tr) => {
        tr.classList.toggle("selected", tr === row); tr.setAttribute("aria-selected", String(tr === row)); tr.tabIndex = tr === row ? 0 : -1;
      });
    }
  });
  document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => view(button.dataset.view)));
  document.querySelectorAll('[name="scope"]').forEach((input) => input.addEventListener("change", () => changeScope(input.value)));
  $("#search").addEventListener("input", (event) => { state.query = event.target.value; state.marked.clear(); render(); });
  $("#apply").addEventListener("click", apply);
  $("#update-all").addEventListener("click", () => packageAction("update-all"));
  $("#discard").addEventListener("click", () => { state.pending.clear(); render(); notice("Changes discarded."); });
  $("#refresh").addEventListener("click", async () => { if (await discardFor("Refresh the resource list.")) work(async () => { await load(); notice("List refreshed."); }); });
  $("#install").addEventListener("click", () => {
    if (state.pending.size) { notice("Apply or discard pending changes first.", true); return; }
    $("#install-title").textContent = state.scope === "user" ? "Install for user" : "Install in project";
    $("#install-notice").hidden = true;
    $("#install-dialog").showModal();
    $("#source").focus();
  });
  $("#install-form").addEventListener("submit", (event) => { event.preventDefault(); packageAction("install", null, $("#source").value.trim()); });
  document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => document.getElementById(button.dataset.close).close()));
  document.querySelectorAll("dialog").forEach((dialog) => dialog.addEventListener("cancel", (event) => { if (state.busy) event.preventDefault(); }));
  document.addEventListener("keydown", (event) => {
    if (state.busy || $("dialog[open]") || event.altKey || event.ctrlKey || event.metaKey || event.isComposing) return;
    const target = event.target;
    if (target.matches("[data-select], #select-all")) {
      // Space is the checkbox's native selection action; Enter still applies.
      if (event.key === "Enter") { event.preventDefault(); apply(); return; }
      if (!["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    } else if (target.matches("input, textarea, select") || target.isContentEditable) {
      if (target === $("#search") && ["ArrowDown", "Escape", "Enter"].includes(event.key)) { event.preventDefault(); focusSelected(); }
      return;
    }
    const visible = rows();
    let index = Math.max(0, visible.findIndex((row) => row.id === state.selected));
    if (["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      index = event.key === "Home" ? 0 : event.key === "End" ? visible.length - 1 : Math.max(0, Math.min(visible.length - 1, index + (event.key === "ArrowDown" ? 1 : -1)));
      state.selected = visible[index]?.id || null; render(true);
    } else if (["ArrowLeft", "ArrowRight"].includes(event.key)) {
      event.preventDefault(); view(views[(views.indexOf(state.view) + (event.key === "ArrowRight" ? 1 : views.length - 1)) % views.length], true);
    } else if (event.key === " " && (target.closest("#resource-table") || target === document.body)) {
      event.preventDefault(); toggle(visible[index]);
    } else if (event.key === "Enter" && (target.closest("#resource-table") || target === document.body)) {
      event.preventDefault(); apply();
    } else if (event.key === "/") { event.preventDefault(); $("#search").focus(); }
    else if (event.key.toLowerCase() === "g") { event.preventDefault(); changeScope(state.scope === "project" ? "user" : "project"); }
    else if (event.key.toLowerCase() === "r") { event.preventDefault(); $("#refresh").click(); }
  });
  window.addEventListener("beforeunload", (event) => { if (state.pending.size || state.busy) { event.preventDefault(); event.returnValue = ""; } });
  work(() => load());
})();
