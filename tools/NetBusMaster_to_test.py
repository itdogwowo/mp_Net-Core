import socket
import time
import threading
import os, sys
import hashlib
import struct
import json
import copy
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from collections import defaultdict, deque

# ==================== 全局默認配置 ====================
DEFAULT_CONFIG = {
    "sync_delay_ms": 150,
    "mapping": {},
    "ws_port": 8000,
    "upt_port": 9000,
    "deploy_timeout": 120,
    "max_workers": 50
}

# ==================== 跨平台輸入處理 ====================
class InputHandler:
    def __init__(self):
        self.is_windows = os.name == 'nt'
        self.old_settings = None
        if self.is_windows:
            import msvcrt
            self.msvcrt = msvcrt
        else:
            import select
            import tty
            import termios
            self.select = select
            self.tty = tty
            self.termios = termios

    def enter_raw_mode(self):
        """進入 Raw 模式 (禁用回顯、行緩衝) - 持續生效"""
        if not self.is_windows:
            try:
                fd = sys.stdin.fileno()
                self.old_settings = self.termios.tcgetattr(fd)
                # setcbreak: 禁用行緩衝和回顯，但保留 Ctrl+C 等信號
                self.tty.setcbreak(fd)
            except Exception as e:
                print(f"Failed to enter raw mode: {e}")

    def exit_raw_mode(self):
        """退出 Raw 模式，恢復原始設置"""
        if not self.is_windows and self.old_settings:
            try:
                fd = sys.stdin.fileno()
                self.termios.tcsetattr(fd, self.termios.TCSADRAIN, self.old_settings)
            except Exception:
                pass

    def kbhit(self):
        if self.is_windows:
            return self.msvcrt.kbhit()
        else:
            # 在 Raw 模式下，select 依然有效
            dr, dw, de = self.select.select([sys.stdin], [], [], 0)
            return dr != []

    def getch(self):
        """讀取單個字符 (假設已在 Raw 模式 或 Windows)"""
        if self.is_windows:
            return self.msvcrt.getwch()
        else:
            try:
                # 直接讀取，因為已經在 enter_raw_mode 中設置了 cbreak
                return sys.stdin.read(1)
            except Exception:
                return ''
            
    def flush_input(self):
        """清空輸入緩衝區 (Unix only)"""
        if not self.is_windows:
            try:
                import termios
                termios.tcflush(sys.stdin, termios.TCIOFLUSH)
            except:
                pass

input_handler = InputHandler()

# ==================== 音頻模式自動檢測 (修復導入) ====================
AUDIO_MODE = 'miniaudio'
mixer = None  # 全局變量

try:
    import miniaudio
except ImportError:
    AUDIO_MODE = 'pygame'
    try:
        import pygame
        pygame.mixer.init()
        mixer = pygame.mixer  # 正確引用
    except ImportError:
        print("⚠️ 警告: pygame 和 miniaudio 都未安裝,音訊功能不可用")
        AUDIO_MODE = None

print(f"[Audio Mode] {AUDIO_MODE}")

# ==================== 路徑初始化 ====================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.chdir(SCRIPT_DIR)

# ==================== 協議層導入 ====================
try:
    from slave.lib.proto import Proto, StreamParser
    from slave.lib.schema_loader import SchemaStore
    from slave.lib.schema_codec import SchemaCodec
    from tools.PXLDv3Splitter import PXLDv3Decoder
except ImportError as e:
    print(f"❌ 導入錯誤: {e}")
    sys.exit(1)


# ==================== 增強版設備監控模型 ====================
class DeviceMonitor:
    """
    多階段數據融合監控模型
    """
    
    def __init__(self, device_id):
        # ========== 基礎信息 ==========
        self.device_id = device_id
        self.play_id = None
        self.status = "離線"
        
        # ========== 傳輸階段數據 ==========
        self.upload_progress = 0.0
        self.upload_speed = 0.0
        self.send_speed = 0.0
        self.ack_rtt_ms = 0.0
        self.uploaded_bytes = 0
        self.total_bytes = 0
        self.upload_start_time = 0
        self.upload_end_time = 0
        self.upload_send_time = 0.0
        self.upload_ack_time = 0.0
        
        # ========== 播放階段數據 ==========
        self.total_frames = 0
        self.current_frame = 0
        self.render_fps = 0.0
        self.calculated_fps = 0.0
        
        # ========== 性能監控 ==========
        self.mem_free = 0
        self.block_count = 0
        self.avg_fps = 0.0
        
        # ========== 歷史數據 (用於計算) ==========
        self.frame_history = deque(maxlen=10)
        self.last_update = time.time()
        self.last_frame_update = time.time()
        
        # ========== 錯誤信息 ==========
        self.error_msg = ""
        
        # ========== 線程安全鎖 ==========
        self.lock = threading.Lock()
    
    def update_frame(self, frame_num):
        """更新当前帧号并计算实时 FPS"""
        with self.lock:
            now = time.time()
            
            # 🔧 如果是第一次更新，只记录不计算
            if self.current_frame == 0 or self.last_frame_update == 0:
                self.current_frame = frame_num
                self.last_frame_update = now
                return
            
            # 🔧 计算帧差和时间差
            frame_delta = frame_num - self.current_frame
            time_delta = now - self.last_frame_update
            
            # 🔧 简单直接：本次帧号 - 上次帧号 / 时间差
            if time_delta > 0 and frame_delta > 0:
                self.calculated_fps = frame_delta / time_delta
            
            # 更新记录
            self.current_frame = frame_num
            self.last_frame_update = now
    
    def get_play_progress(self):
        """返回播放進度百分比"""
        with self.lock:
            if self.total_frames > 0:
                return (self.current_frame / self.total_frames) * 100
            return 0.0
    
    def reset_play_stats(self):
        """重置播放統計數據"""
        with self.lock:
            self.current_frame = 0
            self.render_fps = 0.0
            self.calculated_fps = 0.0
            self.block_count = 0
            self.avg_fps = 0.0
            self.frame_history.clear()


# ==================== 終端 UI 渲染引擎 ====================
class ConsoleUI:
    """ANSI 轉義序列終端控制"""
    
    @staticmethod
    def clear_screen():
        print("\033[2J\033[H", end="")
    
    @staticmethod
    def move_cursor(row, col):
        print(f"\033[{row};{col}H", end="")
    
    @staticmethod
    def hide_cursor():
        print("\033[?25l", end="")
    
    @staticmethod
    def show_cursor():
        print("\033[?25h", end="")
    
    @staticmethod
    def get_color(value, threshold_good=80, threshold_warn=50):
        if value >= threshold_good:
            return "\033[92m"
        elif value >= threshold_warn:
            return "\033[93m"
        else:
            return "\033[91m"
    
    @staticmethod
    def reset_color():
        return "\033[0m"
    
    @staticmethod
    def draw_progress_bar(percent, width=30):
        filled = int(width * percent / 100)
        bar = "█" * filled + "░" * (width - filled)
        color = ConsoleUI.get_color(percent)
        return f"{color}{bar}{ConsoleUI.reset_color()} {percent:5.1f}%"


