(() => {
  const el = (id) => document.getElementById(id);
  const netList = el("netList");
  const scanMeta = el("scanMeta");
  const msgLine = el("msgLine");
  const ssidInput = el("ssidInput");
  const pwInput = el("pwInput");
  const saveCheck = el("saveCheck");
  const netPill = el("netPill");
  const deviceLine = el("deviceLine");
  const stMode = el("stMode");
  const stSsid = el("stSsid");
  const stIp = el("stIp");
  const stGw = el("stGw");
  const ipHint = el("ipHint");
  const btnScan = el("btnScan");
  const btnRefresh = el("btnRefresh");
  const btnConnect = el("btnConnect");
  const btnClear = el("btnClear");
  const btnTogglePw = el("btnTogglePw");
  const connectForm = el("connectForm");

  const state = {
    lastScanAt: 0,
    connecting: false,
    pollTimer: null,
  };

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

  const setMsg = (kind, text) => {
    msgLine.textContent = text || "";
    msgLine.style.color =
      kind === "good" ? "rgba(73,247,177,.95)" :
      kind === "bad" ? "rgba(255,80,115,.95)" :
      kind === "warn" ? "rgba(255,207,90,.95)" :
      "var(--mut)";
  };

  const rssiBars = (rssi) => {
    const v = typeof rssi === "number" ? rssi : -120;
    const n = v >= -55 ? 4 : v >= -67 ? 3 : v >= -78 ? 2 : v >= -88 ? 1 : 0;
    const wrap = document.createElement("div");
    wrap.className = "bars";
    for (let i = 0; i < 4; i++) {
      const b = document.createElement("div");
      b.className = "bar" + (i < n ? " on" : "");
      b.style.height = 6 + i * 3 + "px";
      wrap.appendChild(b);
    }
    return wrap;
  };

  const renderNetworks = (list) => {
    netList.innerHTML = "";
    if (!Array.isArray(list) || list.length === 0) {
      const empty = document.createElement("div");
      empty.className = "net";
      empty.textContent = "未掃描到網路。可以再按一次掃描，或手動輸入 SSID。";
      netList.appendChild(empty);
      return;
    }

    list.forEach((n) => {
      const ssid = (n.ssid || "").trim() || "<Hidden>";
      const secure = n.auth !== 0;
      const root = document.createElement("div");
      root.className = "net";

      const top = document.createElement("div");
      top.className = "net__top";

      const left = document.createElement("div");
      const title = document.createElement("div");
      title.className = "net__ssid";
      title.textContent = ssid;
      const meta = document.createElement("div");
      meta.className = "net__meta";
      const bits = [];
      if (typeof n.rssi === "number") bits.push("RSSI " + n.rssi);
      if (typeof n.channel === "number") bits.push("CH " + n.channel);
      meta.textContent = bits.join(" · ");
      left.appendChild(title);
      left.appendChild(meta);

      const right = document.createElement("div");
      right.appendChild(rssiBars(n.rssi));

      top.appendChild(left);
      top.appendChild(right);

      const bottom = document.createElement("div");
      bottom.style.display = "flex";
      bottom.style.alignItems = "center";
      bottom.style.justifyContent = "space-between";
      bottom.style.gap = "10px";

      const tag = document.createElement("div");
      tag.className = "tag " + (secure ? "bad" : "good");
      tag.textContent = secure ? "需要密碼" : "開放網路";

      const pick = document.createElement("button");
      pick.className = "btn btn--tiny";
      pick.type = "button";
      pick.textContent = "選擇";
      pick.addEventListener("click", () => {
        ssidInput.value = ssid === "<Hidden>" ? "" : ssid;
        pwInput.value = "";
        pwInput.focus();
        setMsg("info", "已選擇 SSID，可輸入密碼後連接");
      });

      bottom.appendChild(tag);
      bottom.appendChild(pick);

      root.appendChild(top);
      root.appendChild(bottom);
      netList.appendChild(root);
    });
  };

  const renderStatus = (st) => {
    const mode = st?.mode || "—";
    const connected = !!st?.connected;
    const ip = st?.ip || "—";
    const gw = st?.gw || "—";
    const ssid = st?.ssid || "—";
    const slaveId = st?.slave_id || "";

    stMode.textContent = mode;
    stSsid.textContent = ssid;
    stIp.textContent = ip;
    stGw.textContent = gw;
    deviceLine.textContent = slaveId ? "裝置：" + slaveId : "裝置：—";

    if (connected) {
      netPill.textContent = "已連線";
      netPill.style.color = "rgba(73,247,177,.95)";
      netPill.style.borderColor = "rgba(73,247,177,.25)";
      ipHint.innerHTML = ip && ip !== "—" ? `可嘗試用新 IP 開啟：<span style="font-family:var(--mono)">${ip}</span>` : "";
    } else {
      netPill.textContent = mode === "ap" ? "AP 模式" : "未連線";
      netPill.style.color = mode === "ap" ? "rgba(255,207,90,.95)" : "rgba(170,179,214,.95)";
      netPill.style.borderColor = mode === "ap" ? "rgba(255,207,90,.25)" : "rgba(255,255,255,.10)";
      ipHint.textContent = "";
    }
  };

  const refreshStatus = async (silent = false) => {
    try {
      const st = await fetchJson("/api/wifi/status", { timeoutMs: 5000 });
      renderStatus(st);
      if (!silent) setMsg("info", "狀態已更新");
      return st;
    } catch (e) {
      if (!silent) setMsg("warn", "狀態讀取失敗，請確認裝置仍在線上");
      return null;
    }
  };

  const scan = async () => {
    btnScan.disabled = true;
    scanMeta.textContent = "掃描中…";
    setMsg("info", "正在掃描附近 Wi‑Fi");
    try {
      const res = await fetchJson("/api/wifi/scan", { timeoutMs: 12000 });
      const list = res?.networks || [];
      renderNetworks(list);
      state.lastScanAt = Date.now();
      scanMeta.textContent = Array.isArray(list) ? `${list.length} 個 · ${new Date().toLocaleTimeString()}` : "—";
      setMsg("good", "掃描完成");
    } catch (e) {
      scanMeta.textContent = "掃描失敗";
      setMsg("bad", "掃描失敗，稍後再試");
      renderNetworks([]);
    } finally {
      btnScan.disabled = false;
    }
  };

  const connect = async (ssid, password, save) => {
    if (!ssid) {
      setMsg("warn", "請先輸入或選擇 SSID");
      ssidInput.focus();
      return;
    }
    state.connecting = true;
    btnConnect.disabled = true;
    btnScan.disabled = true;
    btnRefresh.disabled = true;
    setMsg("info", "已送出連接請求，裝置可能會切換網路");

    try {
      await fetchJson("/api/wifi/connect", {
        method: "POST",
        body: JSON.stringify({ ssid, password: password || "", save: !!save }),
        timeoutMs: 5000,
      });
    } catch (e) {
      setMsg("bad", "送出失敗，請再試一次");
      state.connecting = false;
      btnConnect.disabled = false;
      btnScan.disabled = false;
      btnRefresh.disabled = false;
      return;
    }

    let ok = false;
    for (let i = 0; i < 12; i++) {
      await sleep(1500);
      const st = await refreshStatus(true);
      if (st?.connected) {
        ok = true;
        break;
      }
    }

    if (ok) {
      setMsg("good", "已連線。若 IP 改變，請用新的 IP 重新開啟此頁");
    } else {
      setMsg("warn", "尚未確認連線成功。可再按更新或重新連接");
    }

    state.connecting = false;
    btnConnect.disabled = false;
    btnScan.disabled = false;
    btnRefresh.disabled = false;
  };

  btnTogglePw.addEventListener("click", () => {
    const isPw = pwInput.type === "password";
    pwInput.type = isPw ? "text" : "password";
    btnTogglePw.textContent = isPw ? "隱藏" : "顯示";
    pwInput.focus();
  });

  btnClear.addEventListener("click", () => {
    ssidInput.value = "";
    pwInput.value = "";
    setMsg("info", "");
    ssidInput.focus();
  });

  btnScan.addEventListener("click", () => scan());
  btnRefresh.addEventListener("click", () => refreshStatus());

  connectForm.addEventListener("submit", (ev) => {
    ev.preventDefault();
    if (state.connecting) return;
    const ssid = (ssidInput.value || "").trim();
    const password = pwInput.value || "";
    connect(ssid, password, saveCheck.checked);
  });

  const boot = async () => {
    await refreshStatus(true);
    await scan();
  };

  boot();
})();
