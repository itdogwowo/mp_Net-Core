export const init = async (ctx) => {
  const el = ctx.el;

  const cmdOut = el("cmdOut");
  const cmdMsg = el("cmdMsg");

  const wsSendInput = el("wsSendInput");
  const btnWsSend = el("btnWsSend");
  const btnWsClear = el("btnWsClear");

  const state = {
    busy: false,
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
    btnWsSend.disabled = state.busy;
    ctx.setPagePill("info", state.busy ? "送出中" : "就緒");
  };

  const send = () => {
    if (state.busy) return;
    const text = (wsSendInput.value || "").trim();
    if (!text) {
      setMsg("warn", "請輸入 JSON");
      return;
    }
    let obj = null;
    try {
      obj = JSON.parse(text);
    } catch {
      setMsg("bad", "JSON 格式錯誤");
      return;
    }
    if (!obj || typeof obj !== "object" || Array.isArray(obj) || obj.cmd == null) {
      setMsg("warn", "需要 {cmd, payload}");
      return;
    }
    state.busy = true;
    setUi();
    setMsg("info", "送出中…");
    log({ event: "send", via: "http", url: "/api/cmd", body: obj });
    ctx.fetchJson("/api/cmd", { method: "POST", body: JSON.stringify(obj), timeoutMs: 8000 })
      .then((res) => {
        setMsg("good", "完成");
        log({ event: "resp", data: res });
      })
      .catch((e) => {
        const detail = e && e.data != null ? e.data : String(e?.message || e);
        setMsg("bad", "失敗");
        log({ event: "error", detail });
      })
      .finally(() => {
        state.busy = false;
        setUi();
      });
  };

  btnWsSend.addEventListener("click", () => send());
  wsSendInput.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") send();
  });

  btnWsClear.addEventListener("click", () => {
    cmdOut.textContent = "{}";
    setMsg("info", "");
  });

  setUi();

  return {
    onShow: () => setUi(),
  };
};
