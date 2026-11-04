/* A list with a card panel (SYSAI_Hub, the storage): quick filter, chips, the card opens without reloading the page.
   Without JavaScript every link and form still works as a plain page. */
(function () {
  "use strict";
  const root = document.querySelector("[data-nx-root]");
  if (!root) return;
  root.classList.add("nx-js");
  const panel = root.querySelector("#nx-panel");
  const search = root.querySelector("[data-nx-search]");
  const select = root.querySelector("[data-nx-dept]");
  const chips = Array.from(root.querySelectorAll("[data-nx-chip]"));
  const narrow = window.matchMedia("(max-width: 1100px)");
  let chip = (root.querySelector("[data-nx-chip].on") || {dataset: {nxChip: "all"}}).dataset.nxChip;

  const tags = (row) => (row.dataset.tags || "").split(" ");
  const textOk = (row, text) => !text || (row.dataset.search || "").includes(text);
  const deptOk = (row) => !select || !select.value || row.dataset.dept === select.value;

  function filter() {
    const text = ((search && search.value) || "").trim().toLowerCase();
    const filtering = !!text || chip !== "all" || !!(select && select.value);
    let shown = 0;
    root.querySelectorAll("[data-nx-group]").forEach((group) => {
      let n = 0;
      group.querySelectorAll("[data-nx-row]").forEach((row) => {
        const ok = textOk(row, text) && deptOk(row) && (chip === "all" || tags(row).includes(chip));
        row.hidden = !ok;
        if (ok) n += 1;
      });
      group.hidden = n === 0 && filtering;  // without a filter an empty group stays, with its hint
      group.querySelectorAll(".nx-group-note").forEach((note) => { note.hidden = filtering; });
      const head = group.querySelector("[data-nx-label]");
      if (head) head.textContent = `${head.dataset.nxLabel} (${n})`;
      shown += n;
    });
    chips.forEach((button) => {
      const tag = button.dataset.nxChip;
      const out = button.querySelector(".nx-chip-count");
      if (!out) return;
      const seen = new Set();  // an item in the review queue and in its own group is one item
      root.querySelectorAll("[data-nx-row]").forEach((row) => {
        if ((tag === "all" || tags(row).includes(tag)) && textOk(row, text) && deptOk(row)) seen.add(row.dataset.item || row.dataset.key);
      });
      const n = seen.size;
      out.textContent = n ? String(n) : "";
      button.hidden = !n && tag !== "all" && !button.classList.contains("on");
    });
    const none = root.querySelector("[data-nx-none]");
    if (none) none.hidden = shown !== 0 || !filtering;
  }

  chips.forEach((button) => button.addEventListener("click", (event) => {
    if (button.tagName === "A") return;  // a server-side filter (the trash): a plain link
    event.preventDefault();
    chip = button.classList.contains("on") && button.dataset.nxChip !== "all" ? "all" : button.dataset.nxChip;
    chips.forEach((b) => b.classList.toggle("on", b.dataset.nxChip === chip));
    filter();
  }));
  if (search) search.addEventListener("input", filter);
  if (select) select.addEventListener("change", filter);
  filter();

  function modal(open) {
    document.body.classList.toggle("nx-modal", open && narrow.matches);
  }

  async function show(url, push) {
    if (!panel) { window.location.href = url; return; }
    panel.setAttribute("aria-busy", "true");
    try {
      const response = await fetch(url, {credentials: "same-origin", headers: {"X-Requested-With": "fetch"}});
      if (!response.ok || response.redirected && new URL(response.url).pathname !== new URL(url, location.href).pathname) {
        window.location.href = url;
        return;
      }
      const doc = new DOMParser().parseFromString(await response.text(), "text/html");
      const next = doc.querySelector("#nx-panel");
      if (!next) { window.location.href = url; return; }
      panel.innerHTML = next.innerHTML;
      const open = next.dataset.open === "1";
      root.classList.toggle("panel-open", open);
      const selected = next.dataset.selected || "";
      root.querySelectorAll("[data-nx-row]").forEach((row) => row.classList.toggle("selected", !!selected && row.dataset.key === selected));
      if (push) window.history.pushState({nx: true}, "", url);
      if (doc.title) document.title = doc.title;
      panel.scrollTop = 0;
      modal(open);
    } catch (error) {
      window.location.href = url;
    } finally {
      panel.removeAttribute("aria-busy");
    }
  }

  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-nx-open]");
    if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    show(link.href, true);
  });
  window.addEventListener("popstate", () => show(window.location.href, false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && root.classList.contains("panel-open")) {
      const close = panel && panel.querySelector(".nx-close");
      if (close) close.click();
    }
  });
  modal(root.classList.contains("panel-open"));

  // dropping files onto the list uploads them into the open folder (the storage)
  const drop = root.querySelector("[data-nx-drop]");
  const form = document.getElementById("nx-upload-form");
  if (drop && form) {
    const input = form.querySelector("input[type=file][name=files]");
    let depth = 0;
    drop.addEventListener("dragenter", (event) => { if (event.dataTransfer && Array.from(event.dataTransfer.types).includes("Files")) { depth += 1; drop.classList.add("nx-drop"); } });
    drop.addEventListener("dragleave", () => { depth = Math.max(0, depth - 1); if (!depth) drop.classList.remove("nx-drop"); });
    drop.addEventListener("dragover", (event) => { if (drop.classList.contains("nx-drop")) event.preventDefault(); });
    drop.addEventListener("drop", (event) => {
      depth = 0;
      drop.classList.remove("nx-drop");
      if (!event.dataTransfer || !event.dataTransfer.files.length || !input) return;
      event.preventDefault();
      input.files = event.dataTransfer.files;
      form.submit();
    });
  }
})();
