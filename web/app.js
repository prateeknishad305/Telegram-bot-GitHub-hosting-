const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
if (tg) {
  tg.ready();
  tg.expand();
  if (tg.setHeaderColor) tg.setHeaderColor("#0f1218");
}

const $ = (id) => document.getElementById(id);
let token = null;
let pollTimer = null;
let term = null;
let fitAddon = null;
let socket = null;
let activeJobId = null;

function setMsg(el, text, isError) {
  el.textContent = text || "";
  el.classList.toggle("error-text", Boolean(isError));
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
  $("who").textContent = user.username ? "@" + user.username : user.first_name || "user";
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

function statusClass(status) {
  return ["running", "failed", "building", "cloning", "queued"].includes(status) ? status : "";
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

function renderMetrics(metrics) {
  const box = document.createElement("div");
  box.className = "metrics";
  const healthClass = metrics.health === "healthy" ? "ok" : metrics.health === "unhealthy" ? "bad" : "";
  box.innerHTML =
    '<span class="metric"><b class="health ' +
    healthClass +
    '">' +
    metrics.health +
    "</b></span>" +
    '<span class="metric">uptime ' +
    formatDuration(metrics.uptime_seconds) +
    "</span>" +
    '<span class="metric">req ' +
    metrics.requests_total +
    "</span>" +
    '<span class="metric">failed ' +
    metrics.requests_failed +
    " (" +
    metrics.error_rate +
    "%)</span>" +
    '<span class="metric">' +
    (metrics.avg_latency_ms != null ? "lat " + metrics.avg_latency_ms + "ms" : "lat -") +
    "</span>" +
    '<span class="metric">checks ' +
    metrics.health_checks_up +
    "/" +
    metrics.health_checks +
    "</span>";
  return box;
}

function openExternal(url) {
  if (tg && tg.openLink) tg.openLink(url, { try_instant_view: false });
  else window.open(url, "_blank", "noopener");
}

function renderJobs(jobs) {
  const container = $("jobs");
  container.innerHTML = "";
  if (!jobs.length) {
    container.innerHTML = '<p class="msg">No jobs yet.</p>';
    return;
  }
  jobs.forEach((job) => {
    const card = document.createElement("div");
    card.className = "job";

    const head = document.createElement("div");
    head.className = "head";
    const repo = document.createElement("span");
    repo.className = "repo";
    repo.textContent = job.repo_full_name;
    const badge = document.createElement("span");
    badge.className = "badge " + statusClass(job.status);
    badge.textContent = job.status + (job.is_api ? " - api" : "");
    head.append(repo, badge);

    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent =
      "job " +
      job.id +
      (job.app_port ? " - port " + job.app_port : "") +
      (job.requested_port ? " (requested " + job.requested_port + ")" : "") +
      (job.run_command ? " - " + job.run_command : "") +
      (job.error ? " - " + String(job.error).slice(0, 120) : "");

    if (job.status === "building") {
      const build = document.createElement("div");
      build.className = "meta";
      const steps = job.build_total_steps
        ? job.build_step + "/" + job.build_total_steps
        : String(job.build_step || 0);
      build.textContent =
        "building: step " +
        steps +
        (job.build_eta_seconds != null ? " - ETA " + formatDuration(job.build_eta_seconds) : "");
      meta.append(build);
    }

    const actions = document.createElement("div");
    actions.className = "actions";

    if (job.status === "running" && job.preview_url) {
      const preview = document.createElement("a");
      preview.className = "primary-link";
      preview.href = job.preview_url;
      preview.textContent = job.is_api ? "Open API" : "Open app";
      preview.addEventListener("click", (event) => {
        event.preventDefault();
        openExternal(job.preview_url);
      });
      actions.append(preview);
    }

    if (job.status === "running") {
      const termBtn = document.createElement("button");
      termBtn.textContent = "Terminal";
      termBtn.addEventListener("click", () => openTerminal(job));
      actions.append(termBtn);
    }

    if (job.download_url) {
      const dlBtn = document.createElement("a");
      dlBtn.className = "ghost";
      dlBtn.href = job.download_url;
      dlBtn.textContent = "Download zip";
      dlBtn.addEventListener("click", (event) => {
        event.preventDefault();
        openExternal(job.download_url);
      });
      actions.append(dlBtn);
    }

    const logsBtn = document.createElement("button");
    logsBtn.className = "ghost";
    logsBtn.textContent = "Logs";
    logsBtn.addEventListener("click", () => toggleLogs(job, card));
    actions.append(logsBtn);

    if (!["stopped", "failed", "expired"].includes(job.status)) {
      const stopBtn = document.createElement("button");
      stopBtn.className = "danger";
      stopBtn.textContent = "Stop";
      stopBtn.addEventListener("click", () => stopJob(job.id));
      actions.append(stopBtn);
    }

    card.append(head, meta, actions);
    if (job.status === "running" && job.metrics) {
      card.append(renderMetrics(job.metrics));
    }
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

function fmtBytes(num) {
  let value = Math.max(0, Number(num) || 0);
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return (i === 0 ? value : value.toFixed(1)) + " " + units[i];
}

async function loadInfo() {
  try {
    const info = await api("/api/info");
    const disk = info.disk || {};
    const m = info.maintenance || {};
    const rows = [
      "Disk: " + (disk.percent != null ? disk.percent + "% used" : "?") +
        " (" + (info.disk_human ? info.disk_human.free + " free of " + info.disk_human.total : "") + ")",
      "Cache: " + (m.cache_human || "-") + ", cleaned every " +
        Math.round((m.cleanup_interval_seconds || 0) / 3600) + "h",
      "Active jobs: " + (info.active_jobs || 0) + " / " + (info.max_concurrent_jobs || 0),
    ];
    $("info").textContent = rows.join("  |  ");
  } catch (err) {
    $("info").textContent = "info unavailable: " + err.message;
  }
}

async function runJob() {
  const repoUrl = $("repo-url").value.trim();
  const branch = $("repo-branch").value.trim();
  const port = $("repo-port").value.trim();
  if (!repoUrl) {
    setMsg($("form-msg"), "Enter a GitHub repository or release archive URL.", true);
    return;
  }
  const button = $("run-btn");
  button.disabled = true;
  setMsg($("form-msg"), "Queueing...");
  try {
    await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({ repo_url: repoUrl, branch: branch || null, port: port || null }),
    });
    setMsg($("form-msg"), "Job queued. Watch the status below.");
    $("repo-url").value = "";
    $("repo-branch").value = "";
    $("repo-port").value = "";
    await loadJobs();
    await loadInfo();
  } catch (err) {
    setMsg($("form-msg"), err.message, true);
  } finally {
    button.disabled = false;
  }
}

async function stopJob(jobId) {
  try {
    await api("/api/jobs/" + jobId + "/stop", { method: "POST" });
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
    pre.style.cssText =
      "max-height:240px;overflow:auto;background:#0b0e13;padding:8px;border-radius:8px;font-size:12px;white-space:pre-wrap;";
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
    scrollback: 3000,
    theme: { background: "#0b0e13", foreground: "#e6e9ef", cursor: "#3b82f6" },
  });
  fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open($("terminal"));
  setTimeout(() => fitAddon.fit(), 50);
  term.onData((data) => {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(new TextEncoder().encode(data));
    }
  });
}

function sendResize() {
  if (!term || !fitAddon) return;
  fitAddon.fit();
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: "resize", rows: term.rows, cols: term.cols }));
  }
}

function openTerminal(job) {
  $("terminal-panel").hidden = false;
  $("terminal-title").textContent = "Terminal - " + job.repo_full_name;
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
  activeJobId = null;
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    if (!document.hidden) {
      loadJobs();
      loadInfo();
    }
  }, 5000);
}

async function boot() {
  try {
    await authenticate();
  } catch (err) {
    setMsg($("form-msg"), err.message + " - open this page from the Telegram bot.", true);
    return;
  }
  $("run-btn").addEventListener("click", runJob);
  $("refresh-btn").addEventListener("click", loadJobs);
  $("info-btn").addEventListener("click", loadInfo);
  $("terminal-close").addEventListener("click", closeTerminal);
  window.addEventListener("resize", sendResize);
  await loadJobs();
  await loadInfo();
  startPolling();
}

boot();
