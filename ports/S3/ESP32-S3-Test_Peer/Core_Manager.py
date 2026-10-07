# ports/S3/ESP32-S3-Test_Peer/Core_Manager.py
# ═══════════════════════════════════════════════════════════════════════
# 測試對端（Test Peer）— 純協定對端，零硬體依賴
# ═══════════════════════════════════════════════════════════════════════
#
# 角色：被「遙控器」（S3 控制面板）發現與查詢的假執行端。
#   ① 只回應協定指令，不驅動任何硬體（所有 driver enable=0）
#   ② 模式池是**寫死的假資料** → 不需要 LED / SD / /pixel/modes
#   ③ 開機（與週期性）廣播 0x1002 SLAVE_ANNOUNCE
#
# ★★ 為什麼需要「主動公告」：
#   ESP-NOW 的位址是 **MAC（6 bytes）——無法枚舉**。
#   遙控器不知道對端的 MAC，就送不出 unicast，也就不可能「定向查詢」。
#   而 UART/WS 那種「逐 address 掃描」（0x100D 掃 cid 0x0000~0xFFFE）
#   在 ESP-NOW 上**做不到**（MAC 不是可掃的數值空間）。
#   → 所以發現只能靠**對端主動送東西**：射頻層收到時才知道來源 MAC。
#   → 這是 PeerRegistry 被動學習（learn_from_frame）的唯一入口。
#
# 完整鏈（遙控器 ↔ 本對端）：
#   ① 本機開機 → 廣播 SLAVE_ANNOUNCE(0x1002)
#   ② 遙控器被動學到本機 MAC → bus.shared["peers"] 出現本機
#   ③ 遙控器定向 0x100D IDENTIFY_REQ(reply_cid=它的 cid)   [用剛學到的 MAC]
#   ④ 本機 on_identify_req 回 0x100E {cid, slave_id, ip}
#        ★ reply_cid 只決定**那一封回信**寄哪，不改 master_cid（2026-10 修正）
#   ⑤ 遙控器 SET_MASTER 確認方向 → 定向 0x3101/0x3107 查模式
#        ★ 方向（回覆預設定址）只有這一步會改 —— 掃描不會順便認主人
#
# 預期 log（在 REPL 監看）：
#   [Peer] SLAVE_ANNOUNCE 廣播 (3 modes)
#   🔹 [now] IDENTIFY_REQ (0x100D)         ← 遙控器來敲門了
#   🔹 [now] MODE_LIST_QUERY (0x3101)      ← 查清單
#   🔹 [now] MODE_DETAIL_QUERY (0x3107)    ← 逐一查細節
#
# 上傳：
#   1) slave/ 全量（基礎）
#   2) 本 port 的 config.json + Core_Manager.py（delta 覆蓋）
#   3) RESET
# ═══════════════════════════════════════════════════════════════════════

import time
import _thread
import ubinascii

import machine

from app import App
from lib.sys.sys_bus import bus
from lib.sys.task_manager import TaskManager
from lib.sys.task import Task
from lib.sys.log_service import get_log

from tasks.network import NetworkTask
from tasks.now_task import NowTask
from tasks.bus_decode import BusDecodeTask
from tasks.log_task import LogTask

ANNOUNCE_CMD = 0x1002
# ══════════════════════════════════════════════════════════════════
#  公告週期（使用者 2026-10 定案：**關掉週期，改成手動**）
# ══════════════════════════════════════════════════════════════════
#   0 = **只在開機廣播一次**（自我介紹），之後完全不主動發訊。
#       需要知道誰在線時，由遙控器**手動按「掃描」**（廣播 0x100D）。
#
#  為什麼關掉週期：
#    · 10 秒一次 = 射頻上永遠有訊號。使用者要的是「我有需要才掃」。
#    · 週期公告唯一的好處是「讓遙控器不用掃就看得到我」，但代價是持續佔用
#      空中時間；而 0x100D 已經能做到同一件事，而且是**由問的人發起**。
#    · 開機那次要留著 —— ESP-NOW 的 MAC 無法枚舉，「被發現」只能靠對方
#      主動送上門（或對方廣播掃描）。
#
#  ⚠️ 舊版的判斷式有 bug：`ticks_diff(now, last) < ANNOUNCE_EVERY_MS`
#     在 ANNOUNCE_EVERY_MS = 0 時恆為 False → **每一圈都廣播**（比 10 秒更糟）。
#     所以「0 = 只廣播一次」的註解和程式碼是相反的。下面 loop() 已修正。
ANNOUNCE_EVERY_MS = 0

# ── 假模式池 ──────────────────────────────────────────────────────────
#   16-bit id = (mode_type << 8) | mode_id —— 與 modes/*.json 的 id 同慣例。
#   故意混三種：LED 組(0x00xx) / SERVO 組(0x02xx)，方便測 mode_type 過濾。
FAKE_MODES = {
    0x0001: {"id": 0x0001, "name": "測試A", "index": 1},
    0x0002: {"id": 0x0002, "name": "測試B", "index": 2},
    0x0200: {"id": 0x0200, "name": "可動", "index": 3},
}


