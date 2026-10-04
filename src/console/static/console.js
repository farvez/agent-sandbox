// Console helpers: copy buttons, confirmations, local times. Loaded from 'self' only (CSP).
document.addEventListener("click", async (event) => {
  const button = event.target.closest("button.copy");
  if (!button) return;
  const source = document.getElementById(button.dataset.copy);
  if (!source) return;
  const text = source.textContent.trim();
  try {
    await navigator.clipboard.writeText(text);
    button.textContent = "Copied";
  } catch {
    const range = document.createRange();          // fallback: select it for Ctrl+C
    range.selectNodeContents(source);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    button.textContent = "Press Ctrl+C";
  }
  setTimeout(() => { button.textContent = "Copy"; }, 2000);
});

document.addEventListener("submit", (event) => {
  const message = event.target.dataset.confirm;
  if (message && !window.confirm(message)) event.preventDefault();
});

document.querySelectorAll("time[data-ts]").forEach((el) => {
  const seconds = Number(el.dataset.ts);
  if (seconds) el.textContent = new Date(seconds * 1000).toLocaleString();
});

// Busy label on slow submits (starting a workspace pulls a repository).
document.addEventListener("submit", (event) => {
  if (event.defaultPrevented) return;
  const button = event.target.querySelector("button[data-busy]");
  if (button) { button.disabled = true; button.textContent = button.dataset.busy; }
});

// Workspace terminal: each line is POSTed to /console/workspaces/<id>/exec. All output is
// inserted as text (never HTML), so nothing a sandbox prints can run in this page.
(() => {
  const root = document.getElementById("terminal");
  if (!root) return;
  const output = document.getElementById("term-output");
  const form = document.getElementById("term-form");
  const input = document.getElementById("term-command");
  const prompt = document.getElementById("term-prompt");
  const timeout = document.getElementById("term-timeout");
  const base = `/console/workspaces/${root.dataset.session}`;
  const historyKey = `airlock-history-${root.dataset.session}`;
  let cwd = root.dataset.cwd || root.dataset.home;
  let history = [];
  try { history = JSON.parse(sessionStorage.getItem(historyKey) || "[]"); } catch { history = []; }
  let position = history.length;

  const showPrompt = () => {
    const home = root.dataset.home;
    const shown = cwd === home ? "~" : cwd.startsWith(home + "/") ? "~" + cwd.slice(home.length) : cwd;
    prompt.textContent = `${shown}$`;
  };
  const line = (text, cls) => {
    if (!text) return;
    const el = document.createElement("div");
    el.className = `term-line ${cls || ""}`;
    el.textContent = text;
    output.appendChild(el);
    output.scrollTop = output.scrollHeight;
    return el;
  };
  const post = async (path, body) => {
    const response = await fetch(base + path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": root.dataset.csrf },
      body: JSON.stringify(body),
      credentials: "same-origin",
    });
    let data = {};
    try { data = await response.json(); } catch { data = { error: `HTTP ${response.status}` }; }
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  };
  const busy = (on) => { form.querySelectorAll("input, button, select").forEach((el) => { el.disabled = on; }); };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const command = input.value;
    if (!command.trim()) return;
    input.value = "";
    if (history[history.length - 1] !== command) history.push(command);
    history = history.slice(-200);
    position = history.length;
    try { sessionStorage.setItem(historyKey, JSON.stringify(history)); } catch { /* private mode */ }
    if (command.trim() === "clear") { output.replaceChildren(); return; }

    line(`${prompt.textContent} ${command}`, "cmd");
    const pending = line("running…", "muted");
    const started = performance.now();
    busy(true);
    try {
      const r = await post("/exec", { command, cwd, timeout: Number(timeout.value) });
      pending.remove();
      line(r.stdout.replace(/\n$/, ""));
      line(r.stderr.replace(/\n$/, ""), "err");
      (r.warnings || []).forEach((w) => line(`⚠ ${w}`, "warn"));
      const seconds = ((performance.now() - started) / 1000).toFixed(1);
      if (r.timed_out) line(`timed out after ${timeout.value}s`, "warn");
      else if (r.exit_code !== 0) line(`exit ${r.exit_code} · ${seconds}s`, "warn");
      cwd = r.cwd || cwd;
      showPrompt();
    } catch (error) {
      pending.remove();
      line(error.message, "err");
    } finally {
      busy(false);
      input.focus();
    }
  });

  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowUp" && position > 0) {
      position -= 1; input.value = history[position]; event.preventDefault();
    } else if (event.key === "ArrowDown") {
      position = Math.min(position + 1, history.length);
      input.value = history[position] || ""; event.preventDefault();
    } else if (event.key === "l" && event.ctrlKey) {
      output.replaceChildren(); event.preventDefault();
    }
  });

  const importForm = document.getElementById("import-form");
  importForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const fields = Object.fromEntries(new FormData(importForm));
    const button = importForm.querySelector("button");
    button.disabled = true;
    const pending = line(`importing ${fields.repo}…`, "muted");
    try {
      const r = await post("/import", fields);
      pending.remove();
      line(`Imported ${r.repo}@${r.ref}: ${r.files} files into ${r.path}`, "ok");
      cwd = r.path;
      showPrompt();
      importForm.reset();
    } catch (error) {
      pending.remove();
      line(`Import failed: ${error.message}`, "err");
    } finally {
      button.disabled = false;
      input.focus();
    }
  });

  showPrompt();
  if (window.matchMedia("(min-width: 700px)").matches) input.focus({ preventScroll: true });
})();
