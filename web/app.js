const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
if (tg) {
  tg.ready();
  tg.expand();
  if (tg.setHeaderColor) tg.setHeaderColor("#1c1c1c");
  if (tg.setBackgroundColor) tg.setBackgroundColor("#0b1424");
}

const $ = (id) => document.getElementById(id);

let token = null;
let pollTimer = null;
let toastTimer = null;
let term = null;
let fitAddon = null;
let socket = null;
let activeJobId = null;

const ICONS = {
  play: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M6 4.5v15l13-7.5z"/></svg>',
  terminal:
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="3"/><path d="m8 10 2.5 2.5L8 15"/><path d="M13.5 15H17"/></svg>',
  logs: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M5 6h14M5 12h14M5 18h9"/></svg>',
  stop: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
  copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2.5"/><path d="M6 15H5.5A1.5 1.5 0 0 1 4 13.5v-8A1.5 1.5 0 0 1 5.5 4h8A1.5 1.5 0 0 1 15 5.5V6"/></svg>',
  inbox:
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12.5 5.5 5h13L21 12.5V18a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z"/><path d="M3 12.5h5l1.2 2.2h5.6L16 12.5h5"/></svg>',
};

function icon(name) {
  const span = document.createElement("span");
  span.style.display = "inline-flex";
  span.innerHTML = ICONS[name] || "";
  return span;
}

function setMsg(el, text, isError) {
  el.textContent = text || "";
  el.classList.toggle("error-text", Boolean(isError));
}

function toast(text, kind) {
  const el = $("toast");
  el.textContent = text;
  el.className = "toast" + (kind ? " " + kind : "");
  el.hidden = false;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.hidden = true;
  }, 2600);
}

function startClock() {
  const el = $("clock");
  const tick = () => {
    const now = new Date();
    const date = now.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
    const time = now.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    el.textContent = date + "  " + time;
    el.title = now.toLocaleString();
  };
  tick();
  setInterval(tick, 15000);
}

function setupWindowControls() {
  document.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const win = button.closest(".window");
      if (!win) return;
      const action = button.dataset.action;
      if (action === "collapse") return win.classList.toggle("collapsed");
      if (action === "max") return win.classList.toggle("maxed");
      if (action === "close") {
        if (win.id === "terminal-panel") return closeTerminal();
        return win.classList.toggle("collapsed");
      }
    });
  });

  document.querySelectorAll(".titlebar").forEach((bar) => {
    bar.addEventListener("click", () => {
      const win = bar.closest(".window");
      if (win && win.classList.contains("collapsed")) win.classList.remove("collapsed");
    });
  });
}

async function authenticate() {
  const initData = tg ? tg.initData : "";
  const dev = new URLSearchParams(location.search).get("dev");
  const headers = {};
  if (initData) headers["X-Telegram-Init-Data"] = initData;
  const url = "/api/session" + (dev === "1" ? "?dev=1" : "");
  const res = await fetch(url, { method: "POST", headers });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Login failed");
  token = data.token;
  const user = data.user || {};
  const chip = $("who");
  chip.textContent = user.username ? "@" + user.username : user.first_name || "user";
  chip.hidden = false;
}

