# tasks/dj_task.py
# DjTask — 音訊「合成端」（producer）：播放列表 + 讀檔 + 多軌混音
#
# 兩任務分工（對稱 pixel: PixelTask 合成 → RenderTask 播放）:
#   dj_task（本檔）        = 合成端（本檔 Core 0 / worker_engine 主迴圈）：
#     playlist 快取（無→分批掃/有→pass）、狀態機（IDLE/READY/PLAYING/PAUSED/SEEKING）、
#     每 voice 檔案 handle + stage 緩衝 → 讀 SD → viper 混音（1 軌直通 / N 軌相加+軟限幅）
#     → 混好的 PCM slot commit 進共享 "audio_stream" hub。
#   audio_player_task      = 播放端（Core 1 / worker_engine 獨立 thread）：
#     從 audio_stream hub 取 slot → audio_dac.write()（I2S DMA = 硬體節拍），
#     依 bus.shared 的 audio_streaming / audio_paused 旗標控制 XSMT 靜音。
#
# 控制旗標（bus.shared，dj 寫 / player 讀）:
#   audio_streaming = True 播放中；audio_paused = True 暫停（player 靜音）。
#
# 命令介面（bus.shared，由 action/audio_actions.py 寫入、本任務消費）:
#   audio_cmd_set     → 0x3201 準備單檔 {file_name, play_mode, volume}（=單軌節目）
#   audio_cmd_program → 0x3209 獨立多軌節目 {json}（tracks JSON bytes）
#   audio_cmd_play    → 0x3202 起播 {start_ms}
#   audio_cmd_stop    → 0x3203 停止
#   audio_cmd_pause   → 0x3204 暫停/恢復
#   audio_cmd_seek    → 0x3205 跳轉 {target_ms}
#   audio_cmd_volume  → 0x3206 主音量（0~100，混音時折進每軌增益）
#
# 檔案契約（doc/03_notes/13 §2）:
#   檔名自述 tag: name_<rate>_<bits>_<ch>.wav（不合理 → 當無 tag）
#   WAV header 是真相；tag 不符契約 → 不開檔直接標 compat=0（預測不兼容）
#   播放時再以 header 對契約（audio_dac.fmt）驗證，不符回 READY_ACK{ok=0}
import time
import os
import json
import struct
import sys as _sys
from lib.sys.task import Task
from lib.sys.sys_bus import bus
from lib.sys.proto import Proto
from lib.sys.schema_codec import SchemaCodec
from lib.sys.log_service import get_log

# ── 緩衝參數 ──
SLOT_BYTES = 8192     # 每槽位元組數 ≈ 46ms @44.1k/16bit/stereo
SLOTS = 8             # 每 voice 槽數 ≈ 370ms 前讀深度（64KB/voice）
MAX_VOICES = 4        # 多軌上限（4 × 64KB = 256KB）
DEFAULT_LIMIT = 80    # 軟限幅門檻（% of full scale）
_SCAN_BUDGET_MS = 50  # 掃描每圈時間預算（WDT 分批約束）

# ── 播放狀態 ──
_IDLE = 0
_READY = 2
_PLAYING = 3
_PAUSED = 4
_SEEKING = 5

_PLAYLIST_PATH = "/sd/audio/playlist.json"
_AUDIO_DIR = "/sd/audio"

IS_MICROPYTHON = (_sys.implementation.name == "micropython")


# ═══════════════════════════════════════════════════════════════════
# 混音器：s16 定點增益 + 分段線性軟限幅（防削波）
#   每軌 term = sample * gain(Q15) >> 15；term 上限 32767 → N 軌總和 ≤ N*32767
#   不溢位 int32。軟限幅：|s|>lim → 超出部分壓縮 1/4（交接文件 i2s_clip_test 手法）。
#   裝置 = viper 版本（1~4 軌各一）；PC 單元測試 = 同語意純 Python 版。
# ═══════════════════════════════════════════════════════════════════