class AnnounceTask(Task):
    """開機（與週期性）廣播 0x1002 SLAVE_ANNOUNCE。

    讓遙控器能**被動學到本機 MAC** —— 這是 ESP-NOW 上唯一的「被發現」途徑。
    廣播是允許的：這不是「查詢等回覆」，是**單向公告**。
    """

    log_schema = []

    def __init__(self, name, ctx):
        super().__init__(name, ctx)
        self.app = ctx["app"]
        self._last = 0
        self._ready = False

    def on_start(self):
        super().on_start()
        self._last = 0
        self._ready = False
        if self.app.store.get(ANNOUNCE_CMD) is None:
            get_log().warn("[Peer] schema 0x{:04X} 不存在 → 公告停用".format(ANNOUNCE_CMD))
            self.running = False

    def loop(self):
        if not self.running:
            return
        now_bus = bus.get_service("NowBus")
        if now_bus is None:
            return                      # ESP-NOW 還沒起來，下一圈再試
        now = time.ticks_ms()
        if ANNOUNCE_EVERY_MS > 0:
            # 週期模式：距上次不足 ANNONCE_EVERY_MS 就跳過
            if self._last and time.ticks_diff(now, self._last) < ANNOUNCE_EVERY_MS:
                return
        elif self._last:
            # ★ 0 = **只廣播一次**。舊版沒處理這一支，`diff < 0` 恆為 False
            #   → 每一圈都發，射頻上比 10 秒一次還吵。
            return
        self._last = now
        try:
            # ★ 走統一指令層產生訊框（不手寫 struct）——
            #   disp.make_cmd 回傳的是指向共享 buffer 的 memoryview，
            #   所以「產生 → 送出」必須在同一行內完成，不可存起來。
            frame = self.app.disp.make_cmd(ANNOUNCE_CMD, {
                "slave_id": bus.slave_id,
                "pixel_count": len(FAKE_MODES),
                "hw_version": "test-peer",
            })
            if frame is None:
                return
            ok = now_bus.broadcast(frame)
            get_log().immediate("[Peer] SLAVE_ANNOUNCE 廣播 ({} modes) ret={}".format(
                len(FAKE_MODES), ok))
            if not self._ready:
                self._ready = True
                get_log().info("[Peer] 公告完成（%s）；遙控器按「掃描」可隨時再找到本機"
                               % ("每 %dms 一次" % ANNOUNCE_EVERY_MS
                                  if ANNOUNCE_EVERY_MS > 0 else "只此一次，之後不主動發訊"))
        except Exception as e:
            get_log().error("[Peer] 公告失敗: {}".format(e))


def launcher():
    log = get_log()
    log.info("📂 [CoreManager] TaskManager Mode — S3 Test Peer（純協定對端）")

    st_pixel = bus.get_service("st_pixel")      # 測 Test Peer 無燈 → None

    bus.slave_id = ubinascii.hexlify(machine.unique_id()).decode().upper()
    bus.shared["engine_run"] = True
    bus.shared["spi_busy"] = False

    app = App()

    # ── 假模式池：在 App 之後設（gmode 由 App 建立，會讀這裡）──────────
    bus.shared["pixel_maps"] = FAKE_MODES
    log.info("🧪 [CoreManager] 假模式池: {} 個 {}".format(
        len(FAKE_MODES), [m["name"] for m in FAKE_MODES.values()]))

    ctx = {"app": app, "st_pixel": st_pixel, "bus": bus}
    tm = TaskManager(ctx)

    bus.register_service("log", get_log())

    sys_cfg = bus.shared.get("System", {})
    bus.shared["log_print"] = True
    bus.shared["log_print_interval_ms"] = int(sys_cfg.get("log_interval_ms") or 1000)
    bus.shared["log_print_levels"] = ["info", "warn", "error", "immediate"]
    bus.shared["log_subscribe"] = []

    # ═══════════════════════════════════════════════════════════════════
    # 最小任務集 —— 只留「收得到、解得出、回得去」需要的
    #   刻意不註冊：pixel / render / dj / audio_player / stream（無硬體）
    #               lvgl（無 TFT）web_ui / cpanel / pixel_cpanel / schedule
    # ═══════════════════════════════════════════════════════════════════
    tm.register_task("network", NetworkTask, default_affinity=(1, 0), layer=0)
    tm.register_task("now", NowTask, default_affinity=(1, 0), layer=0)
    tm.register_task("log", LogTask, default_affinity=(1, 0), layer=0)

    # 公告任務（layer 1 → 等 NowBus 上線後才開始送）
    tm.register_task("announce", AnnounceTask, default_affinity=(1, 0), layer=1)

    # 解碼鏈（layer 2 → 保證 NetworkTask/NowTask 已註冊完通道）
    tm.register_task("bus_decode", BusDecodeTask, default_affinity=(1, 0), layer=2)

    tm.finalize()

    try:
        log.info("✨ Starting Core 1 Runner...")
        _thread.stack_size(16 * 1024)
        _thread.start_new_thread(tm.runner_loop, (1,))
        log.info("✨ NetBus System Online (TEST PEER): {}".format(bus.slave_id))
        log.info("✨ Starting Core 0 Runner...")
        tm.runner_loop(0)
    except KeyboardInterrupt:
        print("[CoreManager]👋 User stop requested.")
    except Exception as e:
        print("[CoreManager]❌ System Error: {}".format(e))
    finally:
        bus.shared["engine_run"] = False
        print("[CoreManager]🛑 All cores stopping...")
        time.sleep_ms(500)
        print("[CoreManager]🏁 Clean Exit.")
