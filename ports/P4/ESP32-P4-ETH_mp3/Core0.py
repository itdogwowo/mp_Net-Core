# Core0.py
# Worker/Engine（極速）模式 — Core 0 控制核
#
# 核心理念：簡單、快速、專注完成任務（極速模式），不追求最大靈活性。
# 職責：統一指令線路（網絡 + 實體線）收發 + dispatch。
#   - NetworkTask  : WS / UDP / ESP-NOW 等網絡 bus
#   - CircuitTask  : UART 實體線 bus
#   - BusDecodeTask: poll 所有 bus → 解析封包 → app.disp 分發
#   - LogTask      : 日誌輸出
# 指令（play / pause / 切源）由 action 層寫入 bus.shared。
#
# 由 main.py 在 worker_engine 模式下呼叫 worker_start()（阻塞於本核心）。

import machine, time, ubinascii, _thread
from app import App
from lib.sys.sys_bus import bus
from lib.sys.log_service import get_log

from tasks.network import NetworkTask
from tasks.circuit import CircuitTask
from tasks.bus_decode import BusDecodeTask
from tasks.log_task import LogTask


def _aplay_thread(aplay):
    """audio_player_task 獨立執行緒（ESP32 上落在 Core 1）—— worker_engine 的播放端。

    從 audio_stream hub 取 slot → audio_dac.write()；I2S.write() 阻塞會放 GIL，
    合成端 dj（core0 主迴圈）在播放阻塞期間可繼續讀檔/混音。
    """
    try:
        while bus.shared.get("engine_run", True):
            aplay.loop()
            time.sleep_ms(1)
    finally:
        try:
            aplay.on_stop()
        except Exception:
            pass


def worker_start():
    """Core 0 入口 — 極速控制核主迴圈（阻塞）"""
    log = get_log()
    log.info("⚡ [Core0] Worker/Engine Mode — control core")

    st_pixel = bus.get_service("st_pixel")

    bus.slave_id = ubinascii.hexlify(machine.unique_id()).decode().upper()
    bus.shared["engine_run"] = True
    bus.shared["spi_busy"] = False

    app = App()
    bus.register_service("log", log)

    # 日誌輸出設定（與 Core_Manager 一致）
    sys_cfg = bus.shared.get("System", {})
    interval = sys_cfg.get("log_interval_ms")
    if interval is None:
        log_cfg = sys_cfg.get("Log") or bus.shared.get("Log", {})
        interval = log_cfg.get("print_interval_ms", 1000)
    bus.shared["log_print"] = True
    bus.shared["log_print_interval_ms"] = int(interval or 1000)
    bus.shared["log_print_levels"] = ["info", "warn", "error", "immediate"]
    bus.shared["log_subscribe"] = []

    ctx = {"app": app, "st_pixel": st_pixel, "bus": bus}

    # 指令線路 + 音訊合成端（直接驅動，免 TaskManager 調度開銷）
    # dj_task = 合成端：playlist/讀檔/混音 → audio_stream hub（無 DAC 自行停用）
    tasks = [
        LogTask("log", ctx),
        NetworkTask("network", ctx),
        CircuitTask("circuit", ctx),
        BusDecodeTask("bus_decode", ctx),
    ]
    if bus.get_service("audio_dac") is not None:
        from tasks.dj_task import DjTask
        try:
            tasks.append(DjTask("dj", ctx))
            log.info("⚡ [Core0] DjTask（合成端）加入主迴圈")
        except Exception as e:
            log.error("[Core0] dj task init failed: {}".format(e))

    for t in tasks:
        try:
            t.on_start()
        except Exception as e:
            log.error("[Core0] {} on_start failed: {}".format(t.name, e))

    # ── 音訊播放端（worker_engine 的 Core 1 thread）：audio_dac 在 bus 上才啟動 ──
    try:
        if bus.get_service("audio_dac") is not None:
            from tasks.audio_player_task import AudioPlayerTask
            aplay = AudioPlayerTask("audio_player", ctx)
            aplay.on_start()
            _thread.stack_size(16 * 1024)
            _thread.start_new_thread(_aplay_thread, (aplay,))
            log.info("⚡ [Core0] AudioPlayerTask（播放端）thread started (core1)")
    except Exception as e:
        log.error("[Core0] audio_player task start failed: {}".format(e))

    # ── 看門狗 lazy-arm：全部 task 的 on_start 運行完才建狗（同 taskmanager 語意）──
    #   之後每圈餵狗 + poll_rearm。worker_engine 原本沒有 WDT，這裡補上。
    try:
        from lib.sys import watchdog as _wd
        _wd.init_watchdog()
    except Exception as e:
        _wd = None
        log.error("[Core0] watchdog init failed: {}".format(e))

    log.info("⚡ [Core0] Command line online (net + circuit): {}".format(bus.slave_id))

    try:
        while bus.shared.get("engine_run", True):
            # ── WDT：core0 主線程直接餵狗（同執行緒建立/餵）──
            if _wd is not None:
                try:
                    _wdt = bus.get_service("wdt")
                    if _wdt is not None:
                        _wdt.feed()
                    _wd.poll_rearm()
                except Exception:
                    pass
            for t in tasks:
                try:
                    t.loop()
                except Exception as e:
                    log.error("[Core0] {} loop err: {}".format(t.name, e))
    except KeyboardInterrupt:
        print("[Core0]👋 User stop requested.")
    finally:
        bus.shared["engine_run"] = False
        for t in tasks:
            try:
                t.on_stop()
            except Exception:
                pass
        print("[Core0]🛑 Control core stopped.")