if IS_MICROPYTHON:
    import micropython

    # P4 (RISC-V) 的 viper ptr16 讀取是「無號數」— 負樣本 (-1..-32768) 會被讀成
    # 65536-x 的大正數，混音衰減後全變成大的正輸出 → 負半週被整流 → 拆聲爆音。
    # 解法：ptr8 逐 byte 組回並還原 sign（實測修正正確、速度足夠 44.1k×2）。
    @micropython.viper
    def _mix1(out, a, ga: int, lim: int, n: int):
        po = ptr8(out)
        pa = ptr8(a)
        for i in range(n):
            v = int(pa[i * 2]) | (int(pa[i * 2 + 1]) << 8)
            if v > 32767:
                v -= 65536
            s = v * ga >> 15
            if s > lim:
                s = lim + ((s - lim) >> 2)
            elif s < -lim:
                s = (-lim) + ((s + lim) >> 2)
            po[i * 2] = s & 0xFF
            po[i * 2 + 1] = (s >> 8) & 0xFF

    @micropython.viper
    def _mix2(out, a, b, ga: int, gb: int, lim: int, n: int):
        po = ptr8(out)
        pa = ptr8(a)
        pb = ptr8(b)
        for i in range(n):
            va = int(pa[i * 2]) | (int(pa[i * 2 + 1]) << 8)
            if va > 32767:
                va -= 65536
            vb = int(pb[i * 2]) | (int(pb[i * 2 + 1]) << 8)
            if vb > 32767:
                vb -= 65536
            s = (va * ga >> 15) + (vb * gb >> 15)
            if s > lim:
                s = lim + ((s - lim) >> 2)
            elif s < -lim:
                s = (-lim) + ((s + lim) >> 2)
            po[i * 2] = s & 0xFF
            po[i * 2 + 1] = (s >> 8) & 0xFF

    @micropython.viper
    def _mix3(out, a, b, c, ga: int, gb: int, gc: int, lim: int, n: int):
        po = ptr8(out)
        pa = ptr8(a)
        pb = ptr8(b)
        pc = ptr8(c)
        for i in range(n):
            va = int(pa[i * 2]) | (int(pa[i * 2 + 1]) << 8)
            if va > 32767:
                va -= 65536
            vb = int(pb[i * 2]) | (int(pb[i * 2 + 1]) << 8)
            if vb > 32767:
                vb -= 65536
            vc = int(pc[i * 2]) | (int(pc[i * 2 + 1]) << 8)
            if vc > 32767:
                vc -= 65536
            s = (va * ga >> 15) + (vb * gb >> 15) + (vc * gc >> 15)
            if s > lim:
                s = lim + ((s - lim) >> 2)
            elif s < -lim:
                s = (-lim) + ((s + lim) >> 2)
            po[i * 2] = s & 0xFF
            po[i * 2 + 1] = (s >> 8) & 0xFF

    @micropython.viper
    def _mix4(out, a, b, c, d, ga: int, gb: int, gc: int, gd: int, lim: int, n: int):
        po = ptr8(out)
        pa = ptr8(a)
        pb = ptr8(b)
        pc = ptr8(c)
        pd = ptr8(d)
        for i in range(n):
            va = int(pa[i * 2]) | (int(pa[i * 2 + 1]) << 8)
            if va > 32767:
                va -= 65536
            vb = int(pb[i * 2]) | (int(pb[i * 2 + 1]) << 8)
            if vb > 32767:
                vb -= 65536
            vc = int(pc[i * 2]) | (int(pc[i * 2 + 1]) << 8)
            if vc > 32767:
                vc -= 65536
            vd = int(pd[i * 2]) | (int(pd[i * 2 + 1]) << 8)
            if vd > 32767:
                vd -= 65536
            s = (va * ga >> 15) + (vb * gb >> 15) + \
                (vc * gc >> 15) + (vd * gd >> 15)
            if s > lim:
                s = lim + ((s - lim) >> 2)
            elif s < -lim:
                s = (-lim) + ((s + lim) >> 2)
            po[i * 2] = s & 0xFF
            po[i * 2 + 1] = (s >> 8) & 0xFF

