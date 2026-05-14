# main.py
import machine, network, time, _thread, ubinascii
from app import App
from lib.sys_bus import bus
from lib.buffer_hub import AtomicStreamHub
from lib.fs_manager import fs
from lib.task_manager import TaskManager
from tasks.network import NetworkTask
from tasks.circuit import CircuitTask
from tasks.bus_decode import BusDecodeTask
from tasks.fs_scan_task import FsScanTask
from tasks.log_task import LogTask
from tasks.render import RenderTask
from tasks.web_ui import WebUITask
from tasks.dp_manager_task import DpManagerTask
from tasks.jpeg_decode_task import JpegDecodeTask
from tasks.dp_buffer_task import DpBufferTask
from tasks.display_task import DisplayTask
from lib.log_service import get_log
from apa102 import APA102

def launcher():
    log = get_log()
    log.info("📂 [FS] Initializing File System Manager...")
    
    st_LED = bus.get_service("st_LED")
    
    bus.slave_id = ubinascii.hexlify(machine.unique_id()).decode().upper()
    bus.shared["engine_run"] = True
    bus_sys = bus.shared["System"]
    
    hub = AtomicStreamHub(st_LED.total_bytes * bus_sys["buffer_frames"]) 
    bus.register_service("pixel_stream", hub)

    app = App()
    
    ctx = {
        "app": app,
        "st_LED": st_LED,
        "bus": bus
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

    # ══════════════════════════════════════════════════════
    # Debug：如何使用
    #   1. bus.shared["log_subscribe"] = "__list__"  → 列出所有可訂閱名稱
    #   2. bus.shared["log_subscribe"] = [...]       → 選擇性訂閱
    # ══════════════════════════════════════════════════════
    bus.shared["log_subscribe"] = "__list__"
    bus.shared["log_subscribe"] = [
#                 "cpu0",
#                 "cpu1",
#                 "disp_src_fill",
#                 "fps_total",
#                 "fps_window",
#                 "fs_scan_done",
#                 "fs_scan_progress",
#                 "fs_scan_total",
#                 "jpeg_in_fill",
#                 "jpeg_out_fill",
#                 "bus_decode",
#                 "circuit",
#                 "display",
#                 "dp_buffer",
#                 "dp_manager",
#                 "fs_scan",
                "jpeg_decode",
#                 "log",
#                 "network",
#                 "render",
#                 "web_ui",
            ]

    # ── Layer 0: 網路 + 通訊 + FS 掃描，最先啟動 ──
    tm.register_task("log", LogTask, default_affinity=(1, 0), layer=0)
    tm.register_task("network", NetworkTask, default_affinity=(1, 0), layer=0)
    tm.register_task("circuit", CircuitTask, default_affinity=(1, 0), layer=0)
    tm.register_task("bus_decode", BusDecodeTask, default_affinity=(1, 0), layer=0)
    tm.register_task("web_ui",  WebUITask,   default_affinity=(1, 0), layer=0)
    tm.register_task("fs_scan", FsScanTask,   default_affinity=(0, 1), layer=0)
    
    # ── Layer 1: display pipeline ──
    tm.register_task("render",  RenderTask,  default_affinity=(0, 1), layer=-1)
    
    tm.register_task("dp_manager", DpManagerTask, default_affinity=(1, 0), layer=1)
    tm.register_task("jpeg_decode", JpegDecodeTask, default_affinity=(0, 1), layer=1)
    tm.register_task("dp_buffer", DpBufferTask, default_affinity=(0, 1), layer=1)
    tm.register_task("display", DisplayTask, default_affinity=(1, 0), layer=1)
    
    tm.finalize()

    try:
        log.info("✨ Starting Core 1 Runner...")
        _thread.start_new_thread(tm.runner_loop, (1,))

        log.info("✨ NetBus System Online: {}".format(bus.slave_id))
        log.info("✨ Starting Core 0 Runner...")
        tm.runner_loop(0)

    except KeyboardInterrupt:
        print("[MAIN]👋 User stop requested.")
    except Exception as e:
        print(f"[MAIN]❌ System Error: {e}")
    finally:
        bus.shared["engine_run"] = False
        print("[MAIN]🛑 All cores stopping...")
        time.sleep_ms(500)
        st_LED.big_buffer = bytearray(st_LED.total_bytes) 
        st_LED.show_all()
        print("[MAIN]🏁 Clean Exit.")

if __name__ == "__main__":
    launcher()
