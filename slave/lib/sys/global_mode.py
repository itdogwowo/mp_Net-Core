# lib/sys/global_mode.py
# GlobalMode — 全局模式貫通層（gmode）
#
# 單一事實來源：所有「模式」入口（0x3105 MODE_SET、0x3106 MODE_STOP、UART 面板
# 切換）先收斂到這裡，解析模式後同步扇出給 pixel（PixelTask）與 audio（DjTask），
# 兩邊用同一個 start_delay_ms 起播。
#
# 模式池 = pixel_maps（PixelTask 載入 /pixel/modes/*.json）+ /audio/modes/*.json
# （純音效，mode_type=3，gmode 惰性載入合併）。模式型態：
#   純燈效  map+entries, 無 audio      → 只扇出 pixel
#   純音效  audio, 無 entries          → 只扇出 audio
#   燈+音   entries + audio            → 兩邊同步扇出
#
# 狀態: bus.shared["gmode"] = {"mode_id": int, "started_at": ticks_ms}
# 共用目標狀態（各模組消費同一把 key，名稱不綁 pixel）:
#   bus.shared["mode_id"] / ["mode_seq"] / ["mode_start_at"]
# 服務: bus.register_service("gmode", GlobalMode())（app.py 建立）
import json
import os
import time
from lib.sys.sys_bus import bus
from lib.sys.log_service import get_log

_AUDIO_MODES_DIR = "/audio/modes"


def _list_json(d):
    """目錄下 *.json 檔的完整路徑清單（目錄不存在 → []）。"""
    try:
        return [d.rstrip("/") + "/" + f for f in os.listdir(d) if f.endswith(".json")]
    except OSError:
        return []


class GlobalMode:
    """模式池 + 解析 + 扇出。無執行緒、無定時器——由命令層直接呼叫。"""

    def __init__(self):
        self._audio_modes = None     # 惰性載入（首次 resolve 才掃目錄）
        self._cur_id = 0
        self._started_at = 0
        self._seq = 0                # 每次 MODE_SET/MODE_STOP +1（消費端靠它偵測「新指令」）

    # ── 模式池 ──────────────────────────────
    def _load_audio_modes(self):
        if self._audio_modes is not None:
            return self._audio_modes
        modes = {}
        for fn in _list_json(_AUDIO_MODES_DIR):
            try:
                with open(fn) as f:
                    d = json.load(f)
            except Exception:
                continue
            try:
                mid = int(d.get("id", 0))
            except (TypeError, ValueError):
                continue
            if mid == 0 or mid in modes:
                get_log().warn("[GMode] audio mode id 重複/無效 {} — 跳過".format(mid))
                continue
            modes[mid] = {
                "id": mid,
                "name": d.get("name", ""),
                "index": d.get("index", mid),
                "audio": d.get("audio"),
                "entries": [],          # 純音效：無燈效 entries
                "source": "audio",
            }
        self._audio_modes = modes
        get_log().info("[GMode] audio modes: {} 個".format(len(modes)))
        return modes

    def mode_pool(self):
        """合併池：{id: mode dict}。pixel 池（PixelTask 寫入）優先。"""
        pool = {}
        for mid, m in (bus.shared.get("pixel_maps") or {}).items():
            try:
                key = int(mid)
            except (TypeError, ValueError):
                continue
            pool[key] = dict(m)
            pool[key]["source"] = "pixel"
        pool.update(self._load_audio_modes())
        return pool

    def resolve(self, mode_id):
        try:
            return self.mode_pool().get(int(mode_id))
        except (TypeError, ValueError):
            return None

    def state(self):
        return {"mode_id": self._cur_id, "started_at": self._started_at}

    # ── 扇出 ────────────────────────────────
    def set_mode(self, mode_id, start_delay_ms=0):
        """解析並起播模式。回 True=已扇出；False=模式不存在。

        共用狀態（bus.shared，全系統消費方共用同一把 key，與 pixel 名稱無關）：
          mode_id       : 16-bit 組合 id（(mode_type<<8)|mode_id）
          mode_seq      : 每次 set/stop +1 —— 消費端比對「有無新指令」
          mode_start_at : 起播時間點（ticks_ms；延遲 0 = 現在）
        pixel 消費：PixelTask 讀 mode_id/seq/start_at，有 entries 就播、無 entries
        （純音效）就熄燈停。
        audio 消費：audio_cmd_program/play（DjTask/MP3，與燈效同一時刻）。
        """
        m = self.resolve(mode_id)
        if m is None:
            get_log().warn("[GMode] mode {} 不存在 — 忽略".format(mode_id))
            return False
        self._cur_id = int(mode_id)
        self._started_at = time.ticks_ms()
        delay = max(0, int(start_delay_ms or 0))
        self._seq += 1
        bus.shared["mode_id"] = int(mode_id)
        bus.shared["mode_seq"] = self._seq
        bus.shared["mode_start_at"] = self._started_at + delay

        audio = m.get("audio")
        if audio and audio.get("tracks"):
            shifted = []
            for t in audio["tracks"]:
                tt = dict(t)
                tt["start_ms"] = max(0, int(tt.get("start_ms", 0) or 0)) + delay
                shifted.append(tt)
            bus.shared["audio_cmd_program"] = {"json": json.dumps({
                "tracks": shifted,
                "limit": audio.get("limit", 80),
            })}
            bus.shared["audio_cmd_play"] = {"start_ms": 0}
        else:
            bus.shared["audio_cmd_stop"] = True    # 無音效段 → 停音

        bus.shared["gmode"] = self.state()
        get_log().info("[GMode] ▶ mode {} ({}) delay={}ms seq={}".format(
            mode_id, m.get("name", ""), delay, self._seq))
        return True

    def stop_mode(self, action=0):
        """停模式：共用狀態清空（mode_id=0），audio 也停（action 語意同 0x3106）。"""
        self._seq += 1
        bus.shared["mode_id"] = 0
        bus.shared["mode_seq"] = self._seq
        bus.shared["mode_start_at"] = 0
        bus.shared["audio_cmd_stop"] = True
        self._cur_id = 0
        self._started_at = 0
        bus.shared["gmode"] = self.state()
        get_log().info("[GMode] ■ stop action={} seq={}".format(action, self._seq))
        return True

    @staticmethod
    def filter_ids(mode_pool, mode_type):
        """MODE_LIST_QUERY 的組別過濾：0=全部、1=LED、2=SERVO、3=AUDIO（高 byte）。"""
        ids = []
        for i in sorted(mode_pool.keys()):
            i = int(i)
            if mode_type in (1, 2, 3) and (i >> 8) != mode_type:
                continue
            ids.append(i)
        return ids
