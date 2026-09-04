# tasks/stream_task.py
# StreamTask — stream (0x30xx) 串流播放的專屬任務（生產者 + 狀態機）
#
# 取代原先寄生在 NetworkTask.loop() P3 段的 handle_supply_chain() 函式。
# 本任務只做三件事：
#   1. 讀取文件：自持 data.bin 檔案 handle，逐幀讀取，檔尾依 play_mode 循環或停止。
#   2. 決定讀多少 + 放進準確插槽：每幀讀取量 = System.num_pixels * 4（期望燈數）；
#      插槽大小 = st_pixel.total_bytes（實際燈數，由各 controller 的 Q*4 加總）。
#      兩者不等時做「多除小補」：
#        期望 > 實際 → 丟棄多餘尾段（多除）
#        期望 < 實際 → 尾段補中性值（少補：燈=0 熄滅、motor=0x80 死區停）
#      seek 偏移也用期望幀大小（frame * _frame_bytes），否則照樣錯位。
#   3. 狀態管理：IDLE / LOADING / READY / PLAYING / PAUSED / SEEKING 狀態機。
#
# 命令介面（bus.shared，由 stream_actions 命令層寫入、本任務消費）：
#   stream_cmd_set   → 0x3009 準備檔案與播放模式
#   stream_cmd_play  → 0x300A 開始播放（可帶 start_frame 中途加入）
#   stream_cmd_pause → 0x3005 暫停/恢復
#   stream_cmd_stop  → 0x3002 停止
#   stream_cmd_seek  → 0x3004 跳轉
#
# 消費端 = RenderTask（從 pixel_stream hub 取幀推硬體）。與本地燈效 PixelTask
# 共用同一 hub，靠 bus.shared["stream_active"] 旗標互斥：pixel_actions 的
# MODE_SET/MODE_STOP 會把 stream_active 設 False，本任務偵測到即視為外部停止。

from lib.sys.task import Task
from lib.sys.sys_bus import bus
from lib.sys.proto import Proto
from lib.sys.schema_codec import SchemaCodec
from lib.sys.log_service import get_log
from lib.sys.fs_manager import fs
from action import stream_actions
from action import status_actions
import time


# ── 狀態 ──
_IDLE = 0
_LOADING = 1
_READY = 2
_PLAYING = 3
_PAUSED = 4
_SEEKING = 5