# ==================== 監控面板核心 ====================
class MonitorPanel:
    """實時監控面板"""
    
    def __init__(self):
        self.monitors = {}
        self.lock = threading.Lock()
        self.running = False
        self.refresh_rate = 0.1
        self.render_thread = None
        self.interactive_mode = False
    
    def register_device(self, device_id, play_id=None, total_frames=0):
        with self.lock:
            if device_id not in self.monitors:
                monitor = DeviceMonitor(device_id)
                monitor.play_id = play_id
                monitor.total_frames = total_frames
                monitor.status = "待機"
                self.monitors[device_id] = monitor
            else:
                monitor = self.monitors[device_id]
                if play_id is not None:
                    monitor.play_id = play_id
                if total_frames > 0:
                    monitor.total_frames = total_frames
    
    def update_device(self, device_id, **kwargs):
        with self.lock:
            if device_id in self.monitors:
                monitor = self.monitors[device_id]
                
                if 'current_frame' in kwargs:
                    monitor.update_frame(kwargs.pop('current_frame'))
                
                for key, value in kwargs.items():
                    if hasattr(monitor, key):
                        setattr(monitor, key, value)
                
                monitor.last_update = time.time()
                if monitor.total_bytes and monitor.uploaded_bytes >= monitor.total_bytes and monitor.upload_start_time:
                    if not monitor.upload_end_time:
                        monitor.upload_end_time = monitor.last_update
    
    def remove_device(self, device_id):
        with self.lock:
            if device_id in self.monitors:
                self.monitors[device_id].status = "離線"
    
    def start(self, interactive=False):
        if not self.running:
            self.running = True
            self.interactive_mode = interactive
            ConsoleUI.hide_cursor()
            ConsoleUI.clear_screen()
            self.render_thread = threading.Thread(target=self._render_loop, daemon=True)
            self.render_thread.start()
    
    def stop(self):
        self.running = False
        if self.render_thread:
            self.render_thread.join(timeout=1.0)
        ConsoleUI.show_cursor()
    
    def _render_loop(self):
        while self.running:
            self._render_frame()
            time.sleep(self.refresh_rate)
    
    def _render_frame(self):
        with self.lock:
            # 使用 ANSI 轉義序列：
            # \033[H : 移動光標到左上角 (1,1)
            # \033[2J: 清除整個屏幕
            # \033[3J: 清除滾動緩衝區 (防止殘留)
            sys.stdout.write("\033[H\033[2J\033[3J")
            
            title = "╔════════════════════════════════════════════════════════════════════════════════════════════════════════════════════╗"
            subtitle = f"║  🎬 NetBus Master Monitor  │  Devices: {len(self.monitors)}  │  Time: {datetime.now().strftime('%H:%M:%S')}                                 ║"
            divider = "╠════════════════════════════════════════════════════════════════════════════════════════════════════════════════════╣"
            
            # 使用列表構建輸出緩衝區，一次性打印以減少閃爍
            buffer = []
            buffer.append(title)
            buffer.append(subtitle)
            buffer.append(divider)
            
            if not self.monitors:
                buffer.append("║  [無設備在線]                                                                                                      ║")
            else:
                for device_id, monitor in sorted(self.monitors.items()):
                    buffer.append(self._get_device_row_str(monitor))
            
            bottom = "╠════════════════════════════════════════════════════════════════════════════════════════════════════════════════════╣"
            buffer.append(bottom)
            
            if self.interactive_mode:
                controls = "║  [SPACE] 暫停/繼續  │  [S] 停止播放  │  [Q] 退出                                                              ║"
                buffer.append(controls)
            
            footer = "╚════════════════════════════════════════════════════════════════════════════════════════════════════════════════════╝"
            buffer.append(footer)
            
            # 確保內容完全覆蓋舊內容
            output_str = "\n".join(buffer)
            sys.stdout.write(output_str + "\n")
            sys.stdout.flush()

    def _get_device_row_str(self, monitor: DeviceMonitor):
        device_str = f"{monitor.device_id[:12]:<12}"
        play_id_str = f"P{monitor.play_id:02d}" if monitor.play_id is not None else "---"
        
        status_colors = {
            "離線": "\033[90m",
            "待機": "\033[96m",
            "傳輸中": "\033[93m",
            "播放中": "\033[92m",
            "暫停": "\033[95m",
            "錯誤": "\033[91m"
        }
        status_color = status_colors.get(monitor.status, "\033[0m")
        status_str = f"{status_color}{monitor.status:<6}{ConsoleUI.reset_color()}"
        
        if monitor.status == "傳輸中" or monitor.status == "測速中" or monitor.status == "Raw測速":
            progress_bar = ConsoleUI.draw_progress_bar(monitor.upload_progress, width=20)
            if monitor.ack_rtt_ms > 0:
                speed_str = f"{monitor.upload_speed:>6.1f} KB/s │ TX {monitor.send_speed:>6.1f} │ ACK {monitor.ack_rtt_ms:>5.1f}ms"
            else:
                speed_str = f"{monitor.upload_speed:>6.1f} KB/s"
            size_str = f"{monitor.uploaded_bytes//1024}/{monitor.total_bytes//1024} KB"
            info = f"{progress_bar} │ {speed_str} │ {size_str}"
        
        elif monitor.status in ["播放中", "暫停"]:
            play_progress = monitor.get_play_progress()
            
            # 🔧 修复: 显示真实计算的 FPS
            calc_fps_color = ConsoleUI.get_color(monitor.calculated_fps, threshold_good=25, threshold_warn=15)
            calc_fps_str = f"{calc_fps_color}{monitor.calculated_fps:>5.1f}{ConsoleUI.reset_color()}"
            
            # 当前帧/总帧
            frame_str = f"{monitor.current_frame}/{monitor.total_frames}"
            progress_percent = f"{play_progress:>5.1f}%"
            
            # 内存显示
            mem_mb = monitor.mem_free / (1024 * 1024)
            mem_color = ConsoleUI.get_color(mem_mb, threshold_good=10, threshold_warn=5)
            mem_str = f"{mem_color}{mem_mb:>6.1f} MB{ConsoleUI.reset_color()}"
            
            # 🔧 简化显示: 只显示 Real_FPS (真实渲染帧率)
            info = f"Progress: {progress_percent} │ Frame: {frame_str:<12} │ FPS: {calc_fps_str} │ Mem: {mem_str}"
        
        elif monitor.status == "完成":
            tx_elapsed = float(monitor.upload_send_time or 0.0)
            e2e_elapsed = monitor.last_update - monitor.upload_start_time
            if tx_elapsed > 0 and monitor.total_bytes > 0:
                tx_avg = (monitor.total_bytes / 1024) / tx_elapsed
                tx_str = f"{tx_avg:>6.1f} KB/s"
            else:
                tx_str = f"{0.0:>6.1f} KB/s"
            if e2e_elapsed > 0 and monitor.total_bytes > 0:
                e2e_avg = (monitor.total_bytes / 1024) / e2e_elapsed
                e2e_str = f"{e2e_avg:>6.1f} KB/s"
            else:
                e2e_str = f"{0.0:>6.1f} KB/s"
            info = f"完成 │ TX Avg: {tx_str} │ E2E Avg: {e2e_str} │ {monitor.total_bytes//1024} KB"
        
        elif monitor.status == "錯誤":
            info = f"\033[91m{monitor.error_msg[:70]}\033[0m"
        
        else:
            idle_time = int(time.time() - monitor.last_update)
            info = f"閒置 {idle_time}s"
        
        return f"║ {device_str} │ {play_id_str} │ {status_str} │ {info:<80} ║"


class DeviceManager:
    """
    設備管理器: 統籌 DeviceMonitor 和 Connection
    負責:
    1. 管理 slaves 連接字典
    2. 處理設備重連/註冊
    3. 執行健康檢查 (Heartbeat)
    4. 提供設備統計數據
    """
    def __init__(self, panel: MonitorPanel):
        self.panel = panel
        self.slaves = {}  # {device_id: {conn, addr, parser, ...}}
        self.lock = threading.Lock()
        self.running = True
        
        # 啟動健康檢查線程
        self.health_thread = threading.Thread(target=self._health_check_loop, daemon=True)
        self.health_thread.start()

    def register_connection(self, cid, conn, addr, parser):
        """處理新連接/重連"""
        with self.lock:
            # 如果設備已存在，先清理舊連接
            if cid in self.slaves:
                old_node = self.slaves[cid]
                try:
                    print(f"🔄 [DeviceManager] 設備 {cid} 重連，關閉舊連接...")
                    old_node["conn"].close()
                    # 通知舊的 handle_client 線程退出 (通過關閉 socket 觸發異常)
                except:
                    pass
            
            # 註冊新連接
            self.slaves[cid] = {
                "conn": conn,
                "addr": addr,
                "parser": parser,
                "ack_event": threading.Event(),
                "query_event": threading.Event(),
                "ram_event": threading.Event(),
                "remote_sha": None,
                "ram_report": None,
                "ram_run_id": None,
                "last_seen": time.time()  # 用於內部連接保活檢查
            }
            
            # 更新面板狀態
            self.panel.update_device(cid, status="待機")

    def unregister_connection(self, cid):
        """移除連接"""
        with self.lock:
            if cid in self.slaves:
                del self.slaves[cid]
            self.panel.remove_device(cid)

    def update_heartbeat(self, cid):
        """更新心跳時間"""
        if cid in self.slaves:
            self.slaves[cid]["last_seen"] = time.time()
            # 同時更新 Monitor 的 last_update (雖然 MonitorPanel 也有 update_device)
            # 這裡主要確保 DeviceManager 內部的 last_seen 也更新

    def get_slave(self, cid):
        return self.slaves.get(cid)

    def get_all_slaves(self):
        return self.slaves

    def _health_check_loop(self):
        """每 5 秒檢查一次設備健康狀態"""
        while self.running:
            time.sleep(5)
            now = time.time()
            timeout = 30.0  # 30秒超時
            
            # 複製 keys 避免遍歷時修改
            with self.lock:
                current_cids = list(self.slaves.keys())
            
            for cid in current_cids:
                # 檢查 MonitorPanel 中的 last_update
                # 因為 update_device 會更新 monitor.last_update
                monitor = self.panel.monitors.get(cid)
                if monitor:
                    if now - monitor.last_update > timeout:
                        if monitor.status != "離線":
                            monitor.status = "離線"
                            # 也可以選擇在這裡主動斷開 socket
                            # self.unregister_connection(cid) 
                            # 但有時候只是心跳包丟失，socket 還活著，保留 socket 讓它有機會恢復?
                            # 用戶要求 "30秒沒有收到就標記離線"
                            
    def get_counts(self):
        """返回 (在線總數, 離線總數)"""
        online = 0
        offline = 0
        with self.lock:
            for m in self.panel.monitors.values():
                if m.status == "離線":
                    offline += 1
                else:
                    online += 1
        return online, offline

    def stop(self):
        self.running = False


