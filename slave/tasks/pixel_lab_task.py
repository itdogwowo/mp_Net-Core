# tasks/pixel_lab_task.py — PixelLab 低優先測試源（APA / RGB / PCA）
#
# 定位：只是測試工具，優先度最低。
#   - 直播 / 燈效模式啟動時會把 pixel_owner 設成 "stream" / "pixel"；
#     PixelLabTask 看到 owner 不是 "test" 就停止、不寫、不備份。
#   - 頁面 return / 離開時把 pixel_lab.active=0；owner=test 時由本 task
#     把本頁 range 歸零（buffer）或把選中的 PCA 16 通道歸零，然後釋放 owner。
#   - 不恢復上一個模式/直播（沒有 LIFO、沒有 stack、沒有 flags 備份）。
#
# 模式：
#   apa / rgb：走 pixel_stream hub（RenderTask 播放）
#       base：本頁 type 的所有 cell 用 wwww 呼吸（0..4095）
#       selected：本頁 index 那顆用 wwww 長亮/2Hz 閃爍
#       其他 type 的 cell 保持 neutral（燈 0；motor 0x80）
#   pca：直接寫 pca9685_ctl 選中的 PCA（不經 hub）
#       base：16 通道全部呼吸
#       selected：選中 channel 長亮/2Hz 閃爍
#       RenderTask 用 is_streaming=False / is_ready=True 保持不動，
#       避免 buffer 的 0x70 廣播蓋掉直接寫入的值。
#
# 選中模式（steady / blink）先固定 blink；未來改 SEL_MODE 即可，
# UI 不提供切換（使用者定案）。
import math
import time
from array import array

from lib.sys.task import Task
from lib.sys.sys_bus import bus
from lib.sys.log_service import get_log

