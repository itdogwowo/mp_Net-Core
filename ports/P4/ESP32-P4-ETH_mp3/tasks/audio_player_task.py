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
        self._feeding = False    # irq 重入防護：true = 已有餵食在執行中
        self._reenter = False    # 重入被擋下時記錄，等目前這份跑完再補跑一次
        self._silencing = False  # DMA 鏈上已有靜音 → 空轉時不重複塞

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
        """補一格（非阻塞）。重入防護 + 正確性三原則（修「irq 播放加速」）：

          1. 單一餵食來源：irq 在 bytecode 間隙插入，可能打斷 loop 的首填或
             上一份 handler。_feeding 旗標把重入擋下，資料只由「正在執行的那
             份」推進；被擋下的重入用 _reenter 記住，等目前這份跑完再補跑。
             避免同一個 slot 被 pop 兩次 / 兩份同時寫。
          2. ring 滿時絕不放棄已取出的 slot：write() 回 0 = DMA ring 暫時滿，
             不是「這槽沒用」。舊版這裡 release_read() → 整槽 46ms 音訊被丟、
             producer 立刻覆寫 → ring 一有空間就跳下一槽 = 聽感「加速/跳段」。
             現在改成掛 _pend=(view, off) 留在原 off，等下一個 irq 續寫，寧慢不丟。
          3. hub 空時只補一次靜音：_silencing 記住 DMA 鏈上已有靜音；irq 高速
             空轉時不會一直塞靜音疊滿 ring，underrun 也只記一次。
        """
        if self._feeding:
            self._reenter = True
            return
        self._feeding = True
        try:
            self._feed_once()
        finally:
            self._feeding = False
        if self._reenter:
            self._reenter = False
            self._feed_nonblock()

    def _feed_once(self):
        dac = self._dac
        p = self._pend
        if p is not None:
            view, off = p
            # 正常路徑下 off 一定 < len(view)（寫完當下就 release 了）。
            # 若 mute 瞬間被 reset 過又立刻復播，_pend 內容已與 hub 狀態
            # 不一致 → 直接當掉這份殘留，重新從 hub 取，不重複釋放。
            if off >= len(view):
                self._pend = None
                return
            n = dac.write(memoryview(view)[off:])
            if n <= 0:
                return                  # ring 滿 → 保留 pend 原位，下次再續
            off += n
            if off >= len(view):
                self._release_pend()
                self._silencing = False
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
                    self._pend = (view, n)   # ring 部分滿 → slot 保持 READING 續寫
                else:
                    self._pend = (view, 0)   # ring 全滿 → 保留不釋放，等下個 irq
                if n > 0:
                    self._silencing = False
                self.success += 1
                return
        # hub 空但 DMA 鏈還在 → 只補一次靜音維持（underrun），不重複疊
        if self._silencing:
            return
        dac.write(self._silence)
        self._silencing = True
        self._underruns += 1

    def _release_pend(self):
        """把 _pend 指向的 hub slot 釋放。只釋放「已由 hub 取出」的槽；
        _pend=None 或未經 get_read_view 的 view（防呆）都直接清空。"""
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
            self._silencing = False
            return

        if self._muted:
            self._dac.mute(False)
            self._muted = False
            self._silencing = False

        if self._mode == "irq":
            # irq 驅動；loop 只做「低成本輪詢退路」：有料（pending 未完或 hub
            # 有槽）就試餵一次。真正的低延遲補格由 irq handler 負責；loop 確保
            # 即使 irq 偶發漏接 / DMA 鏈清空後合成端才補上料，也不會永遠停擺。
            # _feed_nonblock 內部有重入防護（_feeding），loop 與 irq 同時各跑
            # 一份時只會由先到的那份推進，不會 double-pop 同一個 slot。
            if self._pend is not None or self._hub.dirty:
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
