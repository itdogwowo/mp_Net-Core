export const init = async (ctx) => {
  const el = ctx.el;

  const mp4Drop = el("mp4Drop");
  const mp4ImportMode = el("mp4ImportMode");
  const mp4ImportFilesWrap = el("mp4ImportFilesWrap");
  const mp4ImportFolderWrap = el("mp4ImportFolderWrap");
  const mp4ImportFiles = el("mp4ImportFiles");
  const mp4ImportFolder = el("mp4ImportFolder");
  const mp4RangeStart = el("mp4RangeStart");
  const mp4RangeEnd = el("mp4RangeEnd");
  const mp4Scrub = el("mp4Scrub");
  const btnMp4Play = el("btnMp4Play");
  const mp4ScrubMeta = el("mp4ScrubMeta");
  const mp4Video = el("mp4Video");
  const mp4CanvasLive = el("mp4CanvasLive");
  const btnMp4Build = el("btnMp4Build");
  const btnMp4Reset = el("btnMp4Reset");
  const mp4Msg = el("mp4Msg");
  const mp4Fps = el("mp4Fps");
  const mp4StepMode = el("mp4StepMode");
  const mp4Max = el("mp4Max");
  const mp4StepFrames = el("mp4StepFrames");
  const mp4Q = el("mp4Q");
  const mp4CapKB = el("mp4CapKB");
  const mp4W = el("mp4W");
  const mp4H = el("mp4H");
  const mp4ResizeMode = el("mp4ResizeMode");
  const mp4Start = el("mp4Start");
  const mp4End = el("mp4End");
  const mp4AutoFit = el("mp4AutoFit");
  const mp4Bg = el("mp4Bg");
  const mp4Rot = el("mp4Rot");
  const mp4Gray = el("mp4Gray");
  const mp4FlipX = el("mp4FlipX");
  const mp4FlipY = el("mp4FlipY");
  const mp4CropOn = el("mp4CropOn");
  const mp4CropX = el("mp4CropX");
  const mp4CropY = el("mp4CropY");
  const mp4CropW = el("mp4CropW");
  const mp4CropH = el("mp4CropH");
  const mp4Folder = el("mp4Folder");
  const mp4Prefix = el("mp4Prefix");
  const mp4Pad = el("mp4Pad");
  const mp4TarName = el("mp4TarName");
  const mp4DlManifest = el("mp4DlManifest");
  const btnMp4Preview = el("btnMp4Preview");
  const mp4PreviewT = el("mp4PreviewT");
  const mp4Prog = el("mp4Prog");
  const mp4ProgText = el("mp4ProgText");
  const mp4Dur = el("mp4Dur");
  const mp4Est = el("mp4Est");

  const state = {
    mp4Url: null,
    mode: "none",
    imgFiles: [],
    jpkFrames: [],
    playing: false,
    playTimer: null,
    raf: 0,
  };

  const setMsg = (kind, text) => {
    mp4Msg.textContent = text || "";
    mp4Msg.style.color =
      kind === "good" ? "rgba(73,247,177,.95)" :
      kind === "bad" ? "rgba(255,80,115,.95)" :
      kind === "warn" ? "rgba(255,207,90,.95)" :
      "var(--mut)";
  };

  const setProg = (i, n) => {
    if (!mp4Prog || !mp4ProgText) return;
    if (!n || n <= 0) {
      mp4Prog.style.width = "0%";
      mp4ProgText.textContent = "—";
      return;
    }
    const pct = Math.max(0, Math.min(100, Math.round((i / n) * 100)));
    mp4Prog.style.width = pct + "%";
    mp4ProgText.textContent = `${i}/${n}`;
  };

  const waitEvent = (target, name, timeoutMs = 15000) =>
    new Promise((resolve, reject) => {
      const t = setTimeout(() => {
        cleanup();
        reject(new Error("timeout"));
      }, timeoutMs);
      const cleanup = () => {
        clearTimeout(t);
        target.removeEventListener(name, on);
        target.removeEventListener("error", onErr);
      };
      const on = () => {
        cleanup();
        resolve();
      };
      const onErr = () => {
        cleanup();
        reject(new Error("error"));
      };
      target.addEventListener(name, on, { once: true });
      target.addEventListener("error", onErr, { once: true });
    });

  const seek = async (t) => {
    mp4Video.currentTime = t;
    await waitEvent(mp4Video, "seeked", 12000);
  };

  const blobToU8 = async (blob) => {
    const ab = await blob.arrayBuffer();
    return new Uint8Array(ab);
  };

  const u32le = (n) => {
    const b = new Uint8Array(4);
    new DataView(b.buffer).setUint32(0, n >>> 0, true);
    return b;
  };

  const downloadBlob = (blob, name) => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 60000);
  };

  const buildJpk = (frames, maxSize) => {
    const count = frames.length >>> 0;
    const header = new Uint8Array(16);
    header[0] = 0x4a;
    header[1] = 0x50;
    header[2] = 0x4b;
    header[3] = 0x31;
    new DataView(header.buffer).setUint32(4, count, true);
    new DataView(header.buffer).setUint32(8, maxSize >>> 0, true);
    new DataView(header.buffer).setUint32(12, 0, true);

    const parts = [header];
    for (let i = 0; i < frames.length; i++) {
      const u8 = frames[i];
      parts.push(u32le(u8.byteLength), u8);
    }
    return new Blob(parts, { type: "application/octet-stream" });
  };

  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const num = (v, d = 0) => {
    const n = Number(v);
    return Number.isFinite(n) ? n : d;
  };

  const readSettings = () => {
    const stepMode = (mp4StepMode?.value || "fps").trim();
    const fps = clamp(Math.round(num(mp4Fps?.value, 10)), 1, 60);
    const stepFrames = clamp(Math.round(num(mp4StepFrames?.value, 1)), 1, 1000000);
    const maxFrames = clamp(Math.round(num(mp4Max?.value, 300)), 1, 20000);
    const q = clamp(num(mp4Q?.value, 0.8), 0.05, 1);
    const capKB = clamp(Math.round(num(mp4CapKB?.value, 0)), 0, 500000);
    const w = clamp(Math.round(num(mp4W?.value, 480)), 16, 4096);
    const hIn = Math.round(num(mp4H?.value, 0));
    const h = clamp(hIn, 0, 4096);
    const resizeMode = (mp4ResizeMode?.value || "contain").trim();
    const start = Math.max(0, num(mp4Start?.value, 0));
    const end = Math.max(0, num(mp4End?.value, 0));
    const autoFit = (mp4AutoFit?.value || "on") === "on";
    const bg = (mp4Bg?.value || "#000000").trim() || "#000000";
    const rot = clamp(num(mp4Rot?.value, 0), -180, 180);
    const gray = (mp4Gray?.value || "off") === "on";
    const flipX = (mp4FlipX?.value || "off") === "on";
    const flipY = (mp4FlipY?.value || "off") === "on";
    const cropOn = (mp4CropOn?.value || "off") === "on";
    const cropX = clamp(num(mp4CropX?.value, 0), 0, 100);
    const cropY = clamp(num(mp4CropY?.value, 0), 0, 100);
    const cropW = clamp(num(mp4CropW?.value, 100), 1, 100);
    const cropH = clamp(num(mp4CropH?.value, 100), 1, 100);
    const folder = (mp4Folder?.value || "frames").trim() || "frames";
    const prefix = (mp4Prefix?.value || "").trim();
    const padN = clamp(Math.round(num(mp4Pad?.value, 6)), 2, 10);
    const tarName = (mp4TarName?.value || "output").trim() || "output";
    const previewT = Math.max(0, num(mp4PreviewT?.value, 0));
    const dlManifest = !!(mp4DlManifest && mp4DlManifest.checked);

    return {
      stepMode,
      fps,
      stepFrames,
      maxFrames,
      q,
      capKB,
      w,
      h,
      resizeMode,
      start,
      end,
      autoFit,
      bg,
      rot,
      gray,
      flipX,
      flipY,
      cropOn,
      cropX,
      cropY,
      cropW,
      cropH,
      folder,
      prefix,
      padN,
      tarName,
      previewT,
      dlManifest,
    };
  };

  const sanitizePathPart = (s) => {
    const v = (s || "").trim().replace(/\\/g, "/");
    const clean = v.replace(/^\/*/, "").replace(/\.\.+/g, ".").replace(/\/{2,}/g, "/");
    return clean || "frames";
  };

  const calcRanges = (dur, cfg, baseFps = 30) => {
    const start = clamp(cfg.start, 0, Math.max(0, dur || 0));
    const end0 = cfg.end > 0 ? cfg.end : (dur || 0);
    const end = clamp(end0, start, Math.max(start, dur || start));
    const span = Math.max(0, end - start);

    const base = clamp(num(baseFps, 30), 1, 240);
    let stepSec =
      cfg.stepMode === "frames" ? (cfg.stepFrames / base) :
      1 / cfg.fps;
    stepSec = Math.max(0.001, stepSec);

    let n = Math.floor(span / stepSec) + 1;
    if (n < 1) n = 1;
    if (cfg.autoFit && n > cfg.maxFrames) {
      n = cfg.maxFrames;
      stepSec = span / Math.max(1, n - 1);
    } else if (!cfg.autoFit && n > cfg.maxFrames) {
      n = cfg.maxFrames;
    }

    return { start, end, span, stepSec, n };
  };

  const guessVideoFps = () => {
    try {
      const s = mp4Video.captureStream && mp4Video.captureStream();
      const t = s && s.getVideoTracks && s.getVideoTracks()[0];
      const fr = t && t.getSettings && t.getSettings().frameRate;
      try {
        if (t && typeof t.stop === "function") t.stop();
        if (s && s.getTracks) s.getTracks().forEach((x) => x.stop && x.stop());
      } catch {}
      if (Number.isFinite(fr) && fr > 0) return fr;
    } catch {}
    return 30;
  };

  const computeCrop = (vw, vh, cfg) => {
    if (!cfg.cropOn) return { sx: 0, sy: 0, sw: vw, sh: vh };
    const sx = clamp(Math.round((vw * cfg.cropX) / 100), 0, vw - 1);
    const sy = clamp(Math.round((vh * cfg.cropY) / 100), 0, vh - 1);
    const sw = clamp(Math.round((vw * cfg.cropW) / 100), 1, vw - sx);
    const sh = clamp(Math.round((vh * cfg.cropH) / 100), 1, vh - sy);
    return { sx, sy, sw, sh };
  };

  const ensureOutSize = (vw, vh, crop, cfg) => {
    const srcW = crop.sw || vw;
    const srcH = crop.sh || vh;
    const outW = cfg.w;
    const outH = cfg.h > 0 ? cfg.h : Math.max(1, Math.round((outW * srcH) / srcW));
    return { outW, outH };
  };

  const drawFrame = (g, vw, vh, cfg) => {
    const crop = computeCrop(vw, vh, cfg);
    const { outW, outH } = ensureOutSize(vw, vh, crop, cfg);

    if (g.canvas.width !== outW) g.canvas.width = outW;
    if (g.canvas.height !== outH) g.canvas.height = outH;

    g.save();
    g.fillStyle = cfg.bg || "#000";
    g.fillRect(0, 0, outW, outH);
    g.restore();

    g.save();
    if (cfg.gray) g.filter = "grayscale(1)";

    const rad = (cfg.rot * Math.PI) / 180;
    g.translate(outW / 2, outH / 2);
    if (rad) g.rotate(rad);
    g.scale(cfg.flipX ? -1 : 1, cfg.flipY ? -1 : 1);
    g.translate(-outW / 2, -outH / 2);

    const srcW = crop.sw;
    const srcH = crop.sh;

    let dx = 0, dy = 0, dw = outW, dh = outH;
    if (cfg.resizeMode === "contain" || cfg.resizeMode === "cover") {
      const s1 = outW / srcW;
      const s2 = outH / srcH;
      const s = cfg.resizeMode === "contain" ? Math.min(s1, s2) : Math.max(s1, s2);
      dw = srcW * s;
      dh = srcH * s;
      dx = (outW - dw) / 2;
      dy = (outH - dh) / 2;
    }
    g.drawImage(mp4Video, crop.sx, crop.sy, crop.sw, crop.sh, dx, dy, dw, dh);
    g.restore();

    return { crop, outW, outH };
  };

  const drawFromSource = (g, src, sw, sh, cfg) => {
    const crop = computeCrop(sw, sh, cfg);
    const { outW, outH } = ensureOutSize(sw, sh, crop, cfg);

    if (g.canvas.width !== outW) g.canvas.width = outW;
    if (g.canvas.height !== outH) g.canvas.height = outH;

    g.save();
    g.fillStyle = cfg.bg || "#000";
    g.fillRect(0, 0, outW, outH);
    g.restore();

    g.save();
    if (cfg.gray) g.filter = "grayscale(1)";
    const rad = (cfg.rot * Math.PI) / 180;
    g.translate(outW / 2, outH / 2);
    if (rad) g.rotate(rad);
    g.scale(cfg.flipX ? -1 : 1, cfg.flipY ? -1 : 1);
    g.translate(-outW / 2, -outH / 2);

    const srcW = crop.sw;
    const srcH = crop.sh;

    let dx = 0, dy = 0, dw = outW, dh = outH;
    if (cfg.resizeMode === "contain" || cfg.resizeMode === "cover") {
      const s1 = outW / srcW;
      const s2 = outH / srcH;
      const s = cfg.resizeMode === "contain" ? Math.min(s1, s2) : Math.max(s1, s2);
      dw = srcW * s;
      dh = srcH * s;
      dx = (outW - dw) / 2;
      dy = (outH - dh) / 2;
    }
    g.drawImage(src, crop.sx, crop.sy, crop.sw, crop.sh, dx, dy, dw, dh);
    g.restore();

    return { crop, outW, outH };
  };

  const stopPlaying = () => {
    state.playing = false;
    if (state.raf) cancelAnimationFrame(state.raf);
    state.raf = 0;
    if (state.playTimer) clearInterval(state.playTimer);
    state.playTimer = null;
    try {
      mp4Video.pause();
    } catch {}
    if (btnMp4Play) btnMp4Play.textContent = "播放";
  };

  const setMode = (mode) => {
    state.mode = mode;
    stopPlaying();
  };

  const ensureCanvasCtx = () => {
    const g = mp4CanvasLive.getContext("2d", { alpha: false });
    return g;
  };

  const setScrubMeta = (text) => {
    if (mp4ScrubMeta) mp4ScrubMeta.textContent = text || "—";
  };

  const getRangePct = () => {
    const a = Number(mp4RangeStart?.value || 0);
    const b = Number(mp4RangeEnd?.value || 100);
    const lo = Math.max(0, Math.min(100, Math.min(a, b)));
    const hi = Math.max(0, Math.min(100, Math.max(a, b)));
    return { lo, hi };
  };

  const getScrubPct = () => {
    return Math.max(0, Math.min(100, Number(mp4Scrub?.value || 0)));
  };

  const setScrubPct = (p) => {
    if (!mp4Scrub) return;
    mp4Scrub.value = String(Math.max(0, Math.min(100, p)));
  };

  const getVideoRangeSec = async () => {
    if (!isFinite(mp4Video.duration) || mp4Video.duration <= 0) {
      await waitEvent(mp4Video, "loadedmetadata", 15000);
    }
    const dur = mp4Video.duration || 0;
    const { lo, hi } = getRangePct();
    const start = (dur * lo) / 100;
    const end = (dur * hi) / 100;
    return { dur, start, end };
  };

  const getImageRangeIdx = (count) => {
    const { lo, hi } = getRangePct();
    const max = Math.max(0, count - 1);
    const s = Math.round((max * lo) / 100);
    const e = Math.round((max * hi) / 100);
    return { startIdx: Math.max(0, Math.min(max, Math.min(s, e))), endIdx: Math.max(0, Math.min(max, Math.max(s, e))) };
  };

  const decodeBitmapFromU8 = async (u8) => {
    const blob = new Blob([u8], { type: "image/jpeg" });
    if (typeof createImageBitmap === "function") {
      return await createImageBitmap(blob);
    }
    const url = URL.createObjectURL(blob);
    const img = new Image();
    img.decoding = "async";
    img.src = url;
    await new Promise((resolve, reject) => {
      img.onload = resolve;
      img.onerror = reject;
    });
    URL.revokeObjectURL(url);
    return img;
  };

  const decodeBitmapFromFile = async (file) => {
    if (typeof createImageBitmap === "function") {
      return await createImageBitmap(file);
    }
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.decoding = "async";
    img.src = url;
    await new Promise((resolve, reject) => {
      img.onload = resolve;
      img.onerror = reject;
    });
    URL.revokeObjectURL(url);
    return img;
  };

  const jpegWithCap = async (canvas, q, capKB) => {
    const capBytes = capKB > 0 ? capKB * 1024 : 0;
    if (!capBytes) {
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", q));
      if (!blob) throw new Error("toBlob failed");
      return await blobToU8(blob);
    }
    let lo = 0.05;
    let hi = Math.max(lo, Math.min(1, q));
    let best = null;
    for (let i = 0; i < 9; i++) {
      const mid = (lo + hi) / 2;
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", mid));
      if (!blob) throw new Error("toBlob failed");
      if (blob.size <= capBytes) {
        best = await blobToU8(blob);
        lo = mid;
      } else {
        hi = mid;
      }
    }
    if (best) return best;
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", lo));
    if (!blob) throw new Error("toBlob failed");
    return await blobToU8(blob);
  };

  const renderAfterFromVideo = async (t) => {
    const cfg = readSettings();
    const g = ensureCanvasCtx();
    const vw = mp4Video.videoWidth || 0;
    const vh = mp4Video.videoHeight || 0;
    if (!vw || !vh) return;
    await seek(t);
    drawFrame(g, vw, vh, cfg);
  };

  const renderAfterFromImage = async (bmp) => {
    const cfg = readSettings();
    const g = ensureCanvasCtx();
    const sw = bmp.width || bmp.naturalWidth || 0;
    const sh = bmp.height || bmp.naturalHeight || 0;
    if (!sw || !sh) return;
    drawFromSource(g, bmp, sw, sh, cfg);
  };

  const renderAtScrub = async () => {
    if (state.mode === "video" && mp4Video.src) {
      const { start, end } = await getVideoRangeSec();
      const p = getScrubPct() / 100;
      const t = start + (end - start) * p;
      setScrubMeta(`t=${t.toFixed(3)}s`);
      await renderAfterFromVideo(t);
      return;
    }
    if (state.mode === "images") {
      const n = state.imgFiles.length;
      if (!n) return;
      const { startIdx, endIdx } = getImageRangeIdx(n);
      const p = getScrubPct() / 100;
      const idx = Math.round(startIdx + (endIdx - startIdx) * p);
      setScrubMeta(`frame=${idx + 1}/${n}`);
      const bmp = await decodeBitmapFromFile(state.imgFiles[idx]);
      await renderAfterFromImage(bmp);
      return;
    }
    if (state.mode === "jpk") {
      const n = state.jpkFrames.length;
      if (!n) return;
      const { startIdx, endIdx } = getImageRangeIdx(n);
      const p = getScrubPct() / 100;
      const idx = Math.round(startIdx + (endIdx - startIdx) * p);
      setScrubMeta(`frame=${idx + 1}/${n}`);
      const bmp = await decodeBitmapFromU8(state.jpkFrames[idx]);
      await renderAfterFromImage(bmp);
    }
  };

  const reset = () => {
    stopPlaying();
    if (state.mp4Url) URL.revokeObjectURL(state.mp4Url);
    state.mp4Url = null;
    state.imgFiles = [];
    state.jpkFrames = [];
    setMode("none");
    try {
      mp4Video.removeAttribute("src");
      mp4Video.load();
    } catch {}
    if (mp4ImportFiles) mp4ImportFiles.value = "";
    if (mp4ImportFolder) mp4ImportFolder.value = "";
    if (mp4RangeStart) mp4RangeStart.value = "0";
    if (mp4RangeEnd) mp4RangeEnd.value = "100";
    if (mp4Scrub) mp4Scrub.value = "0";
    setScrubMeta("—");
    const g = ensureCanvasCtx();
    g.canvas.width = 1;
    g.canvas.height = 1;
    g.clearRect(0, 0, 1, 1);
    mp4Dur.textContent = "—";
    mp4Est.textContent = "—";
    setProg(0, 0);
    setMsg("info", "");
    ctx.setPagePill("info", "本地處理");
  };

  const parseJpk = (u8) => {
    if (u8.byteLength < 16) throw new Error("JPK too small");
    if (u8[0] !== 0x4a || u8[1] !== 0x50 || u8[2] !== 0x4b || u8[3] !== 0x31) throw new Error("Not JPK1");
    const dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
    const count = dv.getUint32(4, true);
    const frames = [];
    let off = 16;
    for (let i = 0; i < count; i++) {
      if (off + 4 > u8.byteLength) break;
      const len = dv.getUint32(off, true);
      off += 4;
      if (off + len > u8.byteLength) break;
      frames.push(u8.slice(off, off + len));
      off += len;
    }
    return frames;
  };

  const loadMp4 = async (file) => {
    reset();
    setMode("video");
    state.mp4Url = URL.createObjectURL(file);
    mp4Video.src = state.mp4Url;
    mp4Video.load();
    setMsg("info", "載入影片中…");
    try {
      await waitEvent(mp4Video, "loadedmetadata", 15000);
      const dur = mp4Video.duration;
      mp4Dur.textContent = dur ? dur.toFixed(3) + " s" : "—";
      setMsg("good", "影片已就緒");
      mp4PreviewT.value = "0";
      setProg(0, 0);
      await renderAtScrub();
    } catch (e) {
      setMsg("bad", "影片載入失敗");
    }
  };

  const loadJpk = async (file) => {
    reset();
    setMode("jpk");
    setMsg("info", "讀取 JPK 中…");
    try {
      const ab = await file.arrayBuffer();
      const frames = parseJpk(new Uint8Array(ab));
      state.jpkFrames = frames;
      mp4Dur.textContent = `${frames.length} frames`;
      setMsg("good", "JPK 已就緒");
      setProg(0, 0);
      await renderAtScrub();
    } catch (e) {
      setMsg("bad", "JPK 讀取失敗");
    }
  };

  const loadImages = async (files) => {
    reset();
    setMode("images");
    const list = Array.from(files || []).filter((f) => /^image\//.test(f.type) || /\.(png|jpe?g|webp)$/i.test(f.name));
    list.sort((a, b) => (a.webkitRelativePath || a.name).localeCompare(b.webkitRelativePath || b.name));
    state.imgFiles = list;
    mp4Dur.textContent = `${list.length} frames`;
    setMsg("good", "圖片序列已就緒");
    setProg(0, 0);
    await renderAtScrub();
  };

  const buildJpkOut = async () => {
    const cfg = readSettings();

    btnMp4Build.disabled = true;
    btnMp4Reset.disabled = true;
    if (btnMp4Preview) btnMp4Preview.disabled = true;
    setMsg("info", "準備中…");
    ctx.setPagePill("info", "處理中");
    setProg(0, 0);

    try {
      const canvas = document.createElement("canvas");
      const g = canvas.getContext("2d", { alpha: false });
      if (!g) {
        setMsg("bad", "Canvas 初始化失敗");
        return;
      }

      let outName = cfg.tarName || "output.jpk";
      if (!/\.jpk$/i.test(outName)) outName = outName + ".jpk";

      const folder = sanitizePathPart(cfg.folder);
      const prefix = cfg.prefix || "";
      const padN = cfg.padN || 6;

      const framesBin = [];
      let maxSize = 0;
      const manifest = {
        kind: "frames_pack",
        settings: cfg,
        frames: [],
      };

      if (state.mode === "video" && mp4Video.src) {
        const { dur } = await getVideoRangeSec();
        const range = calcRanges(dur, cfg, guessVideoFps());
        const vw = mp4Video.videoWidth || 0;
        const vh = mp4Video.videoHeight || 0;
        if (!vw || !vh) {
          setMsg("bad", "讀取影片尺寸失敗");
          return;
        }
        manifest.source = { type: "video", duration: dur, videoWidth: vw, videoHeight: vh };
        mp4Est.textContent = `${range.n} 張 · step ${range.stepSec.toFixed(4)}s · ${cfg.w}x${cfg.h || "auto"}`;

        await seek(range.start);
        for (let i = 0; i < range.n; i++) {
          const t = clamp(range.start + i * range.stepSec, range.start, range.end);
          setMsg("info", `處理中… ${i + 1}/${range.n}`);
          setProg(i + 1, range.n);
          await seek(t);
          const outMeta = drawFrame(g, vw, vh, cfg);
          const u8 = await jpegWithCap(canvas, cfg.q, cfg.capKB);
          framesBin.push(u8);
          if (u8.byteLength > maxSize) maxSize = u8.byteLength;
          const name = `${folder}/${prefix}${String(i + 1).padStart(padN, "0")}.jpg`;
          manifest.frames.push({ name, t, out: { w: outMeta.outW, h: outMeta.outH }, crop: outMeta.crop, size: u8.byteLength });
        }
      } else if (state.mode === "images" || state.mode === "jpk") {
        const srcList = state.mode === "images" ? state.imgFiles : state.jpkFrames;
        const n = srcList.length;
        if (!n) {
          setMsg("warn", "沒有輸入素材");
          return;
        }
        const idxRange = getImageRangeIdx(n);
        const fps = cfg.fps;
        const spanFrames = Math.max(0, idxRange.endIdx - idxRange.startIdx);
        let stepF = cfg.stepMode === "frames" ? cfg.stepFrames : 1;
        if (cfg.stepMode === "frames") {
          let nn = Math.floor(spanFrames / stepF) + 1;
          if (cfg.autoFit && nn > cfg.maxFrames) {
            stepF = Math.ceil(spanFrames / Math.max(1, cfg.maxFrames - 1));
            stepF = clamp(stepF, 1, 1000000);
            nn = Math.floor(spanFrames / stepF) + 1;
          } else if (!cfg.autoFit && nn > cfg.maxFrames) {
            nn = cfg.maxFrames;
          }
          manifest.source = { type: state.mode, count: n, fps };
          mp4Est.textContent = `${nn} 張 · step ${stepF} frame · ${cfg.w}x${cfg.h || "auto"}`;

          for (let i = 0; i < nn; i++) {
            const idx0 = idxRange.startIdx + i * stepF;
            const idx = clamp(idx0, idxRange.startIdx, idxRange.endIdx);
            const t = idx / Math.max(1, fps);
            setMsg("info", `處理中… ${i + 1}/${nn}`);
            setProg(i + 1, nn);
            const bmp = state.mode === "images" ? await decodeBitmapFromFile(srcList[idx]) : await decodeBitmapFromU8(srcList[idx]);
            const sw = bmp.width || bmp.naturalWidth || 0;
            const sh = bmp.height || bmp.naturalHeight || 0;
            const outMeta = drawFromSource(g, bmp, sw, sh, cfg);
            const u8 = await jpegWithCap(canvas, cfg.q, cfg.capKB);
            framesBin.push(u8);
            if (u8.byteLength > maxSize) maxSize = u8.byteLength;
            const name = `${folder}/${prefix}${String(i + 1).padStart(padN, "0")}.jpg`;
            manifest.frames.push({ name, t, src_idx: idx, out: { w: outMeta.outW, h: outMeta.outH }, crop: outMeta.crop, size: u8.byteLength });
          }
        } else {
          const dur = Math.max(0, (n - 1) / fps);
          const range = calcRanges(dur, cfg, fps);
          manifest.source = { type: state.mode, count: n, fps };
          mp4Est.textContent = `${range.n} 張 · step ${range.stepSec.toFixed(4)}s · ${cfg.w}x${cfg.h || "auto"}`;

          for (let i = 0; i < range.n; i++) {
            const t = clamp(range.start + i * range.stepSec, range.start, range.end);
            const idx0 = Math.round(t * fps);
            const idx = clamp(idx0, idxRange.startIdx, idxRange.endIdx);
            setMsg("info", `處理中… ${i + 1}/${range.n}`);
            setProg(i + 1, range.n);
            const bmp = state.mode === "images" ? await decodeBitmapFromFile(srcList[idx]) : await decodeBitmapFromU8(srcList[idx]);
            const sw = bmp.width || bmp.naturalWidth || 0;
            const sh = bmp.height || bmp.naturalHeight || 0;
            const outMeta = drawFromSource(g, bmp, sw, sh, cfg);
            const u8 = await jpegWithCap(canvas, cfg.q, cfg.capKB);
            framesBin.push(u8);
            if (u8.byteLength > maxSize) maxSize = u8.byteLength;
            const name = `${folder}/${prefix}${String(i + 1).padStart(padN, "0")}.jpg`;
            manifest.frames.push({ name, t, src_idx: idx, out: { w: outMeta.outW, h: outMeta.outH }, crop: outMeta.crop, size: u8.byteLength });
          }
        }
      } else {
        setMsg("warn", "請先匯入 MP4 / 圖片 / JPK");
        return;
      }

      setMsg("info", "封裝中…");
      const jpk = buildJpk(framesBin, maxSize);
      downloadBlob(jpk, outName);

      if (cfg.dlManifest) {
        const manText = JSON.stringify(manifest, null, 2);
        const manBlob = new Blob([manText], { type: "application/json" });
        downloadBlob(manBlob, "manifest.json");
      }

      ctx.setPagePill("good", "完成");
      setMsg("good", "已下載 " + outName);
    } catch (e) {
      setMsg("bad", "處理失敗：" + String(e?.message || e));
      ctx.setPagePill("bad", "失敗");
    } finally {
      btnMp4Build.disabled = false;
      btnMp4Reset.disabled = false;
      if (btnMp4Preview) btnMp4Preview.disabled = false;
      setProg(0, 0);
    }
  };

  const preview = async () => {
    stopPlaying();
    const cfg = readSettings();
    if (state.mode === "video" && mp4Video.src) {
      const t = clamp(cfg.previewT, 0, Math.max(0, mp4Video.duration || 0));
      await renderAfterFromVideo(t);
      setMsg("good", "預覽完成");
      return;
    }
    if (state.mode === "images" || state.mode === "jpk") {
      const n = state.mode === "images" ? state.imgFiles.length : state.jpkFrames.length;
      if (!n) {
        setMsg("warn", "沒有輸入素材");
        return;
      }
      const fps = cfg.fps;
      const dur = Math.max(0, (n - 1) / fps);
      const t = clamp(cfg.previewT, 0, dur);
      const idx = clamp(Math.round(t * fps), 0, n - 1);
      const bmp = state.mode === "images" ? await decodeBitmapFromFile(state.imgFiles[idx]) : await decodeBitmapFromU8(state.jpkFrames[idx]);
      await renderAfterFromImage(bmp);
      setMsg("good", "預覽完成");
    }
  };

  const playToggle = async () => {
    if (state.playing) {
      stopPlaying();
      return;
    }
    if (state.mode === "video" && mp4Video.src) {
      const cfg = readSettings();
      const g = ensureCanvasCtx();
      const vw = mp4Video.videoWidth || 0;
      const vh = mp4Video.videoHeight || 0;
      if (!vw || !vh) return;
      const { start, end } = await getVideoRangeSec();
      state.playing = true;
      btnMp4Play.textContent = "停止";
      try {
        mp4Video.currentTime = start;
        await mp4Video.play();
      } catch {}
      const loop = () => {
        if (!state.playing) return;
        if (mp4Video.currentTime > end) {
          mp4Video.currentTime = start;
        }
        drawFrame(g, vw, vh, cfg);
        const p = (mp4Video.currentTime - start) / Math.max(0.0001, (end - start));
        setScrubPct(p * 100);
        setScrubMeta(`t=${mp4Video.currentTime.toFixed(3)}s`);
        state.raf = requestAnimationFrame(loop);
      };
      state.raf = requestAnimationFrame(loop);
      return;
    }
    if (state.mode === "images" || state.mode === "jpk") {
      const cfg = readSettings();
      const n = state.mode === "images" ? state.imgFiles.length : state.jpkFrames.length;
      if (!n) return;
      const { startIdx, endIdx } = getImageRangeIdx(n);
      const stepMs = Math.round(1000 / cfg.fps);
      const stepF = cfg.stepMode === "frames" ? cfg.stepFrames : 1;
      let idx = startIdx;
      state.playing = true;
      btnMp4Play.textContent = "停止";
      state.playTimer = setInterval(async () => {
        if (!state.playing) return;
        const p = (idx - startIdx) / Math.max(1, (endIdx - startIdx));
        setScrubPct(p * 100);
        setScrubMeta(`frame=${idx + 1}/${n}`);
        const bmp = state.mode === "images" ? await decodeBitmapFromFile(state.imgFiles[idx]) : await decodeBitmapFromU8(state.jpkFrames[idx]);
        await renderAfterFromImage(bmp);
        idx += stepF;
        if (idx > endIdx) idx = startIdx;
      }, Math.max(30, stepMs));
    }
  };

  const onDroppedFiles = async (files) => {
    const list = Array.from(files || []);
    const mp4 = list.find((f) => /\.mp4$/i.test(f.name));
    if (mp4) {
      await loadMp4(mp4);
      return;
    }
    const jpk = list.find((f) => /\.jpk$/i.test(f.name));
    if (jpk) {
      await loadJpk(jpk);
      return;
    }
    const imgs = list.filter((f) => /^image\//.test(f.type) || /\.(png|jpe?g|webp)$/i.test(f.name));
    if (imgs.length) {
      await loadImages(imgs);
    }
  };

  const syncImportModeUi = () => {
    const mode = (mp4ImportMode?.value || "files").trim();
    const isFolder = mode === "folder";
    if (mp4ImportFilesWrap) mp4ImportFilesWrap.style.display = isFolder ? "none" : "";
    if (mp4ImportFolderWrap) mp4ImportFolderWrap.style.display = isFolder ? "" : "none";
  };

  if (mp4Drop) {
    mp4Drop.addEventListener("dragover", (e) => {
      e.preventDefault();
      mp4Drop.classList.add("on");
    });
    mp4Drop.addEventListener("dragleave", () => mp4Drop.classList.remove("on"));
    mp4Drop.addEventListener("drop", async (e) => {
      e.preventDefault();
      mp4Drop.classList.remove("on");
      const files = e.dataTransfer && e.dataTransfer.files;
      if (files && files.length) await onDroppedFiles(files);
    });
  }

  if (mp4ImportMode) {
    mp4ImportMode.addEventListener("change", () => syncImportModeUi());
    mp4ImportMode.addEventListener("input", () => syncImportModeUi());
    syncImportModeUi();
  }

  if (mp4ImportFiles) {
    mp4ImportFiles.addEventListener("change", async () => {
      const files = mp4ImportFiles.files;
      if (!files || !files.length) return;
      await onDroppedFiles(files);
      mp4ImportFiles.value = "";
    });
  }

  if (mp4ImportFolder) {
    mp4ImportFolder.addEventListener("change", async () => {
      const files = mp4ImportFolder.files;
      if (!files || !files.length) return;
      await onDroppedFiles(files);
      mp4ImportFolder.value = "";
    });
  }

  if (mp4RangeStart) mp4RangeStart.addEventListener("input", () => renderAtScrub());
  if (mp4RangeEnd) mp4RangeEnd.addEventListener("input", () => renderAtScrub());
  if (mp4Scrub) mp4Scrub.addEventListener("input", () => renderAtScrub());
  if (btnMp4Play) btnMp4Play.addEventListener("click", () => playToggle());

  btnMp4Build.addEventListener("click", () => buildJpkOut());
  btnMp4Reset.addEventListener("click", () => reset());
  if (btnMp4Preview) btnMp4Preview.addEventListener("click", () => preview());

  ctx.setPagePill("info", "本地處理");

  return {
    onShow: () => ctx.setPagePill("info", "本地處理"),
  };
};
