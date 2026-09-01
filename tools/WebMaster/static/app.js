/* NetBus WebMaster 前端 */
(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  let uiWs = null;
  let activeSlave = null;
  let cmds = [];
  let fileCache = {};
  let showOffline = false;
  let lastDevices = [];

  const terminal = $("#terminal");
  const deviceList = $("#deviceList");
  const connState = $("#connState");
  const deviceBadge = $("#deviceBadge");
  const settingsBtn = $("#btnOpenDeviceTab");
  const port = location.port || "80";

  function switchTab(tab) {
    $$(".tab").forEach((t) => t.classList.remove("active"));
    $$(".tab-panel").forEach((p) => p.classList.remove("active"));
    const tabBtn = document.querySelector(`.tab[data-tab="${tab}"]`);
    if (tabBtn) tabBtn.classList.add("active");
    const panel = $("#tab-" + tab);
    if (panel) panel.classList.add("active");
  }

  // ── terminal ────────────────────────────────────────────────
  function log(msg, level) {
    const div = document.createElement("div");
    div.className = "t-line t-" + (level || "info");
    div.textContent = "[" + new Date().toLocaleTimeString() + "] " + msg;
    terminal.appendChild(div);
    terminal.scrollTop = terminal.scrollHeight;
    while (terminal.children.length > 500) terminal.removeChild(terminal.firstChild);
  }

  // ── WS ──────────────────────────────────────────────────────
  function connectUI() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    uiWs = new WebSocket(`${proto}://${location.host}/ws/ui`);
    uiWs.onopen = () => { connState.classList.remove("off"); connState.classList.add("on"); connState.textContent = "已連線"; log("UI 已連線", "ok"); };
    uiWs.onclose = () => { connState.classList.remove("on"); connState.classList.add("off"); connState.textContent = "未連線"; log("UI 已斷線", "warn"); setTimeout(connectUI, 2000); };
    uiWs.onerror = () => log("WS 錯誤", "err");
    uiWs.onmessage = (ev) => { let m; try { m = JSON.parse(ev.data); } catch (e) { return; } handleMsg(m); };
  }

  function sendUI(obj) {
    if (uiWs && uiWs.readyState === WebSocket.OPEN) uiWs.send(JSON.stringify(obj));
    else log("UI 未連線，無法送出指令", "err");
  }

  function handleMsg(msg) {
    switch (msg.type) {
      case "device_list": renderDevices(msg.data || []); break;
      case "ok":
        if (msg.action === "cmd" && msg.resp) {
          $("#cmdResult").textContent = "resp cmd=0x" + msg.resp.cmd.toString(16) + "\n" + JSON.stringify(msg.resp.args, null, 2);
          log("cmd 回應: 0x" + msg.resp.cmd.toString(16), "ok");
        } else if (msg.action === "file_list") {
          renderFileList(msg.data || {});
        } else if (["file_delete", "file_confirm", "file_undo", "file_promote"].includes(msg.action)) {
          log(`✅ ${msg.action} ${msg.path || ""} ${msg.ok ? "" : "(失敗)"}`, msg.ok ? "ok" : "err"); refreshFiles();
        } else if (msg.action === "file_download") {
          log(`✅ 已下載 ${msg.path} (${msg.size}B)`, "ok");
        } else {
          log("✅ " + (msg.action || "") + (msg.sha ? " sha=" + msg.sha.slice(0, 8) : ""), "ok");
        }
        break;
      case "error": log("❌ " + msg.err, "err"); if ($("#cmdResult")) $("#cmdResult").textContent = "錯誤: " + msg.err; break;
      case "pong": break;
      default: log("⬅ " + JSON.stringify(msg), "info");
    }
  }

  // ── devices ──────────────────────────────────────────────────
  function renderDevices(devices) {
    lastDevices = devices || [];
    deviceList.innerHTML = "";
    const list = lastDevices.slice().sort((a, b) => (a.slave_id < b.slave_id ? -1 : 1));
    const online = list.filter((d) => d.online);
    const offline = list.filter((d) => !d.online);
    const shown = showOffline ? list : online;
    if (!shown.length) {
      const li = document.createElement("li");
      li.textContent = showOffline ? "(尚無已知設備)" : "(無連線設備)";
      li.className = "muted-body";
      deviceList.appendChild(li);
      return;
    }
    shown.forEach((d) => {
      const li = document.createElement("li");
      if (d.slave_id === activeSlave) li.className = "active";
      if (!d.online) li.classList.add("offlined");
      const dot = document.createElement("span");
      dot.className = "dot" + (d.online ? "" : " offline");
      const info = document.createElement("span");
      info.className = "info";
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = d.slave_id + (d.play_id != null ? " ▸P" + d.play_id : "");
      const sub = document.createElement("span");
      sub.className = "sub";
      sub.textContent = d.online ? ((d.addr ? "@" + d.addr + " · " : "") + d.uptime_s + "s") : "離線";
      info.appendChild(name); info.appendChild(sub);
      li.appendChild(dot); li.appendChild(info);
      if (d.online) {
        const gear = document.createElement("button");
        gear.className = "mini gear";
        gear.textContent = "⚙";
        gear.title = "設備維護 " + d.slave_id;
        gear.onclick = (e) => { e.stopPropagation(); selectDevice(d.slave_id); switchTab("device"); };
        li.appendChild(gear);
      }
      li.onclick = () => { if (d.online) selectDevice(d.slave_id); else log(`${d.slave_id} 離線，無法操作`, "warn"); };
      deviceList.appendChild(li);
    });
  }

  function selectDevice(sid) {
    activeSlave = sid;
    deviceBadge.textContent = sid;
    deviceBadge.classList.remove("off");
    settingsBtn.disabled = false;
    $("#activeSlave").textContent = sid;
    refreshDevices();
    refreshFiles();
    refreshDevSummary();
  }

  function refreshDevSummary() {
    if (!activeSlave) return;
    const d = lastDevices.find((x) => x.slave_id === activeSlave);
    if (!d) return;
    $("#devPlayId").value = (d.play_id != null ? d.play_id : "—");
    $("#devAddr").value = d.online ? ((d.addr || "?") + " · " + d.uptime_s + "s") : "離線";
    const st = d.status || {};
    const stTxt = st.stream_active ? ("streaming · " + (st.stream_pos_frame != null ? "frame " + st.stream_pos_frame : "")) : "待機";
    $("#devStatus").value = stTxt;
    $("#devSummary").textContent = sid2str(activeSlave) + " · " + (d.online ? "在線" : "離線");
  }

  function sid2str(sid) { return sid; }

  function requireSlave() {
    if (!activeSlave) { log("請先在左側選擇設備", "warn"); return false; }
    return true;
  }

  function refreshDevices() {
    fetch("/api/devices").then((r) => r.json()).then((j) => { if (j.ok) renderDevices(j.data); }).catch(() => {});
  }

  // ── tabs / sidebar ───────────────────────────────────────────
  function bindTabs() {
    $$(".tab").forEach((tab) => {
      tab.onclick = () => {
        $$(".tab").forEach((t) => t.classList.remove("active"));
        $$(".tab-panel").forEach((p) => p.classList.remove("active"));
        tab.classList.add("active");
        $("#tab-" + tab.dataset.tab).classList.add("active");
      };
    });
  }

  function bindSidebar() {
    $("#btnCollapseSide").onclick = () => { document.body.classList.toggle("sidebar-collapsed"); };
    $("#showOffline").onchange = (e) => { showOffline = e.target.checked; refreshDevices(); };
    $("#btnDiscover").onclick = async () => {
      log("🔍 廣播發現（DISCOVER）…", "info");
      try {
        const r = await fetch(`/api/knock?broadcast=1&port=${port}`, { method: "POST" });
        const j = await r.json();
        log(`發現: 送出 ${j.sent} 包 → ${j.targets.join(", ")}`, j.ok ? "ok" : "err");
      } catch (e) { log("發現失敗: " + e, "err"); }
    };
    $("#btnKnockIp").onclick = async () => {
      const ip = $("#knockIp").value.trim();
      if (!ip) { log("請輸入 IP", "warn"); return; }
      log(`🔔 敲門 ${ip} …`, "info");
      try {
        const r = await fetch(`/api/knock?ip=${encodeURIComponent(ip)}&port=${port}`, { method: "POST" });
        const j = await r.json();
        log(`敲門: 送出 ${j.sent} 包 → ${j.targets.join(", ")}`, j.ok ? "ok" : "err");
      } catch (e) { log("敲門失敗: " + e, "err"); }
    };
  }

  // ── control / stream ─────────────────────────────────────────
  function bindControls() {
    $("#btnPrepare").onclick = () => {
      if (!requireSlave()) return;
      sendUI({ action: "stream_prepare", slave_id: activeSlave, file_name: $("#fileName").value, play_mode: parseInt($("#playMode").value, 10) });
      log(`準備 ${$("#fileName").value}`, "info");
    };
    $("#btnPlay").onclick = () => {
      if (!requireSlave()) return;
      const fps = parseInt($("#fps").value, 10) || 40;
      sendUI({ action: "stream_fps", slave_id: activeSlave, fps });
      sendUI({ action: "stream_play", slave_id: activeSlave, start_frame: parseInt($("#startFrame").value, 10) || 0 });
      log(`播放 (fps=${fps}, start=${$("#startFrame").value})`, "info");
    };
    $("#btnPause").onclick = () => { if (requireSlave()) sendUI({ action: "stream_pause", slave_id: activeSlave, paused: true }); };
    $("#btnStop").onclick = () => { if (requireSlave()) sendUI({ action: "stream_stop", slave_id: activeSlave }); };
    $("#btnSeek").onclick = () => { if (requireSlave()) sendUI({ action: "stream_seek", slave_id: activeSlave, frame: parseInt($("#startFrame").value, 10) || 0 }); };
  }

  // ── RAM upload ───────────────────────────────────────────────
  function bindRamUpload() {
    $("#btnRamUpload").onclick = async () => {
      if (!requireSlave()) return;
      const file = $("#ramFile").files[0];
      if (!file) { log("請選擇要上傳的檔案", "warn"); return; }
      const chunk = parseInt($("#ramChunk").value, 10) || 4096;
      const remote = $("#ramPath").value || "/ram/live.bin";
      const buf = await file.arrayBuffer();
      const b64 = b64FromArrayBuffer(buf);
      log(`上傳 ${file.name} (${buf.byteLength} B) → ${remote}`, "info");
      const bar = $("#ramProgress"); bar.style.width = "0";
      const SLICE = 512 * 1024;
      for (let i = 0; i < b64.length; i += SLICE) {
        sendUI({ action: "ram_upload", slave_id: activeSlave, remote_path: remote, chunk_size: chunk, data_b64: b64.slice(i, i + SLICE) });
        bar.style.width = Math.min(100, Math.round(i / b64.length * 100)) + "%";
      }
      log("已送出 RAM 上傳指令", "info");
    };
  }

  function b64FromArrayBuffer(buf) {
    const bytes = new Uint8Array(buf);
    let bin = "";
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin);
  }

  // ── file upload (REST) ───────────────────────────────────────
  function bindUpload() {
    $("#btnUpload").onclick = async () => {
      if (!requireSlave()) return;
      const file = $("#upFile").files[0];
      if (!file) { log("請選擇要上傳的檔案", "warn"); return; }
      const remote = $("#upPath").value || "/sd/upload.bin";
      const bar = $("#upProgress"); bar.style.width = "0";
      try {
        const r = await fetch(`/api/upload/${activeSlave}?remote_path=${encodeURIComponent(remote)}&chunk_size=4096`, { method: "POST", body: file });
        const j = await r.json();
        bar.style.width = "100%";
        if (j.ok) log(`✅ 上傳 ${file.name} → ${remote} (${j.size}B, sha=${j.sha.slice(0, 8)})`, "ok");
        else log("❌ " + j.err, "err");
        refreshFiles();
      } catch (e) { log("上傳失敗: " + e, "err"); }
    };
  }

  // ── file list / actions ──────────────────────────────────────
  async function refreshFiles() { if (!activeSlave) return; sendUI({ action: "file_list", slave_id: activeSlave }); }

  function renderFileList(files) {
    fileCache = files;
    const tbody = $("#fileList");
    const entries = Object.keys(files).sort();
    if (!entries.length) { tbody.innerHTML = '<tr><td colspan="4" class="muted">(無檔案 / 尚未載入)</td></tr>'; return; }
    tbody.innerHTML = "";
    entries.forEach((path) => {
      const info = files[path] || {};
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(path)}</td>
        <td>${fmtSize(info.s || 0)}</td>
        <td>${info.pending ? '<span class="pending-badge">待確認</span>' : (info.s ? '<span class="muted">已部署</span>' : '<span class="muted">—</span>')}</td>
        <td></td>`;
      const td = tr.children[3];
      td.appendChild(actBtn("下載", () => downloadFile(path)));
      if (info.pending) {
        td.appendChild(actBtn("確認", () => sendUI({ action: "file_confirm", slave_id: activeSlave, path }), "ok"));
        td.appendChild(actBtn("還原", () => sendUI({ action: "file_undo", slave_id: activeSlave, path }), "warn"));
      }
      td.appendChild(actBtn("刪除", () => sendUI({ action: "file_delete", slave_id: activeSlave, path }), "danger"));
      tbody.appendChild(tr);
    });
  }

  function actBtn(label, fn, cls) {
    const b = document.createElement("button");
    b.className = "act-btn " + (cls || "");
    b.textContent = label;
    b.onclick = fn;
    return b;
  }

  async function downloadFile(path) {
    if (!activeSlave) return;
    try {
      const r = await fetch(`/api/download/${activeSlave}?path=${encodeURIComponent(path)}`);
      if (!r.ok) { log("下載失敗: " + (await r.json()).err, "err"); return; }
      const blob = await r.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = path.split("/").pop() || "download.bin";
      a.click();
      URL.revokeObjectURL(a.href);
      log(`✅ 下載 ${path} (${fmtSize(blob.size)})`, "ok");
    } catch (e) { log("下載失敗: " + e, "err"); }
  }

  function bindDownload() {
    $("#btnDownload").onclick = () => { if (requireSlave()) downloadFile($("#dlRemote").value); };
    $("#btnRefreshFiles").onclick = () => refreshFiles();
  }

  // ── 固件更新 (delta) ─────────────────────────────────────────
  function fmtFirmware(j) {
    if (!j.ok) return "錯誤: " + j.err;
    const d = j.data;
    if (d.dry_run) return `[預覽] 本地 ${d.total} 檔 / 差異 ${d.changed} 檔 / 一致 ${d.matched}\n` + (d.changed === 0 ? "全部一致，無需更新" : "執行「更新」會只上傳差異檔。");
    return `[完成] 本地 ${d.total} 檔 / 已上傳 ${d.uploaded.length} 檔\n上傳清單:\n` + d.uploaded.join("\n");
  }

  function bindFirmware() {
    $("#btnFwPreview").onclick = async () => {
      if (!requireSlave()) return;
      $("#fwResult").textContent = "預覽中...";
      try {
        const j = await (await fetch(`/api/firmware/${activeSlave}?dry_run=1&confirm=1&reboot=0`)).json();
        $("#fwResult").textContent = fmtFirmware(j);
        log(`固件預覽: ${j.ok ? j.data.changed + " 檔差異" : j.err}`, j.ok ? "info" : "err");
      } catch (e) { log("固件預覽失敗: " + e, "err"); }
    };
    $("#btnFwUpdate").onclick = async () => {
      if (!requireSlave()) return;
      const confirm = $("#fwConfirm").checked ? "1" : "0";
      const reboot = $("#fwReboot").checked ? "1" : "0";
      $("#fwResult").textContent = "執行中...";
      try {
        const j = await (await fetch(`/api/firmware/${activeSlave}?dry_run=0&confirm=${confirm}&reboot=${reboot}`)).json();
        $("#fwResult").textContent = fmtFirmware(j);
        log(`固件更新: ${j.ok ? j.data.changed + " 檔已上傳" : j.err}`, j.ok ? "ok" : "err");
      } catch (e) { log("固件更新失敗: " + e, "err"); }
    };
  }

  // ── command console ──────────────────────────────────────────
  async function loadCommands() {
    try {
      const j = await (await fetch("/api/commands")).json();
      if (!j.ok) return;
      cmds = j.data || [];
      const sel = $("#cmdSelect"); sel.innerHTML = "";
      cmds.forEach((c) => {
        const opt = document.createElement("option");
        opt.value = c.cmd; opt.textContent = `${c.cmd} ${c.name}`; opt.dataset.fields = JSON.stringify(c.fields);
        sel.appendChild(opt);
      });
    } catch (e) { log("載入命令清單失敗: " + e, "err"); }
  }

  function bindConsole() {
    $("#cmdSelect").onchange = () => {
      const opt = $("#cmdSelect").selectedOptions[0];
      const fields = opt ? JSON.parse(opt.dataset.fields || "[]") : [];
      const sample = {};
      fields.forEach((f) => { sample[f] = (f === "path" || f.endsWith("_path") || f === "src" || f === "dst") ? "/boot.py" : (f.includes("_id") ? 1 : 0); });
      $("#cmdArgs").value = JSON.stringify(sample, null, 0);
    };
    $("#btnCmdSend").onclick = () => {
      if (!requireSlave()) return;
      let args; try { args = JSON.parse($("#cmdArgs").value || "{}"); } catch (e) { log("args 不是合法 JSON", "err"); return; }
      sendUI({ action: "cmd", slave_id: activeSlave, cmd_id: $("#cmdSelect").value, args, expect: $("#cmdExpect").value || null, timeout: parseFloat($("#cmdTimeout").value) || 5 });
      log("送出 cmd " + $("#cmdSelect").value, "info");
    };
    $("#btnCmdFill").onclick = () => { $("#cmdArgs").value = "{}"; $("#cmdExpect").value = ""; };
  }

  // ── 設備 tab (per-slave profile: config / delta / manifest + 設備操作) ──
  function bindDevice() {
    settingsBtn.onclick = () => { if (requireSlave()) switchTab("device"); };

    async function loadDeviceDoc(path, label) {
      if (!requireSlave()) return;
      $("#devDocBlock").textContent = "讀取中...";
      try {
        const r = await fetch(`/api/download/${activeSlave}?path=${encodeURIComponent(path)}`);
        if (!r.ok) { $("#devDocBlock").textContent = `❌ ${path} 讀取失敗`; return; }
        const text = await r.text();
        try { $("#devDocBlock").textContent = path + "\n" + JSON.stringify(JSON.parse(text), null, 2); }
        catch (e) { $("#devDocBlock").textContent = path + "\n" + text; }
        log(`已讀取 ${activeSlave} ${label}`, "ok");
      } catch (e) { $("#devDocBlock").textContent = "❌ " + e; }
    }

    $("#btnCfgDownload").onclick = async () => {
      if (!requireSlave()) return;
      const r = await fetch(`/api/download/${activeSlave}?path=/config.json`);
      if (!r.ok) { log("config 下載失敗", "err"); return; }
      const text = await r.text();
      try { $("#cfgText").value = JSON.stringify(JSON.parse(text), null, 2); } catch (e) { $("#cfgText").value = text; }
      log("config 已下載", "ok");
    };
    $("#btnCfgUpload").onclick = async () => {
      if (!requireSlave()) return;
      const body = $("#cfgText").value;
      try {
        const r = await fetch(`/api/upload/${activeSlave}?remote_path=/config.json&chunk_size=4096`, { method: "POST", body: body });
        const j = await r.json();
        if (j.ok) { log(`✅ config 已上傳 (${j.size}B)`, "ok"); setTimeout(() => log("提示: 部分 config 需重啟生效", "warn"), 500); }
        else log("❌ " + j.err, "err");
      } catch (e) { log("config 上傳失敗: " + e, "err"); }
    };

    $("#btnCfgDelta").onclick = () => loadDeviceDoc("/sd/.delta.json", "delta");
    $("#btnCfgManifest").onclick = () => loadDeviceDoc("/manifest.json", "manifest");
    $("#btnCfgManifestSd").onclick = () => loadDeviceDoc("/sd/.manifest.json", "SD manifest");

    $("#btnOpReboot").onclick = () => { if (requireSlave()) { sendUI({ action: "cmd", slave_id: activeSlave, cmd_id: "0x100F", args: { delay_ms: 500 } }); log(`重啟 ${activeSlave}`, "info"); } };
    $("#btnOpScan").onclick = () => { if (requireSlave()) { sendUI({ action: "cmd", slave_id: activeSlave, cmd_id: "0x200B", args: { target: 0 } }); log(`觸發 ${activeSlave} 重掃 (core1)`, "info"); } };
    $("#btnOpRebuildLocal").onclick = () => { if (requireSlave()) { sendUI({ action: "cmd", slave_id: activeSlave, cmd_id: "0x2009", args: { path: "/manifest.json" } }); log(`${activeSlave}: 剷 /manifest.json + 重啟 (開機自動重建索引)`, "warn"); } };
    $("#btnOpRebuildSd").onclick = () => { if (requireSlave()) { sendUI({ action: "cmd", slave_id: activeSlave, cmd_id: "0x200B", args: { target: 1 } }); log(`${activeSlave}: 重建 SD 索引 (0x200B target=1, 背景掃描)`, "info"); } };
    $("#btnOpListDelta").onclick = () => { if (requireSlave()) refreshFiles(); };
  }

  // ── PoE ──────────────────────────────────────────────────────
  function bindPoe() {
    $("#btnPoeRun").onclick = async () => {
      const action = $("#poeAction").value;
      const switches = Array.from($("#poeSwitch").selectedOptions).map((o) => o.value);
      const ports = $("#poePort").value.trim();
      const dryRun = $("#poeDryRun").checked ? "1" : "0";
      $("#poeResult").textContent = "執行中...";
      try {
        const j = await (await fetch(`/api/poe?action=${action}&dry_run=${dryRun}&switches=${encodeURIComponent(switches.join(","))}&ports=${encodeURIComponent(ports)}`, { method: "POST" })).json();
        $("#poeResult").textContent = j.ok ? j.output : ("錯誤: " + j.err);
        log(`PoE ${action} ${switches.join("+")} ${dryRun ? "(DRY-RUN)" : ""}`, j.ok ? "info" : "err");
      } catch (e) { $("#poeResult").textContent = "❌ " + e; }
    };
  }

  // ── misc ─────────────────────────────────────────────────────
  function bindMisc() {
    $("#themeToggle").onclick = () => {
      const cur = document.documentElement.getAttribute("data-theme");
      document.documentElement.setAttribute("data-theme", cur === "dark" ? "light" : "dark");
    };
    $("#btnClearLog").onclick = () => { terminal.innerHTML = ""; };
  }

  // helpers
  function fmtSize(n) {
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / (1024 * 1024)).toFixed(2) + " MB";
  }
  function escapeHtml(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

  // ── init ─────────────────────────────────────────────────────
  function init() {
    bindTabs(); bindSidebar(); bindControls(); bindRamUpload(); bindUpload(); bindDownload();
    bindFirmware(); bindConsole(); bindDevice(); bindPoe(); bindMisc();
    loadCommands(); refreshDevices(); connectUI();
    log("WebMaster 已啟動", "ok");
  }

  document.addEventListener("DOMContentLoaded", init);
})();