async function api(path, options) {
  const opts = Object.assign({}, options);
  opts.headers = Object.assign({ Authorization: "Bearer " + token }, opts.headers || {});
  if (opts.body && !opts.headers["Content-Type"]) {
    opts.headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

const STATUS_LABELS = {
  queued: "Queued",
  cloning: "Cloning",
  building: "Building",
  running: "Running",
  failed: "Failed",
  stopped: "Stopped",
  expired: "Expired",
};

function statusClass(status) {
  return STATUS_LABELS[status] ? status : "";
}

function formatDuration(seconds) {
  seconds = Math.max(0, Math.floor(seconds || 0));
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (d) return d + "d " + h + "h " + m + "m";
  if (h) return h + "h " + m + "m " + s + "s";
  if (m) return m + "m " + s + "s";
  return s + "s";
}

function timeAgo(epochSeconds) {
  if (!epochSeconds) return "";
  const diff = Math.max(0, Math.floor(Date.now() / 1000 - epochSeconds));
  if (diff < 60) return diff + "s ago";
  if (diff < 3600) return Math.floor(diff / 60) + "m ago";
  if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
  return Math.floor(diff / 86400) + "d ago";
}

function metricChip(label, value) {
  const span = document.createElement("span");
  span.className = "metric";
  span.append(label + " ");
  const b = document.createElement("b");
  b.textContent = value;
  span.append(b);
  return span;
}

function renderMetrics(metrics) {
  const box = document.createElement("div");
  box.className = "metrics";

  const health = document.createElement("span");
  health.className = "metric";
  const healthValue = document.createElement("b");
  healthValue.className =
    metrics.health === "healthy" ? "health ok" : metrics.health === "unhealthy" ? "health bad" : "health";
  healthValue.textContent = metrics.health || "unknown";
  health.append(healthValue);

  const latency = metrics.avg_latency_ms != null ? metrics.avg_latency_ms + "ms" : "-";

  box.append(
    health,
    metricChip("uptime", formatDuration(metrics.uptime_seconds)),
    metricChip("req", String(metrics.requests_total)),
    metricChip("failed", metrics.requests_failed + " (" + metrics.error_rate + "%)"),
    metricChip("latency", latency),
    metricChip("checks", metrics.health_checks_up + "/" + metrics.health_checks)
  );
  return box;
}

function openExternal(url) {
  if (tg && tg.openLink) tg.openLink(url, { try_instant_view: false });
  else window.open(url, "_blank", "noopener");
}

async function copyLink(url) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(url);
    } else {
      const input = document.createElement("input");
      input.value = url;
      document.body.append(input);
      input.select();
      document.execCommand("copy");
      input.remove();
    }
    toast("Link copied", "ok");
  } catch (err) {
    toast("Could not copy link", "err");
  }
}

function actionButton(label, iconName, className, onClick) {
  const button = document.createElement("button");
  button.className = "btn small " + (className || "");
  if (iconName) button.append(icon(iconName));
  button.append(document.createTextNode(label));
  button.addEventListener("click", onClick);
  return button;
}

function renderEmpty() {
  const box = document.createElement("div");
  box.className = "empty";
  box.append(icon("inbox"));
  const line1 = document.createElement("span");
  line1.textContent = "No jobs in this folder";
  const line2 = document.createElement("span");
  line2.textContent = "Paste a GitHub repo above and hit Run to start one.";
  box.append(line1, line2);
  return box;
}

function renderJobs(jobs) {
  const container = $("jobs");
  const count = $("job-count");
  container.innerHTML = "";
  count.textContent = jobs.length ? String(jobs.length) : "";
  count.style.display = jobs.length ? "inline-block" : "none";

  if (!jobs.length) {
    container.append(renderEmpty());
    return;
  }

  jobs.forEach((job) => {
    const card = document.createElement("div");
    card.className = "job";

    const head = document.createElement("div");
    head.className = "head";

    const titleWrap = document.createElement("div");
    const repo = document.createElement("div");
    repo.className = "repo";
    repo.textContent = job.repo_full_name;
    const sub = document.createElement("div");
    sub.className = "sub";
    sub.textContent =
      (job.branch ? job.branch + " - " : "") + timeAgo(job.created_at) + (job.is_api ? " - API" : "");
    titleWrap.append(repo, sub);

    const badge = document.createElement("span");
    badge.className = "badge " + statusClass(job.status);
    const dot = document.createElement("span");
    dot.className = "dot";
    badge.append(dot, document.createTextNode(STATUS_LABELS[job.status] || job.status));
    head.append(titleWrap, badge);

    card.append(head);

    if (job.error) {
      const error = document.createElement("div");
      error.className = "error";
      error.textContent = String(job.error).slice(0, 240);
      card.append(error);
    }

    if (job.status === "running" && job.metrics) {
      card.append(renderMetrics(job.metrics));
    }

    const actions = document.createElement("div");
    actions.className = "actions";

    if (job.status === "running" && job.preview_url) {
      const preview = document.createElement("a");
      preview.className = "primary-link";
      preview.href = job.preview_url;
      preview.append(icon("play"));
      preview.append(document.createTextNode(job.is_api ? "Open API" : "Open app"));
      preview.addEventListener("click", (event) => {
        event.preventDefault();
        openExternal(job.preview_url);
      });
      actions.append(preview);

      actions.append(actionButton("Copy link", "copy", "ghost", () => copyLink(job.preview_url)));
    }

    if (job.status === "running") {
      actions.append(actionButton("Terminal", "terminal", "", () => openTerminal(job)));
    }

    actions.append(actionButton("Logs", "logs", "ghost", () => toggleLogs(job, card)));

    if (!["stopped", "failed", "expired"].includes(job.status)) {
      actions.append(actionButton("Stop", "stop", "danger", () => stopJob(job.id)));
    }

    card.append(actions);
    container.append(card);
  });
}

