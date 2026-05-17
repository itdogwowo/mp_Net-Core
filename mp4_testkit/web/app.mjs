const el = (id) => document.getElementById(id);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const fetchJson = async (url, opts = {}) => {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), opts.timeoutMs ?? 8000);
  try {
    const res = await fetch(url, {
      ...opts,
      headers: {
        "Content-Type": "application/json",
        ...(opts.headers || {}),
      },
      signal: ctl.signal,
    });
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = { raw: text };
    }
    if (!res.ok) throw Object.assign(new Error("HTTP " + res.status), { data });
    return data;
  } finally {
    clearTimeout(t);
  }
};

const state = {
  wifiStatus: null,
  modules: {},
};

const netPill = el("netPill");
const pagePill = el("pagePill");
const pageTitle = el("pageTitle");
const deviceLine = el("deviceLine");

const setPill = (pillEl, kind, text) => {
  pillEl.textContent = text || "—";
  pillEl.style.color =
    kind === "good" ? "rgba(73,247,177,.95)" :
    kind === "bad" ? "rgba(255,80,115,.95)" :
    kind === "warn" ? "rgba(255,207,90,.95)" :
    "rgba(170,179,214,.95)";
  pillEl.style.borderColor =
    kind === "good" ? "rgba(73,247,177,.25)" :
    kind === "bad" ? "rgba(255,80,115,.25)" :
    kind === "warn" ? "rgba(255,207,90,.25)" :
    "rgba(255,255,255,.10)";
};

const renderGlobalStatus = (st) => {
  const mode = st?.mode || "—";
  const connected = !!st?.connected;
  const ip = st?.ip || "";
  const slaveId = st?.slave_id || "";

  deviceLine.textContent = slaveId ? "裝置：" + slaveId : "裝置：—";

  if (connected) {
    setPill(netPill, "good", "已連線");
    setPill(pagePill, "good", "已連線");
    return;
  }
  if (mode === "ap") {
    setPill(netPill, "warn", "AP 模式");
    setPill(pagePill, "warn", "AP 模式");
    return;
  }
  if (ip) {
    setPill(netPill, "info", "未連線");
    setPill(pagePill, "info", "未連線");
    return;
  }
  setPill(netPill, "info", "狀態讀取中");
  setPill(pagePill, "info", "—");
};

const refreshStatus = async (silent = true) => {
  try {
    const st = await fetchJson("/api/wifi/status", { timeoutMs: 5000 });
    state.wifiStatus = st;
    renderGlobalStatus(st);
    return st;
  } catch (e) {
    if (!silent) throw e;
    return null;
  }
};

const showPage = (route) => {
  const pages = document.querySelectorAll(".page[data-page]");
  pages.forEach((p) => {
    p.hidden = p.getAttribute("data-page") !== route;
  });
  const items = document.querySelectorAll(".nav__item[data-route]");
  items.forEach((a) => {
    a.classList.toggle("active", a.getAttribute("data-route") === route);
  });
  pageTitle.textContent =
    route === "wifi" ? "Wi‑Fi 設定" :
    route === "console" ? "指令台" :
    route === "mp4" ? "MP4 處理器" :
    "Net‑Core";
};

const getRoute = () => {
  const h = (location.hash || "").replace(/^#\/?/, "");
  const r = (h.split("/")[0] || "").trim();
  if (r === "console") return "console";
  if (r === "mp4") return "mp4";
  return "wifi";
};

const ensureModule = async (route) => {
  if (state.modules[route]) return state.modules[route];
  const mod =
    route === "wifi" ? await import("./pages/wifi.mjs") :
    route === "console" ? await import("./pages/console.mjs") :
    route === "mp4" ? await import("./pages/mp4.mjs") :
    null;
  if (!mod || typeof mod.init !== "function") return null;
  const api = await mod.init(ctx);
  state.modules[route] = api || {};
  return state.modules[route];
};

const route = async () => {
  const r = getRoute();
  showPage(r);
  const m = await ensureModule(r);
  if (m && typeof m.onShow === "function") m.onShow();
};

const ctx = {
  el,
  sleep,
  fetchJson,
  state,
  setPagePill: (kind, text) => setPill(pagePill, kind, text),
  setSidePill: (kind, text) => setPill(netPill, kind, text),
  refreshStatus,
  renderGlobalStatus,
};

window.addEventListener("hashchange", route);

const boot = async () => {
  renderGlobalStatus(state.wifiStatus);
  await route();
  refreshStatus(true);
  setInterval(() => {
    refreshStatus(true);
  }, 2500);
};

boot();
