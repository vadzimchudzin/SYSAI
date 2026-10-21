"use strict";
(() => {
  const root = document.getElementById("coordinator");
  const output = document.getElementById("pilot-result");
  let busy = false;
  async function post(operation, data) {
    if (busy) return;
    busy = true;
    output.textContent = "Выполняется один шаг…";
    try {
      const response = await fetch(`/api/coordinator/${operation}`, {
        method: "POST", credentials: "same-origin",
        headers: {"Content-Type": "application/json", "X-CSRF-Token": root.dataset.csrf},
        body: JSON.stringify(data)
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Ошибка");
      output.textContent = JSON.stringify(result);
      window.location.reload();
    } catch (error) { output.textContent = error.message; }
    finally { busy = false; }
  }
  document.getElementById("pilot-config").addEventListener("submit", event => {
    event.preventDefault();
    const form = event.currentTarget.elements;
    const ids = form.allowed.value.trim().split(",").filter(Boolean).map(value => Number(value.trim()));
    post("config", {enabled: form.enabled.checked, dry_run: form.dry_run.checked,
      allowed_employee_ids: ids, daily_actions: Number(form.daily.value)});
  });
  document.getElementById("pilot-enroll").addEventListener("submit", event => {
    event.preventDefault();
    post("enroll", {task_id: Number(event.currentTarget.elements.task.value)});
  });
  document.getElementById("pilot-tick").addEventListener("click", () => post("tick", {}));
  root.querySelectorAll(".pilot-review").forEach(button => button.addEventListener("click", () =>
    post("review", {run_id: button.dataset.run, decision: button.dataset.decision})));
})();
