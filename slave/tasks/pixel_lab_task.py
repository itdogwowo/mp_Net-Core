# tasks/pixel_lab_task.py — PixelLab 低優先測試源（APA / RGB / PCA）
#
# 定位：只是測試工具，優先度最低。
#   - 直播 / 燈效模式啟動時會把 pixel_owner 設成 "stream" / "pixel"；
#     PixelLabTask 看到 owner 不是 "test" 就停止、不寫、不備份。
#   - 頁面 return / 離開時把 pixel_lab.active=0；owner=test 時由本 task
#     把本頁 range 歸零（buffer）或把選中的 PCA 16 通道歸零，然後釋放 owner。
#   - 不恢復上一個模式/直播（沒有 LIFO、沒有 stack、沒有 flags 備份）。
#
# 模式（兩個效果都由 lib.sw.effect_core 的波生成工具產生，步進式）：
#   base   = Effect("lab_base")  正弦，200 幀 = 4s @ 50fps
#   blink  = Effect("lab_blink") 方波，25 幀 = 0.5s（2Hz）
#   每 commit 一幀 step += 1；不是用 time.ticks_ms 切時間。
#
#   apa / rgb：走 pixel_stream hub（跟 stream/pixel 任務一樣是 producer）
#       hub.get_write_view() / commit()；RenderTask 統一 read_into + show_all()
#       hub 滿（RenderTask 未消化）→ get_write_view() 回 None → 自然停
#       base：全部燈 cell（APA/WS/PCA）一起呼吸；不是只有本頁 type
#       selected：本頁 index 那顆最後寫入（固定次序；閃爍不會被 base 覆蓋）
#   pca：直接寫 pca9685_ctl 選中的 PCA（direct 特例，不經 hub）
#       base：16 通道全部呼吸
#       selected：單通道或 ALL 最後寫入（固定次序；閃爍不會被 base 覆蓋）
#       ALL 時 16 通道一起閃；RenderTask 用 is_streaming=False / is_ready=True
#       保持不動，避免 buffer 的 0x70 廣播蓋掉直接寫入的值。
#       PCA 仍用 System.frame_interval_ms 自己 pacing（RenderTask 不消費）。
#
# 選中模式（steady / blink）先固定 blink；未來改 SEL_MODE 即可，
# UI 不提供切換（使用者定案）。
import time
from array import array

from lib.sys.task import Task
from lib.sys.sys_bus import bus
from lib.sys.log_service import get_log
from lib.sw.effect_core import Effect

FRAME_MS = 20
LEASE_MS = 3000
SEL_MODE = "blink"           # "blink" | "steady"；未來直接改這裡
PCA_CH = 16
ALL_CH = 0xFF

# 兩個效果都由 lib.sw.effect_core（波生成工具）產生，步進式、不是時間切片。
#   F=10 + end_Time=N → 一個完整週期 N 幀（50fps）。
#   base：正弦呼吸 200 幀 = 4s；blink：方波 25 幀 = 0.5s（2Hz）。
BASE_PROGRAM = [{"type": "math_now", "F": 10, "l_max": 4095,
                 "l_lim": 0, "phi": 0, "end_Time": 200}]
BLINK_PROGRAM = [{"type": "square_wave_now", "F": 10, "l_max": 4095,
                  "l_lim": 0, "phi": 0, "end_Time": 25}]
BASE_PARAMS = {"pixel_n": 1, "step": 1, "spacing": 0, "offset": 0,
               "speed": 1, "reverse": False, "program": BASE_PROGRAM}
BLINK_PARAMS = {"pixel_n": 1, "step": 1, "spacing": 0, "offset": 0,
                "speed": 1, "reverse": False, "program": BLINK_PROGRAM}

TYPE_MAP = {
    "i2c_pixel": "pca9685",
    "WS2812": "ws2812",
    "APA102": "apa102",
    "uartMotor1": "motor",
}
PAGE_TYPE = {"apa": "apa102", "rgb": "ws2812"}