async function loadJobs() {
  try {
    const data = await api("/api/jobs");
    renderJobs(data.jobs || []);
  } catch (err) {
    setMsg($("form-msg"), err.message, true);
  }
}

function setRunning(busy) {
  const button = $("run-btn");
  button.disabled = busy;
  button.classList.toggle("busy", busy);
  button.querySelector(".btn-label").textContent = busy ? "Starting..." : "Run repository";
}

async function runJob() {
  const repoUrl = $("repo-url").value.trim();
  const branch = $("repo-branch").value.trim();
  if (!repoUrl) {
    setMsg($("form-msg"), "Enter a GitHub repository URL.", true);
    return;
  }
  setRunning(true);
  setMsg($("form-msg"), "Queueing...");
  try {
    await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({ repo_url: repoUrl, branch: branch || null }),
    });
    setMsg($("form-msg"), "Job queued. Watch the status below.");
    toast("Job queued", "ok");
    $("repo-url").value = "";
    $("repo-branch").value = "";
    await loadJobs();
  } catch (err) {
    setMsg($("form-msg"), err.message, true);
  } finally {
    setRunning(false);
  }
}

async function stopJob(jobId) {
  try {
    await api("/api/jobs/" + jobId + "/stop", { method: "POST" });
    toast("Job stopped", "ok");
    await loadJobs();
  } catch (err) {
    setMsg($("form-msg"), err.message, true);
  }
}

async function toggleLogs(job, card) {
  const existing = card.querySelector("pre.log");
  if (existing) {
    existing.remove();
    return;
  }
  try {
    const data = await api("/api/jobs/" + job.id);
    const pre = document.createElement("pre");
    pre.className = "log";
    pre.textContent = data.job.log_tail || "(no output yet)";
    card.append(pre);
  } catch (err) {
    setMsg($("form-msg"), err.message, true);
  }
}

function ensureTerminal() {
  if (term) return;
  term = new Terminal({
    convertEol: true,
    fontSize: 13,
    fontFamily: '"DejaVu Sans Mono", "Liberation Mono", monospace',
    scrollback: 3000,
    cursorBlink: true,
    theme: { background: "#1d1d20", foreground: "#d8dbdf", cursor: "#f0f0f0" },
  });
  fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open($("terminal"));
  setTimeout(() => fitAddon.fit(), 60);
  term.onData((data) => {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(new TextEncoder().encode(data));
    }
  });
}

function sendResize() {
  if (!term || !fitAddon || $("terminal-panel").hidden) return;
  fitAddon.fit();
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: "resize", rows: term.rows, cols: term.cols }));
  }
}

function openTerminal(job) {
  $("terminal-panel").hidden = false;
  $("terminal-title").textContent = job.repo_full_name + " - Terminal";
  setMsg($("terminal-msg"), "");
  ensureTerminal();
  term.clear();
  if (socket) {
    socket.close();
    socket = null;
  }
  activeJobId = job.id;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url =
    proto + "://" + location.host + "/ws/terminal/" + job.id + "?token=" + encodeURIComponent(token);
  socket = new WebSocket(url);
  socket.binaryType = "arraybuffer";
  socket.onopen = () => {
    sendResize();
    term.focus();
  };
  socket.onmessage = (event) => {
    if (typeof event.data === "string") term.write(event.data);
    else term.write(new Uint8Array(event.data));
  };
  socket.onclose = () => term.writeln("\r\n[disconnected]");
  socket.onerror = () => setMsg($("terminal-msg"), "Terminal connection error.", true);
}

function closeTerminal() {
  if (socket) {
    socket.close();
    socket = null;
  }
  $("terminal-panel").hidden = true;
  $("terminal-panel").classList.remove("collapsed");
  activeJobId = null;
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    if (!document.hidden) loadJobs();
  }, 5000);
}

async function boot() {
  startClock();
  setupWindowControls();
  try {
    await authenticate();
  } catch (err) {
    setMsg($("form-msg"), err.message + " - open this page from the Telegram bot.", true);
    return;
  }
  $("run-btn").addEventListener("click", runJob);
  $("refresh-btn").addEventListener("click", async () => {
    await loadJobs();
    toast("Refreshed");
  });
  $("terminal-close").addEventListener("click", closeTerminal);
  $("terminal-panel").querySelector("[data-close]").addEventListener("click", closeTerminal);
  document.querySelectorAll("#repo-url, #repo-branch").forEach((input) => {
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") runJob();
    });
  });
  window.addEventListener("resize", sendResize);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) loadJobs();
  });
  await loadJobs();
  startPolling();
}

boot();