else:
    # CPython（PC 單元測試）：同語意純 Python 版
    def _mix1(out, a, ga, lim, n):
        for i in range(n):
            s = (struct.unpack_from("<h", a, i * 2)[0] * ga) >> 15
            if s > lim:
                s = lim + ((s - lim) >> 2)
            elif s < -lim:
                s = -lim + ((s + lim) >> 2)
            struct.pack_into("<h", out, i * 2, s)

    def _mix2(out, a, b, ga, gb, lim, n):
        for i in range(n):
            s = ((struct.unpack_from("<h", a, i * 2)[0] * ga) >> 15) + \
                ((struct.unpack_from("<h", b, i * 2)[0] * gb) >> 15)
            if s > lim:
                s = lim + ((s - lim) >> 2)
            elif s < -lim:
                s = -lim + ((s + lim) >> 2)
            struct.pack_into("<h", out, i * 2, s)

    def _mix3(out, a, b, c, ga, gb, gc, lim, n):
        for i in range(n):
            s = ((struct.unpack_from("<h", a, i * 2)[0] * ga) >> 15) + \
                ((struct.unpack_from("<h", b, i * 2)[0] * gb) >> 15) + \
                ((struct.unpack_from("<h", c, i * 2)[0] * gc) >> 15)
            if s > lim:
                s = lim + ((s - lim) >> 2)
            elif s < -lim:
                s = -lim + ((s + lim) >> 2)
            struct.pack_into("<h", out, i * 2, s)

    def _mix4(out, a, b, c, d, ga, gb, gc, gd, lim, n):
        for i in range(n):
            s = ((struct.unpack_from("<h", a, i * 2)[0] * ga) >> 15) + \
                ((struct.unpack_from("<h", b, i * 2)[0] * gb) >> 15) + \
                ((struct.unpack_from("<h", c, i * 2)[0] * gc) >> 15) + \
                ((struct.unpack_from("<h", d, i * 2)[0] * gd) >> 15)
            if s > lim:
                s = lim + ((s - lim) >> 2)
            elif s < -lim:
                s = -lim + ((s + lim) >> 2)
            struct.pack_into("<h", out, i * 2, s)


# ═══════════════════════════════════════════════════════════════════
# WAV header / 檔名自述 tag
# ═══════════════════════════════════════════════════════════════════

def parse_name_tag(name):
    """檔名自述契約: name_<rate>_<bits>_<ch>.wav → (rate, bits, ch)。

    三個尾段皆為數字且通過合理性驗證才算 tag（避免 track_1_2_3_4.wav 誤判）。
    """
    stem = name[:-4] if name.lower().endswith(".wav") else name
    parts = stem.split("_")
    if len(parts) < 4:
        return None
    a, b, c = parts[-3], parts[-2], parts[-1]
    if not (a.isdigit() and b.isdigit() and c.isdigit()):
        return None
    rate, bits, ch = int(a), int(b), int(c)
    if rate >= 8000 and bits in (8, 16, 24, 32) and ch in (1, 2):
        return (rate, bits, ch)
    return None


def parse_wav_file(path):
    """解析 WAV header → dict；不是 16-bit PCM 契約的 RIFF/WAVE → None。

    掃 RIFF/`fmt `/`data` chunk，容忍 LIST/INFO 多餘 chunk。
    回傳 data_off/data_size/rate/bits/ch/align/byte_rate/duration_ms。
    """
    try:
        f = open(path, "rb")
    except Exception:
        return None
    try:
        head = f.read(12)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            return None
        fmt = None
        data_off = None
        data_size = 0
        while True:
            ch = f.read(8)
            if len(ch) < 8:
                break
            cid = ch[:4]
            size = struct.unpack("<I", ch[4:8])[0]
            if cid == b"fmt ":
                fmt = f.read(size)
                if size & 1:
                    f.read(1)          # 奇數長度 pad byte
            elif cid == b"data":
                data_off = f.tell()
                data_size = size
                break
            else:
                f.seek(size + (size & 1), 1)
        if fmt is None or len(fmt) < 16 or data_off is None:
            return None
        audio_fmt, chn, rate, byte_rate, align, bits = struct.unpack("<HHIIHH", fmt[:16])
        if audio_fmt != 1 or byte_rate <= 0:
            return None    # 非 PCM / 參數無效
        return {
            "data_off": data_off, "data_size": data_size,
            "ch": chn, "rate": rate, "bits": bits,
            "align": align or 1, "byte_rate": byte_rate,
            "duration_ms": (data_size * 1000) // byte_rate,
        }
    finally:
        f.close()


# ═══════════════════════════════════════════════════════════════════
# WAV 目錄索引快取（playlist.json）
# ═══════════════════════════════════════════════════════════════════