FRAME_MS = 20
LEASE_MS = 3000
BLINK_HALF_MS = 250          # 2 Hz 方波（on 250 / off 250）
BREATH_MS = 2000
SEL_MODE = "blink"           # "blink" | "steady"；未來直接改這裡
PCA_CH = 16

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
        self._table = None
        self._one = array("H", [0])
        self._one_sel = array("H", [0])
        self._sel_off = array("H", [0])

        self._active = False
        self._mode = None
        self._next_us = 0
        self._pca_key = None
        self._pca_rec = None

    # ── 啟動 ─────────────────────────────────────────────
    def on_start(self):
        super().on_start()
        self._st_pixel = bus.get_service("st_pixel")
        self._hub = bus.get_service("pixel_stream")
        if self._st_pixel is None or self._hub is None:
            self._disabled = True
            get_log().warn("[Lab] 無 st_pixel / pixel_stream — PixelLab 停用")
            return

        self._ensure_layout()
        self._base = self._build_neutral()
        self._ranges = self._build_ranges()
        self._table = self._build_table()

        # 初始狀態（頁面 build 前也可能先讀）
        st = bus.shared.get("pixel_lab")
        if not isinstance(st, dict):
            bus.shared["pixel_lab"] = {
                "active": 0, "mode": "apa", "index": 0, "ch": 0,
                "target": 0x70, "target_bus": 0, "hb": 0,
            }
        if not isinstance(bus.shared.get("pixel_lab_view"), dict):
            bus.shared["pixel_lab_view"] = {}
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
        if 0 <= bus_idx < len(items):
            g = items[bus_idx].get("GPIO") or {}
            if g.get("scl") is not None:
                s += " · SCL:{}".format(g["scl"])
            if g.get("sda") is not None:
                s += " · SDA:{}".format(g["sda"])
        return s

    @staticmethod
    def _build_table():
        n = 256
        tab = array("H", [0]) * n
        for i in range(n):
            tab[i] = int((math.sin(2.0 * math.pi * i / n) + 1.0) * 2047.5)
        return tab

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
                 index=0, ch=0, ch_pcts=None, target="", io="", count=0):
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
        v["count"] = int(count)
        v["sel_mode"] = SEL_MODE

    def _idle_view(self):
        self._publish(False, False, "")

    def _busy_view(self, st):
        self._publish(False, True, st.get("mode", ""),
                      target="", io="", count=0)

    # ── 主迴圈 ───────────────────────────────────────────
    def loop(self):
        if not self.running or self._disabled:
            return
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

        now_us = time.ticks_us()
        if time.ticks_diff(now_us, self._next_us) < 0:
            return
        self._next_us = time.ticks_add(now_us, FRAME_MS * 1000)

        if mode == "pca":
            self._tick_pca(st)
        else:
            self._tick_buffer(st, mode)

    # ── 起停 ─────────────────────────────────────────────
    def _activate(self, mode):
        self._active = True
        self._mode = mode
        self._next_us = time.ticks_us()
        if mode == "pca":
            # 不讓 RenderTask 用 buffer（含 0x70 廣播）覆蓋直接寫入
            bus.shared.update({
                "is_streaming": False,
                "is_ready": True,
                "is_paused": False,
            })
            self._pca_key = None
            self._pca_rec = None
        else:
            try:
                self._hub.flush()
            except Exception:
                pass
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

    def _deactivate(self, zero=True, release=True):
        mode = self._mode
        was = self._active
        self._active = False
        self._mode = None

        owner = bus.shared.get("pixel_owner")
        if zero and owner == "test":
            if mode in ("apa", "rgb"):
                try:
                    self._hub.flush()
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
            else:
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
        view = self._hub.get_write_view()
        if view is None:
            return

        base = self._base_value()
        base_pct = base * 100 // 4095
        try:
            view[:] = self._base
        except Exception:
            pass

        self._one[0] = base
        try:
            self._lay.scatter_offs(view, r["offs"], self._one, "wwww")
        except Exception as e:
            get_log().warn("[Lab] scatter range err: {}".format(e))
            return

        try:
            idx = int(st.get("index", 0) or 0)
        except Exception:
            idx = 0
        idx = max(0, min(idx, r["count"] - 1))
        sv = self._sel_value()
        self._sel_off[0] = r["start"] + idx
        self._one_sel[0] = sv
        try:
            self._lay.scatter_offs(view, self._sel_off, self._one_sel, "wwww")
        except Exception as e:
            get_log().warn("[Lab] scatter selected err: {}".format(e))
            return
        try:
            self._hub.commit()
        except Exception:
            return

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

        base = self._base_value()
        base_pct = base * 100 // 4095
        try:
            ch = int(st.get("ch", 0) or 0)
        except Exception:
            ch = 0
        ch = max(0, min(ch, PCA_CH - 1))
        sv = self._sel_value()
        try:
            for i in range(PCA_CH):
                pca.buf[i] = base
            pca.buf[ch] = sv
            pca.show()
        except Exception as e:
            get_log().warn("[Lab] pca write err: {}".format(e))
            self._publish(False, True, "pca", target="", io="", count=PCA_CH)
            return

        pcts = [base_pct] * PCA_CH
        pcts[ch] = 100 if sv else 0
        name = "0x{:02X}".format(addr)
        self._publish(True, False, "pca", base_pct,
                      100 if sv else 0, 0, ch, pcts,
                      name, self._io_pca(rec), PCA_CH)

    # ── 數值 ─────────────────────────────────────────────
    def _base_value(self):
        if self._table is None:
            return 0
        idx = (time.ticks_ms() % BREATH_MS) * 256 // BREATH_MS
        return int(self._table[idx]) & 0xFFF

    def _sel_value(self):
        if SEL_MODE == "steady":
            return 4095
        return 4095 if (time.ticks_ms() // BLINK_HALF_MS) & 1 == 0 else 0

    def on_stop(self):
        super().on_stop()
        try:
            self._deactivate(zero=True, release=True)
        except Exception:
            pass