class PixelLabTask(Task):
    def __init__(self, name, ctx):
        super().__init__(name, ctx)
        self._st_pixel = None
        self._hub = None
        self._lay = None
        self._disabled = False

        self._base = b""
        self._ranges = {}
        self._all_ranges = ()
        self._base_fx = None
        self._blink_fx = None
        self._step = 0
        self._one = array("H", [0])
        self._one_sel = array("H", [0])
        self._sel_off = array("H", [0])

        self._active = False
        self._mode = None
        self._next_us = 0
        self._frame_ms = FRAME_MS
        self._pca_key = None
        self._pca_rec = None
        self._pca_zeroed = False
        self._pca_last = None
        self._pca_show_n = 0
        self._pca_skip_n = 0
        self._pca_gate_us = 0
        self._pca_show_us = 0
        self._fps_n = 0
        self._fps_t0 = time.ticks_ms()
        self._fps_us = 0
        self._fps_max = 0

    # ── 啟動 ─────────────────────────────────────────────
    def on_start(self):
        super().on_start()
        self._st_pixel = bus.get_service("st_pixel")
        if self._st_pixel is None:
            self._disabled = True
            get_log().warn("[Lab] 無 st_pixel — PixelLab 停用")
            return
        # APA/RGB 走 pixel_stream hub，由 RenderTask 統一輸出（跟 stream/pixel 一樣）。
        self._hub = bus.get_service("pixel_stream")

        self._ensure_layout()
        self._base = self._build_neutral()
        self._ranges = self._build_ranges()
        self._all_ranges = self._build_all_ranges()
        self._base_fx = Effect("lab_base", BASE_PARAMS)
        self._blink_fx = Effect("lab_blink", BLINK_PARAMS)
        self._step = 0

        # 初始狀態（頁面 build 前也可能先讀）
        st = bus.shared.get("pixel_lab")
        if not isinstance(st, dict):
            bus.shared["pixel_lab"] = {
                "active": 0, "mode": "apa", "index": 0, "ch": 0,
                "target": 0x70, "target_bus": 0, "hb": 0,
            }
        if not isinstance(bus.shared.get("pixel_lab_view"), dict):
            bus.shared["pixel_lab_view"] = {}

        # 全域幀率：System.frame_interval_ms（不再硬寫 FRAME_MS）。
        try:
            iv = int((bus.shared.get("System") or {}).get("frame_interval_ms", FRAME_MS) or FRAME_MS)
        except Exception:
            iv = FRAME_MS
        self._frame_ms = iv if iv > 0 else FRAME_MS

        # 測試用：/bench 內容 rgb / apa / pca；量完移除檔案即可停用。
        try:
            import os
            if "bench" in os.listdir("/"):
                mode = "rgb"
                try:
                    f = open("/bench")
                    mode = f.read().strip() or "rgb"
                    f.close()
                except Exception:
                    pass
                if mode not in ("rgb", "apa", "pca"):
                    mode = "rgb"
                bus.shared["pixel_lab"] = {
                    "active": 1, "mode": mode, "index": 0, "ch": 0,
                    "target": 0x70, "target_bus": 0, "hb": 0,
                }
                get_log().info("[Lab] BENCH mode={}".format(mode))
        except Exception:
            pass

        get_log().info("[Lab] online | ranges={} | total_bytes={}".format(
            sorted(self._ranges.keys()), self._st_pixel.total_bytes))

    def _ensure_layout(self):
        lay = bus.shared.get("pixel_layout")
        if lay is None or not getattr(lay, "counts", None):
            from lib.sw.pixel_layout import PixelLayout
            order = []
            counts = {}
            for c in self._st_pixel.controllers:
                t = TYPE_MAP.get(getattr(c, "pixel_type", ""))
                if t is None:
                    continue
                if t not in counts:
                    counts[t] = 0
                    order.append(t)
                counts[t] += int(c.num_pixels)
            lay = PixelLayout(order, counts)
            bus.shared["pixel_layout"] = lay
        self._lay = lay

    def _build_all_ranges(self):
        """全部燈 cell（APA/WS/PCA）的連續區段；motor 不在內。

        回傳 [(start, count), ...]；_tick_buffer 用 slice 一次填一段，
        取代 4016 顆逐顆 scatter（C 層 slice assignment 比 viper 迴圈快）。
        """
        offs = array("H")
        for t in ("apa102", "ws2812", "pca9685"):
            cnt = int(getattr(self._lay, "counts", {}).get(t, 0) or 0)
            if cnt <= 0:
                continue
            start = int(self._lay.type_offsets[t])
            for i in range(start, start + cnt):
                offs.append(i)
        out = []
        n = len(offs)
        if n:
            start = int(offs[0])
            prev = start
            cnt = 1
            for k in range(1, n):
                o = int(offs[k])
                if o == prev + 1:
                    cnt += 1
                else:
                    out.append((start, cnt))
                    start = o
                    cnt = 1
                prev = o
            out.append((start, cnt))
        return tuple(out)

    def _build_neutral(self):
        """整槽 neutral：燈 0、motor/其他用 controller.neutral_value（W byte）。"""
        st = self._st_pixel
        buf = bytearray(st.total_bytes)
        for i, c in enumerate(st.controllers):
            neutral = int(getattr(c, "neutral_value", 0) or 0) & 0xFF
            off = int(st.offsets[i])
            for k in range(c.num_pixels):
                o = off + (k << 2)
                buf[o] = 0
                buf[o + 1] = 0
                buf[o + 2] = 0
                buf[o + 3] = neutral
        return bytes(buf)

    def _build_ranges(self):
        """APA/RGB 兩頁的 global pixel range。

        PixelLayout 的 type_offsets 是 type-contiguous；本專案的 pixel_drv
        是把同型別 controller 排在一起（apa, ws, pca, motor），所以直接
        用 lay.counts/type_offsets 就是 buffer 裡的 index 空間。
        """
        lay = self._lay
        out = {}
        for page, t in PAGE_TYPE.items():
            if t not in lay.counts:
                continue
            start = int(lay.type_offsets.get(t, 0))
            count = int(lay.counts[t])
            if count <= 0:
                continue
            out[page] = {
                "start": start,
                "count": count,
                "offs": array("H", range(start, start + count)),
                "io": self._io_label(t),
            }
        return out

    def _io_label(self, t):
        if t == "ws2812":
            cfg = bus.shared.get("WS2812") or {}
            gs = []
            for it in (cfg.get("list") or []):
                g = it.get("GPIO")
                if g is not None:
                    gs.append("GPIO{}".format(g))
            return " / ".join(gs) if gs else "GPIO"
        if t == "apa102":
            cfg = bus.shared.get("APA102") or {}
            items = cfg.get("list") or []
            spi_idx = 0
            if items:
                spi_idx = int((items[0].get("GPIO") or {}).get("spi", 0) or 0)
            s = "SPI{}".format(spi_idx)
            scfg = bus.shared.get("SPI") or {}
            slist = scfg.get("list") or []
            if 0 <= spi_idx < len(slist):
                g = slist[spi_idx].get("GPIO") or {}
                if g.get("sck") is not None:
                    s += " · SCK:{}".format(g["sck"])
                if g.get("mosi") is not None:
                    s += " · MOSI:{}".format(g["mosi"])
            return s
        return t

    def _io_pca(self, rec):
        cfg = bus.shared.get("I2C") or {}
        items = cfg.get("list") or []
        bus_idx = int(rec.get("i2c", 0) or 0) if rec else 0
        s = "I2C{}".format(bus_idx)
        pins = ""
        if 0 <= bus_idx < len(items):
            g = items[bus_idx].get("GPIO") or {}
            parts = []
            if g.get("sda") is not None:
                parts.append("SDA {}".format(g["sda"]))
            if g.get("scl") is not None:
                parts.append("SCL {}".format(g["scl"]))
            pins = " · ".join(parts)
        return s, pins

    # ── 狀態/視圖 ────────────────────────────────────────
    def _state(self):
        st = bus.shared.get("pixel_lab")
        if not isinstance(st, dict):
            st = {
                "active": 0, "mode": "apa", "index": 0, "ch": 0,
                "target": 0x70, "target_bus": 0, "hb": 0,
            }
            bus.shared["pixel_lab"] = st
        return st

    def _publish(self, active, busy, mode, base_pct=0, sel_pct=0,
                 index=0, ch=0, ch_pcts=None, target="", io="", count=0,
                 pins=""):
        v = bus.shared.get("pixel_lab_view")
        if not isinstance(v, dict):
            v = {}
            bus.shared["pixel_lab_view"] = v
        v["active"] = 1 if active else 0
        v["busy"] = 1 if busy else 0
        v["mode"] = mode or ""
        v["base_pct"] = int(base_pct)
        v["sel_pct"] = int(sel_pct)
        v["index"] = int(index)
        v["ch"] = int(ch)
        v["ch_pcts"] = ch_pcts
        v["target"] = target
        v["io"] = io or ""
        v["pins"] = pins or ""
        v["count"] = int(count)
        v["sel_mode"] = SEL_MODE

    def _idle_view(self):
        self._publish(False, False, "")

    def _busy_view(self, st):
        self._publish(False, True, st.get("mode", ""),
                      target="", io="", count=0)

    # ── 主迴圈 ───────────────────────────────────────────
    def _fps_log(self):
        now = time.ticks_ms()
        if time.ticks_diff(now, self._fps_t0) < 1000:
            return
        el = time.ticks_diff(now, self._fps_t0)
        n = self._fps_n
        get_log().info("[FPS] lab {:.1f}/s tick avg {}us max {}us step {}".format(
            n * 1000.0 / el, self._fps_us // max(n, 1), self._fps_max, self._step))
        try:
            sw, sk, ga, sa, ov = self._st_pixel.stats()
            get_log().info("[GATE] lab show={} skip={} gate_avg={}us show_avg={}us overhead={:.3f}%".format(
                sw, sk, ga, sa, ov))
            for i, t, csh, csk, cavg, cstart, ccnt in self._st_pixel.ctrl_stats():
                get_log().info("[GATE] lab ctrl #{} {} [{}+{}) show={} skip={} avg={}us".format(
                    i, t, cstart, ccnt, csh, csk, cavg))
        except Exception:
            pass
        pn = self._pca_show_n + self._pca_skip_n
        if pn:
            ga = self._pca_gate_us // pn
            sa = self._pca_show_us // max(self._pca_show_n, 1)
            ov = (ga * 100000.0 / (ga + sa)) / 1000.0 if (ga + sa) else 0.0
            get_log().info("[GATE] pca show={} skip={} gate_avg={}us show_avg={}us overhead={:.3f}%".format(
                self._pca_show_n, self._pca_skip_n, ga, sa, ov))
        self._fps_t0 = now
        self._fps_n = 0
        self._fps_us = 0
        self._fps_max = 0

    def loop(self):
        if not self.running or self._disabled:
            return
        self._fps_log()
        st = self._state()
        active = bool(st.get("active"))
        owner = bus.shared.get("pixel_owner")
        now = time.ticks_ms()

        if active:
            hb = int(st.get("hb", 0) or 0)
            if hb and time.ticks_diff(now, hb) > LEASE_MS:
                # 頁面卡死/離開沒通知 → 歸零並釋放
                self._deactivate(zero=True, release=True)
                st["active"] = 0
                self._idle_view()
                return

        if not active:
            if self._active:
                self._deactivate(zero=True, release=True)
            self._idle_view()
            return

        if owner not in (None, "test"):
            # 直播/模式優先：停止、不寫、不備份
            if self._active:
                self._preempt()
            self._busy_view(st)
            return

        if owner is None:
            bus.shared["pixel_owner"] = "test"

        mode = st.get("mode", "apa")
        if self._active and self._mode != mode:
            # 換頁：先清舊 range，再起新 range；owner 保持 test
            self._deactivate(zero=True, release=False)

        if not self._active:
            self._activate(mode)

        if mode == "pca":
            self._tick_pca(st)
        else:
            self._tick_buffer(st, mode)

    # ── 起停 ─────────────────────────────────────────────
    def _activate(self, mode):
        self._active = True
        self._mode = mode
        self._step = 0
        if mode == "pca":
            # PCA 仍是特例：直接寫 pca9685_ctl，不讓 RenderTask 用 buffer 覆蓋。
            self._next_us = time.ticks_us()
            bus.shared.update({
                "is_streaming": False,
                "is_ready": True,
                "is_paused": False,
            })
            self._pca_key = None
            self._pca_rec = None
            self._pca_zeroed = False
            self._pca_last = None
        else:
            # APA/RGB 只當 producer：寫 pixel_stream hub，RenderTask 統一 show_all()。
            bus.shared.update({
                "is_streaming": True,
                "is_ready": True,
                "is_paused": False,
            })
        get_log().info("[Lab] activate {}".format(mode))

    def _preempt(self):
        """被直播/模式接手：停止、不寫、不清；owner 不動。"""
        self._active = False
        self._mode = None
        self._pca_key = None
        self._pca_rec = None
        self._pca_zeroed = False
        self._pca_last = None
        self._fps_n = 0
        self._fps_t0 = time.ticks_ms()
        self._fps_us = 0
        self._fps_max = 0

    def _deactivate(self, zero=True, release=True):
        mode = self._mode
        was = self._active
        self._active = False
        self._mode = None

        owner = bus.shared.get("pixel_owner")
        if zero and owner == "test":
            if mode in ("apa", "rgb"):
                # 先讓 RenderTask 停止取幀，再 flush + 清燈，避免它又顯示舊 slot。
                bus.shared.update({
                    "is_streaming": False,
                    "is_ready": False,
                    "is_paused": False,
                })
                try:
                    if self._hub is not None:
                        self._hub.flush()
                except Exception:
                    pass
                try:
                    self._st_pixel.clear_all()
                except Exception:
                    pass
            else:
                try:
                    if self._hub is not None:
                        self._hub.flush()
                except Exception:
                    pass
                try:
                    self._zero_pca(self._pca_rec)
                except Exception:
                    pass
                try:
                    self._st_pixel.clear_all()
                except Exception:
                    pass
                bus.shared.update({
                    "is_streaming": False,
                    "is_ready": False,
                    "is_paused": False,
                })

        self._pca_key = None
        self._pca_rec = None
        self._pca_zeroed = False
        self._pca_last = None
        self._fps_n = 0
        self._fps_t0 = time.ticks_ms()
        self._fps_us = 0
        self._fps_max = 0
        if release and bus.shared.get("pixel_owner") == "test":
            bus.shared["pixel_owner"] = None
        if was:
            get_log().info("[Lab] deactivate {}".format(mode))

    # ── APA / RGB ────────────────────────────────────────
    def _tick_buffer(self, st, mode):
        r = self._ranges.get(mode)
        if not r:
            self._deactivate(zero=True, release=True)
            return
        if not bus.shared.get("led_enabled", True):
            self._step += 1
            return
        # 跟 stream/pixel 一樣：只當 producer，寫 hub slot 後 commit；
        # hub 滿（RenderTask 還沒消化）→ get_write_view 回 None，自然停。
        if self._hub is None:
            return
        view = self._hub.get_write_view()
        if view is None:
            return

        t0 = time.ticks_us()
        base = self._base_value()
        base_pct = base * 100 // 4095
        # 一次 C 層 copy 回中性，再把每段燈用 slice 填 base（不逐顆 scatter）。
        try:
            view[:] = self._base
        except Exception:
            pass
        pat = bytes((0, 0, base & 0xFF, base >> 8))
        try:
            for start, count in self._all_ranges:
                a = start << 2
                b = (start + count) << 2
                view[a:b] = pat * count
        except Exception as e:
            get_log().warn("[Lab] fill all err: {}".format(e))
            return

        try:
            idx = int(st.get("index", 0) or 0)
        except Exception:
            idx = 0
        idx = max(0, min(idx, r["count"] - 1))
        sv = self._sel_value()
        # 固定次序：base 先填，選中/閃爍最後寫，閃爍一定不會被 base 覆蓋。
        self._sel_off[0] = r["start"] + idx
        self._one_sel[0] = sv
        try:
            self._lay.scatter_offs(view, self._sel_off, self._one_sel, "wwww")
        except Exception as e:
            get_log().warn("[Lab] scatter selected err: {}".format(e))
            return
        try:
            self._hub.commit()
        except Exception as e:
            get_log().warn("[Lab] hub commit err: {}".format(e))
            return
        dt = time.ticks_diff(time.ticks_us(), t0)
        self._fps_n += 1
        self._fps_us += dt
        if dt > self._fps_max:
            self._fps_max = dt

        self._step += 1
        self._publish(True, False, mode, base_pct,
                      100 if sv else 0, idx, 0, None,
                      "", r["io"], r["count"])

    # ── PCA direct ───────────────────────────────────────
    def _find_pca(self, addr, bus_idx):
        ctl = bus.get_service("pca9685_ctl")
        if not isinstance(ctl, list):
            return None
        for r in ctl:
            try:
                if int(r.get("addr", -1)) == addr and int(r.get("i2c", 0)) == bus_idx:
                    return r
            except Exception:
                continue
        for r in ctl:
            try:
                if int(r.get("addr", -1)) == addr:
                    return r
            except Exception:
                continue
        return None

    def _zero_pca(self, rec):
        if not rec:
            return
        pca = rec.get("pca")
        if pca is None:
            return
        try:
            for i in range(PCA_CH):
                pca.buf[i] = 0
            pca.show()
        except Exception:
            pass

    def _tick_pca(self, st):
        try:
            addr = int(st.get("target", 0x70) or 0)
        except Exception:
            addr = 0x70
        try:
            bus_idx = int(st.get("target_bus", 0) or 0)
        except Exception:
            bus_idx = 0
        rec = self._find_pca(addr, bus_idx)
        if rec is None or rec.get("pca") is None:
            # 目標不見了：不要亂寫，顯示被接手/等待
            self._publish(False, True, "pca", target="", io="", count=PCA_CH)
            return

        key = (int(rec.get("i2c", 0) or 0), addr)
        if self._pca_key is not None and self._pca_key != key:
            self._zero_pca(self._pca_rec)
        self._pca_key = key
        self._pca_rec = rec
        pca = rec.get("pca")

        # PCA 是 direct 特例，仍需自己的節拍（APA/RGB 已交給 hub/RenderTask）。
        now_us = time.ticks_us()
        if time.ticks_diff(now_us, self._next_us) < 0:
            return
        self._next_us = time.ticks_add(now_us, self._frame_ms * 1000)

        if not bus.shared.get("led_enabled", True):
            if not self._pca_zeroed:
                self._zero_pca(rec)
                self._pca_zeroed = True
            self._pca_last = None
            self._step += 1
            return
        self._pca_zeroed = False

        base = self._base_value()
        base_pct = base * 100 // 4095
        try:
            ch = int(st.get("ch", 0) or 0)
        except Exception:
            ch = 0
        # 0xFF = ALL_LED（頁面「全部通道」）；其餘 0..15 單通道。
        all_ch = (ch & 0xFF) == ALL_CH
        if not all_ch:
            ch = max(0, min(ch, PCA_CH - 1))
        sv = self._sel_value()
        # 小緩衝：記錄這一次 PCA 系列的核心值；一樣就跳過 pca.show()。
        key = (addr, ch, all_ch, base, sv)
        t0 = time.ticks_us()
        if self._pca_last == key:
            self._pca_skip_n += 1
            self._pca_gate_us += time.ticks_diff(time.ticks_us(), t0)
            self._step += 1
            if all_ch:
                pcts = [100 if sv else 0] * PCA_CH
            else:
                pcts = [base_pct] * PCA_CH
                pcts[ch] = 100 if sv else 0
            name = "0x{:02X}".format(addr)
            io, pins = self._io_pca(rec)
            self._publish(True, False, "pca", base_pct,
                          100 if sv else 0, 0, ch, pcts,
                          name, io, PCA_CH, pins)
            return
        # 固定次序：base 先寫，選中/閃爍最後寫，閃爍不會被 base 覆蓋。
        try:
            for i in range(PCA_CH):
                pca.buf[i] = base
            if all_ch:
                for i in range(PCA_CH):
                    pca.buf[i] = sv
            else:
                pca.buf[ch] = sv
            pca.show()
        except Exception as e:
            get_log().warn("[Lab] pca write err: {}".format(e))
            self._publish(False, True, "pca", target="", io="", count=PCA_CH)
            return
        self._pca_last = key
        t_end = time.ticks_us()
        dt = time.ticks_diff(t_end, t0)
        self._pca_show_n += 1
        self._pca_show_us += dt
        self._fps_n += 1
        self._fps_us += dt
        if dt > self._fps_max:
            self._fps_max = dt
        self._step += 1

        if all_ch:
            pcts = [100 if sv else 0] * PCA_CH
        else:
            pcts = [base_pct] * PCA_CH
            pcts[ch] = 100 if sv else 0
        name = "0x{:02X}".format(addr)
        io, pins = self._io_pca(rec)
        self._publish(True, False, "pca", base_pct,
                      100 if sv else 0, 0, ch, pcts,
                      name, io, PCA_CH, pins)

    # ── 數值 ─────────────────────────────────────────────
    def _base_value(self):
        fx = self._base_fx
        if fx is None:
            return 0
        return int(fx.frame(self._step)[0]) & 0xFFF

    def _sel_value(self):
        if SEL_MODE == "steady":
            return 4095
        fx = self._blink_fx
        if fx is None:
            return 0
        return int(fx.frame(self._step)[0]) & 0xFFF

    def on_stop(self):
        super().on_stop()
        try:
            self._deactivate(zero=True, release=True)
        except Exception:
            pass
