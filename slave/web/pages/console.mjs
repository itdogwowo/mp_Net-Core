export const init = async (ctx) => {
  const el = ctx.el;

  const cmdOut = el("cmdOut");
  const cmdMsg = el("cmdMsg");

  const wsUrlInput = el("wsUrlInput");
  const btnWsToggle = el("btnWsToggle");
  const wsSendInput = el("wsSendInput");
  const btnWsSend = el("btnWsSend");
  const btnWsClear = el("btnWsClear");

  const state = {
    ws: null,
    connected: false,
  };

  const setMsg = (kind, text) => {
    cmdMsg.textContent = text || "";
    cmdMsg.style.color =
      kind === "good" ? "rgba(73,247,177,.95)" :
      kind === "bad" ? "rgba(255,80,115,.95)" :
      kind === "warn" ? "rgba(255,207,90,.95)" :
      "var(--mut)";
  };

  const log = (obj) => {
    const now = new Date().toLocaleTimeString();
    const prev = cmdOut.textContent || "";
    const line = typeof obj === "string" ? obj : JSON.stringify(obj, null, 2);
    cmdOut.textContent = (prev ? prev + "\n\n" : "") + "[" + now + "]\n" + line;
    cmdOut.scrollTop = cmdOut.scrollHeight;
  };

  const setUi = () => {
    btnWsToggle.textContent = state.connected ? "斷開" : "連接";
    btnWsSend.disabled = !state.connected;
    ctx.setPagePill(state.connected ? "good" : "info", state.connected ? "WS 已連接" : "等待連接");
  };

  const connect = () => {
    const url = (wsUrlInput.value || "").trim();
    if (!url) {
      setMsg("warn", "請輸入 WS URL");
      return;
    }
    try {
      localStorage.setItem("netcore_ws_url", url);
    } catch {}

    setMsg("info", "連接中…");
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";

    ws.onopen = () => {
      state.ws = ws;
      state.connected = true;
      setMsg("good", "已連接");
      log({ event: "open", url });
      setUi();
    };

    ws.onclose = () => {
      state.connected = false;
      state.ws = null;
      setMsg("warn", "已斷開");
      log({ event: "close" });
      setUi();
    };

    ws.onerror = () => {
      setMsg("bad", "連接錯誤");
      log({ event: "error" });
    };

    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        log({ event: "msg", text: ev.data });
        return;
      }
      if (ev.data instanceof ArrayBuffer) {
        log({ event: "msg", bytes: ev.data.byteLength });
        return;
      }
      log({ event: "msg", data: String(ev.data) });
    };
  };

  const disconnect = () => {
    if (state.ws) {
      try {
        state.ws.close();
      } catch {}
    }
  };

  const send = () => {
    if (!state.ws || !state.connected) return;
    const text = wsSendInput.value || "";
    try {
      state.ws.send(text);
      log({ event: "send", text });
      setMsg("good", "已送出");
    } catch (e) {
      setMsg("bad", "送出失敗");
      log({ event: "send_error", error: String(e) });
    }
  };

  btnWsToggle.addEventListener("click", () => {
    if (state.connected) disconnect();
    else connect();
  });

  btnWsSend.addEventListener("click", () => send());
  wsSendInput.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") send();
  });

  btnWsClear.addEventListener("click", () => {
    cmdOut.textContent = "{}";
    setMsg("info", "");
  });

  try {
    const saved = localStorage.getItem("netcore_ws_url");
    if (saved && !wsUrlInput.value) wsUrlInput.value = saved;
  } catch {}

  setUi();

  return {
    onShow: () => setUi(),
  };
};