class StreamTask(Task):
    def __init__(self, name, ctx):
        super().__init__(name, ctx)
        self.st = ctx.get("st_pixel")
        self.hub = None
        self._disabled = False

        self.state = _IDLE
        self._fp = None            # data.bin 檔案 handle (file 來源)
        self._src_kind = "file"    # 串流來源: "file" = 檔案; "ram" = RAM 緩衝區 (/ram/...)
        self._frame_bytes = 0      # 期望幀大小（num_pixels*4；無 num_pixels 時 = total_bytes）
        self._frame_buf = None     # 讀取用的整幀緩衝
        self._mv_frame = None      # _frame_buf 的 memoryview
        self._neutral_buf = None   # 整槽中性值（燈=0、motor=0x80）
        self._mv_neutral = None
        self._play_mode = 0
        self._cur_block = 0        # READY_ACK 回報用
        self._resume = _READY      # seek 完成後回到的狀態
        self._last_report = 0      # 🔧 主動回報播放進度節流 (ticks_ms)
        self._report_ms = 1000     # 🔧 回報間隔 (ms) — 由 config System.heartbeat_interval 控制

    def on_start(self):
        super().on_start()
        self.hub = bus.get_service("pixel_stream")
        if self.st is None:
            self.st = bus.get_service("st_pixel")

        if self.hub is None or self.st is None:
            self._disabled = True
            get_log().warn("[Stream] 無 pixel_stream hub / st_pixel — stream 停用")
            return

        total_bytes = self.st.total_bytes
        sys_cfg = bus.shared.get("System", {}) or {}
        num_pixels = int(sys_cfg.get("num_pixels", 0) or 0)
        self._frame_bytes = num_pixels * 4 if num_pixels > 0 else total_bytes
        self._frame_buf = bytearray(self._frame_bytes)
        self._mv_frame = memoryview(self._frame_buf)
        self._neutral_buf = self._build_neutral()
        self._mv_neutral = memoryview(self._neutral_buf)

        # 🔧 主動回報間隔: 直接吃 config 的 System.heartbeat_interval (ms)。
        #    沒設定/0 → 預設 1000ms; 最小值 200ms 保護 (避免把 Core0 淹沒)。
        try:
            iv = int(sys_cfg.get("heartbeat_interval", 1000) or 0)
        except Exception:
            iv = 0
        self._report_ms = max(200, iv) if iv > 0 else 1000

        get_log().info("[Stream] online | frame_bytes={} slot_bytes={} report_ms={}".format(
            self._frame_bytes, total_bytes, self._report_ms))

    # ── 中性值（與 PixelStreamer.clear_all() 同源）──────────────
    def _build_neutral(self):
        st = self.st
        buf = bytearray(st.total_bytes)
        for i, c in enumerate(st.controllers):
            neutral = getattr(c, "neutral_value", 0)
            off = st.offsets[i]
            for k in range(c.num_pixels):
                o = off + (k << 2)
                buf[o] = 0
                buf[o + 1] = 0
                buf[o + 2] = 0
                buf[o + 3] = neutral
        return buf

    # ── 多除小補：把讀入的一幀放進準確插槽 ──────────────
    def _fill_slot(self, view, n_read):
        slot = len(view)
        k = min(n_read, slot)
        if k > 0:
            view[:k] = self._mv_frame[:k]
        if slot > k:
            view[k:] = self._mv_neutral[k:]

    # ── 來源統一讀寫介面 (file / RAM 分流) ──────────────
    def _release_src(self):
        """關閉目前串流來源 (file 關 handle, ram 清 fs 串流狀態)。"""
        if self._src_kind == "ram":
            try:
                fs.end_read()
            except Exception:
                pass
        elif self._fp is not None:
            try:
                self._fp.close()
            except Exception:
                pass
        self._fp = None
        self._src_kind = "file"

    def _read_into(self, buf):
        """依來源讀下一段到 buf, 回傳位元組數 (0=結束)。"""
        if self._src_kind == "ram":
            return fs.read_into(buf)
        if self._fp is not None:
            return self._fp.readinto(buf)
        return 0

    def _seek(self, off):
        """依來源 seek (位元組偏移)。"""
        if self._src_kind == "ram":
            fs.seek(off)
        elif self._fp is not None:
            self._fp.seek(off)

    def _tell(self):
        """依來源回傳目前位元組偏移。"""
        if self._src_kind == "ram":
            return fs.tell()
        if self._fp is not None:
            return self._fp.tell()
        return 0

    # ── READY_ACK（0x3008）──────────────
    def _send_ready(self):
        app = self.ctx.get("app")
        if app is None:
            return
        ctrl = bus.get_service("net_bus_ctrl")
        if ctrl is None:
            return
        try:
            cmd_def = app.store.get(0x3008)
            if not cmd_def:
                return
            payload = SchemaCodec.encode(cmd_def, {"block_id": self._cur_block})
            ctrl.write(Proto.pack(0x3008, payload))
        except Exception as e:
            get_log().error("[Stream] READY_ACK 發送失敗: {}".format(e))

    # ── 主動回報播放進度（0x1102, 間隔 = config System.heartbeat_interval）──
    def _send_status_push(self):
        """串流播放中主動推 0x1102 STATUS_RSP 給 master (含 stream_pos_frame)。

        這是「slave 主動回報」通道: 與 PC 端 0x1101 查詢 (PC 主動) 互補 —
        PC 不用一直問也能收到播放進度, 同時順帶證明設備還活著。
        回報間隔由 config 的 System.heartbeat_interval (ms) 設定,
        只在此處 (StreamTask 領域) 觸發, 不動網絡層。
        """
        try:
            now = time.ticks_ms()
            if time.ticks_diff(now, self._last_report) < self._report_ms:
                return
            self._last_report = now
            app = self.ctx.get("app")
            ctrl = bus.get_service("net_bus_ctrl")
            if app is None or ctrl is None or not ctrl.connected:
                return
            status_actions.on_status_get(
                {"app": app, "send": ctrl.write},
                {"query_type": 0},
            )
        except Exception as e:
            get_log().error("[Stream] status push failed: {}".format(e))

    # ── 命令消費 ──────────────
    def _consume_cmds(self):
        s = bus.shared

        if s.pop("stream_cmd_stop", None) is not None:
            self._reset()
            return

        seek = s.pop("stream_cmd_seek", None)
        if seek is not None:
            resume = _PLAYING if self.state in (_PLAYING, _PAUSED) else _READY
            self._begin_seek(int(seek.get("target_frame", 0) or 0), resume)
            return

        play = s.pop("stream_cmd_play", None)
        if play is not None:
            start_frame = int(play.get("start_frame", 0) or 0)
            if start_frame > 0:
                self._begin_seek(start_frame, _PLAYING)
            else:
                self._start_playing()
            return

        pause = s.pop("stream_cmd_pause", None)
        if pause is not None:
            self._set_paused(bool(pause))
            return

        cmd_set = s.pop("stream_cmd_set", None)
        if cmd_set is not None:
            self._begin_load(cmd_set)
            return

    # ── 狀態轉換 ──────────────
    def _begin_load(self, cmd):
        file_name = cmd.get("file_name", "")
        block_id = int(cmd.get("block_id", 0) or 0)
        play_mode = int(cmd.get("play_mode", 0) or 0)

        # 🔧 重連/中途加入時可能已有開啟中的來源 (舊 stream 還在播) — 先關掉再重開,
        #    否則反覆重連會把檔案描述子耗盡 (RAM 來源則清 fs 串流狀態)。
        self._release_src()

        # ── 來源分流: /ram/... = RAM 緩衝區 (實時播放), 其餘 = 檔案 ──
        try:
            kind, full, _raw = fs.resolve(file_name)
            if kind == "ram":
                if fs.begin_read(file_name) <= 0:
                    get_log().error("[Stream] RAM 緩衝區不存在或為空: {}".format(file_name))
                    self._reset()
                    return
                self._src_kind = "ram"
                self._path = full
            else:
                self._fp = open(full, "rb")
                self._src_kind = "file"
                self._path = full
        except Exception as e:
            get_log().error("[Stream] 開檔失敗 {}: {}".format(file_name, e))
            self._reset()
            return

        self.hub.flush()
        self._play_mode = play_mode
        self._cur_block = block_id
        self.state = _LOADING
        bus.shared.update({
            "stream_active": True,
            # 🔧 準備下一段時保持「有畫面」(is_ready=True), 不要設 False ——
            #    RenderTask 看到 is_streaming=False 且 is_ready=False 會 clear_all()
            #    把燈熄掉 (準備下一段時閃黑)。保持 True 讓硬體停留在最後一幀,
            #    等 0x300A PLAY 時才無縫接上新段落。
            "is_ready": True,
            "is_streaming": False,
            "is_paused": False,
        })
        stream_actions._STREAM_STATE["mode"] = play_mode
        stream_actions._STREAM_STATE["streaming"] = False
        stream_actions._STREAM_STATE["frame_count"] = 0
        get_log().info("[Stream] load {} play_mode={}".format(path, play_mode))

    # ── 檔內絕對幀號 (進度回報用) ──────────────
    def _update_pos(self):
        """commit 一幀後把目前檔內幀號寫進 _STREAM_STATE["pos_frame"]。

        fp.tell() = (剛讀完那幀的下一位置) → 剛 commit 的幀號 = tell//frame_bytes - 1。
        用絕對幀號而非 played_frames (RenderTask 的 session 計數, 暫停會歸零、
        seek 後不重置) — PC 端顯示/自動續播計算才準。
        """
        try:
            if self._frame_bytes <= 0:
                return
            p = self._tell() // self._frame_bytes
            stream_actions._STREAM_STATE["pos_frame"] = max(0, p - 1)
        except Exception:
            pass

    def _do_load(self):
        view = self.hub.get_write_view()
        if view is None:
            return
        n = self._read_into(self._frame_buf)
        if n <= 0:
            self._reset()
            return
        self._fill_slot(view, n)
        self.hub.commit()
        self._update_pos()
        stream_actions._STREAM_STATE["frame_count"] += 1
        self.state = _READY
        bus.shared["is_ready"] = True
        self._send_ready()

    def _start_playing(self):
        if self.state in (_READY, _PAUSED):
            self.state = _PLAYING
            bus.shared["is_streaming"] = True
            bus.shared["is_paused"] = False
            stream_actions._STREAM_STATE["streaming"] = True
            get_log().info("[Stream] ▶ play")

    def _set_paused(self, paused):
        if paused:
            bus.shared["is_paused"] = True
            if self.state == _PLAYING:
                self.state = _PAUSED
        else:
            bus.shared["is_paused"] = False
            if self.state == _PAUSED:
                self.state = _PLAYING
                bus.shared["is_streaming"] = True

    def _begin_seek(self, frame, resume):
        if self._src_kind == "file" and self._fp is None:
            return
        self.hub.flush()
        try:
            self._seek(frame * self._frame_bytes)
        except Exception as e:
            get_log().error("[Stream] seek 失敗: {}".format(e))
            return
        self._resume = resume
        self.state = _SEEKING
        bus.shared["is_ready"] = False

    def _do_seek(self):
        view = self.hub.get_write_view()
        if view is None:
            return
        n = self._read_into(self._frame_buf)
        if n <= 0:
            self._reset()
            return
        self._fill_slot(view, n)
        self.hub.commit()
        self._update_pos()
        stream_actions._STREAM_STATE["frame_count"] += 1
        self.state = self._resume
        bus.shared["is_ready"] = True
        if self.state == _PLAYING:
            bus.shared["is_streaming"] = True
            stream_actions._STREAM_STATE["streaming"] = True
        self._send_ready()

    def _do_play(self):
        view = self.hub.get_write_view()
        if view is None:
            return  # hub 滿（RenderTask 還沒消化）→ 這輪不補，不阻塞
        n = self._read_into(self._frame_buf)
        if n <= 0:
            # 檔尾
            if self._play_mode == 1:
                try:
                    self._seek(0)
                except Exception:
                    self._reset()
                    return
                n = self._read_into(self._frame_buf)
                if n <= 0:
                    self._reset()
                    return
            else:
                # 🔧 非循環自然播完: 保持最後一幀 (final pose) 不立即熄燈,
                #    等 master 延遲 10s 後送 0x3002 停止指令才真正熄燈。
                self._reset(hold=True)
                return
        self._fill_slot(view, n)
        self.hub.commit()
        self._update_pos()
        stream_actions._STREAM_STATE["frame_count"] += 1

    def _reset(self, hold=False):
        """回到 IDLE 並清掉串流狀態。

        hold=True: 自然播完 (檔尾, 非循環) 時用 —— 保持最後一幀亮著, 等 master
        的延遲停止指令 (0x3002) 才熄燈。hold=False (預設): 明確停止/錯誤 ——
        is_ready=False 讓 RenderTask 立即熄燈。
        """
        self._release_src()
        self.state = _IDLE
        bus.shared.update({
            "stream_active": False,
            "is_streaming": False,
            "is_ready": True if hold else False,
            "is_paused": False,
        })
        stream_actions._STREAM_STATE["streaming"] = False
        stream_actions._STREAM_STATE["mode"] = 0

    # ── 主迴圈 ──────────────
    def loop(self):
        if not self.running or self._disabled:
            return

        self._consume_cmds()

        # 外部停止：pixel 的 MODE_SET/MODE_STOP 把 stream_active 設 False
        if bus.shared.get("stream_active") is False and self.state != _IDLE:
            self._reset()
            return

        if self.state == _LOADING:
            self._do_load()
        elif self.state == _SEEKING:
            self._do_seek()
        elif self.state == _PLAYING:
            self._do_play()
            self._send_status_push()   # 🔧 播放中主動回報進度 (間隔=config heartbeat_interval)

    def on_stop(self):
        super().on_stop()
        self._reset()
        get_log().info("StreamTask Stopped")
