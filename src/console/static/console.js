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
