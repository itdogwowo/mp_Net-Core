"""
pcm5102_drv.py — PCM5102 DAC 模組管理（走 I2S，模組層）

設定來源: bus.shared["PCM5102"]  ({enable, list})
          list item: {"GPIO": {"i2s": <i2s_list index>, "xsmt": <gpio>}}
產物:    bus.register_service("audio_dac", <Pcm5102Dac>)

硬體模組獨有的東西（XSMT 靜音、DAC 封裝、契約 fmt）全收在本 driver，
不進資料通道；dj_task 只經 audio_dac API 播放，不碰腳位/I2S 物件細節。

接線注意（交接文件三條命脈，無聲先查這三條）:
  1. XSMT 必須拉高（懸空 = 靜音）—— 本 driver 初始化時自動解除靜音
  2. SCK 接 GND（強制內部 PLL 從 BCK 恢復主時鐘）
  3. 與 ESP32 共地
"""
from machine import Pin, I2S
from lib.sys.sys_bus import bus
from lib.sys.log_service import get_log


class Pcm5102Dac:
    """PCM5102 封裝：持有 I2S 輸出 + XSMT 靜音腳，對外只給 write/mute/fmt。"""

    def __init__(self, i2s, xsmt_pin=None, fmt=(44100, 16, 2)):
        self._i2s = i2s
        self._xsmt = xsmt_pin
        self._fmt = tuple(int(v) for v in fmt)   # (rate, bits, channels) 契約
        self._muted = True
        # 先解除靜音再開始使用（XSMT 懸空/低電位 = 完全無聲，歷史頭號踩坑）
        self.unmute()

    def write(self, buf):
        """寫 PCM 進 I2S DMA。阻塞至 DMA 有空間（節拍由硬體驅動，會放 GIL）。"""
        return self._i2s.write(buf)

    def mute(self, on):
        if self._xsmt is None:
            self._muted = bool(on)
            return
        self._muted = bool(on)
        self._xsmt.value(0 if on else 1)   # XSMT 低 = 靜音

    def unmute(self):
        self.mute(False)

    def set_irq(self, handler):
        """註冊 I2S irq（method 2 非阻塞播放用，見計劃書 §3.6）。

        irq 註冊後 write() 變非阻塞（回實際寫入位元組）；DMA 吃完一格緩衝會
        觸發 handler = 「緩衝空了，來補下一格」的通知。各固件 irq 簽名不一
        （實測 ESP32-P4 只接受 bare `irq(handler)`），依序嘗試並回報成功方式：
          "bare" = irq(handler) / "pos" = irq(handler, I2S.TX) / "kw" = irq(trigger=...)
        全失敗 → 回 None（維持阻塞模式）。
        """
        i2s = self._i2s
        for name, call in (
            ("bare", lambda: i2s.irq(handler)),
            ("pos", lambda: i2s.irq(handler, I2S.TX)),
            ("kw", lambda: i2s.irq(handler, trigger=I2S.TX)),
        ):
            try:
                call()
                return name
            except Exception:
                continue
        return None

    @property
    def muted(self):
        return self._muted

    @property
    def fmt(self):
        """目前輸出契約 (rate, bits, channels) — dj_task 拿來驗證 WAV header。"""
        return self._fmt


def init_pcm5102(sysbus=None):
    sysbus = sysbus or bus
    cfg = sysbus.shared.get("PCM5102") or {}
    if not cfg.get("enable"):
        return None

    i2s_list = sysbus.get_service("i2s_list") or []
    i2s_cfg_list = (sysbus.shared.get("I2S") or {}).get("list", [])
    dac = None
    for item in cfg.get("list", []):
        gpio = item.get("GPIO", {})
        i2s_idx = gpio.get("i2s", 0)
        if i2s_idx < 0 or i2s_idx >= len(i2s_list):
            get_log().error("PCM5102: i2s index {} not found".format(i2s_idx))
            continue
        xsmt_gpio = gpio.get("xsmt")
        xsmt_pin = None
        if xsmt_gpio is not None:
            xsmt_pin = Pin(int(xsmt_gpio), Pin.OUT)
        # 契約 fmt：取自對應 I2S 區塊的 config（rate/bits/format→channels）
        icfg = (i2s_cfg_list[i2s_idx] or {}).get("config", {}) \
            if i2s_idx < len(i2s_cfg_list) else {}
        rate = int(icfg.get("rate", 44100) or 44100)
        bits = int(icfg.get("bits", 16) or 16)
        ch = 1 if str(icfg.get("format", "stereo")).lower() == "mono" else 2
        dac = Pcm5102Dac(i2s_list[i2s_idx], xsmt_pin, fmt=(rate, bits, ch))
        break   # 目前單 DAC 單一 service

    sysbus.register_service("audio_dac", dac)
    get_log().info("PCM5102: {} device(s)".format(1 if dac else 0))
    return dac


def gpios(sysbus=None):
    """boot.py Phase 1 用：回報模組獨有腳位（xsmt）做 GPIO 衝突檢查。"""
    sysbus = sysbus or bus
    cfg = sysbus.shared.get("PCM5102") or {}
    if not cfg.get("enable"):
        return {}

    result = {}
    for item in cfg.get("list", []):
        xsmt = item.get("GPIO", {}).get("xsmt")
        if xsmt is not None:
            result[int(xsmt)] = "pcm5102_xsmt"
    return result
