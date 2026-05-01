# main.py
import machine, network, time, _thread, ubinascii
from app import App
from lib.sys_bus import bus
from lib.buffer_hub import AtomicStreamHub
from lib.fs_manager import fs
from lib.task_manager import TaskManager
from tasks.network import NetworkTask
from tasks.bus_decode import BusDecodeTask
from tasks.render import RenderTask
from tasks.web_ui import WebUITask
from tasks.dp_manager_task import DpManagerTask
from tasks.jpeg_decode_task import JpegDecodeTask
from tasks.dp_buffer_task import DpBufferTask
from tasks.display_task import DisplayTask
from apa102 import APA102

def launcher():
    print(f"📂 [FS] Initializing File System Manager...")
    # fs 已經在導入時自動初始化 (load manifest or scan)
    
    st_LED = bus.get_service("st_LED")
    
    # 2. 總線與 ID
    bus.slave_id = ubinascii.hexlify(machine.unique_id()).decode().upper()
    bus.shared["engine_run"] = True
    bus_sys = bus.shared["System"]
    
    # 3. 🚀 註冊核心交換服務 (不修改 lib，在此處申請)
    hub = AtomicStreamHub(st_LED.total_bytes * bus_sys["buffer_frames"]) 
    bus.register_service("pixel_stream", hub)

    # 4. App
    app = App()
    
    # 5. Task Manager Context
    ctx = {
        "app": app,
        "st_LED": st_LED,
        "bus": bus
    }

    # 6. Task Manager
    tm = TaskManager(ctx)
    
    # Register Tasks
    # Default: Network & Web on Core 0, Render on Core 1
    # 這裡實現了您要求的 "靈活控制"
    # 您可以隨時透過 tm.set_affinity('network', (0, 1)) 來遷移任務
    tm.register_task("network", NetworkTask, default_affinity=(1, 0)) # Core 0
    tm.register_task("bus_decode", BusDecodeTask, default_affinity=(1, 0)) # Core 0
    tm.register_task("web_ui",  WebUITask,   default_affinity=(1, 0)) # Core 0
    tm.register_task("render",  RenderTask,  default_affinity=(0, 1)) # Core 1
    tm.register_task("dp_manager", DpManagerTask, default_affinity=(1, 0))
    tm.register_task("jpeg_decode", JpegDecodeTask, default_affinity=(0, 1))
    tm.register_task("dp_buffer", DpBufferTask, default_affinity=(0, 1))
    tm.register_task("display", DisplayTask, default_affinity=(0, 1))

    try:
        # 7. 啟動 Core 1 Runner (新線程)
        print("✨ Starting Core 1 Runner...")
        _thread.start_new_thread(tm.runner_loop, (1,))

        print(f"✨ NetBus System Online: {bus.slave_id}")
        
        # 8. 啟動 Core 0 Runner (主線程阻塞)
        print("✨ Starting Core 0 Runner...")
        tm.runner_loop(0)

    except KeyboardInterrupt:
        print("\n👋 User stop requested.")
    except Exception as e:
        print(f"❌ System Error: {e}")
    finally:
        # 🚀 統一機制替代 shutdown()
        bus.shared["engine_run"] = False
        print("🛑 All cores stopping...")
        time.sleep_ms(500) # 給 Core 1 一點時間收尾
        st_LED.big_buffer = bytearray(st_LED.total_bytes) 
        st_LED.show_all()
        print("🏁 Clean Exit.")

if __name__ == "__main__":
    launcher()