# ==================== NetBusMaster 主類 ====================
class NetBusMaster:
    def __init__(self, config_file="slave_map.json"):
        self.store = SchemaStore(dir_path=f"{PROJECT_ROOT}/slave/schema")
        self.panel = MonitorPanel()
        self.device_manager = DeviceManager(self.panel)
        self.slaves = self.device_manager.slaves  # 兼容舊代碼，指向 Manager 的字典
        
        self.running = True
        self.local_ip = self.get_local_ip()
        
        self.is_playing = False
        self.is_paused = False
        self.play_lock = threading.Lock()
        
        self.config_file = config_file
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.load_config()
        self.selected_targets = []
        self.prepared_data = {}
        self.pxld_metadata = {}
        
        threading.Thread(target=self.start_ws_server, daemon=True).start()
    
    def load_config(self):
        """載入配置，支持熱更新，並自動補全缺失的默認值"""
        needs_save = False
        
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    file_data = json.load(f)
                
                # 1. 檢查是否有缺失的默認 Key
                for k in DEFAULT_CONFIG:
                    if k not in file_data:
                        needs_save = True
                
                # 2. 更新內存配置 (File -> Memory)
                # 先從 DEFAULT_CONFIG 重新初始化，確保有最新的 defaults
                self.config = copy.deepcopy(DEFAULT_CONFIG)
                
                # 再用 file_data 覆蓋
                for k, v in file_data.items():
                    if k in self.config and isinstance(self.config[k], dict) and isinstance(v, dict):
                        self.config[k].update(v)
                    else:
                        self.config[k] = v
                            
                print(f"✅ Config loaded: {self.config_file}")
            except Exception as e:
                print(f"❌ Config load error: {e}")
        else:
            needs_save = True
        
        if needs_save:
            print("💾 自動補全缺失的配置項...")
            self.save_config()
            
        return self.config
    
    def save_config(self):
        # 為了方便手動編輯，將 "mapping" 移到最後
        ordered_config = {}
        # 先加入所有非 mapping 的 key
        for k, v in self.config.items():
            if k != "mapping":
                ordered_config[k] = v
        # 最後再加入 mapping
        if "mapping" in self.config:
            ordered_config["mapping"] = self.config["mapping"]
            
        with open(self.config_file, 'w', encoding='utf-8') as f:
            json.dump(ordered_config, f, indent=4, ensure_ascii=False)
    
    def get_local_ip(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('8.8.8.8', 80))
            return s.getsockname()[0]
        except:
            return '127.0.0.1'
        finally:
            s.close()
    
    def start_ws_server(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        port = self.config.get("ws_port", 8000)
        s.bind(('0.0.0.0', port))
        s.listen(20)
        print(f"[WS Server] 監聽 0.0.0.0:{port}")
        
        while self.running:
            try:
                conn, addr = s.accept()
                threading.Thread(target=self.handle_client, args=(conn, addr), daemon=True).start()
            except:
                break
    
    def handle_client(self, conn, addr):
        cid = f"PENDING_{addr[1]}"
        
        try:
            header_data = conn.recv(1024).decode()
            if not header_data or "Upgrade: websocket" not in header_data:
                conn.close()
                return
            
            first_line = header_data.split('\r\n')[0]
            parts = first_line.split(' ')
            if len(parts) >= 2:
                path = parts[1].strip('/')
                if path and path != 'ws':
                    # Fix: 取最後一段作為 ID (去除路徑前綴如 ws/)
                    cid = path.split('/')[-1]
            
            resp = ("HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    "Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n\r\n")
            conn.send(resp.encode())
            
            # 自動遷移舊配置格式 (ws/ID -> ID)
            if cid not in self.config["mapping"] and f"ws/{cid}" in self.config["mapping"]:
                print(f"🔄 Migrating config: ws/{cid} -> {cid}")
                self.config["mapping"][cid] = self.config["mapping"].pop(f"ws/{cid}")
                self.save_config()
            
            if cid not in self.config["mapping"]:
                pids = [v["play_id"] for v in self.config["mapping"].values() if "play_id" in v]
                new_pid = max(pids) + 1 if pids else 0
                self.config["mapping"][cid] = {"play_id": new_pid, "last_sha": ""}
                self.save_config()
            
            play_id = self.config["mapping"][cid]["play_id"]
            total_frames = self.pxld_metadata.get(play_id, {}).get("total_frames", 0)
            
            self.panel.register_device(cid, play_id, total_frames)
            
            # 使用 DeviceManager 註冊連接 (自動處理重連)
            self.device_manager.register_connection(
                cid, conn, addr, StreamParser()
            )
            
            parser = self.slaves[cid]["parser"]
            while self.running:
                raw = conn.recv(4096)
                if not raw:
                    break
                
                if raw[0] == 0x82:
                    plen = raw[1] & 0x7F
                    off = 2
                    if plen == 126:
                        off = 4
                    elif plen == 127:
                        off = 10
                    parser.feed(raw[off:])
                else:
                    parser.feed(raw)
                
                for ver, addr_pkt, cmd, payload in parser.pop():
                    # 收到任何數據都視為心跳
                    self.device_manager.update_heartbeat(cid)
                    cid = self.dispatch_logic(cid, cmd, payload)
        
        except Exception as e:
            self.panel.update_device(cid, status="錯誤", error_msg=str(e))
        
        finally:
            # 智能清理: 只有當前連接是自己的時候才移除
            # 避免重連時新連接剛建立就被舊連接的 finally 刪除
            current_node = self.device_manager.get_slave(cid)
            if current_node and current_node["conn"] == conn:
                self.device_manager.unregister_connection(cid)
            
            try:
                conn.close()
            except:
                pass
    
    def dispatch_logic(self, cid, cmd, payload):
        c_def = self.store.get(cmd)
        args = SchemaCodec.decode(c_def, payload)
        
        # ========== 0x1102: 状态心跳 ==========
        if cmd == 0x1102:
            try:
                status_data = json.loads(args["status_json"])
                
                # 🔧 修复: MCU 回报的 render_fps 实际是当前帧号
                current_frame = status_data.get('render_fps', 0)  # ✅ 这是帧号
                mem_free = status_data.get('mem_free', 0)
                real_id = status_data.get('id')
                
                # 🔧 更新当前帧号 (触发 FPS 计算)
                self.panel.update_device(
                    cid,
                    current_frame=current_frame,  # ✅ 更新帧号
                    mem_free=mem_free
                )
                
                # 设备 ID 转移
                if real_id and real_id != cid:
                    if cid in self.panel.monitors:
                        self.panel.monitors[real_id] = self.panel.monitors.pop(cid)
                        self.panel.monitors[real_id].device_id = real_id
                    self.slaves[real_id] = self.slaves.pop(cid)
                    cid = real_id
            
            except Exception as e:
                pass
        
        elif cmd == 0x3012:
            block_id = args.get("block_id", 0)
            current_frame = args.get("end_frame", 0)
            actual_fps = args.get("actual_fps", 0) / 100.0
            
            self.panel.update_device(
                cid,
                current_frame=current_frame
            )
            
            if cid in self.panel.monitors:
                monitor = self.panel.monitors[cid]
                with monitor.lock:
                    monitor.block_count += 1
                    monitor.avg_fps = (monitor.avg_fps * (monitor.block_count - 1) + actual_fps) / monitor.block_count
        
        elif cmd == 0x2004:
            if cid in self.slaves:
                self.slaves[cid]["ack_event"].set()
        
        elif cmd == 0x2006:
            if cid in self.slaves:
                self.slaves[cid]["remote_sha"] = args["sha256"]
                self.slaves[cid]["query_event"].set()

        elif cmd == 0x1814:
            run_id = args.get("run_id")
            if cid in self.slaves:
                node = self.slaves[cid]
                if node.get("ram_run_id") == run_id:
                    node["ram_report"] = args
                    node["ram_event"].set()
            try:
                b = int(args.get("bytes", 0))
                elapsed_ms = int(args.get("elapsed_ms", 0))
                kb_s = (b / 1024.0) / (elapsed_ms / 1000.0) if elapsed_ms > 0 else 0.0
                self.panel.update_device(
                    cid,
                    status="完成",
                    upload_progress=100,
                    upload_speed=kb_s,
                    uploaded_bytes=b
                )
            except Exception:
                pass
        
        return cid
    
    def send_pkt(self, targets, cmd_id, args):
        c_def = self.store.get(cmd_id)
        data_pkt = Proto.pack(cmd_id, SchemaCodec.encode(c_def, args))
        l = len(data_pkt)
        
        hdr = bytearray([0x82])
        if l <= 125:
            hdr.append(l)
        elif l <= 65535:
            hdr.append(126)
            hdr.extend(struct.pack(">H", l))
        else:
            hdr.append(127)
            hdr.extend(struct.pack(">Q", l))
        
        pkt = hdr + data_pkt
        
        for tid in targets:
            # Fix: 不檢查 self.slaves，直接嘗試發送
            # 只要 tid 在 self.slaves 中有記錄 (即 socket 未被物理移除)，就嘗試發送
            # 即使標記為 "離線" 也可以嘗試發送，因為 socket 可能只是暫時沒心跳
            if tid in self.slaves:
                try:
                    self.slaves[tid]["conn"].sendall(pkt)
                except:
                    pass
            # 如果 tid 根本不在 slaves (socket 已 close/清除)，則無法發送，忽略
    
    # ==================== New Step 1: 掃描與選擇 ====================
    def scan_devices(self):
        """僅掃描 (發送廣播包)"""
        # print("\nDEBUG: scan_devices ENTERED") # Debug print
        self.load_config()  # Reload config
        self.panel.stop()
        ConsoleUI.clear_screen()
        ConsoleUI.show_cursor()
        
        print("\n[Scan] 正在廣播發現包...")
        
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            
            # Refresh local IP
            self.local_ip = self.get_local_ip()
            
            # Remove bind as it might cause issues on some systems
            # try:
            #     s.bind((self.local_ip, 0))
            # except Exception as e:
            #     print(f"⚠️ Bind warning: {e}")

            port = self.config.get("ws_port", 8000)
            udp_port = self.config.get("upt_port", 9000)
            
            p_data = SchemaCodec.encode(
                self.store.get(0x1001),
                {"server_ip": self.local_ip, "ws_url": f"ws://{self.local_ip}:{port}/ws"}
            )
            
            print(f"📡 Broadcasting DISCOVER to port {udp_port} (Server IP: {self.local_ip})")

            # 1. Send to Global Broadcast
            try:
                s.sendto(Proto.pack(0x1001, p_data), ('255.255.255.255', udp_port))
            except Exception as e:
                print(f"⚠️ Global broadcast failed: {e}")
                
            # 2. Send to Subnet Broadcast (Assuming /24)
            try:
                parts = self.local_ip.split('.')
                parts[-1] = '255'
                subnet_broadcast = '.'.join(parts)
                s.sendto(Proto.pack(0x1001, p_data), (subnet_broadcast, udp_port))
                print(f"📡 Subnet broadcast sent to {subnet_broadcast}:{udp_port}")
            except Exception as e:
                print(f"⚠️ Subnet broadcast failed: {e}")
                
            s.close()
            print("✅ 廣播已發送，請等待設備連線...")
        except Exception as e:
            print(f"❌ 廣播失敗: {e}")
            
        time.sleep(1)
        input("\n按 Enter 返回主菜單...")
        self.panel.start()

    def select_devices(self):
        """選擇設備"""
        self.load_config()  # Reload config
        self.panel.stop()
        ConsoleUI.clear_screen()
        ConsoleUI.show_cursor()
        
        # 取得所有在線設備 ID
        online_ids = list(self.slaves.keys())
        
        if not online_ids:
            print("❌ 當前無在線設備，請先執行 [Scan]")
            input("\n按 Enter 返回...")
            self.panel.start()
            return

        # 根據 PlayID 進行排序
        sorted_ids = sorted(
            online_ids, 
            key=lambda sid: self.config["mapping"].get(sid, {}).get("play_id", 999)
        )

        print(f"\n✅ 當前在線 {len(sorted_ids)} 個設備:")
        print("-" * 50)
        for i, sid in enumerate(sorted_ids):
            pid = self.config["mapping"].get(sid, {}).get("play_id", "N/A")
            mark = "*" if sid in self.selected_targets else " "
            print(f" {mark} {i+1:2d}. {sid:15} (PlayID: {pid})")
        
        print("-" * 50)
        print("操作說明:")
        print(" - 輸入編號 (例: 1,3,5) 選擇/取消選擇")
        print(" - 輸入 'a' 全選")
        print(" - 輸入 'c' 清空選擇")
        print(" - 直接按 Enter 完成並返回")
        
        choice = input("\n👉 請輸入: ").strip().lower()
        
        if not choice:
            self.panel.start()
            return
            
        if choice == 'a':
            self.selected_targets = sorted_ids[:]
            print("✅ 已全選")
        elif choice == 'c':
            self.selected_targets = []
            print("✅ 已清空選擇")
        else:
            try:
                indices = [int(x.strip()) - 1 for x in choice.split(',')]
                current_set = set(self.selected_targets)
                
                for i in indices:
                    if 0 <= i < len(sorted_ids):
                        target = sorted_ids[i]
                        if target in current_set:
                            current_set.remove(target)
                        else:
                            current_set.add(target)
                
                # 保持排序順序
                self.selected_targets = [tid for tid in sorted_ids if tid in current_set]
                print(f"✅ 更新選擇: {len(self.selected_targets)} 個設備")
            except:
                print("❌ 輸入無效")
        
        time.sleep(1)
        self.panel.start()

    def clear_device_list(self):
        """清除設備列表 (斷開所有連接)"""
        self.panel.stop()
        ConsoleUI.clear_screen()
        ConsoleUI.show_cursor()
        
        count = len(self.slaves)
        print(f"\n⚠️ 即將斷開 {count} 個設備的連接並清除列表。")
        confirm = input("👉 確認? (y/n): ").lower()
        
        if confirm == 'y':
            # 複製一份列表進行操作，避免遍歷時修改錯誤
            targets = list(self.slaves.values())
            for node in targets:
                try:
                    node["conn"].close()
                except:
                    pass
            
            # 等待線程清理
            print("⏳ 正在清理連接...")
            time.sleep(1)
            
            # 強制清理殘留
            self.slaves.clear()
            self.panel.monitors.clear()
            self.selected_targets.clear()
            
            print("✅ 列表已清除")
        else:
            print("已取消")
            
        time.sleep(1)
        self.panel.start()
    
    # ==================== Step 2: 準備數據 (修復版) ====================
    def _save_bins(self):
        """將 prepared_data 保存到 bins/ 目錄"""
        bins_dir = os.path.join('.', 'bins')
        os.makedirs(bins_dir, exist_ok=True)

        for pid, data in self.prepared_data.items():
            bin_path = os.path.join(bins_dir, f'pid_{pid}.bin')
            with open(bin_path, 'wb') as f:
                f.write(data)
            print(f"  💾 已保存 {bin_path} ({len(data)//1024} KB)")

    def _load_bins(self):
        """從 bins/ 目錄載入 bin 檔案到 prepared_data"""
        bins_dir = os.path.join('.', 'bins')
        needed_pids = {self.config["mapping"][tid].get("play_id") for tid in self.selected_targets}
        needed_pids.discard(None)

        self.prepared_data.clear()
        self.pxld_metadata.clear()

        loaded = 0
        missing = []
        for pid in needed_pids:
            bin_path = os.path.join(bins_dir, f'pid_{pid}.bin')
            if os.path.isfile(bin_path):
                with open(bin_path, 'rb') as f:
                    self.prepared_data[pid] = bytearray(f.read())
                print(f"  📂 已載入 pid_{pid}.bin ({len(self.prepared_data[pid])//1024} KB)")
                loaded += 1
            else:
                missing.append(pid)

        if missing:
            print(f"  ⚠️ 缺少 PlayID: {missing}")

        return loaded, missing

    def step_2_prepare_data(self):
        """切分 PXLD 動畫數據"""
        self.load_config()  # Reload config
        self.panel.stop()
        ConsoleUI.clear_screen()
        ConsoleUI.show_cursor()

        if not self.selected_targets:
            print("⚠️ 請先執行 Step 1 選擇設備")
            input("\n按 Enter 繼續...")
            self.panel.start()
            return

        # 檢查 bins/ 是否有現成的 bin 檔案
        bins_dir = os.path.join('.', 'bins')
        has_bins = os.path.isdir(bins_dir) and any(f.endswith('.bin') for f in os.listdir(bins_dir))

        pxld_files = [f for f in os.listdir('.') if f.endswith('.pxld')]

        if has_bins:
            bin_files = sorted(f for f in os.listdir(bins_dir) if f.endswith('.bin'))
            print("\n📂 [Step 2] 選擇數據來源:")
            print(f"  1. 從 bins/ 載入已切分的數據 ({len(bin_files)} 個檔案)")
            if pxld_files:
                print(f"  2. 重新從 .pxld 切分")

            try:
                src = input("\n👉 請選擇 (1/2): ").strip()
            except:
                src = "1"

            if src == "1":
                print(f"\n⚙️ 正在從 bins/ 載入...")
                loaded, missing = self._load_bins()
                if loaded > 0:
                    print(f"\n✅ 已載入 {loaded} 個 PlayID 的數據")
                else:
                    print("❌ 沒有載入任何數據")
                input("\n按 Enter 繼續...")
                self.panel.start()
                return

        if not pxld_files:
            print("❌ 當前目錄下找不到 .pxld 文件")
            input("\n按 Enter 繼續...")
            self.panel.start()
            return

        print("\n📂 [Step 2] 選擇動畫源:")
        for i, f in enumerate(pxld_files):
            size_kb = os.path.getsize(f) // 1024
            print(f"  {i+1}. {f} ({size_kb} KB)")

        try:
            choice = int(input("\n👉 請選擇編號: ")) - 1
            if choice < 0 or choice >= len(pxld_files):
                raise ValueError
            path = pxld_files[choice]
        except:
            print("❌ 選擇無效")
            input("\n按 Enter 繼續...")
            self.panel.start()
            return
        
        print(f"\n⚙️ 正在解析動畫: {path}...")
        
        self.prepared_data.clear()
        self.pxld_metadata.clear()
        
        try:
            with PXLDv3Decoder(path) as decoder:
                # 🔧 修復: 從打印信息獲取總幀數
                # 根據您的輸出: "總影格: 10707"
                # PXLDv3Decoder 可能沒有 header 屬性,而是直接在 __enter__ 時打印
                
                # 嘗試多種方法獲取總幀數
                total_frames = 0
                if hasattr(decoder, 'total_frames'):
                    total_frames = decoder.total_frames
                elif hasattr(decoder, 'frame_count'):
                    total_frames = decoder.frame_count
                else:
                    # 如果都沒有,則通過遍歷計算
                    print("  ⚙️ 正在計算總幀數...")
                    total_frames = sum(1 for _ in decoder.iterate_frames())
                    # 重新打開文件以便後續切分
                    decoder.__exit__(None, None, None)
                    decoder = PXLDv3Decoder(path).__enter__()
                
                print(f"  📊 總幀數: {total_frames}")
                
                # Ask for frame range
                start_frame = 0
                end_frame = total_frames
                
                print(f"\n✂️  [切分範圍設置] (預設: 0 - {total_frames})")
                try:
                    s_in = input(f"👉 起始幀 [Enter=0]: ").strip()
                    if s_in:
                        start_frame = int(s_in)
                    
                    e_in = input(f"👉 結束幀 [Enter={total_frames}]: ").strip()
                    if e_in:
                        end_frame = int(e_in)
                        
                    # Validate
                    start_frame = max(0, start_frame)
                    end_frame = min(total_frames, max(start_frame + 1, end_frame))
                    
                    print(f"✅ 設定範圍: {start_frame} -> {end_frame} (共 {end_frame - start_frame} 幀)")
                except:
                    print(f"⚠️  輸入無效, 使用預設範圍: 0 - {total_frames}")
                    start_frame = 0
                    end_frame = total_frames
                
                # 提取所需 PlayID 數據
                needed_pids = {self.config["mapping"][tid].get("play_id") for tid in self.selected_targets}
                
                for pid in needed_pids:
                    if pid is None:
                        continue
                    
                    print(f"  📦 提取 PlayID {pid}...", end="", flush=True)
                    
                    data = bytearray()
                    
                    # Fix: Use iterate_frames with range
                    for frame in decoder.iterate_frames(start_frame=start_frame, end_frame=end_frame):
                        slave_data = decoder.get_slave_data(frame, pid)
                        if slave_data:
                            data.extend(slave_data)
                    
                    self.prepared_data[pid] = data
                    # Update metadata with actual sliced frame count
                    sliced_frames = end_frame - start_frame
                    self.pxld_metadata[pid] = {"total_frames": sliced_frames}
                    
                    # 更新監控面板的 total_frames
                    for tid in self.selected_targets:
                        if self.config["mapping"][tid].get("play_id") == pid:
                            self.panel.register_device(tid, pid, sliced_frames)
                    
                    print(f" OK ({len(data)//1024} KB, {sliced_frames} Frames)")
        
        except Exception as e:
            print(f"\n❌ 解析失敗: {e}")
            import traceback
            traceback.print_exc()
            input("\n按 Enter 繼續...")
            self.panel.start()
            return

        # 保存 bin 檔案到 bins/
        print("\n💾 正在保存切分數據到 bins/...")
        self._save_bins()

        print("\n✅ 動畫數據準備完成")
        input("\n按 Enter 繼續...")
        self.panel.start()
    
    # ==================== Step 3: 部署數據 ====================
    def step_3_deploy(self):
        self.load_config()
        if not self.prepared_data:
            print("⚠️ 無預備數據,請先執行 Step 2")
            time.sleep(1)
            return
        
        self.panel.stop()
        ConsoleUI.clear_screen()
        ConsoleUI.show_cursor()
        
        print(f"\n🔍 [Step 3.1] 正在檢查 {len(self.selected_targets)} 個設備狀態...")
        
        # 準備每個設備的 SHA (不管在線離線，只要選中且有數據就準備)
        local_sha_cache = {}
        for tid in self.selected_targets:
            pid = self.config["mapping"][tid].get("play_id")
            data = self.prepared_data.get(pid)
            if data:
                sha = hashlib.sha256(data).digest().hex()[:16]
                local_sha_cache[tid] = sha
            else:
                local_sha_cache[tid] = None
        
        # 嘗試向所有目標發送查詢，不管狀態
        valid_tids = []
        for tid in self.selected_targets:
            if tid in self.slaves and local_sha_cache[tid]:
                node = self.slaves[tid]
                node["query_event"].clear()
                node["remote_sha"] = None
                valid_tids.append(tid)
                
        # 批量發送查詢
        self.send_pkt(valid_tids, 0x2005, {"path": "/data.bin"})
        
        tout = self.config.get("deploy_timeout", 120)
        print(f"⏳ 等待設備回報 (Timeout: {tout}s)...")
        start_wait = time.time()
        while time.time() - start_wait < tout:
            # 只要有一個還沒回報，就繼續等 (除非超時)
            # Fix: 不因為 socket 離線就中斷等待，因為可能只是心跳超時但 socket 還在
            # Fix: 使用 get() 避免 KeyError，如果設備徹底斷開(不在slaves中)則不再等待
            pending = []
            for t in valid_tids:
                node = self.slaves.get(t)
                if node and not node["query_event"].is_set():
                    pending.append(t)
            
            if not pending:
                break
            time.sleep(0.1)
        
        deploy_queue = []
        print(f"\n{'編號':<5} | {'設備ID':<15} | {'本地SHA':<16} | {'遠程SHA':<16} | {'狀態'}")
        print("-" * 75)
        
        for i, tid in enumerate(self.selected_targets):
            local_sha = local_sha_cache.get(tid)
            node = self.slaves.get(tid)
            
            if not node or not local_sha:
                print(f"[{i+1:02}]  {tid:15} | {'離線或無數據':^50}")
                continue
            
            remote_sha_bytes = node.get("remote_sha")
            remote_sha = remote_sha_bytes.hex()[:16] if remote_sha_bytes else "TIMEOUT"
            
            is_match = (local_sha == remote_sha)
            status = "✔ 匹配" if is_match else "✖ 不同"
            
            print(f"[{i+1:02}]  {tid:15} | {local_sha} | {remote_sha:16} | {status}")
            deploy_queue.append((tid, local_sha, remote_sha))
        
        if not deploy_queue:
            input("\n❌ 無可用設備,按 Enter 返回...")
            self.panel.start()
            return
        
        print("\n" + "-" * 75)
        choice = input("👉 輸入編號上傳 (例: 1,3,5) | 'a' 僅上傳不一致 | 'all' 全選: ").lower()
        
        final_targets = []
        if choice == 'all':
            final_targets = [item[0] for item in deploy_queue]
        elif choice == 'a':
            final_targets = [item[0] for item in deploy_queue if item[1] != item[2]]
        else:
            try:
                idxs = [int(x.strip()) - 1 for x in choice.split(',')]
                final_targets = [deploy_queue[i][0] for i in idxs if 0 <= i < len(deploy_queue)]
            except:
                print("❌ 輸入錯誤")
                self.panel.start()
                return
        
        if not final_targets:
            print("ℹ️ 無設備被選中")
            time.sleep(1)
            self.panel.start()
            return
        
        self.panel.start()
        
        for tid in self.selected_targets:
            if tid not in final_targets:
                self.panel.update_device(tid, status="待機", upload_progress=100)
            else:
                self.panel.update_device(tid, status="傳輸中", upload_progress=0)
        
        max_workers = self.config.get("max_workers", 50)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(self._deploy_to_single_slave, tid): tid for tid in final_targets}
            
            for future in futures:
                tid = futures[future]
                try:
                    future.result()
                    self.panel.update_device(tid, status="待機", upload_progress=100)
                except Exception as e:
                    self.panel.update_device(tid, status="錯誤", error_msg=str(e))
        
        time.sleep(2)
        print("\n✅ 部署完成")
    
    def _deploy_to_single_slave(self, tid):
        node = self.slaves.get(tid)
        pid = self.config["mapping"][tid].get("play_id")
        data = self.prepared_data.get(pid)
        
        if not node or data is None:
            raise Exception("無數據或離線")
        
        local_sha = hashlib.sha256(data).digest()
        target_path = "/data.bin"
        total_len = len(data)
        chunk_size = 1024 * 1
        
        start_time = time.time()
        last_t = time.perf_counter()
        last_done = 0
        speed_ema = 0.0
        send_speed_ema = 0.0
        ack_ms_ema = 0.0
        send_total = 0.0
        ack_total = 0.0
        
        self.panel.update_device(
            tid,
            uploaded_bytes=0,
            total_bytes=total_len,
            upload_start_time=start_time
        )
        
        self.send_pkt([tid], 0x2001, {
            "file_id": 1,
            "total_size": total_len,
            "chunk_size": chunk_size,
            "sha256": local_sha,
            "path": target_path
        })
        time.sleep(0.1)
        
        for off in range(0, total_len, chunk_size):
            chunk = data[off : off + chunk_size]
            
            node["ack_event"].clear()
            t_send0 = time.perf_counter()
            self.send_pkt([tid], 0x2002, {
                "file_id": 1,
                "offset": off,
                "data": chunk
            })
            t_send1 = time.perf_counter()
            
            t_ack0 = time.perf_counter()
            if not node["ack_event"].wait(timeout=5.0):
                raise Exception(f"Offset {off} 超時")
            t_ack1 = time.perf_counter()
            send_total += (t_send1 - t_send0)
            ack_total += (t_ack1 - t_ack0)
            
            done = off + len(chunk)
            now_t = time.perf_counter()
            dt = now_t - last_t
            if dt > 0:
                delta_kb = (done - last_done) / 1024
                inst = delta_kb / dt
                speed_ema = inst if speed_ema <= 0 else (speed_ema * 0.8 + inst * 0.2)
                dt_send = t_send1 - t_send0
                if dt_send > 0:
                    inst_tx = delta_kb / dt_send
                    send_speed_ema = inst_tx if send_speed_ema <= 0 else (send_speed_ema * 0.8 + inst_tx * 0.2)
                dt_ack = t_ack1 - t_ack0
                ack_ms = dt_ack * 1000.0
                ack_ms_ema = ack_ms if ack_ms_ema <= 0 else (ack_ms_ema * 0.8 + ack_ms * 0.2)
                last_t = now_t
                last_done = done
            speed = speed_ema
            progress = (done / total_len) * 100
            
            self.panel.update_device(
                tid,
                upload_progress=progress,
                upload_speed=speed,
                send_speed=send_speed_ema,
                ack_rtt_ms=ack_ms_ema,
                upload_send_time=send_total,
                upload_ack_time=ack_total,
                uploaded_bytes=done
            )
        
        self.send_pkt([tid], 0x2003, {"file_id": 1})
        
        self.config["mapping"][tid]["last_sha"] = local_sha.hex()
        self.save_config()
    
    # ==================== Step 7: RAM 測速 ====================
    def step_7_ram_speed_test(self):
        self.load_config()
        self.panel.stop()
        ConsoleUI.clear_screen()
        ConsoleUI.show_cursor()
        
        if not self.selected_targets:
            print("⚠️ 請先執行 Step 1 選擇設備")
            input("\n按 Enter 繼續...")
            self.panel.start()
            return

        print("\n🚀 [RAM Speed Test] 純 RAM 上傳測速 (Protocol)")
        print("   此模式會送 RAM_BENCH_* 指令，設備端只做 RAM 消耗/統計，不寫入檔案。")
        
        try:
            size_mb = float(input("\n👉 請輸入測試大小 (MB) [默認 2]: ").strip() or "2")
        except:
            size_mb = 2.0
            
        total_len = int(size_mb * 1024 * 1024)
        try:
            chunk_size = int(input("\n👉 Chunk Size (bytes) [默認 16384]: ").strip() or "16384")
        except:
            chunk_size = 16384
        if chunk_size <= 0:
            chunk_size = 16384
        if chunk_size > 65000:
            chunk_size = 65000
        try:
            mode = int(input("👉 Mode 0=discard 1=ring_copy 2=hub_copy [默認 2]: ").strip() or "2")
        except:
            mode = 2
        if mode not in (0, 1, 2):
            mode = 2
        try:
            ring_kb = int(input("👉 Param (mode1=Ring KB, mode2=Hub Buffers) [默認 8]: ").strip() or "8")
        except:
            ring_kb = 8
        if ring_kb < 0:
            ring_kb = 0
        
        print(f"\n📦 準備發送 {size_mb} MB 數據...")
        print(f"   Chunk Size: {chunk_size} bytes")
        
        # 使用與 Deploy 相同的邏輯，但目標是 0xFFFF
        self.panel.start()
        
        for tid in self.selected_targets:
            self.panel.update_device(tid, status="測速中", upload_progress=0)
            
        max_workers = self.config.get("max_workers", 50)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self._ram_test_single_slave, tid, total_len, chunk_size, mode, ring_kb): tid
                for tid in self.selected_targets
            }
            
            for future in futures:
                tid = futures[future]
                try:
                    future.result()
                    self.panel.update_device(tid, status="完成", upload_progress=100)
                except Exception as e:
                    self.panel.update_device(tid, status="錯誤", error_msg=str(e))
        
        time.sleep(1)
        print("\n✅ 測速完成")
        input("\n按 Enter 返回...")
        self.panel.start()

    # ==================== Step 8: Raw Stream Test ====================
    def step_8_raw_stream_test(self):
        self.load_config()
        self.panel.stop()
        ConsoleUI.clear_screen()
        ConsoleUI.show_cursor()
        
        if not self.selected_targets:
            print("⚠️ 請先執行 Step 1 選擇設備")
            input("\n按 Enter 繼續...")
            self.panel.start()
            return

        print("\n🚫 [Raw Stream Test] 已停用")
        print("   Raw Mode/Enter Raw Mode 已與目前 Slave 協議脫節，避免誤用造成連線混亂。")
        input("\n按 Enter 返回...")
        self.panel.start()
        return
        
        try:
            size_mb = float(input("\n👉 請輸入測試大小 (MB) [默認 5]: ").strip() or "5")
        except:
            size_mb = 5.0
            
        total_len = int(size_mb * 1024 * 1024)
        
        print(f"\n📦 準備發送 {size_mb} MB 數據...")
        
        self.panel.start()
        
        for tid in self.selected_targets:
            self.panel.update_device(tid, status="Raw測速", upload_progress=0)
            
        max_workers = self.config.get("max_workers", 50)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self._raw_test_single_slave, tid, total_len): tid 
                for tid in self.selected_targets
            }
            
            for future in futures:
                tid = futures[future]
                try:
                    future.result()
                    self.panel.update_device(tid, status="完成", upload_progress=100)
                except Exception as e:
                    self.panel.update_device(tid, status="錯誤", error_msg=str(e))
        
        time.sleep(1)
        print("\n✅ 測速完成")
        input("\n按 Enter 返回...")
        self.panel.start()

    def _raw_test_single_slave(self, tid, total_len):
        node = self.slaves.get(tid)
        if not node: raise Exception("設備離線")
            
        start_time = time.time()
        conn = node["conn"]
        
        self.panel.update_device(
            tid,
            uploaded_bytes=0,
            total_bytes=total_len,
            upload_start_time=start_time
        )
        
        # 1. Send ENTER RAW MODE (0x2007)
        self.send_pkt([tid], 0x2007, {
            "file_id": 0xFFFF,
            "total_size": total_len,
            "path": "raw_stream_test"
        })
        
        # Give slave a moment to switch context
        time.sleep(0.1)
        
        # 2. Send Raw Bytes
        chunk_size = 65536
        sent = 0
        dummy_chunk = b'\xAA' * chunk_size
        
        while sent < total_len:
            rem = total_len - sent
            if rem < chunk_size:
                conn.sendall(dummy_chunk[:rem])
                sent += rem
            else:
                conn.sendall(dummy_chunk)
                sent += chunk_size
            
            # Update UI
            elapsed = time.time() - start_time
            speed = (sent / 1024) / elapsed if elapsed > 0 else 0
            progress = (sent / total_len) * 100
            self.panel.update_device(
                tid, 
                upload_progress=progress, 
                upload_speed=speed, 
                uploaded_bytes=sent
            )
            
        # 3. Send End (NetBus Protocol)
        time.sleep(0.1) 
        self.send_pkt([tid], 0x2003, {"file_id": 0xFFFF})

    def _ram_test_single_slave(self, tid, total_len, chunk_size, mode, ring_kb):
        node = self.slaves.get(tid)
        if not node:
            raise Exception("設備離線")

        start_time = time.time()
        last_t = time.perf_counter()
        last_sent = 0
        speed_ema = 0.0
        send_total = 0.0
        run_id = int(time.time() * 1000) & 0xFFFF
        node["ram_run_id"] = run_id
        node["ram_report"] = None
        node["ram_event"].clear()
        
        self.panel.update_device(
            tid,
            uploaded_bytes=0,
            total_bytes=total_len,
            upload_start_time=start_time
        )
        
        self.send_pkt([tid], 0x1811, {
            "run_id": run_id,
            "total_size": total_len,
            "chunk_size": chunk_size,
            "mode": mode,
            "ring_kb": ring_kb
        })
        time.sleep(0.1)
        
        sent = 0
        seq = 0
        dummy_chunk = b"\xAA" * chunk_size
        while sent < total_len:
            rem = total_len - sent
            chunk = dummy_chunk if rem >= chunk_size else dummy_chunk[:rem]
            t_send0 = time.perf_counter()
            self.send_pkt([tid], 0x1812, {
                "run_id": run_id,
                "seq": seq,
                "data": chunk
            })
            t_send1 = time.perf_counter()
            send_total += (t_send1 - t_send0)
            seq += 1
            sent += len(chunk)

            now_t = time.perf_counter()
            dt = now_t - last_t
            if dt > 0:
                inst = ((sent - last_sent) / 1024) / dt
                speed_ema = inst if speed_ema <= 0 else (speed_ema * 0.8 + inst * 0.2)
                last_t = now_t
                last_sent = sent
            speed = speed_ema
            progress = (sent / total_len) * 100 if total_len > 0 else 0
            
            self.panel.update_device(
                tid,
                upload_progress=progress,
                upload_speed=speed,
                send_speed=speed,
                ack_rtt_ms=0.0,
                upload_send_time=send_total,
                upload_ack_time=0.0,
                uploaded_bytes=sent
            )
            
        self.send_pkt([tid], 0x1813, {"run_id": run_id})
        if node["ram_event"].wait(timeout=5.0):
            rep = node.get("ram_report") or {}
            mb_s = (float(rep.get("mb_s_x1000", 0)) / 1000.0) if rep else 0.0
            print(f"[RAM_BENCH] {tid} run={run_id} {mb_s:.2f} MB/s")

    # ==================== Step 4: 同步播放 (修復音訊) ====================
    def step_4_sync_play(self):
        self.load_config()  # Reload config
        global mixer  # 使用全局 mixer 變量
        
        self.panel.stop()
        ConsoleUI.clear_screen()
        ConsoleUI.show_cursor()
        
        if not self.selected_targets:
            print("⚠️ 請先執行 Step 1")
            input("\n按 Enter 繼續...")
            self.panel.start()
            return
        
        # 選擇播放模式
        print("\n🎛️ [播放模式選擇]")
        print("  1. 本地文件播放 (Local File)")
        print("  2. 即時串流播放 (Live Stream) - RAM Only")
        print("  3. 基準測試 (Benchmark) - RAM Only")
        
        try:
            mode_choice = input("\n👉 請選擇模式 (默認 1): ").strip()
        except:
            mode_choice = "1"
            
        play_mode = "local"
        stream_path = ""
        
        if mode_choice == "2":
            play_mode = "stream"
            stream_path = input("   輸入 Stream Hub 名稱 (默認 pixel_stream): ").strip()
            if not stream_path: stream_path = "pixel_stream"
        elif mode_choice == "3":
            play_mode = "benchmark"
            stream_path = "benchmark_test"
        else:
            play_mode = "local"
        
        # 如果是本地文件模式，檢查音訊
        if play_mode == "local":
            if AUDIO_MODE is None:
                print("❌ 音訊模塊未安裝,無法播放")
                input("\n按 Enter 繼續...")
                self.panel.start()
                return
            
            mp3_files = [f for f in os.listdir('.') if f.endswith('.mp3')]
            if not mp3_files:
                print("❌ 找不到 MP3 文件 (可選)")
            
            print(f"\n🎵 [音訊準備] 模式: {AUDIO_MODE}")
            print(f"  0. 不播放音訊 (僅觸發動畫)")
            for i, f in enumerate(mp3_files):
                print(f"  {i+1}. {f}")
            print("  q. 取消返回")
            
            selected_mp3 = None
            try:
                raw_choice = input("\n👉 選擇編號: ").strip().lower()
                if raw_choice == 'q':
                    self.panel.start()
                    return
                
                choice = int(raw_choice)
                if choice == 0:
                    selected_mp3 = None
                    print("✅ 已選擇: 靜音模式")
                elif 1 <= choice <= len(mp3_files):
                    selected_mp3 = mp3_files[choice-1]
                    print(f"✅ 已選擇: {selected_mp3}")
                else:
                    print("❌ 選擇無效")
                    time.sleep(1)
                    self.panel.start()
                    return
            except ValueError:
                print("❌ 輸入無效")
                time.sleep(1)
                self.panel.start()
                return
        else:
            # Stream/Benchmark 模式通常不配音訊，或者由 PC 端推流
            selected_mp3 = None
            print(f"✅ 進入 {play_mode} 模式 (Target: {stream_path})")
        
        print(f"\n⚙️ 正在預備設備...")
        
        for tid in self.selected_targets:
            self.panel.update_device(tid, status="待機")
            if tid in self.panel.monitors:
                self.panel.monitors[tid].reset_play_stats()
        
        # 根據模式發送預備指令
        if play_mode == "local":
            self.send_pkt(self.selected_targets, 0x3009, {
                "file_name": "data.bin",
                "block_id": 0,
                "play_mode": 0
            })
        else:
            # Stream / Benchmark 模式：發送特殊 FILE_BEGIN (0xFFFF)
            # 這會觸發 Slave 端建立 AtomicStreamHub 並進入 Turbo/Stream 模式
            # 注意：這裡只是"建立管道"，實際數據需要另外推送 (Stream) 或者 Slave 自己產生 (Benchmark)?
            # 根據之前的修改，Slave 收到 0xFFFF + path="benchmark" 會進入 Turbo Mode 並等待數據
            # 如果是 Benchmark，我們可能需要 Master 瘋狂發送數據？
            # 或者 Slave 端的 Benchmark 是指 "接收並丟棄以測速"？
            # 根據之前的代碼，Slave 只是開啟了 Turbo Mode (不限速消費)，數據仍需從 Hub 讀取
            # 而 Hub 的數據需要 Master 推送 (0x2002)
            
            # 發送開啟指令
            self.send_pkt(self.selected_targets, 0x2001, {
                "file_id": 0xFFFF,
                "total_size": 0,
                "chunk_size": 0,
                "sha256": b'\x00'*32,
                "path": stream_path
            })
            
            # 如果是 Benchmark，我們需要準備一個發送線程
            if play_mode == "benchmark":
                print("🚀 Benchmark 模式：將循環發送隨機數據以測試頻寬...")
        
        while True:
            print("\n" + "!" * 50)
            print("     系統就緒,等待擊發")
            print(f"     延遲設定: {self.config.get('sync_delay_ms', 0)} ms")
            print("     輸入 'go' 開始 | 't' 微調延遲 | 'q' 取消")
            print("!" * 50)
            
            trigger = input("\n🚀 指令: ").lower().strip()
            
            if trigger == 'go':
                break
            elif trigger == 'q':
                # 如果是 Stream 模式，發送結束指令
                if play_mode != "local":
                    self.send_pkt(self.selected_targets, 0x2003, {"file_id": 0xFFFF})
                
                print("🛑 已取消")
                time.sleep(1)
                self.panel.start()
                return
            elif trigger == 't':
                try:
                    curr = self.config.get("sync_delay_ms", 150)
                    new_val = input(f"👉 輸入新延遲 (當前 {curr}ms): ").strip()
                    if new_val:
                        self.config["sync_delay_ms"] = int(new_val)
                        self.save_config()
                        print(f"✅ 延遲已更新為: {self.config['sync_delay_ms']} ms")
                except ValueError:
                    print("❌ 輸入無效")
            else:
                print("❌ 指令無效")

        for tid in self.selected_targets:
            self.panel.update_device(tid, status="播放中")
        
        self.panel.start(interactive=True)
        
        # 啟動 Benchmark 發送線程 (如果是 Benchmark 模式)
        benchmark_running = False
        if play_mode == "benchmark":
            benchmark_running = True
            threading.Thread(target=self._benchmark_sender_task, args=(stream_path,), daemon=True).start()
        
        delay_ms = self.config.get("sync_delay_ms", 150)
        delay_sec = abs(delay_ms) / 1000.0
        
        if play_mode == "local":
            if selected_mp3:
                if delay_ms >= 0:
                    self._start_audio_stream(selected_mp3)
                    if delay_ms > 0:
                        time.sleep(delay_sec)
                    self.send_pkt(self.selected_targets, 0x300A, {})
                else:
                    self.send_pkt(self.selected_targets, 0x300A, {})
                    time.sleep(delay_sec)
                    self._start_audio_stream(selected_mp3)
            else:
                # Silent mode: just trigger
                self.is_playing = True # Enable loop
                self.send_pkt(self.selected_targets, 0x300A, {})
        else:
            # Stream / Benchmark 模式無需 0x300A (Play)，因為 0x2001 已經啟動了 Engine
            self.is_playing = True
        
        # print("\n[控制提示] SPACE=暫停/繼續 | S=停止 | Q=退出") # 移除此行，因為 MonitorPanel 已經顯示了控制提示，且此行會導致 UI 錯亂
        
        # 進入 Raw 模式 (持續禁用回顯與行緩衝)
        input_handler.enter_raw_mode()
        input_handler.flush_input()
        
        try:
            while self.is_playing:
                # 檢測按鍵輸入 (非阻塞)
                if input_handler.kbhit():
                    try:
                        # 使用 getch 讀取按鍵
                        key = input_handler.getch()
                        
                        # 處理字節類型
                        if isinstance(key, bytes):
                            key = key.decode('utf-8', errors='ignore')
                        
                        key = key.lower()
                        
                        if key == ' ':
                            if play_mode == "local":
                                with self.play_lock:
                                    self.is_paused = not self.is_paused
                                    if self.is_paused:
                                        self.send_pkt(self.selected_targets, 0x3003, {})
                                        for tid in self.selected_targets:
                                            self.panel.update_device(tid, status="暫停")
                                    else:
                                        self.send_pkt(self.selected_targets, 0x3004, {})
                                        for tid in self.selected_targets:
                                            self.panel.update_device(tid, status="播放中")
                            else:
                                # Stream 模式暫停邏輯 (可選實現)
                                pass
                        
                        elif key == 's':
                            self.stop_all(play_mode)
                            break
                        
                        elif key == 'q':
                            self.stop_all(play_mode)
                            break
                        
                        elif key == '\x03': # Ctrl+C
                             self.stop_all(play_mode)
                             break
                             
                    except Exception:
                        pass
                
                time.sleep(0.05)
        finally:
            # 停止 Benchmark
            if play_mode == "benchmark":
                self.benchmark_running = False # 標記停止
                # 發送結束包
                self.send_pkt(self.selected_targets, 0x2003, {"file_id": 0xFFFF})

            # 確保退出播放循環時恢復原始模式
            input_handler.exit_raw_mode()
        
        for tid in self.selected_targets:
            self.panel.update_device(tid, status="待機")
        
        time.sleep(1)
    
    def _benchmark_sender_task(self, stream_path):
        """Benchmark 模式：全速發送垃圾數據"""
        self.benchmark_running = True
        # 構造一個假數據包 (使用 65000 大包以測極限)
        chunk_size = 65000
        dummy_data = b'\xAA' * chunk_size
        file_id = 0xFFFF
        offset = 0
        
        print(f"\n🚀 Benchmark Sender Started (Target: {stream_path}, Chunk: {chunk_size})")
        
        while self.benchmark_running and self.running:
            # 發送數據塊
            # 注意：這裡不等待 ACK，全速發送 (UDP/WS Flow Control 會處理)
            # 為了測試極限，我們可以使用 0x2002
            # 為了避免 offset 溢出，可以循環使用
            
            self.send_pkt(self.selected_targets, 0x2002, {
                "file_id": file_id,
                "offset": offset,
                "data": dummy_data
            })
            
            offset = (offset + chunk_size) % (1024 * 1024 * 100) # 100MB wrap
            
            # 控制發送速率以免撐爆 Master 端的 Buffer
            # time.sleep(0.001) # 約 1MB/s per device
            # 如果要測極限，可以不 sleep，但要注意 Python GIL 和 Network IO
            time.sleep(0.0001) 
    
    def _start_audio_stream(self, file_path):
        """啟動音訊流 (修復版)"""
        global mixer
        self.is_playing = True
        self.is_paused = False
        
        def _play_task():
            try:
                if AUDIO_MODE == 'miniaudio':
                    with miniaudio.PlaybackDevice() as device:
                        stream = miniaudio.stream_file(file_path)
                        device.start(stream)
                        
                        while device.is_active and self.running and self.is_playing:
                            while self.is_paused and self.is_playing:
                                time.sleep(0.1)
                            time.sleep(0.1)
                        
                        device.stop()
                
                elif AUDIO_MODE == 'pygame' and mixer:
                    # 🔧 確保 mixer 已初始化
                    if not mixer.get_init():
                        mixer.init()
                    
                    mixer.music.load(file_path)
                    mixer.music.play()
                    
                    while mixer.music.get_busy() and self.running and self.is_playing:
                        if self.is_paused:
                            mixer.music.pause()
                            while self.is_paused and self.is_playing:
                                time.sleep(0.1)
                            mixer.music.unpause()
                        
                        time.sleep(0.1)
                    
                    mixer.music.stop()
            
            except Exception as e:
                print(f"\n[Audio Error] {e}")
                import traceback
                traceback.print_exc()
            
            finally:
                self.is_playing = False
        
        threading.Thread(target=_play_task, daemon=True).start()
    
    def stop_all(self, play_mode="local"):
        global mixer
        self.is_playing = False
        self.is_paused = False
        self.benchmark_running = False # Stop benchmark thread
        
        if self.selected_targets:
            if play_mode == "local":
                self.send_pkt(self.selected_targets, 0x3002, {})
            else:
                self.send_pkt(self.selected_targets, 0x2003, {"file_id": 0xFFFF})
            
            for tid in self.selected_targets:
                self.panel.update_device(tid, status="待機")
        
        if AUDIO_MODE == 'pygame' and mixer:
            try:
                if mixer.get_init():
                    mixer.music.stop()
            except:
                pass
    
    def _print_menu(self):
        self.panel.stop()
        ConsoleUI.clear_screen()
        ConsoleUI.show_cursor()
        
        print("\n" + "=" * 60)
        print(" 🎬 NetBus Master Control Panel")
        print("=" * 60)
        online, offline = self.device_manager.get_counts()
        print(" 1. Scan Devices       | 掃描設備 (廣播)")
        print(" 2. Select Devices     | 選擇目標設備 (已選/總數: {}/{})".format(len(self.selected_targets), online))
        print(" 3. Clear List         | 清除設備列表 (離線: {})".format(offline))
        print(" ----------------------------------------")
        print(" 4. Slice Animation    | 切分動畫數據")
        print(" 5. Deploy Data        | 部署到設備 (Flash)")
        print(" 6. Sync Play          | 同步播放 (支持暫停)")
        print(" 7. RAM Speed Test     | 純 RAM 上傳測速 (Benchmark)")
        print(" 8. Raw Stream Test    | 極限 Raw Socket 測速 (No Protocol)")
        print(" 9. Set Decode Core    | 設定解碼 CPU 核心 (0/1)")
        print(" s. STOP ALL           | 緊急停止")
        print(" q. Exit               | 退出程序")
        print("=" * 60)
        # 提示符會在 input_with_refresh 中處理，這裡不打印

    def input_with_refresh(self, prompt):
        """
        帶自動刷新的輸入函數 (已棄用)
        - 模擬標準 input() 行為 (支持回顯、Backspace、Enter確認)
        - 等待期間若設備狀態變化，自動重繪界面並恢復輸入緩衝區
        """
        # 由於兼容性問題，直接調用標準 input
        return input(prompt)

    def step_9_set_decode_core(self):
        """設定解碼 CPU 核心"""
        self.load_config()
        self.panel.stop()
        ConsoleUI.clear_screen()
        ConsoleUI.show_cursor()
        
        if not self.selected_targets:
            print("⚠️ 請先執行 Step 1 選擇設備")
            input("\n按 Enter 繼續...")
            self.panel.start()
            return
            
        print("\n⚙️ [Set Decode Core] 設定解碼 CPU")
        print("   Core 0: 網絡 IO + 解碼 (默認)")
        print("   Core 1: 渲染 + 解碼 (分擔 Core 0 壓力)")
        
        try:
            core = int(input("\n👉 請輸入 Core ID (0/1): ").strip())
            if core not in (0, 1): raise ValueError
        except:
            print("❌ 輸入無效，使用默認 Core 0")
            core = 0
            
        print(f"\n📡 發送設定: Decode Core -> {core}")
        self.send_pkt(self.selected_targets, 0x1005, {"core": core})
        
        time.sleep(0.5)
        print("✅ 指令已發送")
        input("\n按 Enter 返回...")
        self.panel.start()

    def main_loop(self):
        self._print_menu()
        
        while self.running:
            try:
                ch = self.input_with_refresh("\n👉 請選擇操作: ").lower().strip()
            except EOFError:
                break
            
            if not ch: continue
                
            if ch == '1':
                self.scan_devices()
                self._print_menu()
            elif ch == '2':
                self.select_devices()
                self._print_menu()
            elif ch == '3':
                self.clear_device_list()
                self._print_menu()
            elif ch == '4':
                self.step_2_prepare_data()
                self._print_menu()
            elif ch == '5':
                self.step_3_deploy()
                self._print_menu()
            elif ch == '6':
                self.step_4_sync_play()
                self._print_menu()
            elif ch == '7':
                self.step_7_ram_speed_test()
                self._print_menu()
            elif ch == '8':
                self.step_8_raw_stream_test()
                self._print_menu()
            elif ch == '9':
                self.step_9_set_decode_core()
                self._print_menu()
            elif ch == 's':
                self.stop_all()
                print("✅ 已發送停止信號")
                time.sleep(1)
                self._print_menu()
            elif ch == 'q':
                self.stop_all()
                self.running = False
                break
        
        self.panel.stop()
        ConsoleUI.show_cursor()


if __name__ == "__main__":
    app = NetBusMaster()
    try:
        app.main_loop()
    except KeyboardInterrupt:
        print("\n\n🛑 用戶中斷")
    finally:
        app.panel.stop()
        ConsoleUI.show_cursor()
        print("\n再見! 👋")
