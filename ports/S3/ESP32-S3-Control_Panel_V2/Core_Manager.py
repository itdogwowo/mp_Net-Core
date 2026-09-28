# Core_Manager.py — S3 控制面板裝置（Control Panel）
#
# TaskManager 模式 — 取代舊的 main.py launcher()
# 結構對照 slave/Core_Manager.py，但**任務集按「面板角色」重配**（見下方三區註解）。
#
# ═══════════════════════════════════════════════════════════════════════
# 這台裝置是什麼（硬體事實 → 決定跑哪些任務）
# ═══════════════════════════════════════════════════════════════════════
#   有：ST7789 TFT(SPI, 240x320, 橫屏) + 旋轉編碼器(A/B=GPIO18/8, 按壓=17)
#       + 按鍵(GPIO42) + microSD(slot0/4bit) + UART1(39/41) + ESP-NOW
#   無：本地燈效 / 電機 / I2S 音訊（config 的 WS2812 / APA102 / PCA9685 /
#       uartMotor / I2S 全 enable=0 → boot 不會建出 st_pixel）
#
#   角色的職責鏈（面板只發指令，不自己執行）：
#     使用者操作（encoder/按鍵/LVGL 頁面）
#       → LVGL 頁面只寫狀態（bus.shared["_display_cmd"] / ["_pixel_cmd"]）
#       → ControlPanelTask  消費 _display_cmd → 廣播 0x1501 WTT_CTL
#         PixelControlPanelTask 消費 _pixel_cmd → 廣播 0x3105 MODE_SET
#       → ESP-NOW（NowBus）→ 執行裝置自己去解碼/執行
#     回程：執行裝置 0x1502 WTT_STATUS → 本板 dispatch（waiting_to_trash_actions
#       .on_status）→ 寫 _display_* Global → LVGL 頁面顯示「已確認 / 倒數」
#
#   所以：pixel / render / stream / dj / audio_player **不在本樹註冊**。
#   它們在面板上本來就會因 st_pixel=None 自行停用（不會壞），但那是白吃開機
#   時間與 log 噪音；面板要的是「UI + 輸入 + 轉發」這條鏈最短最順。
#
# ═══════════════════════════════════════════════════════════════════════
# 兩個踩過會痛的硬體約束（動 config 前先讀）
# ═══════════════════════════════════════════════════════════════════════
#   1. encoder 的 A/B **只能**寫在 config 的 ENC 段，不要放進 PIN 段。
#      ENC 段 → enc_drv 建 machine.Encoder + 註冊 enc_list；
#      HwSampleTask 採樣 enc_list → _hw_inputs，LvglTask/board 消費。
#      PIN 段若再加 encA/encB，boot.py Phase1 會報 GPIO 衝突直接 SystemExit。
#      （slave/tasks/control_panel.py 的 _find_pin_obj("encA") 是舊路徑，
#        在本 config 下恆為 None → 該 task 的 encoder 分支不會啟用，這是
#        預期的；不要去 PIN 段補 label 來「修」它。）
#   2. LVGL 是 core0-only（Task 宣告 hw=("lcd",)，TaskManager 會擋 core1）。
#      ui/lvgl/lvgl_init.py 自己送 MADCTL(0x60) 轉橫屏，所以 config TFT.rotation
#      必須維持 0，改 1 會 double-rotate。
#
# ═══════════════════════════════════════════════════════════════════════
# 「哪些線進解碼鏈」＝ Router 區塊（本 port 不設 CircuitDecode）
# ═══════════════════════════════════════════════════════════════════════
#   舊的 CircuitDecode（enable + list[].GPIO.uart 索引）在本 port 改用 Router
#   表達：`{"in": X, "out": []}` = V_DROP = 明確不執行（signal_router.py
#   `_verdict_of` 回 V_DROP）。Router.enable=1 下「沒寫 route 的線路」也是
#   V_DROP，但**不能靠刪掉來表達 disable** —— `_autofill()` 會補
#   `{in: X, out: ["self"]}` 並寫回 config.json，所以 disable 必須明寫 []。
#
#   代價（相對 CircuitDecode.enable=0，同步記在 README）：
#     CircuitTask 是 `self._buses = all_buses`（circuit.py:74）+ 每輪全 poll
#     （circuit.py:200）—— 不管 Router 怎麼判，**UART 實體都會被輪詢、進
#     rx_hub，然後才被丟掉**。要真的零成本，正解是把 UART.enable 改 0
#     （那條線不是面板的），但那動到 config 硬體段，待確認後再改。
#     另外每個 `out: []` 都會讓 Router 開機時警告一次「'out' 是空的
#     → 這條 route 沒有作用」—— 那是正常運作，不是錯誤。
#
#   本 port 的解碼介面：now（ESP-NOW，面板的生命線）＝ 本地執行；
#   net / udp（master 的 WS / 發現；wifi+lan 都 0 時不存在）與 uart0 = 關。
#
# ⚠️ 已知未接完的線（不是本檔的問題，改 UI 時一起收）：
#   ui/lvgl/page/pixel_controller.py 的 _set_mode() / _apply_movable() 仍直接抓
#   底層 espnow 物件 esp.send()（見 doc/03_notes/16_pixel_panel_temp_hacks.md
#   §2.1）。因此 PixelControlPanelTask 目前**只實際收到 brightness**
#   （_adj_bright 寫 _pixel_cmd），mode 路徑（_broadcast_mode_set）還沒被走。
#   正規做法是 UI 只寫 _pixel_cmd = {"mode": id} 由 task 轉發。

