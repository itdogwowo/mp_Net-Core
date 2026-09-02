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

        pixel 段：pixel_remote_set + pixel_remote_start_at（PixelTask 延遲起播）。
        audio 段：audio_cmd_program（每軌 start_ms += start_delay_ms，相對 PLAY
        起播時刻）+ audio_cmd_play —— 與燈效同一時刻出聲。
        缺段（純燈/純音）→ 對另一邊發停止，模式是原子表演單元。
        """
        m = self.resolve(mode_id)
        if m is None:
            get_log().warn("[GMode] mode {} 不存在 — 忽略".format(mode_id))
            return False
        self._cur_id = int(mode_id)
        self._started_at = time.ticks_ms()
        delay = max(0, int(start_delay_ms or 0))

        if m.get("entries"):
            bus.shared["pixel_remote_set"] = int(mode_id)
            bus.shared["pixel_remote_start_at"] = time.ticks_ms() + delay
        else:
            bus.shared["pixel_remote_stop"] = 1    # 純音效 → 熄燈

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
            bus.shared["audio_cmd_stop"] = True    # 純燈效 → 停音

        bus.shared["gmode"] = self.state()
        get_log().info("[GMode] ▶ mode {} ({}) delay={}ms".format(
            mode_id, m.get("name", ""), delay))
        return True

    def stop_mode(self, action=0):
        """停模式：兩邊都停（action 語意同 0x3106：0=暫停 1=全關）。"""
        bus.shared["pixel_remote_stop"] = 1
        bus.shared["audio_cmd_stop"] = True
        self._cur_id = 0
        self._started_at = 0
        bus.shared["gmode"] = self.state()
        get_log().info("[GMode] ■ stop action={}".format(action))
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
