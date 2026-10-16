"use strict";

(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const ACTIVE_STATES = ["starting", "running", "paused", "waiting", "reconnecting"];
  const LEVELS = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };

  const ui = {
    tab: "farm",
    status: null,
    lastState: null,
    routines: [],
    routineId: null,
    routineDetail: null,
    optionsDirty: false,
    editorId: null,
    editorDirty: false,
    templates: [],
    scores: {},
    screenDetections: [],
    shownFrameId: null,
    previewFrameId: null,
    capture: null,
    logs: [],
    logLastId: 0,
    lastTap: null,
    jobActive: false,
  };

  // ================================================================= helpers
  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  async function api(path, { method = "GET", body } = {}) {
    const init = { method, cache: "no-store", headers: {} };
    if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    const res = await fetch(path, init);
    const type = res.headers.get("content-type") || "";
    const data = type.includes("json") ? await res.json() : await res.text();
    if (!res.ok) {
      let message = res.statusText || `HTTP ${res.status}`;
      if (data && data.detail) {
        message = Array.isArray(data.detail)
          ? data.detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("\n")
          : data.detail;
      } else if (typeof data === "string" && data) {
        message = data;
      }
      throw new Error(message);
    }
    return data;
  }

  let toastTimer = null;
  function toast(message, kind = "") {
    const el = $("#toast");
    el.textContent = message;
    el.className = `toast show ${kind}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), kind === "error" ? 7000 : 3000);
  }

  async function run(fn, okMessage) {
    try {
      const result = await fn();
      if (okMessage) toast(okMessage, "ok");
      return result;
    } catch (err) {
      toast(err.message || String(err), "error");
      return undefined;
    }
  }

  function fmtDuration(seconds) {
    seconds = Math.max(0, Math.floor(seconds || 0));
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
    if (m) return `${m}m ${String(s).padStart(2, "0")}s`;
    return `${s}s`;
  }

  function ago(ts) {
    if (!ts) return "–";
    return `${fmtDuration(Date.now() / 1000 - ts)} ago`;
  }

  function fmtClock(ts) {
    return new Date(ts * 1000).toLocaleTimeString([], {
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
  }

  function isActive(state) {
    return ACTIVE_STATES.includes(state);
  }

  // =================================================================== tabs
  function showTab(name) {
    if (!$(`#tab-${name}`)) name = "farm";
    ui.tab = name;
    $$(".tab").forEach((section) => { section.hidden = section.dataset.tab !== name; });
    $$(".tabbar button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
    window.scrollTo({ top: 0 });
    if (name === "screen") refreshScreen(true);
    if (name === "templates") { loadTemplates(); renderChecklist(); }
    if (name === "routines") loadEditor(ui.editorId || ui.routineId);
    if (name === "logs") pollLogs();
    if (name === "device") { renderDevice(); loadApps(); }
    if (name === "farm") updatePreview(true);
  }

  $$(".tabbar button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));

  // ================================================================= status
  async function pollStatus() {
    try {
      const status = await api("/api/status");
      ui.status = status;
      renderStatus(status);
    } catch (err) {
      const pill = $("#state-pill");
      pill.textContent = "offline";
      pill.className = "pill pill-error";
    } finally {
      setTimeout(pollStatus, document.hidden ? 6000 : 2000);
    }
  }

  function renderStatus(status) {
    const farm = status.farm;
    const pill = $("#state-pill");
    pill.textContent = farm.state;
    pill.className = `pill pill-${farm.state}`;
    const dot = $("#device-dot");
    dot.className = `dot ${status.device.connected ? "ok" : "bad"}`;
    dot.title = status.device.connected ? `Device ${status.device.serial} connected`
      : `Device not connected: ${status.device.error || "unknown"}`;
    $("#version").textContent = status.version;

    const active = isActive(farm.state);
    $("#farm-message").textContent = farm.message || "";
    $("#farm-routine-name").textContent = active ? farm.routine_name || "" : "";
    const countdown = $("#farm-countdown");
    if (farm.state === "waiting" && farm.wait_until) {
      countdown.textContent = `resumes in ${fmtDuration(farm.wait_until - Date.now() / 1000)}`;
      countdown.classList.remove("hidden");
    } else {
      countdown.classList.add("hidden");
    }

    const last = farm.last_session;
    $("#stat-runs").textContent = active ? farm.runs ?? 0 : last ? last.runs : 0;
    $("#stat-rate").textContent = active && farm.elapsed > 60 ? farm.runs_per_hour : "–";
    $("#stat-elapsed").textContent = active ? fmtDuration(farm.elapsed)
      : last ? fmtDuration(last.seconds) : "–";

    const facts = [];
    if (active) {
      if (farm.last_rule) facts.push(`Last action: <b>${esc(farm.last_rule)}</b> (${ago(farm.last_rule_at)})`);
      facts.push(`Last run: <b>${ago(farm.last_run_at)}</b>`);
      const extra = Object.entries(farm.counters || {}).filter(([k]) => k !== farm.run_counter);
      for (const [key, value] of extra) facts.push(`${esc(key.replace(/_/g, " "))}: <b>${value}</b>`);
      if (farm.recoveries) facts.push(`Recoveries: <b>${farm.recoveries}</b>`);
      if (farm.restarts) facts.push(`Game restarts: <b>${farm.restarts}</b>`);
    } else if (last) {
      facts.push(`Last session: <b>${esc(last.routine)}</b>, ${last.runs} runs in ${fmtDuration(last.seconds)}`);
    }
    if (!farm.active_now) facts.push(`Active hours: <b>${esc(farm.active_hours)}</b>`);
    if (!status.device.connected) facts.push(`<span style="color:var(--err)">Device not connected</span>`);
    $("#farm-facts").innerHTML = facts.map((f) => `<span>${f}</span>`).join("");

    $("#btn-start").classList.toggle("hidden", active);
    $("#btn-pause").classList.toggle("hidden", !active || farm.state === "paused");
    $("#btn-resume").classList.toggle("hidden", farm.state !== "paused");
    $("#btn-stop").classList.toggle("hidden", !active);
    $("#routine-select").disabled = active;
    $$("#options-form input, #options-form select").forEach((el) => { el.disabled = active; });
    $("#btn-save-options").disabled = active;

    const disabled = active ? farm.disabled_rules || [] : [];
    $("#disabled-rules-card").hidden = disabled.length === 0;
    $("#disabled-rules").innerHTML = disabled
      .map((r) => `<li><b>${esc(r.name)}</b>: ${esc(r.reason)}</li>`).join("");

    const life = status.lifetime || {};
    $("#life-runs").textContent = life.runs || 0;
    $("#life-sessions").textContent = life.sessions || 0;
    $("#life-time").textContent = fmtDuration(life.seconds || 0);

    if (ui.lastState && ui.lastState !== farm.state && !active) loadSessions();
    ui.lastState = farm.state;

    if (ui.tab === "farm") updatePreview(false);
    $("#screen-offline").classList.toggle("hidden", status.device.connected);
    if (ui.tab === "device") renderDevice();
  }

  function updatePreview(force) {
    const frameId = ui.status && ui.status.farm.frame_id;
    if (!frameId || (!force && frameId === ui.previewFrameId)) return;
    ui.previewFrameId = frameId;
    const img = $("#preview-img");
    img.onload = () => { img.classList.remove("hidden"); $("#preview-empty").classList.add("hidden"); };
    img.src = `/api/screen.jpg?frame=${frameId}&w=360`;
  }

  async function loadSessions() {
    const stats = await api("/api/stats").catch(() => null);
    if (!stats) return;
    const sessions = (stats.sessions || []).slice(-10).reverse();
    $("#sessions-list").innerHTML = sessions.length ? sessions.map((s) => {
      const when = new Date(s.started_at * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" });
      return `<li><b>${esc(when)}</b> · ${esc(s.routine)} · ${s.runs} runs in ${fmtDuration(s.seconds)}<br>`
        + `<span class="muted">${esc(s.reason)}</span></li>`;
    }).join("") : `<li class="muted">No sessions yet.</li>`;
  }

  // ========================================================== farm controls
  $("#btn-start").addEventListener("click", () => run(async () => {
    const routine = $("#routine-select").value;
    if (!routine) throw new Error("Pick a routine first");
    await api("/api/farm/start", { method: "POST", body: { routine, options: readOptions() } });
    ui.optionsDirty = false;
  }, "Farm started"));
  $("#btn-stop").addEventListener("click", () => run(
    () => api("/api/farm/stop", { method: "POST" }), "Farm stopped",
  ));
  $("#btn-pause").addEventListener("click", () => run(() => api("/api/farm/pause", { method: "POST" })));
  $("#btn-resume").addEventListener("click", () => run(() => api("/api/farm/resume", { method: "POST" })));
  $("#preview-card").addEventListener("click", () => showTab("screen"));

  // ================================================================ routines
  async function loadRoutines() {
    ui.routines = await api("/api/routines").catch(() => []);
    for (const select of [$("#routine-select"), $("#editor-select")]) {
      const current = select.id === "routine-select" ? ui.routineId : ui.editorId || ui.routineId;
      select.innerHTML = ui.routines.map((r) => `<option value="${esc(r.id)}">${esc(r.name)}${r.valid ? "" : " ⚠"}</option>`).join("");
      if (current && ui.routines.some((r) => r.id === current)) select.value = current;
    }
    if (!ui.routineId && ui.routines.length) ui.routineId = ui.routines[0].id;
    if (ui.routineId) await loadRoutineDetail(ui.routineId);
  }

  async function loadRoutineDetail(id) {
    try {
      ui.routineDetail = await api(`/api/routines/${encodeURIComponent(id)}`);
    } catch (err) {
      ui.routineDetail = null;
      toast(err.message, "error");
      return;
    }
    renderRoutineCard();
    renderChecklist();
  }

  function renderRoutineCard() {
    const detail = ui.routineDetail;
    const ready = $("#routine-ready");
    if (!detail) return;
    $("#routine-description").textContent = detail.description || "";
    ready.classList.remove("hidden", "ok", "warn", "error");
    if (!detail.valid) {
      ready.classList.add("error");
      ready.textContent = `This routine has errors:\n${detail.error}`;
    } else if (detail.ready) {
      ready.classList.add("ok");
      const missing = detail.templates.filter((t) => !t.captured && t.level === "recommended");
      ready.textContent = missing.length
        ? `Ready. Recommended templates still missing: ${missing.map((t) => t.name).join(", ")}`
        : "Ready: all required and recommended templates are captured.";
    } else {
      ready.classList.add("warn");
      ready.innerHTML = `Capture the required templates first: <b>${esc(detail.required_missing.join(", "))}</b> `
        + `(<a href="#templates">Templates tab</a>).`;
    }
    if (!ui.optionsDirty) buildOptionsForm(detail.options || []);
  }

  function buildOptionsForm(options) {
    const form = $("#options-form");
    form.innerHTML = "";
    for (const opt of options) {
      const id = `opt-${opt.key}`;
      const label = document.createElement("label");
      label.htmlFor = id;
      let input;
      if (opt.type === "bool") {
        label.className = "switch";
        input = document.createElement("input");
        input.type = "checkbox";
        input.checked = Boolean(opt.value);
        label.append(input);
        const span = document.createElement("span");
        span.textContent = opt.label || opt.key;
        label.append(span);
      } else {
        label.textContent = opt.label || opt.key;
        if (opt.type === "choice") {
          input = document.createElement("select");
          for (const choice of opt.choices || []) {
            const option = document.createElement("option");
            option.value = choice;
            option.textContent = choice;
            input.append(option);
          }
          input.value = opt.value;
        } else {
          input = document.createElement("input");
          if (opt.type === "number") {
            input.type = "number";
            input.step = "any";
            input.inputMode = "decimal";
            if (opt.min !== null && opt.min !== undefined) input.min = opt.min;
            if (opt.max !== null && opt.max !== undefined) input.max = opt.max;
          }
          input.value = opt.value;
        }
        label.append(input);
      }
      input.id = id;
      input.dataset.key = opt.key;
      input.dataset.type = opt.type;
      input.addEventListener("change", () => { ui.optionsDirty = true; });
      const wrap = document.createElement("div");
      wrap.append(label);
      if (opt.help) {
        const help = document.createElement("span");
        help.className = "option-help";
        help.textContent = opt.help;
        wrap.append(help);
      }
      form.append(wrap);
    }
    if (ui.status) {
      const active = isActive(ui.status.farm.state);
      $$("#options-form input, #options-form select").forEach((el) => { el.disabled = active; });
    }
  }

  function readOptions() {
    const values = {};
    for (const input of $$("#options-form [data-key]")) {
      const { key, type } = input.dataset;
      if (type === "bool") values[key] = input.checked;
      else if (type === "number") values[key] = input.value === "" ? null : Number(input.value);
      else values[key] = input.value;
    }
    return values;
  }

  $("#routine-select").addEventListener("change", async (e) => {
    ui.routineId = e.target.value;
    ui.optionsDirty = false;
    await loadRoutineDetail(ui.routineId);
  });
  $("#btn-save-options").addEventListener("click", () => run(async () => {
    await api(`/api/routines/${encodeURIComponent(ui.routineId)}/options`, {
      method: "PUT", body: { options: readOptions() },
    });
    ui.optionsDirty = false;
  }, "Options saved"));

  // ================================================================== screen
  function screenWidth() {
    const width = $("#screen-wrap").clientWidth || 400;
    return Math.min(1080, Math.max(360, Math.round(width * (window.devicePixelRatio || 1))));
  }

  let screenBusy = false;
  function deviceConnected() {
    return Boolean(ui.status && ui.status.device && ui.status.device.connected);
  }

  let lastAnalysis = 0;
  let liveUrl = null;
  let liveTimer = null;

  function liveFps() {
    return Number($("#live-fps").value) || 0;
  }

  async function errorText(res) {
    try {
      const data = await res.json();
      if (data && typeof data.detail === "string") return data.detail;
    } catch (err) {
      // not JSON
    }
    return res.statusText || `HTTP ${res.status}`;
  }

  function showBlob(blob, frameId) {
    return new Promise((resolve) => {
      const img = $("#screen-img");
      const url = URL.createObjectURL(blob);
      img.onload = () => {
        if (liveUrl) URL.revokeObjectURL(liveUrl);
        liveUrl = url;
        $("#screen-empty").classList.add("hidden");
        drawOverlay();
        resolve();
      };
      img.onerror = () => {
        URL.revokeObjectURL(url);
        resolve();
      };
      img.src = url;
      ui.shownFrameId = frameId;
    });
  }

  async function refreshScreen(force) {
    if (screenBusy) return;
    // Without a device every capture fails: wait for a manual Refresh or a connection.
    if (!force && ui.status && !deviceConnected()) return;
    screenBusy = true;
    try {
      const farm = ui.status && ui.status.farm;
      const active = farm && isActive(farm.state);
      // While farming, frames come from the farm loop: ask only for a newer one.
      const after = !force && ui.shownFrameId ? `&after=${ui.shownFrameId}` : "";
      let res;
      try {
        res = await fetch(`/api/screen/live.jpg?w=${screenWidth()}${after}`, { cache: "no-store" });
      } catch (err) {
        if (force) toast("The farmer is not reachable", "error");
        return;
      }
      if (res.status === 204) return;
      if (!res.ok) {
        if (force) toast(await errorText(res), "error");
        return;
      }
      const frameId = Number(res.headers.get("X-Frame-Id"));
      const blob = await res.blob();
      if (active) ui.screenDetections = farm.detections || [];
      else if (!$("#toggle-boxes").checked) ui.screenDetections = [];
      await showBlob(blob, frameId);
      if (!active && $("#toggle-boxes").checked && (force || Date.now() - lastAnalysis > 1500)) {
        lastAnalysis = Date.now();
        api(`/api/screen/analyze?frame=${frameId}`).then((r) => {
          if (ui.shownFrameId !== frameId) return;
          ui.screenDetections = r.results.filter((d) => d.found);
          drawOverlay();
        }).catch(() => {});
      }
    } finally {
      screenBusy = false;
    }
  }

  // The live view: one frame at a time, at most "Live" frames per second.
  async function liveTick() {
    clearTimeout(liveTimer);
    const fps = liveFps();
    if (fps && ui.tab === "screen" && !document.hidden && deviceConnected()) {
      const started = Date.now();
      await refreshScreen(false);
      liveTimer = setTimeout(liveTick, Math.max(50, 1000 / fps - (Date.now() - started)));
    } else {
      liveTimer = setTimeout(liveTick, 1000);
    }
  }

  function drawOverlay() {
    const img = $("#screen-img");
    const canvas = $("#screen-overlay");
    const width = img.clientWidth;
    const height = img.clientHeight;
    if (!width || !height) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    if ($("#toggle-boxes").checked) {
      ctx.lineWidth = 2;
      ctx.font = "600 11px system-ui, sans-serif";
      for (const det of ui.screenDetections) {
        const [x, y, w, h] = det.box;
        ctx.strokeStyle = "#3ecf8e";
        ctx.strokeRect(x * width, y * height, w * width, h * height);
        const label = `${det.name} ${det.score.toFixed(2)}`;
        const tw = ctx.measureText(label).width + 8;
        ctx.fillStyle = "rgba(16,18,23,.85)";
        ctx.fillRect(x * width, Math.max(0, y * height - 16), tw, 16);
        ctx.fillStyle = "#3ecf8e";
        ctx.fillText(label, x * width + 4, Math.max(11, y * height - 4));
      }
    }
    if (ui.lastTap && Date.now() - ui.lastTap.t < 900) {
      ctx.strokeStyle = "#ff9d2e";
      ctx.lineWidth = 3;
      ctx.beginPath();
      ctx.arc(ui.lastTap.x * width, ui.lastTap.y * height, 16, 0, Math.PI * 2);
      ctx.stroke();
    }
  }

  function relPos(e, el) {
    const rect = el.getBoundingClientRect();
    return {
      x: Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width)),
      y: Math.min(1, Math.max(0, (e.clientY - rect.top) / rect.height)),
    };
  }

  (() => {
    const canvas = $("#screen-overlay");
    let start = null;
    canvas.addEventListener("pointerdown", (e) => {
      if (!$("#toggle-control").checked) return;
      e.preventDefault();
      canvas.setPointerCapture(e.pointerId);
      start = { ...relPos(e, canvas), t: Date.now() };
    });
    canvas.addEventListener("pointerup", (e) => {
      if (!start) return;
      const end = relPos(e, canvas);
      const begin = start;
      start = null;
      const distance = Math.hypot(end.x - begin.x, end.y - begin.y);
      run(async () => {
        if (distance < 0.02) {
          ui.lastTap = { x: begin.x, y: begin.y, t: Date.now() };
          drawOverlay();
          await api("/api/device/tap", { method: "POST", body: { x: begin.x, y: begin.y } });
        } else {
          const ms = Math.min(1500, Math.max(120, Date.now() - begin.t));
          await api("/api/device/swipe", {
            method: "POST", body: { x1: begin.x, y1: begin.y, x2: end.x, y2: end.y, ms },
          });
        }
        setTimeout(() => refreshScreen(true), 350);
      });
    });
    canvas.addEventListener("pointercancel", () => { start = null; });
  })();

  $("#toggle-control").addEventListener("change", (e) => {
    $("#screen-wrap").classList.toggle("control", e.target.checked);
    e.target.blur(); // so the keys typed next go to Android, not to this switch
  });
  $("#toggle-boxes").addEventListener("change", () => refreshScreen(false));
  $("#btn-refresh-screen").addEventListener("click", () => refreshScreen(true));
  $$("[data-devkey]").forEach((b) => b.addEventListener("click", () => run(async () => {
    await api("/api/device/key", { method: "POST", body: { key: b.dataset.devkey } });
    setTimeout(() => refreshScreen(true), 350);
  })));
  $$("[data-app]").forEach((b) => b.addEventListener("click", () => run(async () => {
    await api("/api/device/app", { method: "POST", body: { action: b.dataset.app } });
    setTimeout(() => refreshScreen(true), 2500);
  }, "Done")));
  async function typeText() {
    const input = $("#type-text");
    const text = input.value;
    if (!text) return;
    await run(async () => {
      await api("/api/device/text", { method: "POST", body: { text } });
      input.value = "";
      setTimeout(() => refreshScreen(true), 350);
    });
  }
  $("#btn-type").addEventListener("click", typeText);
  $("#type-text").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); typeText(); }
  });
  window.addEventListener("resize", () => { drawOverlay(); drawCapture(); });

  try {
    const saved = localStorage.getItem("liveFps");
    if (saved !== null && $(`#live-fps option[value="${saved}"]`)) $("#live-fps").value = saved;
  } catch (err) {
    // storage blocked: keep the default
  }
  $("#live-fps").addEventListener("change", () => {
    try {
      localStorage.setItem("liveFps", $("#live-fps").value);
    } catch (err) {
      // storage blocked
    }
    $("#live-fps").blur();
    liveTick();
  });

  // Keyboard: with Touch control on, keys go to the device. Requests go one at a time;
  // what is typed meanwhile is sent with the next one (text joined, held keys counted).
  const KEY_MAP = {
    Backspace: "DEL", Delete: "FORWARD_DEL", Enter: "ENTER", Escape: "BACK", Tab: "TAB",
    ArrowUp: "DPAD_UP", ArrowDown: "DPAD_DOWN", ArrowLeft: "DPAD_LEFT", ArrowRight: "DPAD_RIGHT",
  };
  // macOS types characters such as @ or € with Option (= Alt).
  const IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
  const NOT_TEXT_INPUTS = ["checkbox", "radio", "button", "submit", "reset", "range", "color", "file", "image"];
  const keyQueue = [];
  let keyBusy = false;
  let keyRefreshTimer = null;
  let asciiWarnedAt = 0;

  function isTextField(el) {
    if (!el || !el.tagName) return false;
    if (el.isContentEditable || el.tagName === "TEXTAREA") return true;
    return el.tagName === "INPUT" && !NOT_TEXT_INPUTS.includes(el.type);
  }

  function queueInput(item) {
    const last = keyQueue[keyQueue.length - 1];
    if (last && item.key && last.key === item.key && last.count < 50) {
      last.count += 1;
    } else if (last && item.text && last.text && last.text.length + item.text.length <= 500) {
      last.text += item.text;
    } else {
      keyQueue.push(item);
    }
    pumpInput();
  }

  async function pumpInput() {
    if (keyBusy) return;
    keyBusy = true;
    while (keyQueue.length) {
      const item = keyQueue.shift();
      try {
        if (item.key) {
          await api("/api/device/key", { method: "POST", body: { key: item.key, count: item.count } });
        } else {
          await api("/api/device/text", { method: "POST", body: { text: item.text } });
        }
      } catch (err) {
        toast(err.message || String(err), "error");
      }
    }
    keyBusy = false;
    clearTimeout(keyRefreshTimer);
    keyRefreshTimer = setTimeout(() => refreshScreen(true), 350);
  }

  document.addEventListener("keydown", (e) => {
    if (ui.tab !== "screen" || !$("#toggle-control").checked) return;
    if (e.isComposing || e.metaKey || isTextField(e.target)) return;
    if (e.key.length === 1) {
      // AltGr (Ctrl+Alt on Windows) and macOS Option type characters; other Ctrl/Alt
      // combinations stay browser shortcuts.
      const altGraph = e.getModifierState && e.getModifierState("AltGraph");
      const typed = altGraph || (e.ctrlKey && e.altKey) || (!e.ctrlKey && (!e.altKey || IS_MAC));
      if (!typed) return;
      e.preventDefault();
      if (e.key >= " " && e.key <= "~") {
        queueInput({ text: e.key });
      } else if (Date.now() - asciiWarnedAt > 5000) {
        asciiWarnedAt = Date.now();
        toast(`Android's keyboard input only types plain ASCII, not "${e.key}"`, "error");
      }
      return;
    }
    const key = KEY_MAP[e.key];
    if (!key || e.ctrlKey || e.altKey) return; // e.g. Alt+Left = the browser's Back
    e.preventDefault();
    queueInput({ key, count: 1 });
  });

  // =============================================================== templates
  async function loadTemplates() {
    ui.templates = await api("/api/templates").catch(() => []);
    renderTemplates();
    renderChecklist();
    $("#tpl-names").innerHTML = (ui.routineDetail ? ui.routineDetail.templates : [])
      .map((t) => `<option value="${esc(t.name)}">`).join("");
  }

  function scoreBadge(name) {
    const s = ui.scores[name];
    if (!s) return "";
    return `<span class="score ${s.found ? "hit" : "miss"}">${s.found ? "✓ on screen" : "not on screen"} · ${s.score.toFixed(2)}</span>`;
  }

  function renderTemplates() {
    const grid = $("#templates-grid");
    $("#templates-empty").classList.toggle("hidden", ui.templates.length > 0);
    grid.innerHTML = ui.templates.map((t) => `
      <div class="tpl" data-name="${esc(t.name)}">
        <img src="/api/templates/${encodeURIComponent(t.name)}.png?v=${t.created_at}" alt="${esc(t.name)}" loading="lazy">
        <span class="tpl-name">${esc(t.name)}</span>
        <span class="tpl-meta">≥ ${t.threshold.toFixed(2)} · ${t.mode} · ${t.region ? "near" : "whole screen"}</span>
        ${scoreBadge(t.name)}
        <div class="buttons">
          <button class="btn small" data-act="edit" type="button">Edit</button>
          <button class="btn small danger" data-act="delete" type="button">Delete</button>
        </div>
        <div class="tpl-edit hidden">
          <label>Threshold <span class="thr-value">${t.threshold.toFixed(2)}</span>
            <input type="range" min="0.6" max="0.98" step="0.01" value="${t.threshold}" data-field="threshold">
          </label>
          <label class="switch"><input type="checkbox" data-field="full" ${t.region ? "" : "checked"}><span>Whole screen</span></label>
          <label class="switch"><input type="checkbox" data-field="color" ${t.mode === "color" ? "checked" : ""}><span>Colours</span></label>
          <div class="buttons">
            <button class="btn small primary" data-act="save" type="button">Save</button>
            <button class="btn small" data-act="recapture" type="button">Recapture</button>
          </div>
        </div>
      </div>`).join("");
  }

  $("#templates-grid").addEventListener("input", (e) => {
    if (e.target.dataset.field === "threshold") {
      e.target.closest("label").querySelector(".thr-value").textContent = Number(e.target.value).toFixed(2);
    }
  });

  $("#templates-grid").addEventListener("click", (e) => {
    const button = e.target.closest("button[data-act]");
    if (!button) return;
    const card = button.closest(".tpl");
    const name = card.dataset.name;
    const act = button.dataset.act;
    if (act === "edit") {
      card.querySelector(".tpl-edit").classList.toggle("hidden");
    } else if (act === "delete") {
      if (!confirm(`Delete template "${name}"?`)) return;
      run(async () => {
        await api(`/api/templates/${encodeURIComponent(name)}`, { method: "DELETE" });
        await afterTemplatesChanged();
      }, "Template deleted");
    } else if (act === "save") {
      run(async () => {
        await api(`/api/templates/${encodeURIComponent(name)}`, {
          method: "PATCH",
          body: {
            threshold: Number(card.querySelector('[data-field="threshold"]').value),
            search_full: card.querySelector('[data-field="full"]').checked,
            mode: card.querySelector('[data-field="color"]').checked ? "color" : "gray",
          },
        });
        await afterTemplatesChanged();
      }, "Template updated");
    } else if (act === "recapture") {
      const doc = ui.routineDetail && ui.routineDetail.templates.find((t) => t.name === name);
      startCapture(name, doc ? doc.description : "");
    }
  });

  async function afterTemplatesChanged() {
    await loadTemplates();
    if (ui.routineId) await loadRoutineDetail(ui.routineId);
  }

  function renderChecklist() {
    const detail = ui.routineDetail;
    const list = $("#checklist");
    if (!detail || !detail.templates) {
      list.innerHTML = `<li class="muted">Pick a valid routine on the Farm tab.</li>`;
      return;
    }
    $("#checklist-routine").textContent = detail.name || "";
    const captured = new Set(ui.templates.map((t) => t.name));
    list.innerHTML = detail.templates.map((t) => {
      const have = captured.has(t.name) || t.captured;
      return `<li>
        <span class="state">${have ? "✅" : t.level === "required" ? "⛔" : "⬜"}</span>
        <span><span class="name">${esc(t.name)}</span> <span class="badge ${esc(t.level)}">${esc(t.level)}</span><br>${scoreBadge(t.name)}</span>
        <button class="btn small ${have ? "" : "primary"}" data-capture="${esc(t.name)}" type="button">${have ? "Recapture" : "Capture"}</button>
        <span class="desc">${esc(t.description)}</span>
      </li>`;
    }).join("");
  }

  $("#checklist").addEventListener("click", (e) => {
    const button = e.target.closest("button[data-capture]");
    if (!button) return;
    const name = button.dataset.capture;
    const doc = ui.routineDetail.templates.find((t) => t.name === name);
    startCapture(name, doc ? doc.description : "");
  });

  $("#btn-test-templates").addEventListener("click", () => run(async () => {
    await api("/api/screen/capture", { method: "POST" });
    const res = await api("/api/screen/analyze");
    ui.scores = Object.fromEntries(res.results.map((r) => [r.name, r]));
    renderTemplates();
    renderChecklist();
    const hits = res.results.filter((r) => r.found).map((r) => r.name);
    toast(hits.length ? `On screen: ${hits.join(", ")}` : "No template matches the current screen");
  }));

  // ----------------------------------------------------------------- capture
  async function startCapture(name = "", description = "") {
    await run(async () => {
      const cap = await api("/api/screen/capture?pin=true", { method: "POST" });
      ui.capture = { frameId: cap.frame_id, width: cap.width, height: cap.height, rect: null };
      const card = $("#capture-card");
      card.classList.remove("hidden");
      $("#tpl-name").value = name;
      $("#tpl-threshold").value = 0.85;
      $("#tpl-threshold-value").textContent = "0.85";
      $("#tpl-full").checked = /whole screen/i.test(description);
      $("#tpl-color").checked = /\bON\b.*\bOFF\b|\bOFF\b.*\bON\b/.test(description);
      $("#capture-result").classList.add("hidden");
      const img = $("#capture-img");
      img.onload = () => { drawCapture(); updateCropPreview(); };
      img.src = `/api/screen.jpg?frame=${cap.frame_id}&w=${Math.min(1080, cap.width)}`;
      if (ui.tab !== "templates") showTab("templates");
      card.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  function drawCapture() {
    if (!ui.capture) return;
    const img = $("#capture-img");
    const canvas = $("#capture-canvas");
    const width = img.clientWidth;
    const height = img.clientHeight;
    if (!width || !height) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const r = ui.capture.rect;
    if (!r || r.w <= 0 || r.h <= 0) return;
    ctx.fillStyle = "rgba(0,0,0,.5)";
    ctx.fillRect(0, 0, width, height);
    ctx.clearRect(r.x * width, r.y * height, r.w * width, r.h * height);
    ctx.strokeStyle = "#ff9d2e";
    ctx.lineWidth = 2;
    ctx.strokeRect(r.x * width, r.y * height, r.w * width, r.h * height);
  }

  function updateCropPreview() {
    const preview = $("#crop-preview");
    const r = ui.capture && ui.capture.rect;
    const img = $("#capture-img");
    if (!r || r.w <= 0 || r.h <= 0 || !img.naturalWidth) {
      preview.width = 1;
      preview.height = 1;
      return;
    }
    const sx = r.x * img.naturalWidth;
    const sy = r.y * img.naturalHeight;
    const sw = Math.max(1, r.w * img.naturalWidth);
    const sh = Math.max(1, r.h * img.naturalHeight);
    preview.width = Math.round(sw);
    preview.height = Math.round(sh);
    preview.getContext("2d").drawImage(img, sx, sy, sw, sh, 0, 0, sw, sh);
  }

  (() => {
    const canvas = $("#capture-canvas");
    let anchor = null;
    canvas.addEventListener("pointerdown", (e) => {
      if (!ui.capture) return;
      e.preventDefault();
      canvas.setPointerCapture(e.pointerId);
      anchor = relPos(e, canvas);
      ui.capture.rect = { x: anchor.x, y: anchor.y, w: 0, h: 0 };
      drawCapture();
    });
    canvas.addEventListener("pointermove", (e) => {
      if (!anchor) return;
      const p = relPos(e, canvas);
      ui.capture.rect = {
        x: Math.min(anchor.x, p.x), y: Math.min(anchor.y, p.y),
        w: Math.abs(p.x - anchor.x), h: Math.abs(p.y - anchor.y),
      };
      drawCapture();
    });
    const finish = () => {
      if (!anchor) return;
      anchor = null;
      drawCapture();
      updateCropPreview();
    };
    canvas.addEventListener("pointerup", finish);
    canvas.addEventListener("pointercancel", finish);
  })();

  $("#tpl-threshold").addEventListener("input", (e) => {
    $("#tpl-threshold-value").textContent = Number(e.target.value).toFixed(2);
  });
  $("#btn-capture-cancel").addEventListener("click", () => {
    ui.capture = null;
    $("#capture-card").classList.add("hidden");
  });
  $("#btn-recapture").addEventListener("click", () => startCapture($("#tpl-name").value));
  $("#btn-capture-new").addEventListener("click", () => startCapture(""));

  $("#btn-save-template").addEventListener("click", () => run(async () => {
    const cap = ui.capture;
    const r = cap && cap.rect;
    if (!r || r.w * cap.width < 8 || r.h * cap.height < 8) throw new Error("Drag a box around the element first");
    const name = $("#tpl-name").value.trim().toLowerCase();
    if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(name)) throw new Error("Use a name like attempt_again (a-z, 0-9, _ and -)");
    const exists = ui.templates.some((t) => t.name === name);
    if (exists && !confirm(`Replace the existing template "${name}"?`)) return;
    const res = await api("/api/templates", {
      method: "POST",
      body: {
        name, frame_id: cap.frameId, x: r.x, y: r.y, w: r.w, h: r.h,
        threshold: Number($("#tpl-threshold").value),
        mode: $("#tpl-color").checked ? "color" : "gray",
        search_full: $("#tpl-full").checked,
        overwrite: exists,
      },
    });
    const box = $("#capture-result");
    box.classList.remove("hidden", "ok", "warn");
    box.classList.add(res.warnings.length ? "warn" : "ok");
    box.textContent = [`Saved "${name}" (match score ${res.score.toFixed(2)}).`, ...res.warnings].join("\n");
    await afterTemplatesChanged();
    toast(`Template ${name} saved`, "ok");
  }));

  // ================================================================= editor
  async function loadEditor(id) {
    if (!ui.routines.length) await loadRoutines();
    id = id || (ui.routines[0] && ui.routines[0].id);
    if (!id) return;
    if (ui.editorDirty && id !== ui.editorId && !confirm("Discard unsaved changes?")) {
      $("#editor-select").value = ui.editorId;
      return;
    }
    const detail = await run(() => api(`/api/routines/${encodeURIComponent(id)}`));
    if (!detail) return;
    ui.editorId = id;
    ui.editorDirty = false;
    $("#editor-select").value = id;
    $("#routine-editor").value = detail.yaml;
    $("#btn-routine-restore").disabled = !detail.bundled;
    showEditorError(detail.valid ? null : detail.error);
    renderRulesOverview(detail);
  }

  function renderRulesOverview(detail) {
    const list = $("#rules-overview");
    if (!detail.valid || !detail.rules) {
      list.innerHTML = "";
      return;
    }
    list.innerHTML = detail.rules.map((r) => `<li><b>${esc(r.name)}</b>
      <span class="acts">→ ${esc(r.actions.join(", "))}</span>
      ${r.missing.length ? `<br><span class="missing">missing: ${esc(r.missing.join(", "))}</span>` : ""}</li>`).join("");
  }

  function showEditorError(message) {
    const box = $("#routine-error");
    box.classList.toggle("hidden", !message);
    box.textContent = message || "";
  }

  $("#editor-select").addEventListener("change", (e) => loadEditor(e.target.value));
  $("#routine-editor").addEventListener("input", () => { ui.editorDirty = true; });
  $("#routine-editor").addEventListener("keydown", (e) => {
    if (e.key === "Tab") {
      e.preventDefault();
      const el = e.target;
      const { selectionStart: s, selectionEnd: end } = el;
      el.value = `${el.value.slice(0, s)}  ${el.value.slice(end)}`;
      el.selectionStart = el.selectionEnd = s + 2;
      ui.editorDirty = true;
    }
  });

  $("#btn-routine-validate").addEventListener("click", () => run(async () => {
    const res = await api("/api/routines/validate", { method: "POST", body: { yaml: $("#routine-editor").value } });
    showEditorError(res.valid ? null : res.error);
    toast(res.valid ? `Valid: ${res.name} (${res.rules} rules)` : "The routine has errors", res.valid ? "ok" : "error");
  }));

  $("#btn-routine-save").addEventListener("click", () => run(async () => {
    try {
      const detail = await api(`/api/routines/${encodeURIComponent(ui.editorId)}`, {
        method: "PUT", body: { yaml: $("#routine-editor").value },
      });
      showEditorError(null);
      renderRulesOverview(detail);
      ui.editorDirty = false;
    } catch (err) {
      showEditorError(err.message);
      throw err;
    }
    await loadRoutines();
  }, "Routine saved"));

  $("#btn-routine-copy").addEventListener("click", () => run(async () => {
    const id = prompt("Name of the new routine (letters, digits, _ and -):", `${ui.editorId}_copy`);
    if (!id) return;
    if (ui.routines.some((r) => r.id === id) && !confirm(`Overwrite routine "${id}"?`)) return;
    await api(`/api/routines/${encodeURIComponent(id)}`, { method: "PUT", body: { yaml: $("#routine-editor").value } });
    ui.editorDirty = false;
    ui.editorId = id;
    await loadRoutines();
    await loadEditor(id);
    toast(`Created ${id}`, "ok");
  }));

  $("#btn-routine-restore").addEventListener("click", () => run(async () => {
    if (!confirm("Replace this routine with the bundled default? Your edits are lost.")) return;
    await api(`/api/routines/${encodeURIComponent(ui.editorId)}/restore`, { method: "POST" });
    ui.editorDirty = false;
    await loadEditor(ui.editorId);
    await loadRoutines();
    toast("Restored", "ok");
  }));

  $("#btn-routine-delete").addEventListener("click", () => run(async () => {
    if (!confirm(`Delete routine "${ui.editorId}"?`)) return;
    await api(`/api/routines/${encodeURIComponent(ui.editorId)}`, { method: "DELETE" });
    if (ui.routineId === ui.editorId) ui.routineId = null;
    ui.editorId = null;
    ui.editorDirty = false;
    await loadRoutines();
    await loadEditor(null);
    toast("Deleted", "ok");
  }));

  // =================================================================== logs
  async function pollLogs() {
    const res = await api(`/api/logs?after=${ui.logLastId}&limit=500`).catch(() => null);
    if (!res || !res.lines.length) return;
    ui.logLastId = res.last_id;
    ui.logs.push(...res.lines);
    if (ui.logs.length > 2000) ui.logs.splice(0, ui.logs.length - 2000);
    renderLogs(res.lines);
  }

  function logHtml(line) {
    return `<div class="log-line log-${esc(line.level)}"><span class="t">${fmtClock(line.ts)}</span> ${esc(line.msg)}</div>`;
  }

  function renderLogs(newLines) {
    const view = $("#log-view");
    const min = LEVELS[$("#log-level").value] || 0;
    const visible = (lines) => lines.filter((l) => (LEVELS[l.level] || 0) >= min);
    if (newLines) view.insertAdjacentHTML("beforeend", visible(newLines).map(logHtml).join(""));
    else view.innerHTML = visible(ui.logs).map(logHtml).join("");
    if ($("#log-follow").checked) view.scrollTop = view.scrollHeight;
  }

  $("#log-level").addEventListener("change", () => renderLogs(null));
  $("#btn-log-clear").addEventListener("click", () => { ui.logs = []; $("#log-view").innerHTML = ""; });
  setInterval(() => { if (ui.tab === "logs" && !document.hidden) pollLogs(); }, 2000);

  // ================================================================= device
  function renderDevice() {
    const status = ui.status;
    if (!status) return;
    const d = status.device;
    const info = d.info || {};
    const rows = [
      ["Backend", d.backend === "sim" ? "Simulator (demo)" : "ADB"],
      ["Serial", d.serial || "(default device)"],
      ["Status", d.connected ? "✅ connected" : "❌ not connected"],
      ["Device", [info.manufacturer, info.model].filter(Boolean).join(" ") || "–"],
      ["Android", info.android || "–"],
      ["Resolution", info.resolution || "–"],
      ["CPU ABIs", (info.abilist || info.abi || "–").split(",").join(", ")],
    ];
    if ((info.abi || "").startsWith("x86")) {
      const bridge = info.native_bridge && info.native_bridge !== "0" ? info.native_bridge : "";
      rows.push(["ARM apps", bridge
        ? `✅ translated (${bridge})`
        : "❌ no ARM translation: Dokkan cannot run on this Android"]);
    }
    rows.push(["Game package", status.settings.game_package]);
    $("#device-info").innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
    const err = $("#device-error");
    err.classList.toggle("hidden", !d.error);
    err.textContent = d.error || "";
    $$(".adb-only").forEach((el) => el.classList.toggle("hidden", d.backend !== "adb"));
    const connect = $("#connect-serial");
    if (!connect.value && d.serial && document.activeElement !== connect) connect.value = d.serial;
    $("#notify-status").textContent = status.notify_enabled
      ? "Notifications are enabled (NOTIFY_URLS)."
      : "Set NOTIFY_URLS to Apprise URLs (Discord, Telegram, ntfy, …) to get alerts.";
    $("#btn-notify-test").disabled = !status.notify_enabled;
    const s = status.settings;
    const settings = [
      ["ADB_SERIAL", s.adb_serial || "–"],
      ["EMULATOR_CONSOLE", s.emulator_console || "–"],
      ["GAME_PACKAGE", s.game_package],
      ["ROUTINE", s.routine],
      ["AUTOSTART", s.autostart],
      ["ACTIVE_HOURS", s.active_hours || "always"],
      ["NOTIFY_EVERY_RUNS", s.notify_every_runs || "off"],
      ["MATCH_SCALE", s.match_scale],
      ["TAP_JITTER", s.tap_jitter],
      ["KEEP_SCREEN_ON", s.keep_screen_on],
      ["WEB_PASSWORD", s.auth_enabled ? "set" : "not set"],
      ["DATA_DIR", s.data_dir],
    ];
    $("#settings-list").innerHTML = settings.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
  }

  $("#btn-device-refresh").addEventListener("click", () => run(async () => {
    const status = await api("/api/device/refresh", { method: "POST" });
    if (ui.status) ui.status.device = status;
    renderDevice();
    if (!status.connected) throw new Error(status.error || "Not connected");
  }, "Device connected"));

  $("#btn-connect").addEventListener("click", () => run(async () => {
    const serial = $("#connect-serial").value.trim();
    if (!serial) throw new Error("Enter an address such as 192.168.1.50:5555");
    const status = await api("/api/device/connect", { method: "POST", body: { serial } });
    if (ui.status) ui.status.device = status;
    renderDevice();
    if (!status.connected) throw new Error(status.error || "Could not connect");
  }, "Connected"));

  $("#btn-pair").addEventListener("click", () => run(async () => {
    const address = $("#pair-address").value.trim();
    const code = $("#pair-code").value.trim();
    const res = await api("/api/device/pair", { method: "POST", body: { address, code } });
    toast(res.message || "Paired", "ok");
  }));

  $("#btn-devices").addEventListener("click", () => run(async () => {
    const res = await api("/api/device/devices");
    const list = $("#devices-list");
    list.innerHTML = res.devices.length
      ? res.devices.map((d) => `<li><button class="btn small ghost" data-serial="${esc(d.serial)}" type="button">${esc(d.serial)}</button> ${esc(d.state)} ${esc(d.model || "")}</li>`).join("")
      : `<li class="muted">No devices. Connect one first.</li>`;
  }));
  $("#devices-list").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-serial]");
    if (b) $("#connect-serial").value = b.dataset.serial;
  });

  // =================================================================== apps
  const JOB_ACTIVE = ["queued", "uploading", "downloading", "installing"];
  const UPLOAD_LIMIT_HINT = "Behind Cloudflare an upload can be at most 100 MB: "
    + "open the farmer on its local address or install a link instead.";
  let jobTimer = null;

  function fmtBytes(n) {
    if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(2)} GB`;
    return `${((n || 0) / 1024 ** 2).toFixed(1)} MB`;
  }

  function renderJob(job) {
    const el = $("#install-job");
    const busy = Boolean(job && JOB_ACTIVE.includes(job.state));
    ["#btn-install-aurora", "#btn-install-url", "#btn-install-file"].forEach((sel) => {
      $(sel).disabled = busy;
    });
    if (!job) {
      el.className = "notice hidden";
      return;
    }
    const name = job.source === "fdroid:com.aurora.store" ? "Aurora Store" : job.source;
    let text;
    if (job.state === "uploading" || job.state === "downloading") {
      const verb = job.state === "uploading" ? "Uploading" : "Downloading";
      const pct = job.total ? ` (${Math.floor((job.downloaded / job.total) * 100)}%)` : "";
      const size = job.total ? `${fmtBytes(job.downloaded)} of ${fmtBytes(job.total)}` : fmtBytes(job.downloaded);
      text = `${verb} ${name}: ${size}${pct}…`;
    } else if (job.state === "installing") {
      text = `Installing ${name}… (a big game takes a few minutes)`;
    } else if (job.state === "done") {
      text = `✅ Installed ${name}`;
    } else if (job.state === "error") {
      text = `❌ ${name}: ${job.message}`;
    } else {
      text = `Starting ${name}…`;
    }
    el.textContent = text;
    el.className = `notice ${{ done: "ok", error: "error" }[job.state] || ""}`;
  }

  async function pollJob() {
    clearTimeout(jobTimer);
    const res = await api("/api/apps/job").catch(() => null);
    if (!res) {
      if (ui.jobActive) jobTimer = setTimeout(pollJob, 3000);
      return;
    }
    const job = res.job;
    const wasActive = ui.jobActive;
    ui.jobActive = Boolean(job && JOB_ACTIVE.includes(job.state));
    renderJob(job);
    if (ui.jobActive) {
      jobTimer = setTimeout(pollJob, 1500);
    } else if (wasActive && job) {
      if (job.state === "done") {
        toast(`Installed ${job.source === "fdroid:com.aurora.store" ? "Aurora Store" : job.source}`, "ok");
        loadApps();
      } else if (job.state === "error") {
        toast(job.message, "error");
      }
    }
  }

  async function loadApps() {
    const res = await api("/api/apps").catch(() => null);
    if (!res) return;
    const badge = $("#game-installed");
    const broken = res.installed && res.abi_ok === false;
    badge.className = `badge ${res.installed && !broken ? "ok" : "required"}${res.installed === null ? " hidden" : ""}`;
    badge.textContent = res.installed ? `${broken ? "✗" : "✓"} game installed${res.abi ? ` (${res.abi})` : ""}` : "game not installed";
    const warning = $("#game-abi-warning");
    warning.classList.toggle("hidden", !broken);
    warning.textContent = broken
      ? `The game was installed as its ${res.abi} build, which this Android cannot run (it shows "This app isn't compatible"). Uninstall it in Android's settings and install the arm64-v8a version: Aurora Store picks that one by itself.`
      : "";
    renderJob(res.job);
    if (res.job && JOB_ACTIVE.includes(res.job.state) && !ui.jobActive) {
      ui.jobActive = true;
      pollJob();
    }
  }

  function startInstall(source) {
    return run(async () => {
      const res = await api("/api/apps/install", { method: "POST", body: { source } });
      ui.jobActive = true;
      renderJob(res.job);
      pollJob();
    });
  }

  function uploadApp(file, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("PUT", `/api/apps/upload?filename=${encodeURIComponent(file.name)}`);
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.upload.onprogress = (e) => onProgress(e.loaded, e.lengthComputable ? e.total : file.size);
      xhr.onload = () => {
        let data = null;
        try { data = JSON.parse(xhr.responseText); } catch (err) { data = null; }
        if (xhr.status >= 200 && xhr.status < 300 && data) {
          resolve(data);
          return;
        }
        let detail = data && data.detail;
        if (Array.isArray(detail)) detail = detail.map((d) => d.msg).join(", ");
        if (!detail && xhr.status === 413) detail = `The file is too large. ${UPLOAD_LIMIT_HINT}`;
        reject(new Error(detail || `Upload failed (HTTP ${xhr.status})`));
      };
      xhr.onerror = () => reject(new Error(`Upload failed. ${UPLOAD_LIMIT_HINT}`));
      xhr.send(file);
    });
  }

  $("#btn-install-aurora").addEventListener("click", () => startInstall("fdroid:com.aurora.store"));
  $("#btn-open-aurora").addEventListener("click", () => run(async () => {
    await api("/api/device/app", {
      method: "POST", body: { action: "launch", package: "com.aurora.store" },
    });
    $("#toggle-control").checked = true;
    $("#screen-wrap").classList.add("control");
    showTab("screen");
    setTimeout(() => refreshScreen(true), 2500);
  }));
  $("#btn-install-url").addEventListener("click", () => {
    const url = $("#install-url").value.trim();
    if (!url) {
      toast("Enter a link to an .apk, .xapk or .apks file", "error");
      return;
    }
    startInstall(url);
  });
  $("#btn-install-file").addEventListener("click", () => run(async () => {
    const file = $("#install-file").files[0];
    if (!file) throw new Error("Choose an .apk, .xapk or .apks file first");
    ui.jobActive = true;
    const progress = (loaded, total) => renderJob({
      state: "uploading", source: file.name, downloaded: loaded, total,
    });
    progress(0, file.size);
    try {
      const res = await uploadApp(file, progress);
      $("#install-file").value = "";
      renderJob(res.job);
    } catch (err) {
      ui.jobActive = false;
      renderJob({ state: "error", source: file.name, message: err.message });
      throw err;
    }
    pollJob();
  }));

  $("#btn-notify-test").addEventListener("click", () => run(
    () => api("/api/notify/test", { method: "POST" }), "Test notification sent",
  ));

  // ================================================================== start
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && ui.tab === "screen") refreshScreen(false);
  });

  async function init() {
    try {
      ui.status = await api("/api/status");
      ui.routineId = ui.status.selected_routine || null;
      renderStatus(ui.status);
    } catch (err) {
      // pollStatus() keeps retrying and shows "offline".
    }
    await loadRoutines();
    loadTemplates();
    loadSessions();
    showTab(location.hash.slice(1) || "farm");
    setTimeout(pollStatus, 2000);
    liveTick();
  }

  init();
})();
