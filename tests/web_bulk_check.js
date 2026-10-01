// Runs in the browser against test_web.py's isolated settings fixture.
(async () => {
  const $ = (selector) => document.querySelector(selector);
  const checks = [];
  function check(condition, label) { if (!condition) throw new Error(label); checks.push(label); }
  async function wait(predicate) {
    const deadline = Date.now() + 5000;
    while (!predicate()) {
      if (Date.now() > deadline) throw new Error("Timed out waiting for UI");
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
  }
  const idle = () => $("#list-region").getAttribute("aria-busy") === "false";
  const pending = () => Number($("#pending-count").textContent);
  const selected = () => document.querySelectorAll("[data-select]:checked").length;
  const token = $('meta[name="piluli-token"]').content;
  async function data(scope) {
    const response = await fetch(`/api/state?scope=${scope}`, { headers: { "X-Piluli-Token": token } });
    return (await response.json()).data;
  }
  function search(query) { $("#search").value = query; $("#search").dispatchEvent(new Event("input", { bubbles: true })); }
  async function apply() { $("#apply").click(); await wait(() => idle() && pending() === 0); }
  async function scope(value) { $(`[name="scope"][value="${value}"]`).click(); await wait(() => idle() && $(`[name="scope"][value="${value}"]`).checked); }

  await wait(() => idle() && $("[data-select]"));
  check(document.documentElement.lang === "en" && !/[\u0400-\u04ff]/.test(document.body.innerText), "English interface");
  const initial = await data("user");
  $('[data-view="skills"]').click();
  check(document.querySelectorAll("[data-select]").length === 2, "two fixture skills");
  $("[data-select]").click();
  check(selected() === 1 && $("#select-all").indeterminate && pending() === 0, "selection does not stage changes");
  $("#select-all").click();
  check(selected() === 2 && $("#select-all").checked, "select all filtered rows");
  $("#bulk-disable").click();
  check(pending() === 1, "bulk disable skips already disabled rows");
  check((await data("project")).revision === initial.revision, "bulk does not write before Apply");
  $("#bulk-enable").click();
  check(pending() === 1, "bulk enable removes reversals and skips unchanged rows");
  $("#bulk-disable").click();
  $("#clear-selection").click();
  check(selected() === 0 && pending() === 1, "clear selection preserves staged changes");
  $("#select-all").click();
  search("two");
  check(selected() === 0 && $("#bulk-enable").disabled, "search clears hidden selections");
  $("#select-all").click();
  $("#bulk-enable").click();
  check(pending() === 2 && selected() === 1, "filtered bulk only changes visible selected rows");
  await apply();
  const project = await data("project");
  check(project.resources.skills.map((row) => row.enabled).join() === "false,true", "Apply saves the project batch");
  check((await data("user")).resources.skills.map((row) => row.enabled).join() === "true,false", "project bulk preserves user settings");
  check(selected() === 0, "Apply clears selection");

  $("#select-all").click();
  $('[data-view="extensions"]').click();
  check(selected() === 0 && $("#select-all").disabled, "view change clears selection; empty results disable select all");
  search("");
  $("[data-select]").click();
  $(".switch").click();
  check(selected() === 1 && pending() === 1, "individual toggle preserves checkbox selection");
  $("#discard").click();
  await scope("user");
  check(selected() === 0, "scope change clears selection");
  $('[data-view="skills"]').click();
  search("two");
  $("#select-all").click();
  $("#bulk-enable").click();
  await apply();
  check((await data("user")).resources.skills.every((row) => row.enabled), "user bulk saves to user settings");
  check((await data("project")).resources.skills.map((row) => row.enabled).join() === "false,true", "user bulk preserves project overrides");

  $("#select-all").click();
  $("#bulk-disable").click();
  $('[name="scope"][value="project"]').click();
  await wait(() => $("#confirm-dialog").open);
  $('#confirm-dialog [value="cancel"]').click();
  await wait(() => !$("#confirm-dialog").open);
  check(pending() === 1 && $('[name="scope"][value="user"]').checked, "cancelled scope change preserves the batch");
  $("#discard").click();
  await scope("project");
  $('[data-view="packages"]').click();
  check($("#bulk-actions").hidden && $("#select-all").hidden && !$("[data-select]"), "no bulk resource controls in Packages");
  search("");
  $('[data-view="skills"]').click();
  $("[data-select]").focus();
  return { checks };
})()