class WavPlaylist:
    """playlist.json 快取：load（有→pass）/ begin_scan+scan_step（無→分批掃）/ save。

    只認 dj_task 自己改變（掃描）；與 0x20xx 檔案傳輸層零耦合。
    """

    def __init__(self):
        self.files = {}          # name -> entry {name,path,size,duration_ms,rate,bits,channels,compat}
        self.scanning = False
        self._names = []
        self._idx = 0
        self._acc = {}

    def load(self):
        try:
            with open(_PLAYLIST_PATH, "r") as f:
                d = json.load(f)
        except Exception:
            return False
        files = {}
        for e in d.get("files", []):
            n = e.get("name")
            if n:
                files[n] = e
        self.files = files
        return True

    def begin_scan(self):
        try:
            names = [n for n in os.listdir(_AUDIO_DIR) if n.lower().endswith(".wav")]
        except OSError:
            names = []
        self._names = names
        self._idx = 0
        self._acc = {}
        self.scanning = True

    def scan_step(self, budget_ms=_SCAN_BUDGET_MS, contract=None):
        """分批解析（每圈預算 budget_ms）。回 True=仍在掃；False=完成（快取已換新）。"""
        t0 = time.ticks_ms()
        while self._idx < len(self._names):
            name = self._names[self._idx]
            self._idx += 1
            entry = self._parse_one(name, contract)
            if entry is not None:
                self._acc[name] = entry
            if time.ticks_diff(time.ticks_ms(), t0) >= budget_ms:
                return True
        self.files = self._acc
        self.scanning = False
        return False

    def _parse_one(self, name, contract):
        path = _AUDIO_DIR + "/" + name
        tag = parse_name_tag(name)
        if tag is not None and contract is not None and tuple(tag) != tuple(contract):
            # 檔名自述不兼容 → 預測：不開檔，直接標 compat=0（省掃描時間）
            return {"name": name, "path": path, "size": 0, "duration_ms": 0,
                    "rate": tag[0], "bits": tag[1], "channels": tag[2], "compat": 0}
        hdr = parse_wav_file(path)
        if hdr is None:
            return None
        compat = 1
        if contract is not None:
            cr, cb, cc = contract
            compat = 1 if (hdr["rate"] == cr and hdr["bits"] == cb and hdr["ch"] == cc) else 0
        if tag is not None and (hdr["rate"], hdr["bits"], hdr["ch"]) != tuple(tag):
            get_log().warn("[Dj] {} 檔名 tag 與 header 不符 — 以 header 為準".format(name))
        return {"name": name, "path": path, "size": hdr["data_size"],
                "duration_ms": hdr["duration_ms"], "rate": hdr["rate"],
                "bits": hdr["bits"], "channels": hdr["ch"], "compat": compat}

    def save(self):
        d = {"version": 1, "scanned_at": time.ticks_ms(),
             "files": [e for e in self.files.values()]}
        tmp = _PLAYLIST_PATH + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(d, f)
            os.rename(tmp, _PLAYLIST_PATH)
            return True
        except Exception as e:
            get_log().error("[Dj] playlist.json 寫入失敗: {}".format(e))
            return False


# ═══════════════════════════════════════════════════════════════════
# DjTask
# ═══════════════════════════════════════════════════════════════════