import machine, time, _thread, ubinascii
from app import App
from lib.sys.sys_bus import bus
from lib.sys.buffer_hub import AtomicStreamHub
from lib.sys.task_manager import TaskManager
from lib.sys.log_service import get_log

from tasks.network import NetworkTask
from tasks.circuit import CircuitTask
from tasks.bus_decode import BusDecodeTask
from tasks.now_task import NowTask
from tasks.fs_scan_task import FsScanTask
from tasks.log_task import LogTask
from tasks.web_ui import WebUITask
from tasks.control_panel import ControlPanelTask
from tasks.pixel_control_panel import PixelControlPanelTask


def launcher():
    log = get_log()
    log.info("📂 [CoreManager] TaskManager Mode — S3 Control Panel")

    st_pixel = bus.get_service("st_pixel")

    bus.slave_id = ubinascii.hexlify(machine.unique_id()).decode().upper()
    bus.shared["engine_run"] = True
    bus.shared["spi_busy"] = False

    # ── pixel_stream hub：只有真的接出 pixel 硬體才建（本板預設沒有）──
    #   面板沒有本地燈效時 st_pixel=None → 不建 hub，也不註冊 pixel/render。
    #   深度由 config Buffer.pixel_stream_slots 參數化（預設 10，即 10 幀緩衝）。
    if st_pixel:
        buf_cfg = bus.shared.get("Buffer") or {}
        slots = int(buf_cfg.get("pixel_stream_slots", 10) or 10)
        if slots <= 0:
            slots = 1
        hub = AtomicStreamHub(st_pixel.total_bytes, num_buffers=slots)
        bus.register_service("pixel_stream", hub)
        log.info("🎞 [CoreManager] pixel_stream hub: {} slots × {} B".format(
            slots, st_pixel.total_bytes))
    else:
        log.info("⏭ [CoreManager] no st_pixel — 面板角色，不建 pixel_stream hub")

    app = App()

    ctx = {
        "app": app,
        "st_pixel": st_pixel,
        "bus": bus,
    }

    tm = TaskManager(ctx)

    bus.register_service("log", get_log())

    sys_cfg = bus.shared.get("System", {})
    interval = sys_cfg.get("log_interval_ms")
    if interval is None:
        log_cfg = sys_cfg.get("Log")
        if log_cfg is None:
            log_cfg = bus.shared.get("Log", {})
        interval = log_cfg.get("print_interval_ms", 1000)
    bus.shared["log_print"] = True
    bus.shared["log_print_interval_ms"] = int(interval or 1000)
    bus.shared["log_print_levels"] = ["info", "warn", "error", "immediate"]
    bus.shared["log_subscribe"] = []

    # ═══════════════════════════════════════════════════════════════════
    # ▍第一區：系統核心任務（System Core）—— 系統基礎設施，永遠常駐
    #   核心分工（面板實配）:
    #     core0(主線程) = UI + 通訊:log / network / circuit / bus_decode /
    #       now / web_ui / lvgl / cpanel / pixel_cpanel。通訊任務單一呼叫鏈
    #       淺(<8KB 探針實測)，與 LVGL 共用主線程 16KB stack 沒有壓力。
    #     core1(_thread) = 背景重活:fs_scan / hw_sample（輸入採樣）。
    #
    #   ⚠️ now 與 network 都會建 NowBus，但互相 reuse（先查 bus service），
    #      不會二次 espnow.active(True) 撞 ESP_ERR_ESPNOW_EXIST —— 兩個都要留，
    #      誰先跑完不確定，缺一個在時序不利時就沒有 ESP-NOW。
    #   ⚠️ log 放最後：log_task_ready 一設，其它 task 的 print 就改走環形緩衝，
    #      這裡先起會把 boot 期的 driver 訊息吸走；放最後則 boot 全程直接 print。
    # ═══════════════════════════════════════════════════════════════════
    tm.register_task("network", NetworkTask, default_affinity=(1, 0), layer=0)
    tm.register_task("circuit", CircuitTask, default_affinity=(1, 0), layer=0)
    tm.register_task("now", NowTask, default_affinity=(1, 0), layer=0)
    tm.register_task("fs_scan", FsScanTask,  default_affinity=(0, 1), layer=0)
    from tasks.hw_sample_task import HwSampleTask
    tm.register_task("hw_sample", HwSampleTask, default_affinity=(0, 1), layer=0)
    tm.register_task("log", LogTask, default_affinity=(1, 0), layer=0)

    # ═══════════════════════════════════════════════════════════════════
    # ▍解碼鏈（Router 在這裡出生）—— **必須排在「產生通道」的任務之後**
    #   ★ `BusDecodeTask.on_start` 會建立 Router 並做第一次 `sync_ifaces()`
    #     —— 那一刻「現在有哪些通道」就定下來了。
    #     但 `NowBus` / `net_bus_ctrl` / `net_bus_discovery` 是
    #     **NetworkTask / NowTask 在自己的 on_start 裡才註冊**的（不是 boot.py），
    #     所以 bus_decode 若跟它們同層且排在前面，第一次 sync 時它們還不存在。
    #
    #     原本的補救是「loop() 每 100ms 重試 sync_ifaces()」——那是症狀的解法。
    #     **用分層直接解掉**：layer N+1 要等 layer N 全部 on_start 跑完才啟動
    #     （task_manager._check_boot_layer_done），放到 layer 1 就保證看得到。
    #
    #   ⚠️ 不會因為晚啟動而掉幀：各通道 rx_hub 是環形緩衝（u8_rx_slots=8 槽），
    #      bus_decode 起來前收到的幀暫存在那裡，起來後照樣被消費。
    # ═══════════════════════════════════════════════════════════════════
    tm.register_task("bus_decode", BusDecodeTask, default_affinity=(1, 0), layer=1)

    # ═══════════════════════════════════════════════════════════════════
    # ▍第二區：應用任務（Application）—— 面板的使用者面向功能
    #   web_ui   : 設定頁（WiFi 掃描/連線、指令 console）。綁 0.0.0.0:80，
    #              與網絡狀態無關，沒連線不消耗。
    #   lvgl     : 面板主 UI（core0-only）。沒有 LCD 時整段跳過（本板 TFT=1）。
    # ═══════════════════════════════════════════════════════════════════
    tm.register_task("web_ui", WebUITask, default_affinity=(0, 0), layer=2)

    if bus.has_lcd():
        from tasks.lvgl_task import LvglTask
        tm.register_task("lvgl", LvglTask, default_affinity=(1, 0), layer=2)
    else:
        log.info("⏭ [CoreManager] lvgl skipped — no LCD/TFT on bus")

    # ═══════════════════════════════════════════════════════════════════
    # ▍第三區：面板裝置任務（Panel）—— 本樹存在的理由
    #   cpanel       : 兩模式分層（由 bus.shared["_ui_active"] 切換）——
    #                  LVGL 在跑 → 不發 vbtn，改把 LVGL 直寫的 _display_cmd
    #                  轉成 0x1501 WTT_CTL 廣播；
    #                  LVGL 沒跑 → 回到按鈕模式，發 vbtn/enc_delta(0x1401)。
    #   pixel_cpanel : 消費 _pixel_cmd → 廣播 0x3105 MODE_SET / 0x3106 MODE_STOP，
    #                  並套用本板亮度（st_pixel 不在時亮度自動略過）。
    #   兩者都靠 ESP-NOW（NowBus）送出 → config Network.ESP_now.enable 必須為 1，
    #   且 channel 要與執行裝置一致（本板 6）。
    # ═══════════════════════════════════════════════════════════════════
    tm.register_task("cpanel", ControlPanelTask, default_affinity=(1, 0), layer=2)
    tm.register_task("pixel_cpanel", PixelControlPanelTask, default_affinity=(1, 0), layer=2)

    # ── 定時指令排程 Schedule：任務自行找 /schedule.json，無 config 開關 ──
    #   找到就依時間軸把 NC4 指令寫進 vBus（內部虛擬總線）→ 走解碼/執行鏈路；
    #   檔案不存在時第一次啟動自動產生空範本，之後 idle。
    #   （ScheduleTask.on_start 會先建 vBus —— 見該檔的說明）
    from tasks.schedule import ScheduleTask
    tm.register_task("schedule", ScheduleTask, default_affinity=(1, 0), layer=2)

    # ═══════════════════════════════════════════════════════════════════
    # ▍面板上「刻意不註冊」的任務（要復活就把註解打開）
    #   判準：需要本板沒有的硬體，或屬於「執行裝置」的角色。
    #
    #   pixel / render : 本地燈效的計算核(core1) + 播放核(core0)。面板沒有
    #                    WS2812/APA102/PCA9685/uartMotor → st_pixel=None →
    #                    自行停用。面板要看燈效是「廣播 MODE_SET 給執行裝置」，
    #                    不是自己播。
    #   stream         : 0x30xx 串流播放（讀 data.bin 逐幀推燈）。面板不當
    #                    播放端。
    #   dj / audio_player : 音訊合成 + I2S 播放（config I2S.enable=0，兩者
    #                    on_start 自行停用）。面板沒有音訊硬體。
    #   motor / action : 本板 UART1(39/41) 沒有掛電機，也不當動作執行端
    #                    （那是 ActionTask1 的角色，見 temp/motor 的 Core_Manager）。
    #
    #   from tasks.pixel_task import PixelTask
    #   from tasks.render import RenderTask
    #   tm.register_task("pixel", PixelTask, default_affinity=(1, 0), layer=1)
    #   tm.register_task("render", RenderTask, default_affinity=(0, 1), layer=1)
    #   from tasks.dj_task import DjTask
    #   from tasks.audio_player_task import AudioPlayerTask
    #   tm.register_task("dj", DjTask, default_affinity=(0, 1), layer=1)
    #   tm.register_task("audio_player", AudioPlayerTask, default_affinity=(1, 0), layer=1)
    #   from tasks.action_task_1 import ActionTask1
    #   from tasks.action_task import ActionTask
    #   tm.register_task("motor", ActionTask1, default_affinity=(1, 0), layer=0)
    #   tm.register_task("action", ActionTask, default_affinity=(1, 0), layer=0)
    #
    #   stream 若哪天要留（面板當 master 下發串流），記得它需要 pixel_stream
    #   hub；hub 只在 st_pixel 存在時建立（見上方），否則 StreamTask 會自行
    #   warn 並停用。
    # ═══════════════════════════════════════════════════════════════════

    tm.finalize()

    # ── 看門狗（config System.watchdog）—— lazy-arm：全部 on_start 運行完才建狗 ──
    #   看門狗只在「第一輪全部運行完（各 task 的 on_start 級聯完成）」之後才建立：
    #   TaskManager.runner_loop(0) 偵測 boot 完成（_boot_done 首次 True）那一圈才
    #   呼叫 init_watchdog()，之後每圈餵狗。因此不在此建狗。
    #   Ctrl+C → auto_disable_on_interrupt()：存 enable=0 + 立即重啟一次
    #   （硬食一次，可預測；不讓 WDT timeout 後偷襲打斷 REPL），之後測試模式無狗。
    #
    #   本板 watchdog.enable=0（開發/測試優先），要上現場再改 1。
    #   ⚠️ auto_rearm_ms 必須保持 0：它是「測試模式自動回安全態」——
    #      enable=0 且 auto_rearm_ms>0 時，init_watchdog 會啟動倒數，開機後
    #      連續 N ms 沒收到任何有效指令封包 → 自動存 enable=1 + machine.reset()。
    #      面板是**沒人對它發指令**的裝置（它只往外廣播），silence 是常態
    #      → 留 60000 會讓面板開機約 1 分鐘後自己重啟並把 WDT 打開。
    #      要開 WDT 保護就直接 enable=1，不要靠 auto_rearm 這條路。

    try:
        log.info("✨ Starting Core 1 Runner...")
        # stack 統一 16KB（與主線程 MICROPY_TASK_STACK_SIZE 同級）：
        #   thread_stack_probe 實測 core1 任務群(fs_scan/hw_sample 等)
        #   單一呼叫鏈 <8KB，16KB 有餘裕；ESP32 預設只有 5KB 必崩。
        #   將來 core1 若加 C 解碼等深鏈任務再調大。
        _thread.stack_size(16 * 1024)
        _thread.start_new_thread(tm.runner_loop, (1,))

        log.info("✨ NetBus System Online: {}".format(bus.slave_id))
        log.info("✨ Starting Core 0 Runner...")
        tm.runner_loop(0)

    except KeyboardInterrupt:
        print("[CoreManager]👋 User stop requested.")
        # 使用者強制暫停 → WDT 自動關閉（存 config，下次開機生效）。
        # 本次 session：engine_run=False → 不再餵狗，但 WDT 已由 runner_loop
        # 主線程持有，且 enable=0 時根本沒建狗 → REPL 測試不被鎖。
        from lib.sys.watchdog import auto_disable_on_interrupt
        auto_disable_on_interrupt()
    except Exception as e:
        print("[CoreManager]❌ System Error: {}".format(e))
    finally:
        bus.shared["engine_run"] = False
        print("[CoreManager]🛑 All cores stopping...")
        time.sleep_ms(500)
        if st_pixel:
            # 停止/熄燈：填中性值（燈=0 熄滅，motor=0x80 死區停），
            # 不能全清 0 —— UART-412 的 0 = 全速正轉！
            # 面板正常情況 st_pixel=None → 這段不執行（面板不擁有燈）。
            st_pixel.clear_all()
        print("[CoreManager]🏁 Clean Exit.")
