// Interactive terminal for long-running workspaces: xterm.js over a WebSocket to a bash on a PTY
// in the workspace (protocol: src/api/terminal_ws.py). Loaded from 'self' only (CSP).
(() => {
  const el = document.getElementById("xterm");
  if (!el || !window.Terminal) return;
  const term = new window.Terminal({
    cursorBlink: true,
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace',
    fontSize: 14,
    scrollback: 5000,
    theme: { background: "#0d1311", foreground: "#d7e2dd", cursor: "#5fd3b0" },
  });
  const fit = new window.FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(el);
  fit.fit();

  const encoder = new TextEncoder();
  let ws = null;
  let connected = false;

  const connect = () => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const query = new URLSearchParams({ csrf: el.dataset.csrf, rows: term.rows, cols: term.cols });
    ws = new WebSocket(`${proto}://${location.host}/console/workspaces/${el.dataset.session}/terminal?${query}`);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => { connected = true; term.focus(); };
    ws.onmessage = (event) => {
      term.write(typeof event.data === "string" ? event.data : new Uint8Array(event.data));
    };
    ws.onclose = (event) => {
      const wasConnected = connected;
      connected = false;
      const why = event.code === 1008
        ? "The workspace has closed, or your console session expired. Reload the page."
        : wasConnected ? "Disconnected." : "Couldn't connect.";
      term.write(`\r\n\x1b[33m${why} Press Enter to reconnect.\x1b[0m\r\n`);
    };
  };

  term.onData((data) => {
    if (connected && ws.readyState === WebSocket.OPEN) {
      ws.send(encoder.encode(data));
    } else if (data === "\r" && (!ws || ws.readyState === WebSocket.CLOSED)) {
      connect();
    }
  });
  term.onResize(({ rows, cols }) => {
    if (connected) ws.send(JSON.stringify({ type: "resize", rows, cols }));
  });
  let pending = null;
  window.addEventListener("resize", () => {
    clearTimeout(pending);
    pending = setTimeout(() => fit.fit(), 100);
  });

  connect();
})();