class DjTask(Task):
    def __init__(self, name, ctx):
        super().__init__(name, ctx)
        self._dac_fmt = None     # 契約 (rate,bits,ch) —— 取自 audio_dac.fmt（只讀，不碰 DAC）
        self._hub = None         # audio_stream 輸出 hub（混好的 PCM slot，播放端消費）
        self._disabled = False
        self._playlist = WavPlaylist()
        self._rescan_pending = False
        self.state = _IDLE
        self._resume = _READY
        self._voices = []        # 每 voice: {fp,entry,hdr,loop,volume,start_ms,bytes_played,eof,started,stage}
        self._volume = 100       # 主音量 0~100（audio_cmd_volume）
        self._mg_q15 = 32768     # 主音量 Q15 增益
        self._limit_q15 = DEFAULT_LIMIT * 32768 // 100
        self._prog_start_ms = 0  # 節目起播 ticks（start_ms 相對它）
        self._final_done = False # 全部 voice eof 且最後一槽已 commit（等播放端清空）

    # ── 啟動 ──────────────────────────────
    def on_start(self):
        super().on_start()
        dac = bus.get_service("audio_dac")
        if dac is None:
            self._disabled = True
            get_log().warn("[Dj] 無 audio_dac（I2S/PCM5102 未啟用）— dj 停用")
            return
        self._dac_fmt = dac.fmt
        # 共享輸出 hub：合成端（本任務）寫 slot、播放端（audio_player_task）讀。
        from lib.sys.buffer_hub import AtomicStreamHub
        self._hub = AtomicStreamHub(SLOT_BYTES, SLOTS, try_dma=False)
        bus.register_service("audio_stream", self._hub)
        self._rescan_pending = False

        # playlist：沒有就掃、有就 pass（決策：掃描歸 dj_task，分批後台執行）
        # 快取註冊成 service：audio_actions 的 0x320A LIST_QUERY 直接讀它。
        bus.register_service("audio_playlist", self._playlist)
        if not self._playlist.load():
            self._playlist.begin_scan()
            get_log().info("[Dj] playlist.json 不存在 → 後台分批掃描 {}".format(_AUDIO_DIR))
        else:
            get_log().info("[Dj] playlist 載入 {} 檔（pass，不掃 SD）".format(len(self._playlist.files)))

        bus.register_provider("audio_active", lambda: self.state == _PLAYING)
        bus.register_provider("audio_pos_ms", self._pos_ms)
        bus.register_provider("audio_duration_ms", self._duration_ms)
        bus.register_provider("audio_volume", lambda: self._volume)
        get_log().info("🎧 [Dj] online（合成端）| contract fmt={} slot={}B x{} max_voices={} limit={}%".format(
            dac.fmt, SLOT_BYTES, SLOTS, MAX_VOICES, DEFAULT_LIMIT))

    @staticmethod
    def _gain_q15(vol):
        """0~100 音量 → Q15 增益 [0, 32768]。"""
        vol = max(0, min(100, int(vol)))
        return vol * 32768 // 100

    # ── 控制旗標（播放端 audio_player_task 讀）──────────
    def _flags(self, streaming, paused):
        bus.shared["audio_streaming"] = bool(streaming)
        bus.shared["audio_paused"] = bool(paused)

    # ── 狀態查詢（providers）──────────────
    def _pos_ms(self):
        best = 0
        for v in self._voices:
            ms = v["bytes_played"] * 1000 // v["hdr"]["byte_rate"]
            if ms > best:
                best = ms
        return best

    def _duration_ms(self):
        best = 0
        for v in self._voices:
            if v["hdr"]["duration_ms"] > best:
                best = v["hdr"]["duration_ms"]
        return best

    # ── READY_ACK（0x3207）──────────────
    def _send_ready(self, ok, duration_ms):
        app = self.ctx.get("app")
        if app is None:
            return
        ctrl = bus.get_service("net_bus_ctrl")
        if ctrl is None:
            return
        try:
            cmd_def = app.store.get(0x3207)
            if not cmd_def:
                return
            payload = SchemaCodec.encode(cmd_def, {"ok": ok, "duration_ms": duration_ms})
            ctrl.write(Proto.pack(0x3207, payload))
        except Exception as e:
            get_log().error("[Dj] READY_ACK 發送失敗: {}".format(e))

    # ── 命令消費 ──────────────────────────
    # 順序注意：set/program 必須在 play 之前消費 —— gmode.set_mode 會把
    # audio_cmd_program + audio_cmd_play 寫進同一批，play 先吃會因無 voice
    # 而丟失起播。
    def _consume_cmds(self):
        s = bus.shared

        if s.pop("audio_cmd_stop", None) is not None:
            self._teardown("[Dj] ■ stop")
            return

        seek = s.pop("audio_cmd_seek", None)
        if seek is not None:
            resume = _PLAYING if self.state in (_PLAYING, _PAUSED) else _READY
            self._begin_seek(int(seek.get("target_ms", 0) or 0), resume)
            return

        cmd_set = s.pop("audio_cmd_set", None)
        if cmd_set is not None:
            self._begin_load(cmd_set)
            return

        prog = s.pop("audio_cmd_program", None)
        if prog is not None:
            self._begin_program_cmd(prog)
            return

        play = s.pop("audio_cmd_play", None)
        if play is not None:
            start_ms = int(play.get("start_ms", 0) or 0)
            if start_ms > 0:
                self._begin_seek(start_ms, _PLAYING)
            else:
                self._start_playing()
            return

        pause = s.pop("audio_cmd_pause", None)
        if pause is not None:
            self._set_paused(bool(pause))
            return

        vol = s.pop("audio_cmd_volume", None)
        if vol is not None:
            self._volume = max(0, min(100, vol))
            self._mg_q15 = self._gain_q15(self._volume)

        rescan = s.pop("audio_cmd_rescan", None)
        if rescan is not None:
            if self.state == _IDLE:
                self._playlist.begin_scan()
                get_log().info("[Dj] RESCAN → 後台分批掃描 {}".format(_AUDIO_DIR))
            else:
                self._rescan_pending = True    # 播放中 → 回 IDLE 再掃
            return

        remove = s.pop("audio_cmd_remove", None)
        if remove is not None:
            self._do_remove(remove)
            return

    def _begin_program_cmd(self, prog):
        """0x3209 AUDIO_PROGRAM_SET：tracks JSON（可帶 limit）→ 多軌節目。"""
        try:
            data = prog.get("json", b"")
            if isinstance(data, (bytes, bytearray)):
                data = data.decode("utf-8")
            obj = json.loads(data)
        except Exception as e:
            get_log().error("[Dj] PROGRAM_SET JSON 解析失敗: {}".format(e))
            self._send_ready(0, 0)
            return
        if isinstance(obj, dict):
            tracks = obj.get("tracks", [])
            limit = obj.get("limit")
        else:
            tracks = obj
            limit = None
        self._begin_program(tracks, limit=limit)

    # ── 列表管理（M3）──────────────────────
    def _do_remove(self, cmd):
        name = cmd.get("name", "")
        delete_file = bool(cmd.get("delete_file", 0))
        if not name or name not in self._playlist.files:
            get_log().warn("[Dj] REMOVE 檔名不在索引: {!r}".format(name))
            self._send_list_ready(0, len(self._playlist.files))
            return
        if delete_file:
            path = self._playlist.files[name].get("path") or (_AUDIO_DIR + "/" + name)
            # 註: raw-mode SD（alloc.json）的檔不在 VFS，os.remove 會失敗 —
            # 該模式下的刪檔列為已知限制（0x20xx 走 fs 層另有 FILE_DELETE）。
            try:
                os.remove(path)
            except Exception as e:
                get_log().error("[Dj] 刪檔失敗 {}: {}".format(name, e))
                self._send_list_ready(0, len(self._playlist.files))
                return
        del self._playlist.files[name]
        self._playlist.save()
        get_log().info("[Dj] REMOVE {} delete_file={}".format(name, int(delete_file)))
        self._send_list_ready(1, len(self._playlist.files))

    def _send_list_ready(self, ok, count):
        app = self.ctx.get("app")
        if app is None:
            return
        ctrl = bus.get_service("net_bus_ctrl")
        if ctrl is None:
            return
        try:
            cmd_def = app.store.get(0x320E)
            if not cmd_def:
                return
            payload = SchemaCodec.encode(cmd_def, {"ok": ok, "count": min(255, count)})
            ctrl.write(Proto.pack(0x320E, payload))
        except Exception as e:
            get_log().error("[Dj] LIST_READY 發送失敗: {}".format(e))

    # ── 狀態轉換 ──────────────────────────
    def _begin_load(self, cmd):
        """0x3201 AUDIO_SET → 單軌節目（相容 M2 語意）。"""
        self._begin_program([{
            "file": cmd.get("file_name", ""),
            "loop": bool(cmd.get("play_mode", 0)),
            "volume": int(cmd.get("volume", 0) or 0),
            "start_ms": 0,
        }])

    def _begin_program(self, tracks, limit=None):
        """載入多軌節目：每軌開檔 + 驗證契約 + 各建一個 hub。"""
        self._teardown("[Dj] load 前清理", log=False)
        if not tracks:
            get_log().warn("[Dj] 空節目")
            self._send_ready(0, 0)
            return
        if len(tracks) > MAX_VOICES:
            get_log().warn("[Dj] 音軌 {} 超過上限 {} — 截斷".format(len(tracks), MAX_VOICES))
            tracks = tracks[:MAX_VOICES]
        if limit is not None:
            self._limit_q15 = self._gain_q15(limit)

        voices = []
        try:
            for tr in tracks:
                v = self._make_voice(tr)
                if v is None:
                    raise RuntimeError("track open failed: {!r}".format(tr.get("file")))
                voices.append(v)
        except Exception as e:
            get_log().error("[Dj] 節目載入失敗: {}".format(e))
            for v in voices:
                try:
                    v["fp"].close()
                except Exception:
                    pass
            self._send_ready(0, 0)
            return

        self._voices = voices
        self.state = _READY
        dur = self._duration_ms()
        get_log().info("[Dj] program {} 軌 dur={}ms limit={}%".format(
            len(voices), dur, self._limit_q15 * 100 // 32768))
        self._send_ready(1, dur)

    def _make_voice(self, tr):
        """單軌：playlist 解析 → header 驗證（真相）→ 開檔 + 建 hub。"""
        name = tr.get("file", "")
        entry = self._playlist.files.get(name)
        if entry is None:
            get_log().warn("[Dj] 檔名不在 playlist: {!r}（新檔請先 RESCAN）".format(name))
            return None
        if not entry.get("compat", 0):
            get_log().warn("[Dj] 檔案不兼容（compat=0，請重轉）: {!r}".format(name))
            return None
        path = entry.get("path") or (_AUDIO_DIR + "/" + name)
        hdr = parse_wav_file(path)
        if hdr is None:
            get_log().error("[Dj] WAV 解析失敗: {}".format(name))
            return None
        fmt = self._dac_fmt
        if hdr["rate"] != fmt[0] or hdr["bits"] != fmt[1] or hdr["ch"] != fmt[2]:
            get_log().warn("[Dj] 格式不符契約 fmt={}: {}".format(fmt, name))
            return None
        try:
            fp = open(path, "rb")
            fp.seek(hdr["data_off"])
        except Exception as e:
            get_log().error("[Dj] 開檔失敗 {}: {}".format(name, e))
            return None
        return {
            "fp": fp, "entry": entry, "hdr": hdr,
            "loop": bool(tr.get("loop", 0)),
            "volume": max(0, min(100, int(tr.get("volume", 0) or 0))),
            "start_ms": max(0, int(tr.get("start_ms", 0) or 0)),
            "bytes_played": 0, "eof": False, "started": False,
            "stage": bytearray(SLOT_BYTES),   # 該軌一格 PCM（混音輸入）
        }

    def _start_playing(self):
        if self.state in (_READY, _PAUSED):
            self.state = _PLAYING
            self._prog_start_ms = time.ticks_ms()   # start_ms 相對此刻
            self._flags(True, False)                # 播放端解除靜音並開始消費
            get_log().info("[Dj] ▶ play")

    def _set_paused(self, paused):
        if paused:
            if self.state == _PLAYING:
                self.state = _PAUSED
                self._hub.flush()                   # 合成端停產；殘餘 slot 由播放端靜音吞掉
                self._flags(False, True)
                get_log().info("[Dj] ⏸ paused")
        else:
            if self.state == _PAUSED:
                self.state = _PLAYING
                self._prog_start_ms = time.ticks_ms()
                self._flags(True, False)
                get_log().info("[Dj] ▶ resume")

    def _begin_seek(self, target_ms, resume):
        """跳轉：每軌相對自己的 start_ms 平移（未開始的軌維持排程）。"""
        if not self._voices:
            return
        now = time.ticks_ms()
        for v in self._voices:
            v_ms = max(0, target_ms - v["start_ms"])
            v_ms = min(v_ms, v["hdr"]["duration_ms"])
            off = v["hdr"]["data_off"] + v_ms * v["hdr"]["byte_rate"] // 1000
            off -= off % v["hdr"]["align"]    # 對齊幀邊界
            try:
                v["fp"].seek(off)
            except Exception as e:
                get_log().error("[Dj] seek 失敗: {}".format(e))
                return
            v["bytes_played"] = off - v["hdr"]["data_off"]
            v["eof"] = False
            if not v["started"] and time.ticks_diff(now, self._prog_start_ms) >= v["start_ms"]:
                v["started"] = True
        self._hub.flush()
        self._final_done = False
        self._resume = resume
        self.state = _SEEKING

    def _do_seek(self):
        """seek 後預產一格（補齊畫面/聲音即時性）。"""
        out = self._hub.get_write_view()
        if out is None:
            return
        nv = self._produce_slot(out)
        if nv > 0:
            self._hub.commit()
        self.state = self._resume
        if self.state == _PLAYING:
            self._flags(True, False)

    # ── 產出一格混好的 PCM：讀各 started voice 一格 → viper 混進 out ──
    def _produce_slot(self, out):
        """把一格（SLOT_BYTES）混進 out（hub write-view 或任一 buffer）。
        回傳參與混音的 voice 數（0 = 都還沒開始/全部已結束）。
        """
        active = [v for v in self._voices if v["started"]]
        if not active:
            return 0
        for v in active:
            if v["eof"]:
                v["stage"][:] = b"\x00" * SLOT_BYTES     # 已結束 → 純靜音
                continue
            st = v["stage"]
            got = v["fp"].readinto(st)
            if got <= 0:
                if v["loop"]:
                    v["fp"].seek(v["hdr"]["data_off"])
                    v["bytes_played"] = 0
                    got = v["fp"].readinto(st)
            if got <= 0:
                st[:] = b"\x00" * SLOT_BYTES
                v["eof"] = True
                continue
            if got < SLOT_BYTES:
                st[got:] = b"\x00" * (SLOT_BYTES - got)  # 檔尾補靜音
                v["eof"] = True
            v["bytes_played"] += got
        n = SLOT_BYTES // 2
        lim = self._limit_q15
        if len(active) == 1:
            v = active[0]
            _mix1(out, v["stage"], self._gain_q15(v["volume"]) * self._mg_q15 >> 15, lim, n)
        elif len(active) == 2:
            a, b = active
            _mix2(out, a["stage"], b["stage"],
                  self._gain_q15(a["volume"]) * self._mg_q15 >> 15,
                  self._gain_q15(b["volume"]) * self._mg_q15 >> 15, lim, n)
        elif len(active) == 3:
            a, b, c = active
            _mix3(out, a["stage"], b["stage"], c["stage"],
                  self._gain_q15(a["volume"]) * self._mg_q15 >> 15,
                  self._gain_q15(b["volume"]) * self._mg_q15 >> 15,
                  self._gain_q15(c["volume"]) * self._mg_q15 >> 15, lim, n)
        else:
            a, b, c, d = active[:4]
            _mix4(out, a["stage"], b["stage"], c["stage"], d["stage"],
                  self._gain_q15(a["volume"]) * self._mg_q15 >> 15,
                  self._gain_q15(b["volume"]) * self._mg_q15 >> 15,
                  self._gain_q15(c["volume"]) * self._mg_q15 >> 15,
                  self._gain_q15(d["volume"]) * self._mg_q15 >> 15, lim, n)
        return len(active)

    def _do_play(self):
        """合成主迴圈：起播計時 → 產一格 → commit 進 audio_stream（播放端消費）。"""
        now = time.ticks_ms()
        for v in self._voices:
            if not v["started"] and time.ticks_diff(now, self._prog_start_ms) >= v["start_ms"]:
                v["started"] = True

        if self._voices and all(v["started"] and v["eof"] for v in self._voices):
            # 全部已開始且已讀到檔尾：只等最後一槽被播放端清空
            if self._final_done and not self._hub.dirty:
                self._teardown("[Dj] ■ 播完（節目自然結束）")
            return

        out = self._hub.get_write_view()
        if out is None:
            return                                    # 播放端消化中 → 這圈不產
        nv = self._produce_slot(out)
        if nv > 0:
            self._hub.commit()
            self.success += 1
            if all(v["started"] and v["eof"] for v in self._voices):
                self._final_done = True

    def _teardown(self, msg, log=True):
        for v in self._voices:
            fp = v.get("fp")
            if fp is not None:
                try:
                    fp.close()
                except Exception:
                    pass
        self._voices = []
        self._final_done = False
        if self._hub is not None:
            self._hub.flush()
        self._flags(False, False)
        self.state = _IDLE
        if log:
            get_log().info(msg)

    # ── 主迴圈 ──────────────────────────────
    def loop(self):
        if not self.running or self._disabled:
            return

        self._consume_cmds()

        # 延後的 RESCAN（播放中收到的）：回 IDLE 且掃描未在跑 → 開始
        if self._rescan_pending and self.state == _IDLE and not self._playlist.scanning:
            self._rescan_pending = False
            self._playlist.begin_scan()
            get_log().info("[Dj] 延後 RESCAN → 後台分批掃描")

        # 掃描 phase（分批；只在 IDLE 進行，播放指令優先）
        if self._playlist.scanning and self.state == _IDLE:
            if not self._playlist.scan_step(_SCAN_BUDGET_MS, self._dac_fmt):
                ok = 1 if self._playlist.save() else 0
                if ok:
                    get_log().info("[Dj] 掃描完成 {} 檔 → playlist.json".format(
                        len(self._playlist.files)))
                self._send_list_ready(ok, len(self._playlist.files))
            return

        if self.state == _SEEKING:
            self._do_seek()
        elif self.state == _PLAYING:
            self._do_play()

    def on_stop(self):
        super().on_stop()
        self._teardown("[Dj] Stopped", log=False)
        get_log().info("DjTask Stopped")
