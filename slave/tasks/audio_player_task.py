# tasks/audio_player_task.py
# AudioPlayerTask — 音訊「播放端」（consumer）：audio_stream hub → audio_dac
#
# 兩任務分工（對稱 pixel: PixelTask 合成 → RenderTask 播放）:
#   dj_task（合成端）把混好的 PCM slot 寫進共享 "audio_stream" hub
#   （AtomicStreamHub，bus service）。
#   AudioPlayerTask（本檔 = 播放端）從 hub 取 slot 餵給 audio_dac。
#
# 兩種餵法（config `Audio.mode`）:
#   "block"（預設）: 每圈 `write()` 阻塞至 DMA 有空位（~46ms/8KB @44.1k stereo）
#         = 硬體節拍；阻塞放 GIL → 合成端可同時混音。
#   "irq"（method 2 非阻塞）: 註冊 I2S irq（dac.set_irq）後 write 非阻塞、立即
#         回實際寫入位元組；DMA 吃完一格緩衝 → irq 觸發 = 「來補下一格」的通知，
#         handler 從 hub pop 下一格（partial 續寫）。節拍仍由 DMA 決定，只是
#         Python 不再阻塞。P4 固件實測 irq 正常（見 test/audio/i2s_irq_probe.py）。
#
# 控制旗標（bus.shared，dj_task 寫、本任務讀）:
#   audio_streaming = True 播放中；audio_paused = True 暫停。
# 播放端是唯一碰 audio_dac 的地方：XSMT 靜音 / 解除靜音由旗標驅動。
import time
from lib.sys.task import Task
from lib.sys.sys_bus import bus
from lib.sys.log_service import get_log

SLOT_BYTES = 8192


class AudioPlayerTask(Task):
    def __init__(self, name, ctx):
        super().__init__(name, ctx)
        self._dac = None
        self._hub = None
        self._disabled = False
        self._muted = True       # 上一靜音狀態（避免每圈重複 mute）
        self._silence = bytearray(SLOT_BYTES)
        self._underruns = 0
        self._mode = "block"     # "block" / "irq"
        self._pend = None        # irq partial: (view, off) 一槽未完的剩餘
        self._irq_fires = 0

    def on_start(self):
        super().on_start()
        self._dac = bus.get_service("audio_dac")
        if self._dac is None:
            self._disabled = True
            get_log().warn("[Aplay] 無 audio_dac（I2S/PCM5102 未啟用）— 播放端停用")
            return
        # 等合成端（dj_task）註冊 audio_stream hub（RenderTask 等 pixel_stream 同款）
        while self._hub is None:
            self._hub = bus.get_service("audio_stream")
            if self._hub is None:
                time.sleep_ms(5)

        mode = str((bus.shared.get("Audio") or {}).get("mode", "block")).lower()
        if mode in ("irq", "irq2"):
            try:
                style = self._dac.set_irq(self._on_feed)
            except Exception:
                style = None
            if style:
                self._mode = "irq"
                get_log().info("🔊 [Aplay] online（播放端, irq 非阻塞, style={}）".format(style))
            else:
                self._mode = "block"
                get_log().warn("[Aplay] irq 註冊失敗（所有簽名）→ 退回阻塞模式")
        else:
            self._mode = "block"
            get_log().info("🔊 [Aplay] online（播放端, 阻塞模式）")

        bus.register_provider("audio_underruns", lambda: self._underruns)
        bus.register_provider("audio_irq_fires", lambda: self._irq_fires)
        self._dac.mute(True)
        get_log().info("🔊 [Aplay] online | slot={}B x{} mode={}".format(
            self._hub.size, self._hub.num_buffers, self._mode))

    # ── irq handler（method 2）：一格被 DMA 吃光 → 補下一格 ──
    def _on_feed(self, i2s=None):
        self._irq_fires += 1
        if not self.running or self._disabled or self._muted:
            return
        try:
            self._feed_nonblock()
        except Exception:
            pass

    def _feed_nonblock(self):
        """補一格（非阻塞）：先寫完 pending 剩餘 → 再 pop 新 slot。"""
        dac = self._dac
        p = self._pend
        if p is not None:
            view, off = p
            if off >= len(view):
                self._pend = None
                p = None
            else:
                n = dac.write(memoryview(view)[off:])
                if n <= 0:
                    return
                off += n
                if off >= len(view):
                    self._release_pend()
                else:
                    self._pend = (view, off)
                self.success += 1
                return
        if self._hub.dirty:
            view = self._hub.get_read_view()
            if view is not None:
                n = dac.write(view)
                if n >= len(view):
                    self._hub.release_read()
                elif n > 0:
                    self._pend = (view, n)    # ring 滿 → slot 保持 READING 續寫
                else:
                    self._hub.release_read()  # 一格都進不去 → 歸還等下次
                self.success += 1
                return
        # hub 空但 DMA 鏈還在 → 補一格靜音維持（underrun）
        dac.write(self._silence)
        self._underruns += 1

    def _release_pend(self):
        if self._pend is not None:
            try:
                self._hub.release_read()
            except Exception:
                pass
        self._pend = None

    # ── 主迴圈 ──
    def loop(self):
        if not self.running or self._disabled:
            return

        streaming = bool(bus.shared.get("audio_streaming", False))
        paused = bool(bus.shared.get("audio_paused", False))

        # 靜音狀態由旗標驅動：沒在播或暫停 → XSMT 靜音
        if (not streaming) or paused:
            if not self._muted:
                self._dac.mute(True)
                self._muted = True
            self._pend = None     # 外部 flush（合成端）已作廢此 slot → 直接丟
            return

        if self._muted:
            self._dac.mute(False)
            self._muted = False

        if self._mode == "irq":
            # irq 驅動；loop 只負責起播首填與退路（pending 未完時不插隊）
            if self._pend is None and self._hub.dirty:
                self._feed_nonblock()
            return

        # ── 阻塞模式（預設）：一格 blocking write = 硬體節拍 ──
        if self._hub.dirty:
            view = self._hub.get_read_view()
            if view is not None:
                self._dac.write(view)      # 阻塞至 DMA 有空位（~46ms，放 GIL）
                self._hub.release_read()
                self.success += 1
                return
        # hub 空但旗標在播：合成端還沒跟上 → 補靜音維持 DMA 不欠資料（underrun）
        self._dac.write(self._silence)
        self._underruns += 1

    def on_stop(self):
        super().on_stop()
        self._pend = None
        if self._dac is not None:
            try:
                self._dac.mute(True)
            except Exception:
                pass
        get_log().info("AudioPlayerTask Stopped")
