import socket
import time
import threading
import os, sys
import hashlib
import struct
import json
import copy
import csv
import shutil
import subprocess
import errno
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
    "max_workers": 50,
    "download_chunk_size": 1024 *2,
    "download_chunk_min": 1024,
    "upload_chunk_size": 4096,
    "upload_ack_timeout": 5.0,
    "upload_begin_timeout": 5.0,
    "upload_validation_timeout": 30.0,
    "download_read_timeout": 5.0,
    # 🔧 傳輸重試: block/chunk 級重試次數 (逾時重發同一個區塊, 之後檔案級重試)
    "transfer_retry_count": 3,
    # 🔧 新增: 延遲量測 / 中途加入 / 循環播放
    "latency_samples": 5,                 # 每次量測的 ping 次數
    "latency_log_file": "latency_log.csv", # 延遲測試紀錄檔
    "loop_play": 0,                       # 1 = 循環播放 (slave play_mode=1, 播完自動重頭)
    # 🔧 主動同步幀率 (0x3001 STREAM_INFO, 現有指令): 播放期間定時廣播設定 fps
    "active_sync_fps": 0,                 # 0 = 關閉 (被動同步); >0 = 定時廣播此 fps 給所有目標
    "active_sync_interval_s": 10.0,       # 廣播間隔 (秒)
    # 🔧 播放會話 / 離線自癒: 播放期間定期向 slave 查詢播放進度 (0x1101→0x1102)
    "progress_poll_interval_s": 1.0,      # 進度輪詢間隔 (秒)
    # 🔧 播放完全自然結束後 (非循環音檔播完), 延遲多少秒才送 0x3002 停止指令
    #    (slave 端會在檔尾保持最後一幀亮著, 這段延遲 = 最後姿勢定格時間)
    "post_play_stop_delay_s": 10.0,
    # 🔧 中途加入 (mid-join) 的 prepare/READY 握手重試次數
    "join_retry_count": 3
}

# ==================== 垃圾檔過濾 (Python 快取 / macOS / Windows / 編輯器暫存) ====================
JUNK_DIR_NAMES = {
    "__pycache__",                 # Python bytecode cache
    "__MACOSX",                    # macOS 解壓縮產物
    ".Spotlight-V100",             # macOS Spotlight
    ".Trashes",                    # macOS 垃圾桶
    ".fseventsd",                  # macOS 檔案系統事件
    "$RECYCLE.BIN",                # Windows 資源回收桶
    "System Volume Information",   # Windows 系統還原
}
JUNK_FILE_NAMES = {
    ".DS_Store",                   # macOS Finder 元資料
    "Thumbs.db",                   # Windows 縮圖快取
    "ehthumbs.db",                 # Windows 縮圖快取
    "desktop.ini",                 # Windows 資料夾自訂
}
JUNK_FILE_PREFIXES = ("._", "~$")          # AppleDouble / MS Office 暫存
JUNK_FILE_SUFFIXES = (".pyc", ".pyo", ".swp", ".swo", ".tmp")

def is_junk_dir(name):
    """判斷目錄名是否為常見垃圾目錄。"""
    return name in JUNK_DIR_NAMES

def is_junk_name(name):
    """判斷檔名是否為常見垃圾檔 (Python 快取 / macOS / Windows / 編輯器暫存)。"""
    if name in JUNK_FILE_NAMES:
        return True
    if name.startswith(JUNK_FILE_PREFIXES):
        return True
    return name.endswith(JUNK_FILE_SUFFIXES)

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
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.chdir(SCRIPT_DIR)

# ==================== 專案虛擬環境 (依賴統一裝在 .venv) ====================
# 不裝在系統環境: macOS Homebrew Python 受 PEP 668 保護, 系統 pip 會被拒且容易弄壞;
# 一律用專案自己的 venv, Windows / macOS 通用。venv 建立後主程式自動切換執行。
VENV_DIR = os.path.join(SCRIPT_DIR, ".venv")


def _venv_python():
    """回傳 venv 的 python 執行檔路徑 (Windows: Scripts/python.exe, 其他: bin/python)。"""
    if os.name == "nt":
        return os.path.join(VENV_DIR, "Scripts", "python.exe")
    return os.path.join(VENV_DIR, "bin", "python")


def _system_python():
    """回傳「適合拿來建 venv 的系統 python」。

    這個環境的 Homebrew python (3.11/3.12) 在 macOS 26 上有 pip truststore bug,
    連 python -m venv 都會失敗; 用 macOS 系統自帶的 /usr/bin/python3 (3.9, 老 pip
    不踩 truststore) 建 venv 最穩。Windows 一律用目前執行中的 python。
    """
    if os.name != "nt" and os.path.isfile("/usr/bin/python3"):
        return "/usr/bin/python3"
    return sys.executable


def _auto_switch_to_venv():
    """若 .venv 已建立且目前不是用它跑, os.execv 切換到 venv python (取代行程)。"""
    venv_py = _venv_python()
    if not os.path.isfile(venv_py):
        return
    if os.path.realpath(sys.executable) == os.path.realpath(venv_py):
        return
    print(f"🔁 偵測到專案虛擬環境，切換執行: {venv_py}")
    os.execv(venv_py, [venv_py] + sys.argv)

# 🔧 輔助檔案集中存放:
#   slave_map.json 與本程式同目錄 (tools/PC/); 其餘輔助檔案 (log/下載/profile) 放 data/
CONFIG_PATH = os.path.join(SCRIPT_DIR, "slave_map.json")
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
LOG_DIR = os.path.join(DATA_DIR, "logs")
DOWNLOAD_DIR = os.path.join(DATA_DIR, "downloads")
PROFILE_DIR = os.path.join(DATA_DIR, "profiles")
BINS_DIR = os.path.join(DATA_DIR, "bins")
for _d in (DATA_DIR, LOG_DIR, DOWNLOAD_DIR, PROFILE_DIR, BINS_DIR):
    os.makedirs(_d, exist_ok=True)

# ==================== 協議層導入 ====================
try:
    from slave.lib.sys.proto import Proto, StreamParser
    from slave.lib.sys.schema_loader import SchemaStore
    from slave.lib.sys.schema_codec import SchemaCodec
    from tools.PC.PXLDv3Splitter import PXLDv3Decoder
except ImportError as e:
    print(f"❌ 導入錯誤: {e}")
    sys.exit(1)


# ==================== WS 二元幀重組器 ====================
class WSFrameAssembler:
    """WebSocket 二元幀重組 (master 端接收, 解決 TCP 切分/合併)。

    問題: conn.recv() 不保證每次回一個完整 WS frame — 大封包 (FILE_CHUNK 2048B
    → frame ~2071B) 在 Wi-Fi 不穩時會被切段; 舊程式假設「每次 recv 都從 0x82
    開頭」, 切點落在資料中間且下段首 byte 恰為 0x82 時會誤判 header, 吃掉資料
    開頭 → NC4 CRC 失敗 → 回應遺失 → 下載逾時 (「第二次下載總是卡住」)。

    本類維護跨 recv 的 header/payload 狀態, 組出完整 frame 才 yield 給 NC4 parser。
    只處理 binary frame (0x82, 未 mask — 與 slave 端一致)。
    """
    def __init__(self):
        self._hdr = bytearray(14)
        self._hdr_len = 0
        self._need = 0       # 尚需 payload 位元組數
        self._pay = bytearray()

    def feed(self, data):
        """餵入 TCP 資料, yield 完整的 WS binary frame payload (bytes)。"""
        mv = memoryview(data)
        n = len(mv)
        i = 0
        while i < n:
            if self._need <= 0:
                # ── 組 2-byte header ──
                if self._hdr_len < 2:
                    take = min(2 - self._hdr_len, n - i)
                    self._hdr[self._hdr_len:self._hdr_len + take] = mv[i:i + take]
                    self._hdr_len += take
                    i += take
                    if self._hdr_len < 2:
                        return
                b0 = self._hdr[0]
                b1 = self._hdr[1]
                if b0 != 0x82:
                    # 非 binary frame → 重同步: 丟棄 1 byte 再試
                    self._hdr[0] = self._hdr[1]
                    self._hdr_len = 1
                    continue
                plen7 = b1 & 0x7F
                ext_len = 2 if plen7 == 126 else (8 if plen7 == 127 else 0)
                need_hdr = 2 + ext_len
                if self._hdr_len < need_hdr:
                    take = min(need_hdr - self._hdr_len, n - i)
                    self._hdr[self._hdr_len:self._hdr_len + take] = mv[i:i + take]
                    self._hdr_len += take
                    i += take
                    if self._hdr_len < need_hdr:
                        return
                if plen7 == 126:
                    pay_len = (self._hdr[2] << 8) | self._hdr[3]
                elif plen7 == 127:
                    pay_len = 0
                    for k in range(8):
                        pay_len = (pay_len << 8) | self._hdr[4 + k]
                else:
                    pay_len = plen7
                self._hdr_len = 0
                # 未 mask (b1 & 0x80 == 0); 若被 mask 就多收 4 bytes 並跳過
                self._need = pay_len + (4 if (b1 & 0x80) else 0)
                self._pay = bytearray()
            # ── 收 payload ──
            take = self._need
            avail = n - i
            if take > avail:
                take = avail
            if take <= 0:
                break
            self._pay.extend(mv[i:i + take])
            i += take
            self._need -= take
            if self._need <= 0:
                pay = bytes(self._pay)
                yield pay
                self._pay = bytearray()


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
        self.transfer_label = ""
        
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
        self.controls_text = None
        # 🔧 統一 log 出口: 後台執行緒的通知統一進這裡, 面板分區渲染 (不洗掉設備表)
        self.log_buffer = deque(maxlen=200)   # [(ts, level, msg), ...]
        self.log_lock = threading.Lock()

    def log(self, level, msg):
        """把後台通知導進面板 log 區 (非阻塞, 不再直接 print 到 stdout 打亂畫面)。

        level: "info" | "ok" | "warn" | "err" (對應面板著色)。
        面板「沒在跑」(選單/啟動階段) 時 fallback 到 print, 讓通知仍看得到。
        此方法可被任何執行緒安全呼叫。
        """
        try:
            with self.log_lock:
                self.log_buffer.append((datetime.now().strftime("%H:%M:%S"), level, str(msg)))
        except Exception:
            pass
        if not self.running:
            try:
                print(str(msg))
            except Exception:
                pass

    def _drain_logs(self, limit=200):
        """取出目前 log 區 (副本), 供渲染使用。"""
        with self.log_lock:
            return list(self.log_buffer)[-limit:]


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
    
    def start(self, interactive=False, controls_text=None):
        self.controls_text = controls_text
        self.interactive_mode = interactive or bool(controls_text)
        if not self.running:
            self.running = True
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
            offline_n = sum(1 for m in self.monitors.values() if m.status in ("離線", "無響應"))
            _left = f"║  🎬 NetBus Master Monitor  │  Devices: {len(self.monitors)}  │  離線/無響應: {offline_n}  │  Time: {datetime.now().strftime('%H:%M:%S')}"
            subtitle = _left + " " * max(1, 118 - len(_left) - 1) + "║"
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
                controls = self.controls_text or "║  [SPACE] 暫停/繼續  │  [S] 停止  │  [Q] 退出                                                                  ║"
                buffer.append(controls)
            
            footer = "╚════════════════════════════════════════════════════════════════════════════════════════════════════════════════════╝"
            buffer.append(footer)

            # ── 📋 通知 / 日誌區 (固定高度, 不與設備表互相覆蓋) ──
            buffer.append("")
            buffer.append("┌──────────────────────────────────────────── 通知 / 日誌 ────────────────────────────────────────────┐")
            logs = self._drain_logs(24)
            if not logs:
                buffer.append("│  (無通知)                                                                                                          │")
            else:
                for ts, level, msg in logs:
                    color = {
                        "info": "\033[0m",
                        "ok": "\033[92m",
                        "warn": "\033[93m",
                        "err": "\033[91m",
                    }.get(level, "\033[0m")
                    line = f"{ts} {msg}"
                    # 去掉 ANSI 色碼後截斷到框內寬度 (純 ASCII 寬度近似)
                    plain = line
                    if len(plain) > 114:
                        plain = plain[:114]
                    buffer.append(f"│ {color}{plain}\033[0m")
            buffer.append("└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘")
            
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
            "上傳中": "\033[93m",
            "下載中": "\033[93m",
            "播放中": "\033[92m",
            "暫停": "\033[95m",
            "錯誤": "\033[91m",
            "無響應": "\033[31m", # 暗紅/紅色
            "中途加入": "\033[92m", # 🔧 中途加入 (同步播放)
            "配對中": "\033[94m",   # 🔧 配對模式
            "完成": "\033[92m",     # 🔧 傳輸/作業完成
            "已停止": "\033[90m",   # 🔧 使用者取消
            "播完": "\033[96m",     # 🔧 本次串流自然播完
            "重啟中": "\033[94m",   # 🔧 重啟設備等待回連
            "配置更新": "\033[92m"  # 🔧 配置已更新
        }
        status_color = status_colors.get(monitor.status, "\033[0m")
        if monitor.transfer_label:
            status_color = "\033[93m"
            
        status_disp = monitor.status
        if len(status_disp) > 6:
            status_disp = status_disp[:6]
        status_str = f"{status_color}{status_disp:<6}{ConsoleUI.reset_color()}"
        
        if monitor.status in ("傳輸中", "上傳中", "下載中") or monitor.transfer_label:
            progress_bar = ConsoleUI.draw_progress_bar(monitor.upload_progress, width=20)
            if monitor.ack_rtt_ms > 0:
                speed_str = f"{monitor.upload_speed:>6.1f} KB/s │ TX {monitor.send_speed:>6.1f} │ ACK {monitor.ack_rtt_ms:>5.1f}ms"
            else:
                speed_str = f"{monitor.upload_speed:>6.1f} KB/s"
            size_str = f"{monitor.uploaded_bytes//1024}/{monitor.total_bytes//1024} KB"
            info = f"{progress_bar} │ {speed_str} │ {size_str}"
            if monitor.transfer_label:
                info = f"{info} │ {monitor.transfer_label[:16]}"
        
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
        
        elif monitor.status == "錯誤":
            info = f"\033[91m{monitor.error_msg[:70]}\033[0m"
            
        elif monitor.status == "無響應":
            lost_time = int(time.time() - monitor.last_update)
            info = f"\033[31m無響應 {lost_time}s\033[0m"
        
        elif monitor.status == "中途加入":
            # 🔧 中途加入: 顯示正在對齊的幀
            frame_str = f"{monitor.current_frame}/{monitor.total_frames}"
            info = f"對齊中 │ Frame: {frame_str}"
        
        elif monitor.status == "配對中":
            # 🔧 配對模式: 播放本地燈效中
            info = "播放本地燈效,等待確認..."
        
        elif monitor.status == "播完":
            # 🔧 本次串流已自然播完 (非循環)
            frame_str = f"{monitor.current_frame}/{monitor.total_frames}"
            info = f"本次已播完 │ Frame: {frame_str}"
        
        elif monitor.status == "重啟中":
            # 🔧 韌體/配置更新後的重啟等待
            info = "重啟中, 等待重新連線..."
        
        elif monitor.status in ("完成", "配置更新"):
            # 🔧 傳輸/作業完成 (清除殘留的「閒置 Xs」顯示)
            info = f"✅ {monitor.status}"
        
        elif monitor.status == "已停止":
            info = "已停止 (使用者取消)"
        
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
    3. 連線狀態 = WS 通道本身 (recv 斷線/send 失敗 → 離線), 不做「無回應」定時健康檢查
       (設計原則: master 不主動頻繁發 health 檢查, 見 doc/03_notes/12_upload_wdt_diagnosis.md)
    4. 提供設備統計數據
    """
    def __init__(self, panel: MonitorPanel):
        self.panel = panel
        self.slaves = {}  # {device_id: {conn, addr, parser, ...}}
        self.lock = threading.Lock()

    def register_connection(self, cid, conn, addr, parser):
        """處理新連接/重連"""
        with self.lock:
            # 如果設備已存在，先清理舊連接
            if cid in self.slaves:
                old_node = self.slaves[cid]
                try:
                    self.panel.log("warn", f"🔄 [DeviceManager] 設備 {cid} 重連，關閉舊連接...")
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
                "ack_offset": -1,               # 🔧 最近一次 ACK 的 offset (錯位 ACK 防重複寫入)
                "query_event": threading.Event(),
                "read_event": threading.Event(),
                "ready_event": threading.Event(),   # 🔧 0x3008 STREAM_READY_ACK (中途加入等待)
                "ping_event": threading.Event(),    # 🔧 0x100B TIME_SYNC_RSP (延遲量測)
                "ping_t0": 0.0,
                "ping_lock": threading.Lock(),      # 🔧 串行化 ping (延遲量測併發防護)
                "ping_rtt": None,                   # 🔧 最近一次 RTT (ms)
                "ping_offset": None,                # 🔧 最近一次時鐘偏移 (slave-master, ms)
                "latency_ms": None,                 # 🔧 單向延遲估計 (min-RTT/2, ms)
                "clock_offset_ms": None,            # 🔧 min-RTT 樣本的時鐘偏移 (ms)
                "min_rtt_ms": None,                 # 🔧 本次量測最小 RTT (ms)
                "avg_rtt_ms": None,                 # 🔧 本次量測平均 RTT (ms)
                "mode_event": threading.Event(),    # 🔧 0x3102 MODE_LIST_RSP (配對模式)
                "mode_list": None,
                "mode_detail_event": threading.Event(),  # 🔧 0x3108 MODE_DETAIL_RSP
                "mode_detail": None,
                "status_event": threading.Event(),  # 🔧 0x1102 STATUS_RSP (Profile/狀態查詢)
                "status_data": None,
                "remote_exists": 0,
                "remote_sha": None,
                "remote_size": 0,
                "remote_pending": 0,                 # 🔧 FILE_QUERY_RSP 的 pending 旗標 (.bak 待確認)
                "partial_event": threading.Event(),  # 🔧 0x200F FILE_PARTIAL_RSP (斷點續傳查詢)
                "partial_partial": 0,                # 🔧 是否有斷點 (.tmp + delta.partial)
                "partial_written": 0,                # 🔧 已寫入位元組 (續傳起點)
                "partial_total": 0,                  # 🔧 續傳 session 的總大小
                "partial_sha": None,                 # 🔧 續傳 session 的 sha256
                "read_data": None,
                "read_offset": 0,
                "last_seen": time.time()  # 用於內部連接保活檢查
            }
            
            # 更新面板狀態
            self.panel.update_device(cid, status="待機")

    def unregister_connection(self, cid):
        """移除連接 (WS 通道斷線 = 離線; 不再有「無回應」定時判定)"""
        with self.lock:
            if cid in self.slaves:
                del self.slaves[cid]
            self.panel.remove_device(cid)
        self.panel.log("warn", f"📴 [Health] {cid} 離線 (WS 連線中斷)")

    def update_heartbeat(self, cid):
        """更新心跳時間"""
        if cid in self.slaves:
            self.slaves[cid]["last_seen"] = time.time()
            # 🔧 修復: 同步刷新 Monitor 的 last_update。
            # 之前只更新 slaves last_seen, monitor 沒更新 → 有流量卻被標「無響應」。
            self.panel.update_device(cid)

    def get_slave(self, cid):
        return self.slaves.get(cid)

    def get_all_slaves(self):
        return self.slaves

    # 🔧 已移除主動健康檢查 (_probe_device / _health_check_loop / 無響應判定):
    #    連線狀態以 WS 通道本身為準 —— handle_client 的 recv 收到 FIN/RST/錯誤
    #    (含 TCP keepalive 偵測到的半開連線) → finally → unregister_connection
    #    → 標離線; send_pkt 發送失敗也會主動關 socket 觸發同一條清理路徑。
    #    master 不主動頻繁發 0x100A/0x1101 health 檢查; 檢查連線是操作者手動
    #    執行的動作 (查狀態/量延遲), 或播放途中的進度輪詢 (0x1101) 自然附帶。
    #    要叫回離線設備: 選單 1 掃描/敲門 (手動)。見 doc/03_notes/12。

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


# ==================== NetBusMaster 主類 ====================
class NetBusMaster:
    def __init__(self, config_file=None):
        if config_file is None:
            # 配置檔與本程式同目錄 (tools/PC/slave_map.json)
            config_file = CONFIG_PATH
        self.store = SchemaStore(dir_path=f"{PROJECT_ROOT}/slave/schema")
        # 🔧 修復: 建立 dispatch/field 緩衝, 否則 decode 只會回 _name/_cmd
        # (0x1102 心跳、0x2002/0x2006 檔案傳輸、0x3102/0x3108 配對… 全部解不出欄位)
        self.store.finalize()
        self.panel = MonitorPanel()
        self.device_manager = DeviceManager(self.panel)
        self.slaves = self.device_manager.slaves  # 兼容舊代碼，指向 Manager 的字典
        
        self.running = True
        self.local_ip = self.get_local_ip()
        
        self.is_playing = False
        self.is_paused = False
        self.play_lock = threading.Lock()
        self.playback_start_time = 0
        self.paused_since = None     # 🔧 暫停起始時刻 (中途加入計算要扣掉暫停時間)
        self.paused_total = 0.0      # 🔧 本次會話累計暫停秒數
        self.current_fps = 40
        self.current_play_mode = 0   # 🔧 目前播放的 play_mode (0=一次, 1=循環), 中途加入沿用
        self._active_sync_stop = threading.Event()   # 🔧 主動同步幀率廣播執行緒
        self._active_sync_thread = None
        
        # 🔧 播放會話狀態 (離線重連自動續播 / 進度輪詢用)。
        # play_session_active: go 開始 → stop_all 結束; 音檔播完不等於會話結束
        # (循環播放時燈效仍繼續), 所以中途加入改用此旗標, 不再看 is_playing。
        self.play_session_active = False
        self.audio_finished = False
        self._dev_finished = set()        # 🔧 本次會話「已自然播完」的設備 (重連不再自動續播)
        self._stop_was_manual = False     # 🔧 本次播放是否由使用者手動停止 (s/q), 決定要不要補「延遲停止」
        self._dev_drift = {}              # 🔧 每台設備的「連續進度偏差」次數 (3 次 → SEEK 校正)
        self._progress_poll_stop = threading.Event()   # 🔧 播放進度輪詢執行緒
        self._progress_poll_thread = None
        
        self.config_file = config_file
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self._migrate_legacy_config()
        self._migrate_legacy_data()
        self.load_config()
        self.selected_targets = []
        self.prepared_data = {}
        self.pxld_metadata = {}
        self.transfer_cancel = threading.Event()
        self._transfer_kb_stop = threading.Event()
        self._transfer_kb_thread = None
        
        # 🔧 只載入 bins/metadata.json (總幀數等), 不載入大型 bin 資料 —
        # 讓工具重開後連上設備時面板就有正確的 total_frames (進度% 才能顯示)
        self._load_metadata_only()
        
        threading.Thread(target=self.start_ws_server, daemon=True).start()

    def _migrate_legacy_config(self):
        """把舊版 tools/slave_map.json 遷移到 tools/PC/slave_map.json (與本程式同目錄)。

        只在「新位置不存在、舊位置存在」時搬移一次; 舊檔保留不刪, 以免誤刪。
        """
        if os.path.exists(self.config_file):
            return
        legacy = os.path.join(SCRIPT_DIR, "..", "slave_map.json")
        if os.path.exists(legacy):
            try:
                shutil.copy2(legacy, self.config_file)
                print(f"📦 [Config] 已遷移舊設定: {legacy} → {self.config_file}")
            except Exception as e:
                print(f"⚠️ [Config] 遷移失敗: {e}")

    def _migrate_legacy_data(self):
        """把舊版 tools/PC/ 下的輔助檔案遷移到 data/ 集中存放。

        涵蓋 bins/ (切分動畫)、latency_log.csv、download/、profiles/;
        只在「目標不存在、來源存在」時搬移, 不覆蓋既有資料。
        """
        mig = [
            (os.path.join(SCRIPT_DIR, "bins"), BINS_DIR),
            (os.path.join(SCRIPT_DIR, "download"), DOWNLOAD_DIR),
            (os.path.join(SCRIPT_DIR, "profiles"), PROFILE_DIR),
        ]
        for src_dir, dst_dir in mig:
            if not os.path.isdir(src_dir):
                continue
            try:
                for name in os.listdir(src_dir):
                    s = os.path.join(src_dir, name)
                    d = os.path.join(dst_dir, name)
                    if not os.path.exists(d):
                        shutil.move(s, d)
            except Exception as e:
                print(f"⚠️ [Data] 遷移 {src_dir} 失敗: {e}")
        # 單一 CSV
        legacy_csv = os.path.join(SCRIPT_DIR, "latency_log.csv")
        if os.path.isfile(legacy_csv):
            dst_csv = os.path.join(LOG_DIR, "latency_log.csv")
            if not os.path.exists(dst_csv):
                try:
                    shutil.move(legacy_csv, dst_csv)
                except Exception as e:
                    print(f"⚠️ [Data] 遷移 latency_log.csv 失敗: {e}")

    def _log_event(self, level, message, device_id=""):
        """寫入輔助 log (data/logs/), 記錄關鍵事件 (上傳/下載/還原/確認 的成功與失敗)。

        每次啟動依日期分檔; 與延遲 CSV 分開, 這是純文字 log。
        """
        try:
            log_path = os.path.join(LOG_DIR, f"{datetime.now().strftime('%Y%m%d')}.log")
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            line = f"[{ts}] [{level}]"
            if device_id:
                line += f" [{device_id}]"
            line += f" {message}\n"
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

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
        """偵測本機 IP (給 slave 連回用的 ws_url)。

        公司內網常沒有網際網路, 8.8.8.8 連不到會回 127.0.0.1 → slave 會連錯;
        依序嘗試:
          1. UDP connect 8.8.8.8 (需能路由到網際網路)
          2. gethostbyname_ex(主機名) 取第一個非 127. 的 IP (內網可用)
          3. 回退 127.0.0.1
        """
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(('8.8.8.8', 80))
                ip = s.getsockname()[0]
                if ip and not ip.startswith('127.'):
                    return ip
            except Exception:
                pass
            finally:
                s.close()
        except Exception:
            pass
        try:
            hostname = socket.gethostname()
            for ip in socket.gethostbyname_ex(hostname)[2]:
                if ip and not ip.startswith('127.'):
                    return ip
        except Exception:
            pass
        return '127.0.0.1'
    
    def start_ws_server(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        port = self.config.get("ws_port", 8000)
        s.bind(('0.0.0.0', port))
        s.listen(20)
        print(f"[WS Server] 監聽 0.0.0.0:{port} ,IP: {self.local_ip}")
        
        while self.running:
            try:
                conn, addr = s.accept()
                threading.Thread(target=self.handle_client, args=(conn, addr), daemon=True).start()
            except:
                break
    
    def handle_client(self, conn, addr):
        cid = f"PENDING_{addr[1]}"

        # 🔧 TCP keepalive: 半開連線 (對面靜默消失, 無 FIN/RST) 時讓作業系統及早偵測,
        #    之後 recv 才會拋錯觸發清理, 而不是永久阻塞在 recv 上。
        #    這正是「判斷 WS 通道本身的連接狀態」——不靠應用層 ping/回應。
        try:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if hasattr(socket, "SIO_KEEPALIVE_VALS"):
                # Windows: SIO_KEEPALIVE_VALS = (enable, idle_ms, interval_ms)
                #   10s 無流量開始探測、每 3s 一次 → 對面消失 ~20s 內被偵測到。
                conn.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 10000, 3000))
            else:
                # macOS 用 TCP_KEEPALIVE、Linux 用 TCP_KEEPIDLE, 都吃秒數; 設 30s 縮短偵測週期
                for _opt in ("TCP_KEEPALIVE", "TCP_KEEPIDLE"):
                    if hasattr(socket, _opt):
                        conn.setsockopt(socket.IPPROTO_TCP, getattr(socket, _opt), 30)
                        break
        except Exception:
            pass

        try:
            # 🔧 循環讀 HTTP header 直到 \r\n\r\n (TCP 可能切段, 舊版只 recv 一次會
            #    在慢速 Wi-Fi 重連時拿到半截 header → 握手被誤判失敗)
            conn.settimeout(5.0)
            header_data = b""
            try:
                while b"\r\n\r\n" not in header_data and len(header_data) < 8192:
                    chunk = conn.recv(1024)
                    if not chunk:
                        break
                    header_data += chunk
            except socket.timeout:
                pass
            conn.settimeout(None)
            if not header_data or b"Upgrade: websocket" not in header_data:
                conn.close()
                return
            
            header_text = header_data.decode(errors="ignore")
            first_line = header_text.split('\r\n')[0]
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
                # 🔧 新設備自動建記錄的提示 (對方連接後自動更新到 slave_map.json)
                print(f"📝 [Mapping] 新設備 {cid} → play_id {new_pid} (已存入 {os.path.basename(self.config_file)})")
            
            play_id = self.config["mapping"][cid]["play_id"]
            total_frames = self.pxld_metadata.get(play_id, {}).get("total_frames", 0)
            
            self.panel.register_device(cid, play_id, total_frames)
            
            # 使用 DeviceManager 註冊連接 (自動處理重連)
            self.device_manager.register_connection(
                cid, conn, addr, StreamParser()
            )
            
            # 🔧 紀錄 slave 的 IP (DHCP 會變; 啟動時 master 要依此 IP 敲門握手)
            client_ip = addr[0] if addr and len(addr) > 0 else ""
            if client_ip and self.config["mapping"].get(cid, {}).get("ip") != client_ip:
                self.config["mapping"][cid]["ip"] = client_ip
                self.save_config()
                print(f"📝 [Mapping] {cid} IP 紀錄更新 → {client_ip}")
            
            # 🔧 上線打招呼: 敲門/掃描/主動重連連上都會顯示
            print(f"👋 [Connect] {cid} 已上線 (PlayID {play_id})")
            self.panel.log("ok", f"👋 [Connect] {cid} 已上線 (PlayID {play_id})")
            
            # --- Mid-Stream Join Logic (🔧 延遲補償 + READY 等待, 推算最準確幀號) ---
            # 🔧 改用 play_session_active (go→stop_all), 不再用 is_playing:
            #    音檔播完 is_playing 會被音訊執行緒設 False, 之後重連就永遠
            #    不觸發自動續播; 會話旗標只在使用者停止時才結束。
            if self.play_session_active:
                try:
                    # 異步執行加入流程，避免阻塞 recv 主循環
                    threading.Thread(target=self._mid_join_task, args=(cid,), daemon=True).start()
                except Exception as e:
                    self.panel.log("err", f"❌ Mid-Join logic error: {e}")
            # --- END Mid-Join Logic ---
            
            node = self.device_manager.get_slave(cid)
            if node is None:   # 🔧 極端 case: 剛註冊就被移除, 直接收尾
                return
            parser = node["parser"]
            ws_ass = WSFrameAssembler()  # 🔧 跨 recv 重組 WS frame (TCP 切分安全)
            while self.running:
                raw = conn.recv(4096)
                if not raw:
                    break
                
                # 🔧 修復: 不再假設每次 recv 都是完整 WS frame。
                # 重組後才餵給 NC4 parser, 解決大封包切段誤判 → 下載逾時。
                for frame in ws_ass.feed(raw):
                    parser.feed(frame)
                    for ver, addr_pkt, cmd, payload in parser.pop():
                        # 收到任何數據都視為心跳
                        self.device_manager.update_heartbeat(cid)
                        cid = self.dispatch_logic(cid, cmd, payload)
        
        except Exception as e:
            current_node = self.device_manager.get_slave(cid)
            if current_node and current_node["conn"] == conn:
                # 🔧 連線層中斷 (10053/10054/104/EPIPE) = 正常斷線, 交給 finally 標離線;
                #    只有真正的非連線例外才標「錯誤」。
                if isinstance(e, OSError) and (
                    isinstance(e, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError))
                    or getattr(e, "errno", None) in (errno.ECONNABORTED, errno.ECONNRESET, errno.EPIPE, errno.ENOTCONN)
                ):
                    print(f"ℹ️ 連線中斷 {addr} ({e})")
                else:
                    self.panel.update_device(cid, status="錯誤", error_msg=str(e))
            else:
                # 🔧 重連時 register_connection 已關閉舊 socket → 舊執行緒的 recv 拋
                #    [WinError 10053]; 新連線已接管, 不要再覆寫面板狀態。
                print(f"ℹ️ 舊連線 {addr} 結束 ({e}) — 已有新連線接管, 忽略")
        
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
    
    def _mid_join_task(self, target_cid, _attempt=0):
        """🔧 中途加入 (mid-join): 裝置離線後重連, 自動接回目前播放的「正確進度」。

        完整流程 (缺一不可):
          1. 量延遲 (幀號補償)
          2. 0x3009 準備 → 等 0x3008 READY (重試 join_retry_count 次)
          3. 依主控時鐘推算目標幀 (扣暫停時間; 循環取模; 非循環 clamp)
          4. 0x300A 帶幀號播放 + 0x3001 同步 fps (剛接回的設備用主控節拍)
          5. master 暫停中 → 補發 0x3005 同步暫停
          6. 🔧 接回後驗證: 輪詢確認 slave 真的有在播; 沒播 → 再救一輪;
             進度偏差過大 → 0x3004 SEEK 校正回正確進度

        修正重點 (舊版只做到第 4 步就停, 所以 Console 只看到「中途加入」):
          - 觸發條件改用 play_session_active (音檔結束 ≠ 會話結束)
          - ready_event 在 send 之前 clear; 每次嘗試重抓 node (防重連換 dict)
          - 接回後「驗證 + 校正」閉環, 保證按著正確進度播放
        """
        with self.play_lock:
            session = self.play_session_active
            finished = target_cid in self._dev_finished
            paused = self.is_paused
            play_mode = self.current_play_mode
        if not session:
            return
        if finished:
            self.panel.log("info", f"ℹ️ [Mid-Join] {target_cid} 本次會話已自然播完, 不自動續播")
            return

        self.panel.update_device(target_cid, status="中途加入")
        self.panel.log("info", f"🔗 [Mid-Join] {target_cid} 開始接回流程 (attempt {_attempt + 1})...")

        # 0. 量測此設備的單向延遲 (RTT/2), 供幀號補償
        lat_s = 0.0
        lat = self.measure_latency(target_cid, samples=3)
        if lat is not None:
            lat_s = lat / 1000.0
            self._log_latency([(target_cid, lat)], note="mid-join")
            self.panel.log("info", f"   📡 {target_cid} latency {lat:.1f}ms → 補償 {lat_s*1000:.0f}ms")

        # 非循環且主控已播到結尾 → 視為已播完, 不續播
        total = self._device_total_frames(target_cid)
        if play_mode == 0 and total > 0:
            done_frame = int((time.time() - self.playback_start_time) * self.current_fps)
            if done_frame >= total:
                self._dev_finished.add(target_cid)
                self.panel.update_device(target_cid, status="播完")
                self.panel.log("info", f"ℹ️ [Mid-Join] {target_cid} 非循環已播完 (frame {total}), 不續播")
                return

        attempts = max(1, self._cfg_int("join_retry_count", 3))

        # 1+2. 準備 → 等 READY (0x3008), 失敗重試
        node = None
        for attempt in range(1, attempts + 1):
            if not self.play_session_active:
                return
            node = self.device_manager.get_slave(target_cid)
            if node is None:
                return
            # 🔧 先 clear 再 send (修 race); node 每次重抓 (修重連換 dict 的 race)
            node["ready_event"].clear()
            self.send_pkt([target_cid], 0x3009, {
                "file_name": "data.bin",
                "block_id": 0,
                "play_mode": play_mode
            })
            if node["ready_event"].wait(timeout=5.0):
                break
            self.panel.log("warn", f"⚠️ {target_cid} READY timeout (attempt {attempt}/{attempts})")
            if attempt < attempts:
                time.sleep(1.0)
        else:
            # 🔧 握手全數失敗 → 整輪重來一次 (剛連上時 slave 可能還在開機/忙線)
            if _attempt == 0:
                self.panel.log("warn", f"🔁 [Mid-Join] {target_cid} READY 握手失敗 → 整輪重來一次")
                time.sleep(1.0)
                self._mid_join_task(target_cid, _attempt=1)
                return
            self.panel.log("err", f"❌ {target_cid} READY timeout, skip play")
            self.panel.update_device(target_cid, status="錯誤", error_msg="READY timeout")
            return

        # 3. 就緒瞬間推算目標幀 = (有效播放時間 + 單向延遲補償) * fps
        with self.play_lock:
            paused_extra = self.paused_total
            if self.paused_since is not None:
                paused_extra += time.time() - self.paused_since
        elapsed = time.time() - self.playback_start_time - paused_extra
        if elapsed < 0:
            elapsed = 0
        target_frame = int((elapsed + lat_s) * self.current_fps)
        if total > 0:
            target_frame = target_frame % total if play_mode == 1 else min(target_frame, total - 1)
        self.panel.log("info", f"🔄 [Mid-Join] {target_cid} → frame {target_frame}")

        # 4. 帶幀號播放 + 同步 fps; master 暫停中則讓新裝置也跟上暫停
        self.send_pkt([target_cid], 0x300A, {"start_frame": target_frame})
        if self.current_fps > 0:
            self.send_pkt([target_cid], 0x3001, {
                "total_blocks": 0,
                "frames_per_block": 0,
                "fps": int(self.current_fps)
            })
        if paused:
            self.send_pkt([target_cid], 0x3005, {"pause": 1})
        self._dev_finished.discard(target_cid)
        self.panel.update_device(target_cid, status=("暫停" if paused else "播放中"))
        self.panel.update_device(target_cid, current_frame=target_frame)

        # 5. 🔧 接回後驗證閉環: 確認 slave 真的開始播, 進度偏差就 SEEK 拉回
        ok = self._verify_join(target_cid, target_frame)
        if not ok and _attempt == 0:
            self.panel.log("warn", f"🔁 [Mid-Join] {target_cid} 接回後未開始播放 → 整輪重來一次")
            time.sleep(1.0)
            self._mid_join_task(target_cid, _attempt=1)
            return
        if ok:
            self.panel.log("ok", f"✅ [Mid-Join] {target_cid} 已接回播放 (frame {target_frame}{', 同步暫停' if paused else ''})")

    def _verify_join(self, target_cid, target_frame, rounds=3):
        """接回後驗證: 輪詢 slave 狀態, 確認 stream 真的有在跑且進度正確。

        回 True = 播放中/已對齊; False = 3 輪都未開始 (接口/狀態沒生效)。
        進度偏差超過容差 → 發 0x3004 SEEK 校正 (slave 端 seek 後回 0x3008,
        狀態自動回 PLAYING, 不需要重送 0x300A)。
        """
        for rnd in range(1, rounds + 1):
            time.sleep(1.2)
            if not self.play_session_active:
                return True   # 會話已結束, 不再折騰
            node = self.device_manager.get_slave(target_cid)
            if node is None:
                return True   # 又斷線了 — 交給下次重連的中途加入
            st = self.query_status(target_cid, timeout=1.0)
            if not st:
                continue
            cur, pos, active, mem_free, _rid = self._parse_status(st)
            if active is False:
                self.panel.log("info", f"   ⏳ [Verify] {target_cid} 第{rnd}輪: stream_active=False (可能仍在準備)")
                continue
            # 有在播 → 對齊檢查
            self.panel.update_device(target_cid, current_frame=cur, mem_free=mem_free)
            total = self._device_total_frames(target_cid)
            if pos is not None and total > 0:
                expected = self._expected_frame(target_cid)
                drift = abs(pos - expected)
                drift_tol = max(30, int(total * 0.03))
                if drift > drift_tol:
                    self.panel.log("warn", f"   🩹 [Verify] {target_cid} 進度偏差 {drift} 幀 (pos={pos}, expect={expected}) → SEEK 校正")
                    self.send_pkt([target_cid], 0x3004, {"target_block": 0, "target_frame": expected})
                else:
                    self.panel.log("ok", f"   ✅ [Verify] {target_cid} 播放中且進度正確 (frame {pos}/{total})")
            return True
        self.panel.update_device(target_cid, status="錯誤", error_msg="join後未開始播放")
        return False

    # ==================== 狀態解析 (新舊韌體兩種 0x1102 格式相容) ====================
    @staticmethod
    def _parse_status(status_data):
        """解析 slave 回報的 status_json → (current_frame, pos_frame, active, mem_free, real_id)。

        🔧 接口正確性重點: 韌體歷史上存在兩種 0x1102 內容格式 —
          新格式 bus.get_metrics():  stream_pos_frame / played_frames / stream_active / slave_id
          舊格式 get_runtime_info(): frame_count / is_streaming / id (無 played_frames)
        PC 端兩種 key 都接受, 否則舊韌體回報的進度會被當 0 (看起來像「PC 沒收到」)。
        """
        pos = status_data.get("stream_pos_frame")
        if pos is None:
            pos = status_data.get("pos_frame")
        played = status_data.get("played_frames")
        if played is None:
            played = status_data.get("frame_count", 0)
        active = status_data.get("stream_active")
        if active is None:
            active = status_data.get("is_streaming")
        cur = int(pos) if pos is not None else int(played)
        real_id = status_data.get("id") or status_data.get("slave_id")
        return cur, pos, active, status_data.get("mem_free", 0), real_id

    def _device_total_frames(self, cid):
        """該設備對應 PlayID 的總幀數 (metadata), 無資料回 0。"""
        play_id = self.config.get("mapping", {}).get(cid, {}).get("play_id")
        if play_id is None:
            return 0
        return self.pxld_metadata.get(play_id, {}).get("total_frames", 0) or 0

    def _expected_frame(self, cid):
        """依主控時鐘推算該設備「現在應該在哪一幀」。

        有效播放時間 = now - playback_start_time - 暫停時間; 循環模式取模,
        非循環 clamp 到最後一幀。中途加入與進度校正共用此推算, 保證一致。
        """
        with self.play_lock:
            paused_extra = self.paused_total
            if self.paused_since is not None:
                paused_extra += time.time() - self.paused_since
        elapsed = time.time() - self.playback_start_time - paused_extra
        if elapsed < 0:
            elapsed = 0
        frame = int(elapsed * self.current_fps)
        total = self._device_total_frames(cid)
        if total > 0:
            if self.current_play_mode == 1:
                frame = frame % total
            else:
                frame = min(frame, max(0, total - 1))
        return frame

    def dispatch_logic(self, cid, cmd, payload):
        c_def = self.store.get(cmd)
        # 🔧 修復: 帶 store 解碼 (CPython 走純 Python 路徑), 否則欄位全空
        args = SchemaCodec.decode(c_def, payload, store=self.store)
        
        # ========== 0x1102: 状态心跳 ==========
        if cmd == 0x1102:
            try:
                status_data = json.loads(args["status_json"])
                
                # 🔧 用 _parse_status 統一解析 (新舊韌體格式都接受):
                #   stream_pos_frame(檔內絕對幀號) 優先 → played_frames/frame_count 兜底
                current_frame, pos_frame, active, mem_free, real_id = self._parse_status(status_data)
                
                # 🔧 更新当前帧号 (触发 FPS 计算)
                self.panel.update_device(
                    cid,
                    current_frame=current_frame,  # ✅ 更新帧号
                    mem_free=mem_free
                )
                
                # 🔧 快取狀態供 Profile/狀態查詢使用 (query_status)
                if cid in self.slaves:
                    node = self.slaves[cid]
                    node["status_data"] = status_data
                    node["status_event"].set()
                
                # 设备 ID 转移
                if real_id and real_id != cid:
                    # 🔧 防止 real_id 已被另一條連接佔用時靜默覆寫 (舊 socket 洩漏)
                    if real_id in self.slaves and self.slaves[real_id]["conn"] != self.slaves[cid]["conn"]:
                        try:
                            self.slaves[real_id]["conn"].close()
                        except:
                            pass
                    if cid in self.panel.monitors:
                        self.panel.monitors[real_id] = self.panel.monitors.pop(cid)
                        self.panel.monitors[real_id].device_id = real_id
                    self.slaves[real_id] = self.slaves.pop(cid)
                    cid = real_id
            
            except Exception as e:
                pass
        
        elif cmd == 0x1201:
            # 🔧 舊式 HEARTBEAT (0x1201): 部分韌體版本會週期主動推 (slave 主動回報通道)。
            #    它不含播放進度, 只快取 uptime/mem 供查詢使用, 並當作存活心跳。
            if cid in self.slaves:
                self.slaves[cid]["status_data"] = {
                    "slave_id": args.get("slave_id", cid),
                    "uptime_ms": args.get("uptime_ms", 0),
                    "mem_free": args.get("mem_free", 0),
                    "ws_connected": args.get("ws_connected", 1),
                }
                self.slaves[cid]["status_event"].set()

        elif cmd == 0x3008:
            # 🔧 STREAM_READY_ACK: slave 已完成準備/跳轉, 供中途加入計算最準確幀號
            if cid in self.slaves:
                self.slaves[cid]["ready_event"].set()
        
        elif cmd == 0x100B:
            # 🔧 TIME_SYNC_RSP: 延遲量測回應
            #    t0 = 本機送出時刻, t1 = slave 收到時刻 (received_at_ms, slave 時鐘),
            #    t2 = 本機收到時刻 → RTT = t2-t0; 時鐘偏移 = t1 - (t0+t2)/2
            if cid in self.slaves:
                node = self.slaves[cid]
                t2 = time.time()
                t0 = node.get("ping_t0", t2)
                t1 = args.get("received_at_ms", 0) / 1000.0
                rtt = t2 - t0
                if rtt >= 0:
                    node["ping_rtt"] = rtt * 1000.0
                    node["ping_offset"] = (t1 - (t0 + t2) / 2.0) * 1000.0
                    node["ping_event"].set()
        
        elif cmd == 0x3102:
            # 🔧 MODE_LIST_RSP: 配對模式 — 本地燈效模式清單
            if cid in self.slaves:
                node = self.slaves[cid]
                node["mode_list"] = args
                node["mode_event"].set()
        
        elif cmd == 0x3108:
            # 🔧 MODE_DETAIL_RSP: 配對模式 — 單一模式細節 (名稱)
            if cid in self.slaves:
                node = self.slaves[cid]
                node["mode_detail"] = args
                node["mode_detail_event"].set()
        
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
                # 🔧 記下 ACK 的 offset, 供上傳重試時辨識錯位 ACK (防重複寫入)
                self.slaves[cid]["ack_offset"] = args.get("offset", -1)
                self.slaves[cid]["ack_event"].set()
        
        elif cmd == 0x2006:
            if cid in self.slaves:
                self.slaves[cid]["remote_exists"] = args["exists"]
                self.slaves[cid]["remote_sha"] = args["sha256"]
                self.slaves[cid]["remote_size"] = args["size"]
                self.slaves[cid]["remote_pending"] = args.get("pending", 0)
                self.slaves[cid]["query_event"].set()

        elif cmd == 0x200F:
            # 🔧 FILE_PARTIAL_RSP: 斷點續傳查詢回應 (partial/written/total_size/sha256)
            if cid in self.slaves:
                node = self.slaves[cid]
                node["partial_partial"] = args.get("partial", 0)
                node["partial_written"] = args.get("written", 0)
                node["partial_total"] = args.get("total_size", 0)
                node["partial_sha"] = args.get("sha256")
                node["partial_event"].set()

        elif cmd == 0x2010:
            # 🔧 FILE_ERROR_RSP: 檔案操作失敗 (promote/upload 錯誤)
            if cid in self.slaves:
                node = self.slaves[cid]
                node["last_error"] = args
                node["query_event"].set()

        # 复用 0x2002 FILE_CHUNK 作为下载数据的返回
        elif cmd == 0x2002:
            if cid in self.slaves:
                self.slaves[cid]["read_data"] = args["data"]
                self.slaves[cid]["read_offset"] = args["offset"]
                self.slaves[cid]["read_event"].set()
        
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
                node = self.slaves[tid]
                try:
                    node["conn"].sendall(pkt)
                except Exception:
                    # 🔧 WS 通道層級斷線偵測: send 失敗 (RST/EPIPE/半開連線重傳超時)
                    #    = 通道已死 → 關閉 socket, 讓 handle_client 的 recv 結束並
                    #    在 finally 標離線 (不靠任何定時 health 檢查)。
                    try:
                        node["conn"].close()
                    except Exception:
                        pass
            # 如果 tid 根本不在 slaves (socket 已 close/清除)，則無法發送，忽略
    
    # ==================== 延遲量測 / 紀錄 (0x100A/0x100B TIME_SYNC) ====================
    def measure_latency(self, cid, samples=None):
        """量測單一設備的單向延遲 (ms)。回傳平均值或 None (無回應)。

        利用 0x100A TIME_SYNC (master_time_ms) → slave 回 0x100B TIME_SYNC_RSP
        (received_at_ms = slave 本地收到時刻)。每個樣本取:
          RTT    = t2 - t0            (本機送出→收到)
          偏移   = t1 - (t0+t2)/2     (slave 時鐘 − master 時鐘)
        NTP 式取「最小 RTT」樣本 (佇列不對稱最小、最可信):
          單向延遲 ≈ min_RTT / 2      (單一路徑下最精確的估計)
        另把時鐘偏移一併存下, 供顯示/對時參考。
        """
        node = self.slaves.get(cid)
        if not node:
            return None
        samples = int(samples if samples is not None else self._cfg_int("latency_samples", 5))
        samples = max(1, samples)
        samples_data = []  # (rtt_ms, offset_ms)
        with node["ping_lock"]:  # 🔧 避免與健康檢查探測併發互相覆蓋 ping_t0
            for _ in range(samples):
                node["ping_event"].clear()
                node["ping_rtt"] = None
                node["ping_offset"] = None
                node["ping_t0"] = time.time()
                self.send_pkt([cid], 0x100A, {"master_time_ms": int(time.time() * 1000) & 0xFFFFFFFF})
                if node["ping_event"].wait(timeout=1.0):
                    rtt = node.get("ping_rtt")
                    if rtt is not None:
                        samples_data.append((rtt, node.get("ping_offset", 0.0)))
        if not samples_data:
            return None
        # NTP 式: 最小 RTT 樣本最可信 (佇列不對稱最小)
        best = min(samples_data, key=lambda x: x[0])
        node["min_rtt_ms"] = best[0]
        node["avg_rtt_ms"] = sum(r for r, _ in samples_data) / len(samples_data)
        node["latency_ms"] = best[0] / 2.0
        node["clock_offset_ms"] = best[1]
        return node["latency_ms"]

    def _log_latency(self, rows, note=""):
        """把 (device_id, latency_ms) 列表寫入 CSV 紀錄檔 (手動測試紀錄用)。

        rows: [(cid, latency_ms), ...]; note: 附加備註欄 (例如 mid-join)。
        """
        try:
            path = self.config.get("latency_log_file", "latency_log.csv")
            # 🔧 裸檔名 (非絕對/相對路徑) 統一歸到 data/logs/
            if not os.path.isabs(path):
                path = os.path.join(LOG_DIR, path)
            is_new = not os.path.exists(path)
            with open(path, "a", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                if is_new:
                    w.writerow(["timestamp", "device_id", "play_id", "latency_ms", "note"])
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                for cid, lat in rows:
                    pid = self.config["mapping"].get(cid, {}).get("play_id", "")
                    w.writerow([ts, cid, pid, f"{lat:.2f}", note])
        except Exception as e:
            self.panel.log("warn", f"⚠️ 延遲紀錄寫入失敗: {e}")

    def _latency_test_and_log(self, targets=None, note="", ask_note=False):
        """對目標設備逐一量測延遲, 顯示結果並寫入紀錄檔。

        ask_note=True 時會先問使用者備註再寫入 (手動測試紀錄用)。
        """
        targets = targets if targets is not None else self.selected_targets
        if not targets:
            print("⚠️ 無目標設備")
            return
        print("\n📡 [延遲測試] 正在量測 ({} samples/device, min-RTT 法)...".format(
            self._cfg_int("latency_samples", 5)))
        rows = []
        for tid in targets:
            lat = self.measure_latency(tid)
            if lat is None:
                print(f"  ❌ {tid}: 無回應")
            else:
                node = self.slaves.get(tid, {})
                min_rtt = node.get("min_rtt_ms")
                avg_rtt = node.get("avg_rtt_ms")
                off = node.get("clock_offset_ms")
                extra = ""
                if min_rtt is not None:
                    extra = f"  (minRTT {min_rtt:5.1f}ms / avgRTT {avg_rtt:5.1f}ms / offset {off:+7.2f}ms)"
                print(f"  ✅ {tid}: 單向 {lat:6.1f} ms{extra}")
                rows.append((tid, lat))
        if rows:
            if ask_note:
                note = input("  📝 備註 (Enter=無): ").strip()
            self._log_latency(rows, note=note)
            print(f"💾 已紀錄 {len(rows)} 筆 → {os.path.join(LOG_DIR, self.config.get('latency_log_file', 'latency_log.csv'))}")

    def _get_mode_name(self, cid, mode_id):
        """查詢單一本地燈效模式的名稱 (0x3107→0x3108), 失敗回 '?'。

        mode_id = 內部 16-bit 識別碼；發送時拆回 (mode_type, mode_id) 兩欄。
        """
        node = self.slaves.get(cid)
        if not node:
            return "?"
        node["mode_detail_event"].clear()
        node["mode_detail"] = None
        self.send_pkt([cid], 0x3107, {"mode_type": mode_id >> 8, "mode_id": mode_id & 0xFF})
        if node["mode_detail_event"].wait(timeout=1.5):
            d = node.get("mode_detail") or {}
            name = d.get("name")
            if name:
                return name
        return "?"

    # ==================== 設備狀態 / 模式查詢 ====================
    def query_status(self, cid, timeout=2.0):
        """主動查詢設備狀態 (0x1101 STATUS_GET → 0x1102 STATUS_RSP)。

        回傳 status_json 解析後的 dict (含 played_frames 已播幀號、frame_interval_ms、
        stream_frame_count、stream_mode、stream_active、slave_id), 失敗回 None。
        """
        node = self.slaves.get(cid)
        if not node:
            return None
        node["status_event"].clear()
        node["status_data"] = None
        self.send_pkt([cid], 0x1101, {"query_type": 0})
        if node["status_event"].wait(timeout=timeout):
            return node.get("status_data")
        return None

    def _query_modes(self, cid, timeout=2.0):
        """查詢設備本地燈效清單 (0x3101 + 0x3108 名稱)。

        回傳 [(mode_id, name), ...]; 逾時/無模式回 []。
        mode_id = 內部 16-bit 識別碼（entries 每筆 2 bytes, little-endian）。
        """
        node = self.slaves.get(cid)
        if not node:
            return []
        node["mode_event"].clear()
        node["mode_list"] = None
        self.send_pkt([cid], 0x3101, {"mode_type": 0})
        if not node["mode_event"].wait(timeout=timeout):
            return []
        ml = node.get("mode_list") or {}
        entries = ml.get("entries")
        if entries is None:
            return []
        try:
            raw = bytes(entries)
            ids = list(struct.unpack("<{}H".format(len(raw) // 2), raw))
        except Exception:
            return []
        result = []
        for mid in ids:
            name = self._get_mode_name(cid, mid)
            result.append((mid, name))
        return result

    # ==================== 每 id 一個 Profile (profiles/<id>.json) ====================
    def _profile_path(self, cid):
        profiles_dir = PROFILE_DIR
        os.makedirs(profiles_dir, exist_ok=True)
        safe = cid.replace(":", "_")
        return os.path.join(profiles_dir, f"{safe}.json")

    def _save_profile(self, cid, manifest=None):
        """建立/更新單一設備的 Profile: 本地燈效清單 + 狀態 + 檔案清單。

        資料來源: 0x3101/0x3108 (模式)、0x1101 (狀態/fps/ips)、manifest.json
        (檔案清單, 可傳入已下載的 dict)、0x100A (延遲)。離線時仍會用既有
        mapping 資訊寫出, 供之後離線查閱「對方有什麼模式可播放」。
        """
        node = self.slaves.get(cid)
        profile = {
            "device_id": cid,
            "play_id": self.config["mapping"].get(cid, {}).get("play_id"),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "online": node is not None,
        }
        if node:
            # 狀態 (fps / ips / 記憶體)
            status = self.query_status(cid, timeout=2.0)
            if status:
                profile["status"] = {
                    k: status.get(k) for k in
                    ("frame_interval_ms", "played_frames", "stream_frame_count",
                     "stream_mode", "stream_active", "slave_id")
                    if k in status
                }
            # 本地燈效模式
            modes = self._query_modes(cid, timeout=2.0)
            profile["modes"] = [{"id": mid, "name": name} for mid, name in modes]
            # 延遲 (min-RTT 單向)
            lat = self.measure_latency(cid, samples=3)
            if lat is not None:
                profile["latency_ms"] = round(lat, 2)
                profile["clock_offset_ms"] = round(node.get("clock_offset_ms") or 0.0, 2)
        # 檔案清單
        if manifest is not None:
            profile["files"] = sorted(manifest.keys())
            profile["file_count"] = len(manifest)
        try:
            with open(self._profile_path(cid), "w", encoding="utf-8") as f:
                json.dump(profile, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            print(f"⚠️ Profile 寫入失敗 {cid}: {e}")
            return False

    def _load_profile(self, cid):
        """讀取快取的 Profile dict, 沒有回 None。"""
        try:
            with open(self._profile_path(cid), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _print_profile(self, cid, profile):
        """顯示 Profile 內容 (模式清單等)。"""
        if not profile:
            print(f"   📄 無 {cid} 的 profile 快取")
            return
        print(f"   📄 Profile ({profile.get('updated_at', '?')})")
        print(f"      PlayID: {profile.get('play_id')}  │  在線: {'是' if profile.get('online') else '否'}")
        st = profile.get("status") or {}
        if st:
            print(f"      frame_interval_ms: {st.get('frame_interval_ms', '?')}  │  played_frames: {st.get('played_frames', '?')}  │  stream_frame_count: {st.get('stream_frame_count', '?')}")
            print(f"      stream_mode: {st.get('stream_mode', '?')}  │  stream_active: {st.get('stream_active', '?')}")
        if "latency_ms" in profile:
            print(f"      單向延遲: {profile['latency_ms']} ms  │  offset: {profile.get('clock_offset_ms')} ms")
        modes = profile.get("modes") or []
        if modes:
            print(f"      可播放模式 {len(modes)} 個:")
            for m in modes:
                print(f"        - mode {m['id']} ({m['name']})")
        else:
            print("      可播放模式: (無/未查詢)")
        files = profile.get("files") or []
        if files:
            print(f"      檔案 {len(files)} 個 (存於 data/downloads/{cid}/):")
            for p in files[:20]:
                print(f"        - {p}")
            if len(files) > 20:
                print(f"        ... 共 {len(files)} 個")

    # ==================== Step 0: 固件更新 ====================
    def step_0_update_firmware(self):
        self.load_config()
        self.panel.stop()
        ConsoleUI.clear_screen()
        ConsoleUI.show_cursor()
        
        if not self.selected_targets:
            print("⚠️ 請先執行 Step 1 選擇設備")
            input("\n按 Enter 繼續...")
            self.panel.start()
            return

        print("\n🔧 [Step 0] 固件更新流程")
        print("  0. 文件管理器 (上傳/下載/瀏覽)")
        print("  1. 固件全量更新 (批量上傳 slave 目錄)")
        print("  2. Config 編輯器")
        print("  3. 刪除文件")
        print("  4. 重建文件索引 (Scan)")
        print("  5. 還原/確認 (.bak 備份回滾)")
        print("  6. 重試失敗/續傳 (斷點續傳)")
        print("  7. 軟重啟設備 (0x100F Reboot)")
        print("  8. 引導修復 (bootstrap 新韌體到 root)")
        print("  9. 批量 Config 更新 (Profile/模板 → 依順序上傳)")
        print("  q. 返回")
        
        choice = input("\n👉 請選擇: ").strip().lower()
        
        if choice == '0':
            self._file_explorer()
        elif choice == '1':
            self._update_firmware_files()
        elif choice == '2':
            self._modify_config()
        elif choice == '3':
            self._delete_file()
        elif choice == '4':
            self._scan_files()
        elif choice == '5':
            self._restore_or_confirm()
        elif choice == '6':
            self._retry_failed_uploads()
        elif choice == '7':
            self._soft_reboot_devices()
        elif choice == '8':
            self._bootstrap_root_fix()
        elif choice == '9':
            self._batch_config_update()
        elif choice == 'q':
            self.panel.start()
            return
        else:
            print("❌ 無效選擇")
            time.sleep(1)
            self.panel.start()
            return
            
        input("\n按 Enter 返回...")
        self.panel.start()

    def _cfg_int(self, key, default):
        try:
            return int(self.config.get(key, default))
        except Exception:
            return int(default)

    def _cfg_float(self, key, default):
        try:
            return float(self.config.get(key, default))
        except Exception:
            return float(default)

    def _transfer_begin(self):
        self.transfer_cancel.clear()
        self._transfer_kb_stop.clear()
        self.panel.start(
            interactive=True,
            controls_text="║  [S] 停止傳輸  │  [Q] 退出                                                                  ║",
        )
        input_handler.enter_raw_mode()
        input_handler.flush_input()

        def _kb_loop():
            while not self._transfer_kb_stop.is_set():
                if input_handler.kbhit():
                    try:
                        key = input_handler.getch()
                        if isinstance(key, bytes):
                            key = key.decode('utf-8', errors='ignore')
                        key = (key or "").lower()
                        if key in ("s", "q", "\x03"):
                            self.transfer_cancel.set()
                            return
                    except Exception:
                        pass
                time.sleep(0.05)

        t = threading.Thread(target=_kb_loop, daemon=True)
        self._transfer_kb_thread = t
        t.start()

    def _transfer_end(self):
        self._transfer_kb_stop.set()
        t = self._transfer_kb_thread
        if t:
            try:
                t.join(timeout=0.2)
            except Exception:
                pass
        self._transfer_kb_thread = None
        input_handler.exit_raw_mode()
        input_handler.flush_input()
        if self.panel.running:
            self.panel.start(interactive=False, controls_text=None)
        else:
            self.panel.interactive_mode = False
            self.panel.controls_text = None

    def _wait_evt(self, evt, timeout):
        end = time.time() + float(timeout)
        while time.time() < end:
            if self.transfer_cancel.is_set():
                return False, "cancel"
            if evt.wait(timeout=0.05):
                return True, None
        return False, "timeout"

    # ==================== FILE_PROMOTE / CONFIRM / UNDO (root 部署 + 備份確認) ====================
    def _promote_file(self, tid, remote_path, wait=3.0):
        """0x2011 FILE_PROMOTE: /sd 暫存 → root 目標 (自動 .bak 備份 + pending 記錄)。

        remote_path 為 root 路徑 (如 /boot.py); src 自動補 /sd 前綴 (上傳暫存處)。
        """
        node = self.slaves.get(tid)
        if not node:
            return False
        src = remote_path if remote_path.startswith("/sd") else "/sd" + remote_path
        node["query_event"].clear()
        node["last_error"] = None
        self.send_pkt([tid], 0x2011, {"src": src, "dst": remote_path})
        ok = node["query_event"].wait(timeout=wait)
        if not ok:
            self.panel.log("warn", f"⚠️ [{tid}] promote {remote_path}: 逾時")
            return False
        if node.get("last_error"):
            err = node["last_error"]
            self.panel.log("err", f"⚠️ [{tid}] promote {remote_path}: 失敗 (error={err})")
            return False
        return True

    def _confirm_file(self, tid, remote_path, wait=3.0):
        """0x2008 FILE_CONFIRM: 確認覆蓋 → 刪 .bak + 清 pending (正式生效)。

        以 slave 回覆 0x2006 的 pending 欄位判斷是否真的清掉; 只看「有沒有收到
        回應」會誤判成功 (slave 找不到 pending 也會回 0x2006), 導致假確認 →
        3 次重啟自動回滾 → 上傳-回滾無限循環。
        """
        node = self.slaves.get(tid)
        if not node:
            return False
        node["query_event"].clear()
        node["remote_pending"] = -1
        self.send_pkt([tid], 0x2008, {"path": remote_path})
        if not node["query_event"].wait(timeout=wait):
            return False
        return node.get("remote_pending", -1) == 0

    def _undo_file(self, tid, remote_path, wait=3.0):
        """0x200A FILE_UNDO: 復原 → 刪新檔 + .bak 改回 + 清 pending (立即回滾)。"""
        node = self.slaves.get(tid)
        if not node:
            return False
        node["query_event"].clear()
        node["remote_pending"] = -1
        self.send_pkt([tid], 0x200A, {"path": remote_path})
        if not node["query_event"].wait(timeout=wait):
            return False
        return node.get("remote_pending", -1) == 0

    def _confirm_path_batch(self, tids, remote_path, wait=3.0):
        """平行對多台設備發 0x2008 FILE_CONFIRM (每台獨立並行), 回傳 {tid: bool}。

        以 slave 回覆的 pending 欄位判定是否真的清掉; 失敗的再重試一次 (slave
        可能忙線未即時清), 避免假確認留下的 pending 在重啟後被自動回滾。
        """
        return self._commit_path_batch(tids, remote_path, 0x2008, wait=wait)

    def _undo_path_batch(self, tids, remote_path, wait=3.0):
        """平行對多台設備發 0x200A FILE_UNDO (每台獨立並行), 回傳 {tid: bool}。"""
        return self._commit_path_batch(tids, remote_path, 0x200A, wait=wait)

    def _commit_path_batch(self, tids, remote_path, cmd, wait=3.0):
        """對每台設備「平行」發送 confirm/undo (cmd 0x2008/0x200A), 回傳 {tid: bool}。

        像上傳一樣用 ThreadPoolExecutor 每台獨立並行發送+等待, 一台卡住不拖住其他台;
        失敗自動重試一次。原先是「一次廣播後串行等待」, 慢的那台會拖累全部。
        """
        tids = [t for t in tids if t in self.slaves]
        if not tids:
            return {}

        def _commit_one(tid):
            node = self.slaves.get(tid)
            if not node:
                return tid, False
            node["query_event"].clear()
            node["remote_pending"] = -1
            self.send_pkt([tid], cmd, {"path": remote_path})
            if node["query_event"].wait(timeout=wait):
                return tid, (node.get("remote_pending", -1) == 0)
            return tid, False

        def _run_all(ts):
            res = {}
            with ThreadPoolExecutor(max_workers=min(16, len(ts))) as ex:
                futs = [ex.submit(_commit_one, tid) for tid in ts]
                for f in futs:
                    tid, ok = f.result()
                    res[tid] = ok
            return res

        res = _run_all(tids)
        # 🔧 失敗的重試一次 (pending 可能因 slave 忙線未即時清)
        retry_tids = [tid for tid, ok in res.items() if not ok]
        if retry_tids:
            time.sleep(0.5)
            for tid, ok in _run_all(retry_tids).items():
                res[tid] = ok
        return res

    def _download_remote_delta(self, tid):
        """下載設備的 /sd/.delta.json → pending dict {path: {...}}。失敗/無 pending 回 {}。

        pending 是「已 promote 待確認」的權威紀錄 (含 .bak 備份), 一次下載即可知道
        該設備有哪些檔案可還原/確認, 不必逐檔查詢。無 panel 包裝, 可並行呼叫。
        """
        node = self.slaves.get(tid)
        if not node:
            return {}
        try:
            data = self._download_bytes(tid, "/sd/.delta.json", expected_size=None, status="Delta")
        except Exception:
            data = None
        finally:
            self.panel.update_device(tid, status="待機", transfer_label="")
        if not data:
            return {}
        try:
            obj = json.loads(data.decode("utf-8"))
        except Exception:
            return {}
        return obj.get("pending", {}) or {}

    def _run_confirm_or_undo(self, promoted, action):
        """依路徑分組後「平行」confirm/undo 給所有受影響設備 (每台獨立並行, 不逐台串行)。

        promoted: {tid: [remote_path, ...]} 或 {tid: {path: rec}}; action: "confirm"|"undo"。
        回傳 (成功數, 失敗數)。
        """
        path_to_tids = {}
        for tid, paths in promoted.items():
            if isinstance(paths, dict):
                paths = list(paths.keys())
            for p in paths:
                path_to_tids.setdefault(p, []).append(tid)
        if not path_to_tids:
            return 0, 0
        ok_n = fail_n = 0
        verb = "確認" if action == "confirm" else "還原"
        level = "CONFIRM" if action == "confirm" else "UNDO"
        total = len(path_to_tids)
        for i, (path, tids) in enumerate(path_to_tids.items(), 1):
            if action == "confirm":
                res = self._confirm_path_batch(tids, path)
            else:
                res = self._undo_path_batch(tids, path)
            got = sum(1 for ok in res.values() if ok)
            ok_n += got
            fail_n += len(res) - got
            for tid, ok in res.items():
                if not ok:
                    self.panel.log("err", f"⚠️ [{tid}] {verb}失敗: {path}")
                self._log_event(level if ok else "FAIL", f"{verb} {path}" + ("" if ok else " (失敗)"), device_id=tid)
            self.panel.log("info", f"[{i}/{total}] {path}: {verb} {len(res)} 台 ({got} 成功)")
        return ok_n, fail_n

    def _prompt_confirm_promoted(self, promoted):
        """批次上傳後的手動確認: [Enter/c]=確認全部 / [u]=復原全部 / [q]=暫不確認。

        預設「確認」——避免一路 Enter 卻沒確認, 導致 pending 留存 → 3 次重啟自動回滾
        → 下次又顯示「需要更新」的無限循環。確認/復原以路徑分組平行發送到所有設備。
        """
        total = sum(len(v) for v in promoted.values())
        print(f"\n📢 [Confirm] {total} 個檔案已寫入 root (舊檔已備份 .bak):")
        print("   [Enter/c] 確認全部 (正式生效, 刪除 .bak 備份) ← 預設")
        print("   [u] 復原全部 (立即回滾, 用 .bak 還原舊版)")
        print("   [q] 暫不確認 (保留 pending, MCU 3 次重啟後自動回滾)")
        ch = input("👉 請選擇: ").strip().lower()
        if ch == 'u':
            ok_n, fail_n = self._run_confirm_or_undo(promoted, "undo")
            print(f"♻️ 已復原 {ok_n} 個檔案; 失敗 {fail_n}")
        elif ch == 'q':
            print("ℹ️ 暫不確認 — MCU 將在 3 次重啟後自動復原未確認的檔案")
            print("   (之後可再用 Step 0 檔案管理 或本工具的確認/復原指令處理)")
        else:
            ok_n, fail_n = self._run_confirm_or_undo(promoted, "confirm")
            print(f"✅ 已確認 {ok_n} 個檔案 (正式生效); 失敗 {fail_n}")
            self._verify_promoted(promoted)

    def _auto_confirm_promoted(self, promoted):
        """批次 promote 後「直接確認」: 對有信心的上傳立即 confirm (刪 .bak, 正式生效)。

        供使用者選擇「直接確認」模式時呼叫; 以路徑分組廣播, 同時發送。
        """
        total = sum(len(v) for v in promoted.values())
        print(f"\n✅ [Promote] 直接確認 {total} 個檔案 (正式生效, 刪 .bak)...")
        ok_n, fail_n = self._run_confirm_or_undo(promoted, "confirm")
        self._log_event("CONFIRM", f"批次直接確認 {ok_n} 成功 / {fail_n} 失敗")
        self.panel.log("ok", f"✅ 已確認 {ok_n} 個檔案; 失敗 {fail_n}")
        self._verify_promoted(promoted)

    def _verify_promoted(self, promoted):
        """promote + confirm 後的驗證閉環: 重新下載每台設備的 manifest 整批比對,
        並下 delta 確認 pending 已清空。固件更新講究「馬上檢查哈希表 + delta」,
        避免快取/殘留 pending 讓「看起來成功」但其實 root 沒落地或 .bak 沒清。

        sha 比對用「整批下載 manifest」而非逐檔 0x2005 查詢——逐檔查詢一次一個
        round-trip 太慢; manifest 是 write-through 的, 上傳時已同步更新,
        下載一次即可比對全部。
        """
        if not promoted:
            return
        remote_to_local = {r: l for l, r in getattr(self, "last_upload_files", [])}
        self.panel.log("info", "🔎 [Verify] 重新下載各設備 manifest 整批比對 + 檢查 delta...")

        # 1. 並行重新下載 manifest（取代逐檔 0x2005 查詢）
        tids = [tid for tid in promoted if tid in self.slaves]
        manifests = {}
        if tids:
            ex = ThreadPoolExecutor(max_workers=min(16, len(tids)))
            futs = {ex.submit(self._download_manifest_core, tid): tid for tid in tids}
            for f in futs:
                tid = futs[f]
                try:
                    manifests[tid] = f.result(timeout=20.0)
                except Exception:
                    manifests[tid] = None
            ex.shutdown(wait=False)

        # 2. 整批比對 local sha vs 遠端 manifest
        for tid, paths in promoted.items():
            if isinstance(paths, dict):
                paths = list(paths.keys())
            if tid not in self.slaves:
                continue
            man = manifests.get(tid)
            if man is None:
                self.panel.log("err", f"❌ [{tid}]: manifest 重新下載失敗, 無法驗證")
                self._log_event("FAIL", "promote 驗證 manifest 下載失敗", device_id=tid)
                continue
            for r in paths:
                l = remote_to_local.get(r)
                if not l or not os.path.isfile(l):
                    self.panel.log("warn", f"⚠️ [{tid}] {r}: 找不到本地來源, 略過 sha 比對")
                    continue
                local_sha = self._calc_local_sha(l)
                remote_hex = man.get(r)
                if remote_hex is None:
                    self.panel.log("err", f"❌ [{tid}] {r}: 遠端 manifest 缺少此檔")
                    self._log_event("FAIL", f"promote 驗證遠端缺失 {r}", device_id=tid)
                elif remote_hex != local_sha.hex():
                    self.panel.log("err", f"❌ [{tid}] {r}: 哈希不符 (remote {remote_hex[:8]} != local {local_sha.hex()[:8]})")
                    self._log_event("FAIL", f"promote 驗證哈希不符 {r}", device_id=tid)
                else:
                    self.panel.log("ok", f"✅ [{tid}] {r}: 哈希一致")
        self.panel.log("info", "🔎 [Verify] 檢查 delta pending 是否已清空...")
        for tid in promoted:
            if tid not in self.slaves:
                continue
            pend = self._download_remote_delta(tid)
            if pend:
                shown = ", ".join(sorted(pend)[:5])
                self.panel.log("warn", f"⚠️ [{tid}]: 仍有 {len(pend)} 個 pending 未清 ({shown})")
                self._log_event("FAIL", f"promote 驗證 pending 未清 ({len(pend)})", device_id=tid)
            else:
                self.panel.log("ok", f"✅ [{tid}]: pending 已清空")

    # ==================== 重啟確認流程 (0x100F REBOOT) ====================
    def _do_reboot(self, chosen, wait_seconds=90):
        """對 chosen 設備送出 0x100F 軟重啟, 等待回連並逐台回報。"""

        # 記下舊連線身份, 之後判斷「是否真的重啟回連」(新 socket)
        old_conns = {}
        for tid in chosen:
            node = self.slaves.get(tid)
            if node is not None:
                old_conns[tid] = id(node["conn"])

        for tid in chosen:
            print(f"🔁 [Reboot] {tid} 送出重啟指令 (0x100F)...")
            self.send_pkt([tid], 0x100F, {"delay_ms": 500})
            self.panel.update_device(tid, status="重啟中")

        print(f"\n⏳ 等待設備重啟回連 (最多 {wait_seconds}s, slave 開機會自動連回 master)...")
        deadline = time.time() + wait_seconds
        back = set()
        while time.time() < deadline and len(back) < len(chosen):
            time.sleep(1)
            for tid in chosen:
                if tid in back:
                    continue
                node = self.slaves.get(tid)
                if node is not None:
                    is_new = tid not in old_conns or id(node["conn"]) != old_conns[tid]
                    if is_new:
                        back.add(tid)
                        print(f"   ✅ {tid} 已回連 (新連線)")
                    else:
                        # 連線沒斷 → 可能沒重啟成功; 稍後統一報告
                        pass
            # 中途可退出
            if input_handler.kbhit():
                try:
                    if input_handler.getch().lower() in ("q", "s"):
                        print("ℹ️ 等待被中斷")
                        break
                except Exception:
                    pass

        print("\n📊 [Reboot] 結果:")
        for tid in chosen:
            if tid in back:
                st = self.query_status(tid, timeout=2.0)
                up = (st or {}).get("uptime_ms")
                if st:
                    up_str = f", uptime {up}ms" if up is not None else ""
                    print(f"   ✅ {tid}: 已回連且狀態正常{up_str}")
                    self.panel.update_device(tid, status="待機")
                else:
                    print(f"   ⚠️ {tid}: 已回連但狀態查詢無回應")
                    self.panel.update_device(tid, status="待機")
            else:
                node = self.slaves.get(tid)
                if node is not None:
                    print(f"   ⚠️ {tid}: 連線未中斷過 (可能未重啟成功, 請確認 0x100F 是否有被執行)")
                    self.panel.update_device(tid, status="待機")
                else:
                    print(f"   ❌ {tid}: {wait_seconds}s 內未回連 (可先 Scan 再確認)")
                    self.panel.update_device(tid, status="離線")
        print("💡 提示: 重啟後 slave 開機自動連 master; 若未回連請用 Step 1 掃描/敲門。")

    def _reboot_and_confirm(self, targets=None, wait_seconds=90, default_yes=True):
        """🔧 重啟確認流程 (預設「是」= 全部軟重啟)。

        固件/關鍵配置要重啟才會生效; 上傳完成後:
          [Enter/是] 全部軟重啟 (預設)
          [n] 挑選幾台
          [q/否] 暫不重啟
        重啟後等待設備自動連回, 逐台回報並查詢狀態確認設備真的活著。
        """
        targets = [t for t in (targets or self.selected_targets) if t in self.slaves]
        if not targets:
            print("⚠️ 無在線設備可重啟")
            return

        print("\n🔁 [Reboot] 軟重啟設備讓新韌體/配置生效:")
        for i, tid in enumerate(targets):
            print(f"   {i+1:2d}. {self._play_id_str(tid)}  {tid}")
        hint = "👉 [Enter] 是/全部重啟" if default_yes else "👉 [a] 全部重啟"
        ch = input(f"{hint} / [n] 挑選部分 / [q] 否: ").strip().lower()
        if ch == "q":
            print("ℹ️ 暫不重啟 (之後可用選單的「軟重啟設備」)")
            return
        chosen = targets
        if ch == "n":
            chosen = []
            sel = input("👉 輸入編號 (逗號分隔, 例: 1,3): ").strip()
            try:
                for part in sel.replace("，", ",").split(","):
                    idx = int(part.strip()) - 1
                    if 0 <= idx < len(targets):
                        chosen.append(targets[idx])
            except Exception:
                print("❌ 輸入無效")
                return
        elif ch not in ("", "a", "y", "yes"):
            print("❌ 無效選擇")
            return
        if not chosen:
            print("ℹ️ 未選擇任何設備")
            return

        self._do_reboot(chosen, wait_seconds=wait_seconds)

    def _soft_reboot_devices(self):
        """獨立「軟重啟設備」按鈕: 對選定設備送出 0x100F 並等待回連。"""
        targets = [t for t in self.selected_targets if t in self.slaves]
        if not targets:
            print("⚠️ 無在線設備可重啟 (請先 Step 1 選擇/掃描)")
            return
        self._reboot_and_confirm(targets=targets, default_yes=True)

    def _query_partial(self, tid, remote_path, timeout=3.0):
        """查詢 slave 上的斷點續傳進度 (0x200E FILE_PARTIAL_QUERY → 0x200F FILE_PARTIAL_RSP)。

        回傳 (partial, written, total_size, sha256) 或 None (逾時/離線)。
        """
        node = self.slaves.get(tid)
        if not node:
            return None
        node["partial_event"].clear()
        self.send_pkt([tid], 0x200E, {"path": remote_path})
        if not node["partial_event"].wait(timeout=timeout):
            return None
        return (node["partial_partial"], node["partial_written"],
                node["partial_total"], node["partial_sha"])

    def _upload_bytes(self, tid, data, remote_path, file_idx=1, total_files=1, file_id=None, resume=True):
        node = self.slaves.get(tid)
        if not node:
            raise Exception("Device Offline")
        if self.transfer_cancel.is_set():
            raise Exception("已停止")

        if data is None:
            data = b""

        local_sha = hashlib.sha256(data).digest()
        total_len = len(data)
        chunk_size = self._cfg_int("upload_chunk_size", 1024)
        ack_timeout = self._cfg_float("upload_ack_timeout", 5.0)
        begin_timeout = self._cfg_float("upload_begin_timeout", 5.0)
        validation_timeout = self._cfg_float("upload_validation_timeout", 30.0)

        if file_id is None:
            file_id = int(file_idx)

        # 🔧 斷點續傳: 上次中斷留下的 .tmp 若與本次檔案身分 (大小 + sha) 一致,
        #    直接從已寫入處續傳, 不必從頭重傳 (快速恢復中斷的上傳)。
        start_offset = 0
        if resume and total_len > 0:
            try:
                pinfo = self._query_partial(tid, remote_path)
                if pinfo:
                    partial, written, ptotal, psha = pinfo
                    if partial and written > 0 and ptotal == total_len and psha == local_sha:
                        aligned = (written // chunk_size) * chunk_size if chunk_size else 0
                        if aligned > 0 and aligned < total_len:
                            start_offset = aligned
                            self.panel.log("info", f"♻️ [Resume] {tid} {remote_path} 續傳 @ {aligned}/{total_len}")
            except Exception:
                pass

        self.panel.update_device(
            tid,
            status="上傳中",
            transfer_label=f"上傳 {file_idx}/{total_files}",
            upload_progress=0,
            uploaded_bytes=start_offset,
            total_bytes=total_len,
            upload_start_time=time.time()
        )

        self.send_pkt([tid], 0x2001, {
            "file_id": file_id,
            "total_size": total_len,
            "chunk_size": chunk_size,
            "sha256": local_sha,
            "path": remote_path
        })

        node["query_event"].clear()
        node["remote_sha"] = None
        self.send_pkt([tid], 0x2005, {"path": remote_path})

        ok, why = self._wait_evt(node["query_event"], begin_timeout)
        if not ok:
            if why == "cancel":
                raise Exception("已停止")
            raise Exception("FILE_BEGIN Handshake Timeout")

        start_time = time.time()
        last_t = time.perf_counter()
        last_done = start_offset
        speed_ema = 0.0
        send_speed_ema = 0.0
        ack_ms_ema = 0.0
        send_total = 0.0
        ack_total = 0.0
        retry_count = self._cfg_int("transfer_retry_count", 3)

        for off in range(start_offset, total_len, chunk_size):
            if self.transfer_cancel.is_set():
                raise Exception("已停止")
            chunk = data[off : off + chunk_size]
            # 🔧 block 級重試: ACK 逾時重發同一個 chunk (最多 retry_count 次)
            retry_left = retry_count
            while True:
                node["ack_event"].clear()
                node["ack_offset"] = -1
                t_send0 = time.perf_counter()
                self.send_pkt([tid], 0x2002, {
                    "file_id": file_id,
                    "offset": off,
                    "data": chunk
                })
                t_send1 = time.perf_counter()

                t_ack0 = time.perf_counter()
                ok, why = self._wait_evt(node["ack_event"], ack_timeout)
                t_ack1 = time.perf_counter()
                if ok:
                    # 🔧 只接受與目前 chunk offset 相符的 ACK; 延遲的舊 ACK 忽略
                    if node.get("ack_offset") == off:
                        break
                    self.panel.log("warn", f"⚠️ 忽略錯位 ACK off={node.get('ack_offset')} expect={off}, 重發")
                    ok = False
                    why = "mismatch"
                if why == "cancel":
                    raise Exception("已停止")
                if retry_left > 0:
                    retry_left -= 1
                    self.panel.log("warn", f"⚠️ 上傳 ACK 逾時/錯位 offset {off} (chunk={chunk_size}), 重發 ({retry_count - retry_left}/{retry_count})...")
                    continue
                raise Exception(f"Timeout at offset {off} (chunk={chunk_size})")
            send_total += (t_send1 - t_send0)
            ack_total += (t_ack1 - t_ack0)

            done = off + len(chunk)
            now_t = time.perf_counter()
            dt_total = now_t - last_t
            if dt_total > 0:
                delta_kb = (done - last_done) / 1024
                inst = delta_kb / dt_total
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
            progress = (done / total_len) * 100 if total_len > 0 else 100

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

        node["query_event"].clear()
        node["remote_sha"] = None
        self.send_pkt([tid], 0x2003, {"file_id": file_id})

        ok, why = self._wait_evt(node["query_event"], validation_timeout)
        if ok:
            remote_sha = node["remote_sha"]
            if remote_sha != local_sha:
                raise Exception(f"SHA Mismatch: {remote_sha.hex()[:8]} != {local_sha.hex()[:8]}")
        else:
            if why == "cancel":
                raise Exception("已停止")
            raise Exception("Validation Timeout (No 0x2006 response)")

        return local_sha

    def _download_to_writer(self, target, remote_path, writer, expected_size=None, status="下載中"):
        node = self.slaves.get(target)
        if not node:
            raise Exception("設備離線")
        if self.transfer_cancel.is_set():
            raise Exception("已停止")

        if expected_size is None:
            node["query_event"].clear()
            node["remote_size"] = 0
            self.send_pkt([target], 0x2005, {"path": remote_path})
            ok, why = self._wait_evt(node["query_event"], 3.0)
            if not ok:
                if why == "cancel":
                    raise Exception("已停止")
                raise Exception("查詢超時, 無法獲取文件大小")
            expected_size = node["remote_size"]

        expected_size = int(expected_size or 0)
        if expected_size <= 0:
            return 0, expected_size

        chunk_size = self._cfg_int("download_chunk_size", 1024)
        chunk_min = self._cfg_int("download_chunk_min", 1024)
        read_timeout = self._cfg_float("download_read_timeout", 5.0)

        self.panel.update_device(
            target,
            status="下載中",
            transfer_label=status if status.startswith("下載") else f"下載 {status}",
            uploaded_bytes=0,
            total_bytes=expected_size,
            upload_start_time=time.time(),
            upload_progress=0,
            upload_speed=0
        )

        offset = 0
        start_time = time.time()
        retry_count = self._cfg_int("transfer_retry_count", 3)
        while offset < expected_size:
            if self.transfer_cancel.is_set():
                raise Exception("已停止")

            req_len = chunk_size
            remain = expected_size - offset
            if req_len > remain:
                req_len = remain

            # 🔧 block 級重試: 同一 offset 重試 retry_count 次; 都失敗才縮小 chunk;
            #    chunk 已到最小值仍失敗 → 檔案級交由呼叫端重試
            retry_left = retry_count
            while True:
                node["read_event"].clear()
                node["read_data"] = None
                self.send_pkt([target], 0x2007, {
                    "path": remote_path,
                    "offset": offset,
                    "length": req_len
                })
                ok, why = self._wait_evt(node["read_event"], read_timeout)
                if ok:
                    # 🔧 只接受與目前請求 offset 相符的回應;
                    #    延遲的舊 chunk 回應 (重試後才到) 會造成重複寫入 → 忽略
                    if node.get("read_offset") == offset:
                        break
                    self.panel.log("warn", f"⚠️ 忽略錯位回應 off={node.get('read_offset')} expect={offset}")
                    ok = False
                    why = "mismatch"
                if why == "cancel":
                    raise Exception("已停止")
                if retry_left > 0:
                    retry_left -= 1
                    self.panel.log("warn", f"⚠️ 下載讀取逾時/錯位 off={offset} (chunk={req_len}), 重試 ({retry_count - retry_left}/{retry_count})...")
                    continue
                if chunk_size > chunk_min:
                    next_req = chunk_size // 2
                    if next_req < chunk_min:
                        next_req = chunk_min
                    self.panel.log("warn", f"⚠️ 下載超時，chunk {chunk_size} -> {next_req} (path={remote_path}, off={offset})")
                    chunk_size = next_req
                    req_len = chunk_size
                    remain = expected_size - offset
                    if req_len > remain:
                        req_len = remain
                    retry_left = retry_count
                    continue
                raise Exception(f"下載超時 at offset {offset} (chunk={chunk_size})")

            chunk = node["read_data"]
            if not chunk:
                break

            writer(chunk)
            offset += len(chunk)

            elapsed = time.time() - start_time
            speed = (offset / 1024) / elapsed if elapsed > 0 else 0
            progress = (offset / expected_size) * 100 if expected_size > 0 else 100

            self.panel.update_device(
                target,
                upload_progress=progress,
                upload_speed=speed,
                uploaded_bytes=offset,
                total_bytes=expected_size
            )

        return offset, expected_size

    def _download_bytes(self, target, remote_path, expected_size=None, status="下載中"):
        buf = bytearray()
        done, total = self._download_to_writer(
            target,
            remote_path,
            buf.extend,
            expected_size=expected_size,
            status=status
        )
        if total > 0 and done <= 0:
            return None
        return bytes(buf)

    def _download_file(self, target, remote_path, local_path, expected_size=None, status="下載中"):
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            done, total = self._download_to_writer(
                target,
                remote_path,
                f.write,
                expected_size=expected_size,
                status=status
            )
        if total > 0 and done <= 0:
            return False
        return True

    def _run_upload_batch(self, files_to_upload, targets=None, confirm_mode="prompt"):
        if targets is None:
            targets = self.selected_targets

        if not targets:
            return

        # 🔧 正規化: files_to_upload 可以是「全部設備同一份清單」(list),
        #    也可以是「每台設備各自的差異清單」(dict {tid: [(l, r), ...]})。
        if isinstance(files_to_upload, dict):
            files_by_target = {t: list(files_to_upload.get(t, [])) for t in targets}
            seen = set()
            union = []
            for t in targets:
                for l, r in files_by_target.get(t, []):
                    if r not in seen:
                        seen.add(r)
                        union.append((l, r))
            self.last_upload_files = union
        else:
            files_by_target = {t: list(files_to_upload) for t in targets}
            self.last_upload_files = list(files_to_upload)

        self._transfer_begin()

        for tid in targets:
            self.panel.update_device(tid, status="準備中", transfer_label="", upload_progress=0)

        max_workers = self.config.get("max_workers", 10)
        promoted = {}   # tid -> [root_paths]
        promoted_lock = threading.Lock()
        results = {}    # tid -> [(remote_path, status, err)]
        results_lock = threading.Lock()

        def _record(tid, path, status, err=""):
            with results_lock:
                results.setdefault(tid, []).append((path, status, err))
            # 🔧 關鍵事件寫入 log (成功/失敗/略過)
            if status == "ok":
                self._log_event("OK", f"上傳成功 {path}", device_id=tid)
            elif status == "fail":
                self._log_event("FAIL", f"上傳失敗 {path}: {err}", device_id=tid)
            else:
                self._log_event("SKIP", f"上傳略過 {path}: {err or '已停止'}", device_id=tid)

        def _task(tid):
            local_files = files_by_target.get(tid, [])
            local_promoted = []
            try:
                for i, (l_path, r_path) in enumerate(local_files):
                    if self.transfer_cancel.is_set():
                        _record(tid, r_path, "skip", "已停止")
                        continue
                    ok = False
                    last_err = ""
                    # 🔧 block 級重試之後, 檔案級重試 3 次; 失敗「記下並繼續」下一個檔案,
                    #    不再讓單一檔案中斷整台設備的其餘上傳。
                    for retry_count in range(3):
                        if self.transfer_cancel.is_set():
                            break
                        try:
                            with open(l_path, "rb") as f:
                                data = f.read()
                            self._upload_bytes(tid, data, r_path, file_idx=i + 1, total_files=len(local_files))
                            ok = True
                            break
                        except Exception as e:
                            if str(e) == "已停止" or self.transfer_cancel.is_set():
                                _record(tid, r_path, "skip", "已停止")
                                return
                            last_err = str(e)
                            # 🔧 狀態保持「傳輸中」, 重試資訊放 transfer_label (panel 才有進度條)
                            self.panel.update_device(tid, status="上傳中", transfer_label=f"上傳重試 {retry_count + 1} {r_path[:16]}")
                            time.sleep(1)
                    if not ok:
                        _record(tid, r_path, "fail", last_err)
                        continue
                    # 🔧 root 目標: _upload_bytes 已把檔案直接寫到 root (兩段式 commit,
                    #    自動 .bak 備份 + pending)。這裡只需加入「待確認」清單, 之後由
                    #    auto/prompt confirm 清 pending —— 不能再 promote, 因為 promote
                    #    是「/sd 暫存 → root」, 而檔案已經在 root; 硬 promote 會因
                    #    src(/sd/xxx) 不存在而失敗 → pending 永不確認 → 3 次重啟自動
                    #    回滾 → 上傳-回滾無限循環。
                    #    /sd/... 開頭的路徑表示本來就在 /sd, 不搬運 (例如 data.bin 走
                    #    deploy 不在此)。
                    if not r_path.startswith("/sd"):
                        local_promoted.append(r_path)
                        _record(tid, r_path, "ok", "")
                    else:
                        _record(tid, r_path, "ok", "")
                fails = [p for p, s, _ in results.get(tid, []) if s == "fail"]
                if fails:
                    self.panel.update_device(tid, status="錯誤", transfer_label="", error_msg=f"{len(fails)} 檔失敗")
                else:
                    self.panel.update_device(tid, status="完成", transfer_label="", upload_progress=100)
            except Exception as e:
                if str(e) == "已停止" or self.transfer_cancel.is_set():
                    self.panel.update_device(tid, status="已停止", transfer_label="", error_msg="")
                else:
                    self.panel.update_device(tid, status="錯誤", transfer_label="", error_msg=str(e))
            with promoted_lock:
                if local_promoted:
                    promoted[tid] = local_promoted

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_task, tid): tid for tid in targets}
            for f in futures:
                f.result()
        self._transfer_end()
        # 🔧 停止面板: 之後要印上傳結果報告 + 確認提示(需要 input),
        #    不能讓面板每 0.1s 重繪把這些文字覆蓋掉 (否則會「顯示完成卻其實在等 Enter」)。
        if self.panel.running:
            self.panel.stop()
        ConsoleUI.show_cursor()

        self.last_upload_results = results

        # 🔧 每台設備逐檔上傳結果報告 (成功/失敗/略過), 解決「不知道更新了什麼」的問題
        self._print_upload_summary(results)

        # 🔧 promote 後的確認: auto=直接確認 / prompt=手動確認 / none=保留 pending
        if promoted:
            if confirm_mode == "auto":
                self._auto_confirm_promoted(promoted)
            elif confirm_mode == "prompt":
                self._prompt_confirm_promoted(promoted)

    def _print_upload_summary(self, results):
        """列印上傳結果報告: 每台設備每個檔案的 成功/失敗/略過 狀態。"""
        if not results:
            print("\nℹ️ 無上傳結果")
            return
        total_ok = total_fail = total_skip = 0
        print("\n" + "=" * 70)
        print("📊 [上傳結果報告]")
        print("=" * 70)
        for tid in sorted(results.keys()):
            rows = results[tid]
            ok_n = sum(1 for _, s, _ in rows if s == "ok")
            fail_n = sum(1 for _, s, _ in rows if s == "fail")
            skip_n = sum(1 for _, s, _ in rows if s == "skip")
            total_ok += ok_n; total_fail += fail_n; total_skip += skip_n
            mark = "✅" if fail_n == 0 else ("⚠️" if ok_n > 0 else "❌")
            print(f"\n{mark} {tid}: 成功 {ok_n} / 失敗 {fail_n} / 略過 {skip_n}")
            for path, status, err in rows:
                if status == "ok":
                    print(f"    ✅ {path}")
                elif status == "skip":
                    print(f"    ⏭  {path} (略過)")
                else:
                    print(f"    ❌ {path}  — {err}")
        print("-" * 70)
        print(f"📈 合計: 成功 {total_ok} / 失敗 {total_fail} / 略過 {total_skip}")
        if total_fail:
            print("💡 失敗的檔案已支援斷點續傳; 可用「6. 重試失敗/續傳」接續上傳。")
        print("=" * 70)

    def _restore_or_confirm(self):
        """獨立還原/確認功能: 直接下載每台設備的 delta 紀錄 (pending), 找出 .bak 待確認檔案。

        一次下載 /sd/.delta.json 就拿到 pending 清單 (含 .bak 備份), 不必逐檔查詢;
        確認/還原以路徑分組廣播到所有設備, 同時發送。
        """
        targets = [t for t in self.selected_targets if t in self.slaves]
        if not targets:
            print("⚠️ 無在線設備 (請先 Step 1 選擇/掃描)")
            return

        print(f"\n🔍 下載各設備的 delta 紀錄 (pending 清單) — {len(targets)} 台...")
        pending_map = {}   # tid -> {path: rec}
        with ThreadPoolExecutor(max_workers=min(8, len(targets))) as ex:
            futs = {ex.submit(self._download_remote_delta, tid): tid for tid in targets}
            for f in futs:
                tid = futs[f]
                try:
                    pend = f.result() or {}
                except Exception:
                    pend = {}
                pending_map[tid] = pend
                n = len(pend)
                print(f"  {'⚠️' if n else '✅'} {tid}: {n} 個待確認" if n else f"  ✅ {tid}: 無待確認")

        total_pending = sum(len(v) for v in pending_map.values())
        if total_pending == 0:
            print("ℹ️ 沒有待確認的檔案 (.bak 備份不存在或已確認)")
            return

        print(f"\n♻️ [還原/確認] 發現 {total_pending} 個待確認檔案 (.bak 備份存在):")
        for tid, pend in pending_map.items():
            if not pend:
                continue
            print(f"  {tid}:")
            for p in sorted(pend.keys()):
                print(f"    - {p}")
        print("  [u] 還原全部 (回滾到 .bak 舊版)")
        print("  [c] 確認全部 (保留新檔, 刪除 .bak)")
        print("  [Enter] 返回")
        ch = input("👉 請選擇: ").strip().lower()
        if ch == 'u':
            ok_n, fail_n = self._run_confirm_or_undo(pending_map, "undo")
            print(f"♻️ 已還原 {ok_n} 個檔案; 失敗 {fail_n}")
        elif ch == 'c':
            ok_n, fail_n = self._run_confirm_or_undo(pending_map, "confirm")
            print(f"✅ 已確認 {ok_n} 個檔案; 失敗 {fail_n}")

    def _retry_failed_uploads(self):
        """重試上次上傳失敗的檔案 (斷點續傳), 先敲門叫回離線設備再續傳。"""
        files = getattr(self, "last_upload_files", None)
        results = getattr(self, "last_upload_results", None)
        if not files or not results:
            print("ℹ️ 尚無上次上傳紀錄 (請先執行「1. 固件全量更新」)")
            return

        failed_paths = set()
        failed_tids = set()
        for tid, rows in results.items():
            for path, status, _ in rows:
                if status == "fail":
                    failed_paths.add(path)
                    failed_tids.add(tid)
        if not failed_paths:
            print("✅ 上次上傳無失敗檔案")
            return

        path_map = {r: l for l, r in files}
        failed_files = [(path_map[r], r) for r in sorted(failed_paths) if r in path_map]
        if not failed_files:
            print("ℹ️ 失敗檔案已不在本地 slave 目錄中, 無法重試")
            return

        # 🔧 先敲門把離線設備叫回 (IP 紀錄), 之後斷點續傳接續中斷處
        print(f"🔔 先敲門叫回離線設備, 再續傳 {len(failed_files)} 個失敗檔案...")
        self._knock_recorded_devices(wait=8)

        retry_tids = [t for t in sorted(failed_tids) if t in self.slaves]
        if not retry_tids:
            print("⚠️ 失敗的設備目前都未在線, 請先用 Step 1 掃描/敲門")
            return

        print(f"🔁 [Retry] 續傳 → 設備 {len(retry_tids)} 台 / 檔案 {len(failed_files)} 個")
        self._log_event("RETRY", f"重試失敗續傳: {len(retry_tids)} 台設備 / {len(failed_files)} 個檔案")
        self._run_upload_batch(failed_files, targets=retry_tids, confirm_mode="auto")

    def _upload_generic_file(self, tid, local_path, remote_path, file_idx=1, total_files=1):
        try:
            with open(local_path, "rb") as f:
                data = f.read()
        except Exception as e:
            raise Exception(f"Read local file failed: {e}")

        return self._upload_bytes(tid, data, remote_path, file_idx=file_idx, total_files=total_files)

    def _file_explorer(self):
        while True:
            print("\n📂 [文件管理器]")
            print("  1. 上傳文件 (Upload)")
            print("  2. 下載文件 (Download)")
            print("  3. 批次備份全部設備 (Bulk Backup + Profile)")
            print("  q. 返回")
            
            choice = input("\n👉 請選擇: ").strip().lower()
            if choice == '1':
                self._fe_upload()
            elif choice == '2':
                self._fe_download()
            elif choice == '3':
                self._bulk_download_all()
            elif choice == 'q':
                return

    def _fe_upload(self):
        print("\n📤 [手動上傳]")
        
        # 1. 輸入本地路徑 (支持文件或文件夾)
        # 提供默認選項：列出當前目錄
        cwd = os.getcwd()
        print(f"當前目錄: {cwd}")
        files = [f for f in os.listdir(cwd) if os.path.isfile(f) and not f.startswith('.')]
        dirs = [d for d in os.listdir(cwd) if os.path.isdir(d) and not d.startswith('.')]
        
        print("\n[本地文件]")
        for i, f in enumerate(files[:10]):
            print(f"  {i+1}. {f}")
        if len(files) > 10: print("  ...")
        
        local_input = input("\n👉 輸入路徑 (或 '0' 自定義輸入): ").strip().strip('"').strip("'")
        
        if local_input == '0':
            local_input = input("👉 輸入完整路徑: ").strip().strip('"').strip("'")
        elif local_input.isdigit() and int(local_input) > 0 and int(local_input) <= len(files):
            local_input = os.path.abspath(files[int(local_input)-1])
            
        if not os.path.exists(local_input):
            print("❌ 路徑不存在")
            return
            
        is_dir = os.path.isdir(local_input)

        # 2. 掃描文件
        files_to_upload = []
        if is_dir:
            base_dir = os.path.abspath(local_input)
            print(f"\n🔍 掃描 {local_input}...")
            for root, dirs, files in os.walk(base_dir):
                dirs[:] = [d for d in dirs if not is_junk_dir(d)]
                for file in files:
                    if is_junk_name(file):
                        continue
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, base_dir)
                    remote_path = ("/" + rel_path.replace("\\", "/")).replace("//", "/")
                    files_to_upload.append((full_path, remote_path))
        else:
            # 單個文件
            filename = os.path.basename(local_input)
            remote_path = ("/" + filename).replace("//", "/")
            files_to_upload.append((local_input, remote_path))
            
        if not files_to_upload:
            print("❌ 無有效文件")
            return
            
        print(f"\n📦 將上傳 {len(files_to_upload)} 個文件:")
        files_to_upload.sort(key=lambda x: x[1])
        for _, r in files_to_upload:
            print(f"  - {r}")
            
        confirm = input("\n👉 確認上傳? (y/n): ").lower()
        if confirm != 'y':
            return

        self._run_upload_batch(files_to_upload, targets=self.selected_targets)
                
        time.sleep(1)
        print("\n✅ 手動上傳完成")
        # 🔧 讓用戶選擇是否重啟確認 (例如上傳了 .py/.json 等需要重啟的檔案)
        self._reboot_and_confirm(targets=self.selected_targets)
        self.panel.stop()
        ConsoleUI.show_cursor()

    def _fe_download(self):
        if self.panel.running:
            self.panel.stop()
        ConsoleUI.show_cursor()

        target = self.selected_targets[0]
        print(f"\n📥 [下載模式] 連接設備: {target}")
        
        node = self.slaves.get(target)
        if not node:
            print("❌ 設備離線")
            return

        # 1. 獲取 Manifest (🔧 包例外: 設備無響應不再炸掉整個程式)
        print("  正在獲取文件列表 (manifest.json)...")
        man_err = ""
        self._transfer_begin()
        try:
            config_data = self._download_bytes(target, "/manifest.json", expected_size=None, status="Manifest")
        except Exception as e:
            config_data = None
            man_err = str(e)
        finally:
            self._transfer_end()
        if not config_data:
            if self.transfer_cancel.is_set():
                print("ℹ️ 已停止")
                return
            print(f"❌ Manifest 下載失敗: {man_err or '無資料'}")
            input("\n按 Enter 返回...")
            return
        if self.panel.running:
            self.panel.stop()
        ConsoleUI.show_cursor()
            
        try:
            manifest = json.loads(config_data.decode('utf-8'))
        except:
            print("❌ Manifest 解析失敗")
            return
            
        # 2. 顯示文件樹
        paths = sorted(manifest.keys())
        print(f"\n📄 遠端文件 ({len(paths)} 個):")
        for i, p in enumerate(paths):
            info = manifest[p]
            size_kb = info['s'] / 1024
            print(f"  {i+1}. {p:<40} | {size_kb:>6.1f} KB")
            
        # 3. 選擇下載
        dl_choice = input("\n👉 輸入序號下載單個文件，或輸入 'all' 下載全部: ").strip().lower()
        
        files_to_download = []
        if dl_choice == 'all':
            files_to_download = paths
        elif dl_choice.isdigit():
            idx = int(dl_choice) - 1
            if 0 <= idx < len(paths):
                files_to_download = [paths[idx]]
        else:
            # 嘗試匹配路徑
            if dl_choice in paths:
                files_to_download = [dl_choice]
        
        if not files_to_download:
            return
            
        # 下載目錄
        save_dir = os.path.join(DOWNLOAD_DIR, target.replace(":", "_"))
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
            
        print(f"\n📂 保存至: {save_dir}")
        self._transfer_begin()
        
        # 執行下載
        fail_count = 0
        file_retry = self._cfg_int("transfer_retry_count", 3)
        try:
            total_files = len(files_to_download)
            for i, r_path in enumerate(files_to_download):
                if self.transfer_cancel.is_set():
                    break
                l_path = os.path.join(save_dir, r_path.lstrip("/"))
                f_size = manifest[r_path]['s']
                # 🔧 檔案級重試: 失敗重試整個檔案 (最多 file_retry 次)
                ok = False
                last_err = ""
                for attempt in range(1, file_retry + 1):
                    if self.transfer_cancel.is_set():
                        break
                    try:
                        ok = self._download_file(
                            target, r_path, l_path,
                            expected_size=f_size,
                            status=f"下載 {i+1}/{total_files}"
                        )
                        if ok:
                            break
                        last_err = "下載未完成"
                    except Exception as e:
                        last_err = str(e)
                        if str(e) == "已停止" or self.transfer_cancel.is_set():
                            break
                        print(f"  ⚠️ {r_path}: {e} (重試 {attempt}/{file_retry})")
                        time.sleep(1)
                if not ok:
                    fail_count += 1
                    print(f"  ⚠️ {r_path}: 下載失敗, 已重試 {file_retry} 次: {last_err or '未知'} (跳過)")
                    self._log_event("FAIL", f"下載失敗 {r_path}: {last_err or '未知'}", device_id=target)
                else:
                    self._log_event("OK", f"下載成功 {r_path}", device_id=target)

            if self.transfer_cancel.is_set():
                self.panel.update_device(target, status="已停止", transfer_label="", upload_progress=0)
            else:
                self.panel.update_device(target, status="完成", transfer_label="", upload_progress=100)
            if fail_count:
                print(f"  ℹ️ 完成, {fail_count} 個檔案失敗/跳過")
            time.sleep(1)
        finally:
            self._transfer_end()
            self.panel.stop()
            ConsoleUI.show_cursor()
        # 🔧 下載完成後建立 Profile (模式/狀態/延遲 + 檔案清單)
        self._save_profile(target, manifest)
        print("\n✅ 所有下載完成")
        input("按 Enter 返回...")

    def _bulk_download_all(self):
        """一次過下載所有 MCU 的檔案, 依 id 分類存放 + 每 id 一個 Profile。"""
        if self.panel.running:
            self.panel.stop()
        ConsoleUI.show_cursor()

        targets = list(self.slaves.keys())
        if not targets:
            print("❌ 無在線設備")
            input("\n按 Enter 返回...")
            self.panel.start()
            return

        print(f"\n📦 [批次備份] 將下載 {len(targets)} 個設備的全部檔案 → data/downloads/<device_id>/")
        confirm = input("👉 確認? (y/n): ").lower()
        if confirm != 'y':
            self.panel.start()
            return

        ok_count = 0
        fail_count = 0
        for target in targets:
            print(f"\n{'='*56}\n📥 設備: {target}")
            node = self.slaves.get(target)
            if not node:
                print("  ❌ 離線, 跳過")
                fail_count += 1
                continue
            # 🔧 跳過已標離線/無響應的設備, 避免每個都等查詢逾時
            mon = self.panel.monitors.get(target)
            if mon and mon.status in ("離線", "無響應"):
                self.panel.log("warn", f"⚠️ {target}: {mon.status}, 跳過 (先 Scan 或確認設備在線)")
                fail_count += 1
                continue

            # 1. Manifest (🔧 包例外: 設備無響應不再炸掉整個程式)
            man_err = ""
            self._transfer_begin()
            try:
                config_data = self._download_bytes(target, "/manifest.json", expected_size=None, status="Manifest")
            except Exception as e:
                config_data = None
                man_err = str(e)
            finally:
                self._transfer_end()
            if not config_data:
                if self.transfer_cancel.is_set():
                    print("ℹ️ 已停止")
                    break
                print(f"  ❌ Manifest 下載失敗: {man_err or '無資料'}")
                fail_count += 1
                continue
            try:
                manifest = json.loads(config_data.decode('utf-8'))
            except Exception:
                print("  ❌ Manifest 解析失敗")
                fail_count += 1
                continue

            paths = sorted(manifest.keys())
            save_dir = os.path.join(DOWNLOAD_DIR, target.replace(":", "_"))
            os.makedirs(save_dir, exist_ok=True)
            print(f"  📄 {len(paths)} 個檔案 → {save_dir}")

            # 2. 下載全部 (🔧 檔案級重試: 每個檔最多 file_retry 次)
            self._transfer_begin()
            try:
                total = len(paths)
                done = 0
                file_retry = self._cfg_int("transfer_retry_count", 3)
                for r_path in paths:
                    if self.transfer_cancel.is_set():
                        break
                    l_path = os.path.join(save_dir, r_path.lstrip("/"))
                    f_size = manifest[r_path]['s']
                    ok = False
                    for attempt in range(1, file_retry + 1):
                        if self.transfer_cancel.is_set():
                            break
                        try:
                            ok = self._download_file(
                                target, r_path, l_path,
                                expected_size=f_size,
                                status=f"下載 {done+1}/{total}"
                            )
                            if ok:
                                break
                        except Exception as e:
                            if str(e) == "已停止" or self.transfer_cancel.is_set():
                                break
                            print(f"  ⚠️ {r_path}: {e} (重試 {attempt}/{file_retry})")
                            time.sleep(1)
                    if ok:
                        done += 1
                        self._log_event("OK", f"備份下載成功 {r_path}", device_id=target)
                    else:
                        print(f"  ⚠️ {r_path}: 下載失敗, 已重試 {file_retry} 次 (跳過)")
                        self._log_event("FAIL", f"備份下載失敗 {r_path}", device_id=target)
            finally:
                self._transfer_end()

            if self.transfer_cancel.is_set():
                print("ℹ️ 已停止")
                break

            # 🔧 Profile 內附順序標籤: 放一個「檔名為 play_id」的小檔 (內容 = 順序號),
            #    人類/工具打開 profile 資料夾就知道它對應哪個順序位置。
            #    資料夾鍵仍是 device_id (不變), 不用 play_id 命名任何檔案/資料夾。
            try:
                pid = self.config["mapping"].get(target, {}).get("play_id")
                if pid is not None:
                    with open(os.path.join(save_dir, "play_id"), "w", encoding="utf-8") as f:
                        f.write(str(pid))
                    print(f"  🏷️ 順序標籤已寫: play_id = {pid}")
            except Exception as e:
                print(f"  ⚠️ 寫順序標籤失敗: {e}")

            # 3. Profile (模式/狀態/延遲 + 檔案清單)
            if self._save_profile(target, manifest):
                print(f"  💾 Profile 已存: data/profiles/{target.replace(':', '_')}.json")
                ok_count += 1
            else:
                fail_count += 1
            self.panel.update_device(target, status="完成", transfer_label="", upload_progress=100)

        print(f"\n✅ 批次備份完成: 成功 {ok_count} / 失敗 {fail_count}")
        print("   🔎 想查設備可播放什麼模式? Step 8 Profiles 或 Step 7 配對會顯示快取")
        input("\n按 Enter 返回...")
        self.panel.start()

    def _collect_firmware_files(self):
        """掃描本地 slave 目錄, 回傳 [(local_path, remote_path), ...] (過濾垃圾檔)。"""
        slave_dir = os.path.join(PROJECT_ROOT, "slave")
        files_to_upload = []
        for root, dirs, files in os.walk(slave_dir):
            # 修剪垃圾目錄 (Python 快取 / macOS / Windows)
            dirs[:] = [d for d in dirs if not is_junk_dir(d)]
            for file in files:
                if file == "config.json" or is_junk_name(file):
                    continue
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, slave_dir)
                remote_path = "/" + rel_path.replace("\\", "/")
                files_to_upload.append((full_path, remote_path))
        return files_to_upload

    # ==================== 固件更新: manifest 比對 + 逐檔差異上傳 ====================
    def _calc_local_sha(self, local_path):
        """計算本地檔案 sha256 (bytes)。快取以 (路徑, mtime, size) 為鍵, 檔案一改
        就自動失效; 否則本地改檔後再上傳, 比對仍拿舊 sha → 漏傳該更新的檔。"""
        cache = getattr(self, "_local_sha_cache", None)
        if cache is None:
            cache = self._local_sha_cache = {}
        try:
            st = os.stat(local_path)
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        key = (local_path, sig)
        if key in cache:
            return cache[key]
        h = hashlib.sha256()
        with open(local_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        digest = h.digest()
        cache[key] = digest
        return digest

    def _download_manifest_core(self, tid):
        """純下載設備 manifest.json → {remote_path: sha_hex}。失敗/逾時/離線回 None。

        不碰 _transfer_begin/_transfer_end（那些是終端交互的全局狀態，並行下載時不能
        重入），供「並行下載」使用：一台卡住不拖住其他台，由呼叫端用 future timeout
        決定跳過。
        """
        node = self.slaves.get(tid)
        if not node:
            return None
        try:
            data = self._download_bytes(tid, "/manifest.json", expected_size=None, status="Manifest")
        except Exception:
            return None
        if not data:
            return None
        try:
            obj = json.loads(data.decode("utf-8"))
        except Exception:
            return None
        result = {}
        for p, info in obj.items():
            if isinstance(info, dict) and "h" in info:
                result[p] = info["h"]
            elif isinstance(info, dict) and "sha256" in info:
                result[p] = info["sha256"]
        return result

    def _query_remote_sha(self, tid, remote_path, timeout=2.0):
        """查詢單一遠端檔案的 sha256 (bytes) 或 None (不存在/逾時)。"""
        node = self.slaves.get(tid)
        if not node:
            return None
        node["query_event"].clear()
        node["remote_sha"] = None
        node["remote_exists"] = 0
        self.send_pkt([tid], 0x2005, {"path": remote_path})
        if not node["query_event"].wait(timeout=timeout):
            return None
        if not node.get("remote_exists", 0):
            return None
        return node.get("remote_sha")

    def _diff_against_manifest(self, tid, files_to_upload, man):
        """用已下載的 manifest 比對本地/遠端, 回傳差異清單 [(local_path, remote_path), ...]。

        man 由呼叫端 (並行下載後) 傳入；None 表示該台 manifest 拿不到 → 回空清單,
        由呼叫端把該台標記為「跳過」。不再逐檔查詢 sha——135 檔 × 每檔 timeout 會
        把整批拖垮 (掉線設備會這樣卡住全部)。
        """
        if not man:
            return []
        diff = []
        for l_path, r_path in files_to_upload:
            local_hex = self._calc_local_sha(l_path).hex()
            remote_hex = man.get(r_path)
            if remote_hex is None or remote_hex != local_hex:
                diff.append((l_path, r_path))
        return diff

    def _wait_fs_scan_idle(self, tids, timeout=30.0):
        """等各設備 root flash 掃描完成 (bus.shared.fs_scan_requested 歸零)。

        掃描由 slave 端 FsScanTask 非同步執行, 用 0x1101 STATUS_GET 輪詢
        fs_scan_busy 旗標 (status_actions 註冊的 provider)。舊韌體無此 provider
        時該台讀不到 busy → 視為「不阻塞」直接放行 (舊韌體 manifest 本就不可靠)。
        """
        deadline = time.time() + timeout
        pending = set(tids)
        while pending and time.time() < deadline:
            for tid in list(pending):
                st = self.query_status(tid, timeout=2.0)
                if st is None:
                    continue          # 離線/逾時: 留在 pending, 由 deadline 兜底
                if not st.get("fs_scan_busy", 0):
                    pending.discard(tid)
            if pending:
                time.sleep(0.5)
        if pending:
            print(f"⚠️ {len(pending)} 台掃描未在 {timeout:.0f}s 內完成, 以現有 manifest 繼續")

    def _update_firmware_files(self):
        files_to_upload = self._collect_firmware_files()
        targets = [t for t in self.selected_targets if t in self.slaves]
        if not targets:
            print("⚠️ 無在線設備 (請先 Step 1 選擇/掃描)")
            return

        print(f"\n🔍 本地固件 {len(files_to_upload)} 個文件; 目標設備 {len(targets)} 台")
        print("📡 並行下載設備 manifest (哈希表) 並比對, 找出需要更新的檔案...")

        # 🔧 每次更新先清掉舊哈希表緩存, 強制每台重新下載 manifest——
        #    否則「上傳後再跑一次」會拿到過期快取, 比對永遠顯示「全部一致」。
        self._firmware_manifest_cache = {}

        # 🔧 直接下載 manifest 比對, 不再每次觸發 root 重掃 (0x200B):
        #    manifest 是 write-through 的權威哈希表, 上傳/還原/確認/刪除都會同步更新;
        #    每跑一次就重掃會拖慢整批, 且掃描本身若未完成會誤用過期 manifest。
        #    需要手動重建時, 用 Step 0 選單的「4. 重建文件索引 (Scan)」。

        # 🔧 並行下載 manifest: 一台卡住/掉線不阻塞其餘設備；個別逾時直接跳過。
        manifests = {}   # tid -> dict|None
        ex = ThreadPoolExecutor(max_workers=min(16, len(targets)))
        futs = {ex.submit(self._download_manifest_core, tid): tid for tid in targets}
        for f in futs:
            tid = futs[f]
            try:
                man = f.result(timeout=20.0)
            except Exception:
                man = None
            manifests[tid] = man
            self._firmware_manifest_cache[tid] = man if man is not None else {}
        ex.shutdown(wait=False)   # 🔧 不阻塞: 慢/卡住的線程在後台自然結束, 不拖住整批

        diff_by_target = {}
        skipped = []
        for tid in targets:
            man = manifests.get(tid)
            if man is None:
                skipped.append(tid)
                continue
            diff_by_target[tid] = self._diff_against_manifest(tid, files_to_upload, man)

        # 🔧 剔除 manifest 拿不到的設備, 本次上傳/重啟不再碰它們 (多半半死/掉線)
        if skipped:
            for tid in skipped:
                self.panel.update_device(tid, status="錯誤", error_msg="manifest 下載失敗")
            targets = [t for t in targets if t not in skipped]

        total_diff = sum(len(v) for v in diff_by_target.values())
        print("\n📊 [比對結果]")
        for tid in skipped:
            print(f"  ⏭  {tid}: manifest 下載失敗 → 跳過")
        for tid in targets:
            d = diff_by_target[tid]
            if not d:
                print(f"  ✅ {tid}: 無需更新 (全部一致)")
            else:
                print(f"  ⚠️ {tid}: {len(d)} 個檔案需要更新")
                for _, r in d:
                    print(f"      - {r}")

        if total_diff == 0:
            if skipped:
                print(f"\n⚠️ {len(skipped)} 台因 manifest 下載失敗被跳過, 其餘設備已一致")
            else:
                print("\n✅ 所有設備的固件都與本地一致, 無需上傳")
            return

        print("\n👉 請選擇上傳方式:")
        print("  [Enter] 全部更新 (只傳差異 + 直接確認 + 軟重啟) ← 預設")
        print("  [p] 全部更新 (只傳差異 + 上傳後手動確認 + 軟重啟)")
        print("  [s] 逐檔上傳 (一個一個來, 每檔即時進度 + 直接確認)")
        print("  [1] 挑選單一檔案上傳到全部設備")
        print("  [q] 返回")
        ch = input("👉 請選擇: ").strip().lower()

        if ch == "q":
            return
        elif ch == "p":
            self._run_upload_batch(diff_by_target, targets=targets, confirm_mode="prompt")
        elif ch == "s":
            self._upload_files_sequential(diff_by_target, targets=targets)
            return
        elif ch == "1":
            self._upload_single_file_interactive(files_to_upload, targets=targets)
            return
        elif ch in ("", "a", "y", "yes"):
            self._run_upload_batch(diff_by_target, targets=targets, confirm_mode="auto")
        else:
            print("❌ 無效選擇")
            return

        time.sleep(1)
        print("\n✅ 固件更新完成")
        # 🔧 預設軟重啟 (直接確認已在上傳流程內處理)
        self._reboot_and_confirm(targets=targets, default_yes=True)

    def _bootstrap_root_fix(self):
        """一次性引導: 把「修復了上傳路徑」的檔案用舊韌體仍支援的 promote 流程
        (上傳到 /sd → 0x2011 promote 到 root → confirm) 推到每台設備 root。

        背景: 設備目前仍跑舊韌體 (on_file_begin 把 root 路徑強制 resolve 到 /sd),
        導致新版 PC 端的「直接寫 root」失效 (上傳落 /sd, root manifest 沒更新)。
        但舊韌體仍支援 0x2011 promote, 用它做一次性引導。重啟後設備跑新韌體,
        之後的「固件全量更新」就能直接寫 root, 不再需要這個引導。
        """
        targets = [t for t in self.selected_targets if t in self.slaves]
        if not targets:
            print("⚠️ 無在線設備 (請先 Step 1 選擇/掃描)")
            return

        boot_files = [
            (os.path.join(PROJECT_ROOT, "slave", "action", "file_actions.py"), "/action/file_actions.py"),
            (os.path.join(PROJECT_ROOT, "slave", "lib", "sys", "fs_manager.py"), "/lib/sys/fs_manager.py"),
            (os.path.join(PROJECT_ROOT, "slave", "action", "status_actions.py"), "/action/status_actions.py"),
            # 🔧 ConfigManager: 補上「寫 config 後立刻 reset 丟寫入」的 os.sync 修正
            (os.path.join(PROJECT_ROOT, "slave", "lib", "sys", "ConfigManager.py"), "/lib/sys/ConfigManager.py"),
        ]
        for l, r in boot_files:
            if not os.path.isfile(l):
                print(f"❌ 找不到 {l}")
                return

        print(f"\n🚑 [Bootstrap] 把修復檔 promote 到 {len(targets)} 台設備的 root (一次性引導)...")
        print("   流程: 上傳到 /sd → promote 到 root → confirm → 重啟")

        self._transfer_begin()
        try:
            for tid in targets:
                for l, r in boot_files:
                    if self.transfer_cancel.is_set():
                        break
                    try:
                        with open(l, "rb") as f:
                            data = f.read()
                        self.panel.update_device(tid, status="上傳中", transfer_label=f"boot {r[:16]}")
                        # 明確寫 /sd 暫存區 (舊韌體對 /sd 前綴不會再 resolve)
                        self._upload_bytes(tid, data, "/sd" + r)
                        if not self._promote_file(tid, r):
                            self.panel.log("warn", f"⚠️ [{tid}] {r}: promote 失敗")
                            self._log_event("FAIL", f"bootstrap promote 失敗 {r}", device_id=tid)
                            continue
                        if not self._confirm_file(tid, r):
                            self.panel.log("warn", f"⚠️ [{tid}] {r}: confirm 失敗 (pending 未清)")
                            self._log_event("FAIL", f"bootstrap confirm 失敗 {r}", device_id=tid)
                            continue
                        self.panel.log("ok", f"✅ [{tid}] {r}: 已引導到 root")
                        self._log_event("OK", f"bootstrap {r}", device_id=tid)
                    except Exception as e:
                        self.panel.log("err", f"❌ [{tid}] {r}: {e}")
                        self._log_event("FAIL", f"bootstrap 失敗 {r}: {e}", device_id=tid)
                self.panel.update_device(tid, status="完成", transfer_label="")
        finally:
            self._transfer_end()

        print("\n✅ Bootstrap 完成, 準備重啟讓新韌體生效...")
        self._reboot_and_confirm(targets=targets, default_yes=True)

    def _play_id_str(self, sid):
        """slave_map 的 play_id → 'P03' 形式 (人讀順序標籤; 沒有就 'P??')。"""
        pid = self.config["mapping"].get(sid, {}).get("play_id")
        if pid is None:
            return "P??"
        try:
            return "P%02d" % int(pid)
        except Exception:
            return "P" + str(pid)

    def _batch_config_update(self):
        """批量 Config 更新: 依「順序 (play_id)」把 config 批量上傳。

        設計 (使用者需求):
        - 40+ 台只有三套 config。每台設備的 profile 資料夾
          (data/downloads/<device_id>/) 放一份 config.json; 資料夾鍵仍是
          device_id (不變, 不用 play_id 命名任何檔案/資料夾), 順序靠資料夾裏
          一個檔名為 `play_id` 的小檔 (內容 = 順序號)。
        - 設備 ID 難讀 → 顯示/排序一律用 play_id 順序。
        - 上傳走既有兩段式 commit (自動 .bak → confirm), 有 .bak 保護,
          不必先完整下載舊 config; 上傳後 sha 驗證 + 可選軟重啟生效。
        - 🔧 最保險: 上傳前先把每台現有的 /config.json 下載留底進該台 profile
          (config.backup.<時間戳>.json); 下載失敗就跳過該台, 不覆蓋。
        """
        if self.panel.running:
            self.panel.stop()
        ConsoleUI.show_cursor()

        print("\n⚙️  [批量 Config 更新]")
        print("Config 來源:")
        print("  1. 每台設備自己的 Profile (data/downloads/<device_id>/config.json) ← 推薦")
        print("  2. 單一檔案 → 已選中設備 (整組同一份, 例如三套模板之一)")
        mode = input("👉 選擇 (1/2): ").strip()
        if mode not in ("1", "2"):
            print("❌ 無效選擇")
            input("\n按 Enter 返回...")
            self.panel.start()
            return

        def _read_play_id(d):
            """讀 profile 資料夾裏的 play_id 標籤檔 → int 或 None。"""
            p = os.path.join(d, "play_id")
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        return int(f.read().strip())
                except Exception:
                    return None
            return None

        def _pid(pid_val):
            return ("P%02d" % pid_val) if isinstance(pid_val, int) else "P??"

        plan = []   # (pid_or_None, device_id, local_path, data, sha_hex)
        skip = []   # (pid_or_None, device_id, why)

        if mode == "1":
            # 掃所有 profile 資料夾 (鍵 = device_id), 依裏面的 play_id 檔排序
            if not os.path.isdir(DOWNLOAD_DIR):
                print("❌ 無 profile 資料夾 (先跑 Step 8 → 3 批次備份, 或手動放 config)")
                input("\n按 Enter 返回...")
                self.panel.start()
                return
            rows = []
            for name in sorted(os.listdir(DOWNLOAD_DIR)):
                d = os.path.join(DOWNLOAD_DIR, name)
                if not os.path.isdir(d):
                    continue
                cfg = os.path.join(d, "config.json")
                if not os.path.isfile(cfg):
                    skip.append((_read_play_id(d), name, "profile 缺 config.json"))
                    continue
                with open(cfg, "rb") as f:
                    data = f.read()
                rows.append((_read_play_id(d), name, cfg, data, hashlib.sha256(data).hexdigest()))
            # 有 play_id 的依順序; 沒標籤的放後面依資料夾名
            rows.sort(key=lambda r: (r[0] is None, r[0] if r[0] is not None else 0, r[1]))
            plan = rows
        else:
            single_path = input("👉 模板檔路徑 (例: tools/PC/configs/group_A.json): ").strip().strip('"')
            if not os.path.isfile(single_path):
                print(f"❌ 找不到檔案: {single_path}")
                input("\n按 Enter 返回...")
                self.panel.start()
                return
            with open(single_path, "rb") as f:
                data = f.read()
            sha_hex = hashlib.sha256(data).hexdigest()

            online_sorted = sorted(
                list(self.slaves.keys()),
                key=lambda sid: self.config["mapping"].get(sid, {}).get("play_id", 999),
            )
            if not online_sorted:
                print("❌ 無在線設備")
                input("\n按 Enter 返回...")
                self.panel.start()
                return
            targets = [t for t in online_sorted if t in self.selected_targets]
            if not targets:
                print("⚠️ 尚未選擇設備。可在此直接挑選 (依 PlayID 順序):")
                print("-" * 58)
                for i, sid in enumerate(online_sorted):
                    ip = self.config["mapping"].get(sid, {}).get("ip", "?")
                    print(f" {i+1:2d}. {self._play_id_str(sid)}  {sid}  ({ip})")
                print("-" * 58)
                ch = input("👉 輸入編號 (例: 1,2,3-10 / a 全選): ").strip().lower()
                if ch == "a":
                    targets = online_sorted[:]
                else:
                    indices = self._parse_index_ranges(ch, len(online_sorted))
                    if not indices:
                        print("❌ 輸入無效")
                        input("\n按 Enter 返回...")
                        self.panel.start()
                        return
                    targets = [online_sorted[i] for i in sorted(indices)]
                self.selected_targets = targets
            if not targets:
                print("❌ 無目標設備")
                input("\n按 Enter 返回...")
                self.panel.start()
                return
            for tid in targets:
                pid = self.config["mapping"].get(tid, {}).get("play_id")
                plan.append((pid, tid, single_path, data, sha_hex))

        # 計劃表 (依順序; 離線的照樣列出, 稍後標記)
        print("\n📋 [計劃]")
        online_ids = set(self.slaves.keys())
        for pid, dev_id, l_path, _data, sha_hex in plan:
            online = "在線" if dev_id in online_ids else "離線"
            print(f"   {_pid(pid)}  {dev_id}  ({online})  ← {l_path}  (sha {sha_hex[:8]})")
        for pid, dev_id, why in skip:
            print(f"   {_pid(pid)}  {dev_id}  ⏭ 跳過 ({why})")

        upload_rows = [r for r in plan if r[1] in online_ids]
        offline_rows = [r for r in plan if r[1] not in online_ids]
        if not upload_rows:
            print("❌ 沒有在線設備可上傳 (離線的之後上線再跑一次即可)")
            input("\n按 Enter 返回...")
            self.panel.start()
            return

        confirm = input(f"\n👉 確認上傳到 {len(upload_rows)} 台在線設備? (y/n): ").lower()
        if confirm != 'y':
            self.panel.start()
            return

        print("\n⚠️ 提醒:")
        print("   - 上傳前會先下載每台現有 config 留底: profile/config.backup.<時間戳>.json (下載失敗就跳過該台)")
        print("   - 若設備還沒做 Step 0 → 8 引導修復, 舊韌體會把 /config.json 導向 /sd 而無效")
        print('   - 模板請把 System.cID 留 "" — 重啟後每台會自動填回自己的 cID')
        print("   - config 要軟重啟才生效 (完成後會問)")

        self._transfer_begin()
        results = {}
        lock = threading.Lock()

        def _task(row):
            pid, tid, l_path, data, sha_hex = row
            try:
                if self.transfer_cancel.is_set():
                    raise Exception("已停止")
                # 🔧 上傳前先下載現有 config 留底 (最保險: 有本地備份才覆蓋)
                try:
                    old = self._download_bytes(tid, "/config.json", expected_size=None, status="備份 Config")
                except Exception as e:
                    raise Exception("備份下載失敗, 跳過上傳: %s" % e)
                if old is None:
                    raise Exception("備份下載失敗 (無資料), 跳過上傳")
                backup_dir = os.path.join(DOWNLOAD_DIR, tid.replace(":", "_"))
                os.makedirs(backup_dir, exist_ok=True)
                backup_name = "config.backup.%s.json" % time.strftime("%Y%m%d_%H%M%S")
                backup_path = os.path.join(backup_dir, backup_name)
                with open(backup_path, "wb") as f:
                    f.write(old)

                self._upload_bytes(tid, data, "/config.json")
                confirm_ok = self._confirm_file(tid, "/config.json")
                rsha = self._query_remote_sha(tid, "/config.json")
                sha_ok = (rsha is not None and rsha.hex() == sha_hex)
                if confirm_ok and sha_ok:
                    status = "ok"
                elif not confirm_ok:
                    status = "confirm-fail"
                else:
                    status = "sha-mismatch"
                with lock:
                    results[tid] = (status, os.path.basename(backup_path))
            except Exception as e:
                with lock:
                    results[tid] = ("err", str(e))

        try:
            with ThreadPoolExecutor(max_workers=self._cfg_int("max_workers", 10)) as ex:
                futs = [ex.submit(_task, r) for r in upload_rows]
                for f in futs:
                    f.result()
        finally:
            self._transfer_end()
        # 🔧 停止面板再印報告 (面板每 0.1s 重繪會把報告文字覆蓋掉)
        if self.panel.running:
            self.panel.stop()
        ConsoleUI.show_cursor()

        # 結果表 (依順序)
        print("\n📊 [結果]")
        ok_tids = []
        marks = {"ok": "✅ 成功", "confirm-fail": "⚠️ confirm 失敗 (pending 未清)",
                 "sha-mismatch": "❌ sha 不符", "err": "❌"}
        for pid, tid, l_path, _data, _sha in upload_rows:
            st, detail = results.get(tid, ("err", "無結果"))
            print(f"   {_pid(pid)}  {tid}  {marks.get(st, st)}" + (f"  ({detail})" if detail else ""))
            if st == "ok":
                ok_tids.append(tid)
        for pid, tid, why in skip:
            print(f"   {_pid(pid)}  {tid}  ⏭ 跳過 ({why})")
        for pid, tid, _l, _d, _s in offline_rows:
            print(f"   {_pid(pid)}  {tid}  📴 離線未上傳 (上線後重跑即可)")

        if ok_tids:
            print(f"\n✅ 成功 {len(ok_tids)} 台")
            self._reboot_and_confirm(targets=ok_tids, default_yes=True)
        self.panel.start()

    def _upload_single_file_to_targets(self, l_path, r_path, targets, confirm_mode="auto"):
        """上傳單一檔案到多台設備 (逐台, 每台即時進度), 之後 promote + 確認。"""
        try:
            with open(l_path, "rb") as f:
                data = f.read()
        except Exception as e:
            print(f"  ❌ 讀取本地檔案失敗: {e}")
            return

        self._transfer_begin()
        promoted = {}
        try:
            for i, tid in enumerate(targets, 1):
                if self.transfer_cancel.is_set():
                    break
                self.panel.update_device(tid, status="準備中", transfer_label=f"{r_path[:16]}", upload_progress=0)
                try:
                    self._upload_bytes(tid, data, r_path, file_idx=i, total_files=len(targets))
                except Exception as e:
                    self.panel.update_device(tid, status="錯誤", transfer_label="", error_msg=str(e))
                    print(f"  ❌ {tid} {r_path}: {e}")
                    continue
                if not r_path.startswith("/sd"):
                    # 🔧 root 檔案已由 _upload_bytes 直接寫到 root (含 .bak + pending),
                    #    只需待確認; 不再 promote (見 _run_upload_batch 同款修正)。
                    promoted.setdefault(tid, []).append(r_path)
                    print(f"  ✅ {tid} {r_path}: 上傳完成 (待確認)")
                else:
                    print(f"  ✅ {tid} {r_path}: 上傳完成")
                self.panel.update_device(tid, status="完成", transfer_label="", upload_progress=100)
        finally:
            self._transfer_end()
        self.last_upload_files = [(l_path, r_path)]
        if promoted:
            if confirm_mode == "auto":
                self._auto_confirm_promoted(promoted)
            else:
                self._prompt_confirm_promoted(promoted)

    def _upload_files_sequential(self, diff_by_target, targets):
        """逐檔上傳: 對每個「有差異」的檔案, 一個一個上傳到受影響設備 (即時進度 + 直接確認)。"""
        seen = set()
        union = []
        for tid in targets:
            for l, r in diff_by_target.get(tid, []):
                if r not in seen:
                    seen.add(r)
                    union.append((l, r))
        if not union:
            print("ℹ️ 無差異檔案")
            return
        print(f"\n🔄 [逐檔上傳] 共 {len(union)} 個差異檔案, 一個一個上傳...")
        for idx, (l, r) in enumerate(union, 1):
            print(f"\n── [{idx}/{len(union)}] {r} ──")
            affected = [tid for tid in targets if any(rr == r for _, rr in diff_by_target.get(tid, []))]
            self._upload_single_file_to_targets(l, r, affected, confirm_mode="auto")
        print("\n✅ 逐檔上傳完成")
        time.sleep(1)
        self._reboot_and_confirm(targets=targets)

    def _upload_single_file_interactive(self, files_to_upload, targets):
        """挑選單一 (或多個) 檔案上傳到全部設備。"""
        print("\n📄 [單檔上傳] 選擇要上傳的檔案:")
        for i, (_, r) in enumerate(files_to_upload, 1):
            print(f"  {i:3d}. {r}")
        sel = input("👉 輸入編號 (逗號分隔, 例: 1,3,5): ").strip()
        if not sel:
            return
        indices = []
        try:
            for part in sel.replace("，", ",").split(","):
                idx = int(part.strip()) - 1
                if 0 <= idx < len(files_to_upload):
                    indices.append(idx)
        except Exception:
            print("❌ 輸入無效")
            return
        if not indices:
            print("❌ 未選擇有效檔案")
            return
        chosen = [files_to_upload[i] for i in indices]
        print(f"\n📦 將上傳 {len(chosen)} 個檔案到 {len(targets)} 台設備:")
        for _, r in chosen:
            print(f"    - {r}")
        ch = input("👉 上傳後直接確認? (y=直接確認 / Enter=手動確認): ").strip().lower()
        confirm_mode = "auto" if ch == "y" else "prompt"
        for l, r in chosen:
            print(f"\n── {r} ──")
            self._upload_single_file_to_targets(l, r, targets, confirm_mode=confirm_mode)
        time.sleep(1)
        print("\n✅ 單檔上傳完成")
        self._reboot_and_confirm(targets=targets)

    def _modify_config(self):
        target = self.selected_targets[0]
        print(f"\n📥 從 {target} 下載 config.json...")
        
        node = self.slaves.get(target)
        if not node:
            print("❌ 設備離線")
            return

        # Query SHA and Size
        node["query_event"].clear()
        node["remote_sha"] = None
        node["remote_size"] = 0
        self.send_pkt([target], 0x2005, {"path": "/config.json"})
        ok, why = self._wait_evt(node["query_event"], 3.0)
        if ok:
            sha_hex = node["remote_sha"].hex() if node["remote_sha"] else "None"
            size = node["remote_size"]
            print(f"  Remote SHA: {sha_hex}")
            print(f"  Remote Size: {size} bytes")
        else:
            if why == "cancel":
                self.panel.update_device(target, status="已停止", transfer_label="", error_msg="")
                return
            print("⚠️ 查詢超時, 無法獲取文件大小")
            return
            
        err_msg = ""
        self._transfer_begin()
        try:
            config_data = self._download_bytes(target, "/config.json", expected_size=size, status="Config")
        except Exception as e:
            config_data = None
            err_msg = str(e)
        finally:
            self._transfer_end()

        if not config_data:
            self.panel.update_device(target, status="錯誤", transfer_label="")
            print(f"❌ Config 下載失敗{': ' + err_msg if err_msg else ''}")
            return

        self.panel.update_device(target, status="完成", transfer_label="", upload_progress=100)
        temp_path = os.path.join(DATA_DIR, "temp_config.json")
        
        try:
            # Format JSON for easier editing
            # decode bytes to string for json.loads
            json_str = config_data.decode('utf-8')
            json_obj = json.loads(json_str)
            with open(temp_path, "w", encoding='utf-8') as f:
                json.dump(json_obj, f, indent=4, ensure_ascii=False)
        except:
            # Binary write if not valid json
            with open(temp_path, "wb") as f:
                f.write(config_data)
                
        print(f"✅ 已保存到 {temp_path}")
        print("👉 請編輯該文件。")
        
        # Open editor
        if os.name == 'nt':
            os.system(f"start notepad {temp_path}")
            
        input("\n⌨️  編輯完成後按 Enter 繼續上傳...")
        
        if not os.path.exists(temp_path):
            print("❌ 文件不存在")
            return
            
        confirm = input(f"👉 確認上傳到 {len(self.selected_targets)} 個設備? (y/n): ").lower()
        if confirm != 'y':
            return

        self._run_upload_batch([(temp_path, "/config.json")], targets=self.selected_targets)

        for tid in self.selected_targets:
            self.panel.update_device(tid, status="配置更新", upload_progress=100)
                
        print("\n✅ Config 更新完成")
        time.sleep(1)
        # 🔧 配置變更多數要重啟才生效 → 讓用戶選擇重啟並確認回連
        self._reboot_and_confirm(targets=self.selected_targets)
        
    def _delete_file(self):
        remote_path = input("\n👉 輸入要刪除的文件/目錄路徑 (e.g. /app.py): ").strip()
        if not remote_path:
            return
            
        if not remote_path.startswith("/"):
            remote_path = "/" + remote_path
            
        confirm = input(f"⚠️ 確認刪除 {len(self.selected_targets)} 個設備上的 '{remote_path}'? (y/n): ").lower()
        if confirm != 'y':
            return
            
        print("\n🗑️ 開始刪除...")
        
        for tid in self.selected_targets:
            node = self.slaves.get(tid)
            if not node:
                print(f"  ❌ {tid}: 離線")
                continue
                
            try:
                # Reset Query State
                node["query_event"].clear()
                node["remote_exists"] = 1 # 默認假設存在，等待更新
                
                # Send Delete (0x2009)
                self.send_pkt([tid], 0x2009, {"path": remote_path})
                
                # Wait for Query Response (0x2006)
                if node["query_event"].wait(timeout=3.0):
                    if node["remote_exists"] == 0:
                        print(f"  ✅ {tid}: 刪除成功 (或已不存在)")
                    else:
                        print(f"  ⚠️ {tid}: 刪除失敗 (文件仍存在)")
                else:
                    print(f"  ⚠️ {tid}: 無回應")
                    
            except Exception as e:
                print(f"  ❌ {tid}: {e}")
                
        input("\n按 Enter 返回...")
        
    def _scan_files(self):
        """重建文件索引 (入口, 選範圍):

          1. 本地 flash (/manifest.json) — 剷除 + 重啟, 開機自動重掃
          2. SD (/sd/.manifest.json) — 0x200B(target=1) 主動掃描重建全表
             (SD manifest 平時 delta 維護, 唔主動掃; 呢個係主動重建)
          3. 兩樣都做 (先 SD 後本地: 本地會重啟設備)
        """
        print("\n🔄 [重建文件索引]")
        print("  1. 本地 flash (/manifest.json) — 剷除 + 重啟, 開機自動重掃")
        print("  2. SD (/sd/.manifest.json) — 主動掃描重建全表 (平時 delta 維護)")
        print("  3. 兩樣都做 (先 SD 後本地)")
        choice = (input("\n👉 請選擇 (1/2/3) [Enter=1]: ").strip() or "1")
        if choice == "2":
            self._scan_files_sd()
        elif choice == "3":
            self._scan_files_sd()
            self._scan_files_local()
        else:
            self._scan_files_local()
        input("\n按 Enter 返回...")

    def _scan_files_sd(self):
        """SD 主動掃描 (0x200B target=1): 重建 /sd/.manifest.json 全表。

        唔加新指令: 0x200B 冇回覆, 靠 STATUS_GET 嘅 fs_scan_busy 旗標
        (slave scan_sd 置 fs_scan_sd_busy=1, provider 已含 SD) 確認
        「開始咗 (busy=1) → 做完 (busy=0)」。
        """
        print("\n🔄 [SD 重建] 送 0x200B(target=1) 主動掃描 /sd ...")
        targets = [t for t in self.selected_targets if t in self.slaves]
        for tid in self.selected_targets:
            if tid not in self.slaves:
                print(f"  ❌ {tid}: 離線 (跳過)")
        for tid in targets:
            try:
                self.send_pkt([tid], 0x200B, {"target": 1})
                print(f"  → {tid}: 指令已送出")
            except Exception as e:
                print(f"  ❌ {tid}: {e}")
        if not targets:
            return

        # ── 階段 1: 確認「開始咗」(busy=1), 最多 5s ──
        print("\n⏳ 等 SD 掃描開始 (busy=1)...")
        started = set()
        deadline = time.time() + 5.0
        while time.time() < deadline and len(started) < len(targets):
            for tid in targets:
                if tid in started:
                    continue
                st = self.query_status(tid, timeout=1.5)
                if st is not None and st.get("fs_scan_busy", 0):
                    started.add(tid)
                    print(f"  ✅ {tid}: 掃描已開始")
            if len(started) < len(targets):
                time.sleep(0.3)
        for tid in targets:
            if tid not in started:
                print(f"  ⚠️ {tid}: 未見 busy=1 (舊韌體冇 SD busy 旗標?) — 照等完成")

        # ── 階段 2: 等掃描完成 (busy=0), 最多 90s ──
        print("\n⏳ 等 SD 掃描完成 (最多 90s)...")
        self._wait_fs_scan_idle(targets, timeout=90.0)
        for tid in targets:
            if tid not in started:
                print(f"  ⚠️ {tid}: 舊韌體冇 SD busy — 無法確認, 請自行驗證 manifest")
                continue
            st = self.query_status(tid, timeout=2.0)
            if st is None:
                print(f"  ❌ {tid}: 查無狀態 (離線?)")
            elif st.get("fs_scan_busy", 0):
                print(f"  ⚠️ {tid}: 90s 內未完成 (大檔較多, 可再確認)")
            else:
                print(f"  ✅ {tid}: SD 表重建完成")

    def _scan_files_local(self):
        """本地 flash 重建索引: 剷除 /manifest.json → 設備自己重啟 → 開機自動重掃。

        唔加新指令、唔等回覆 (重用舊指令 0x2009): slave 剷完 manifest 即刻
        self-reset 且唔回覆, master 見到 WS 斷線 = 已執行; 設備重新上線後
        開機背景掃描已重建 manifest, 再輪詢 fs_scan_busy 確認完成。
        0x2004 係 chunk ACK、0x2006 係查詢回覆, 語意都唔啱呢度 — 用
        「通道斷線」本身做確認最直接。
        """
        print("\n🔄 [本地重建] 剷除 /manifest.json → 設備重啟 → 開機自動重掃")
        targets = [t for t in self.selected_targets if t in self.slaves]
        for tid in self.selected_targets:
            if tid not in self.slaves:
                print(f"  ❌ {tid}: 離線 (跳過)")
        for tid in targets:
            try:
                self.send_pkt([tid], 0x2009, {"path": "/manifest.json"})
                print(f"  → {tid}: 已送出剷除指令 (設備會即刻重啟, WS 斷線 = 已執行)")
            except Exception as e:
                print(f"  ❌ {tid}: {e}")
        if not targets:
            return

        # ── 階段 1: 等 WS 斷線 (設備 self-reset 嘅證明), 最多 10s ──
        print("\n⏳ 等設備重啟 (WS 斷線)...")
        dropped = set()
        deadline = time.time() + 10.0
        while len(dropped) < len(targets) and time.time() < deadline:
            for tid in targets:
                if tid not in dropped and tid not in self.slaves:
                    dropped.add(tid)
                    print(f"  ✅ {tid}: 已重啟 (WS 斷線)")
            if len(dropped) < len(targets):
                time.sleep(0.2)
        for tid in targets:
            if tid not in dropped:
                print(f"  ⚠️ {tid}: 10s 內未見斷線 (舊韌體冇 self-reset?) — 仍會等佢上線")

        # ── 階段 2: 等設備重新上線 (開機自動連回 stored master), 最多 60s ──
        print("\n⏳ 等設備重新上線 (開機自動連回 + 背景重掃)...")
        back = set()
        deadline = time.time() + 60.0
        while len(back) < len(targets) and time.time() < deadline:
            for tid in targets:
                if tid not in back and tid in self.slaves:
                    back.add(tid)
                    print(f"  👋 {tid}: 已上線")
            if len(back) < len(targets):
                time.sleep(0.5)
        for tid in targets:
            if tid not in back:
                print(f"  ❌ {tid}: 60s 內未回線 (用選單 1 手動掃描/敲門叫回)")

        # ── 階段 3: 等開機背景掃描完成 (fs_scan_busy 歸零) ──
        if back:
            print("\n⏳ 等開機掃描完成 (core1 背景, 唔會頂看門狗)...")
            self._wait_fs_scan_idle(sorted(back), timeout=30.0)
            for tid in sorted(back):
                if tid not in dropped:
                    # 冇斷線 = 冇重啟 → 唔會有開機重掃 (舊韌體冇 self-reset 特例,
                    # 只係回咗 0x2006 + 剷咗 manifest, 唔會自動重建索引)
                    print(f"  ⚠️ {tid}: 未見重啟 (舊韌體冇 self-reset?) — manifest 已剷但索引未重建, 請手動重啟/部署新韌體")
                    continue
                st = self.query_status(tid, timeout=2.0)
                if st is None:
                    print(f"  ❌ {tid}: 查無狀態 (離線?)")
                elif st.get("fs_scan_busy", 0):
                    print(f"  ⚠️ {tid}: 30s 內未完成 (大檔較多, 可再確認)")
                else:
                    print(f"  ✅ {tid}: 文件索引重建完成")

    def _view_manifest(self):
        target = self.selected_targets[0]
        print(f"\n📥 從 {target} 下載 manifest.json...")
        
        node = self.slaves.get(target)
        if not node:
            print("❌ 設備離線")
            return

        # 1. Query Size
        node["query_event"].clear()
        node["remote_size"] = 0
        self.send_pkt([target], 0x2005, {"path": "/manifest.json"})
        ok, why = self._wait_evt(node["query_event"], 3.0)
        if not ok:
            if why == "cancel":
                return
            print("⚠️ 查詢超時 (Manifest 可能不存在)")
            return
            
        size = node["remote_size"]
        print(f"  Remote Size: {size} bytes")
        
        if size == 0:
            print("⚠️ 文件為空")
            return

        err_msg = ""
        self._transfer_begin()
        try:
            config_data = self._download_bytes(target, "/manifest.json", expected_size=size, status="Manifest")
        except Exception as e:
            config_data = None
            err_msg = str(e)
        finally:
            self._transfer_end()
        if not config_data:
            print(f"❌ 下載失敗{': ' + err_msg if err_msg else ''}")
            return

        print("\n✅ 下載完成")
        self.panel.update_device(target, status="待機", transfer_label="")
        
        try:
            # Decode and Print
            json_str = config_data.decode('utf-8')
            # 嘗試格式化顯示 (雖然它已經是格式化過的，但為了保險)
            try:
                obj = json.loads(json_str)
                print("\n📜 [Manifest Content]")
                print(json.dumps(obj, indent=2)) # 強制重新格式化以確保可讀性
            except:
                print("\n📜 [Raw Content]")
                print(json_str)
        except Exception as e:
            print(f"❌ 解析失敗: {e}")
            
        input("\n按 Enter 返回...")

    # ==================== Step 1: 掃描與選擇 ====================
    def scan_devices(self):
        """掃描設備: 1. 廣播 / 2. 定向 (輸入 IP) / 3. 依記錄敲門。"""
        self.load_config()  # Reload config
        self.panel.stop()
        ConsoleUI.clear_screen()
        ConsoleUI.show_cursor()

        print("\n[Scan] 掃描方式:")
        print("  1. 廣播掃描 (全網段, 公司網路可能被防火牆擋)")
        print("  2. 定向掃描 (點對點, 輸入設備 IP, 不發廣播)")
        print("  3. 依紀錄批量敲門 (用 slave_map.json 記下的 IP 一次叫全部設備上線)")
        mode = input("\n👉 請選擇 (1/2/3) [Enter=廣播]: ").strip().lower()

        if mode == '2':
            self._direct_scan()
        elif mode == '3':
            self._knock_recorded_devices(wait=10)
        else:
            self._broadcast_scan()

        time.sleep(1)
        input("\n按 Enter 返回主菜單...")
        self.panel.start()

    def _build_discover_packet(self, ws_port=None):
        """建立 0x1001 DISCOVER 封包 (server_ip + ws_url)。"""
        if ws_port is None:
            ws_port = self.config.get("ws_port", 8000)
        p_data = SchemaCodec.encode(
            self.store.get(0x1001),
            {"server_ip": self.local_ip, "ws_url": f"ws://{self.local_ip}:{ws_port}"}
        )
        return Proto.pack(0x1001, p_data)

    def _send_unicast_discover(self, ips, label=""):
        """對指定 IP 清單逐一 unicast DISCOVER (0x1001), 每個重發 3 次。"""
        self.local_ip = self.get_local_ip()
        udp_port = self.config.get("upt_port", 9000)
        pkt = self._build_discover_packet()
        print(f"📡 {label} → {len(ips)} 個 IP (UDP {udp_port}, Server IP: {self.local_ip})")
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            for attempt in range(3):
                for ip in ips:
                    try:
                        s.sendto(pkt, (ip, udp_port))
                        print(f"    → {ip}:{udp_port} (attempt {attempt+1}/3)")
                    except Exception as e:
                        print(f"    ⚠️ {ip} 發送失敗: {e}")
                time.sleep(0.3)
            s.close()
        except Exception as e:
            print(f"❌ 發送失敗: {e}")
            return 0
        return len(ips)

    def _wait_connections(self, before, timeout=10.0, label="握手"):
        """等待新設備連回, 回傳新連上的 cid 清單。"""
        print(f"\n⏳ 等待設備連回 (最多 {int(timeout)} 秒)...")
        deadline = time.time() + timeout
        joined = []
        while time.time() < deadline:
            time.sleep(0.5)
            new = set(self.slaves.keys()) - before
            if new:
                joined = sorted(new)
                break
        if joined:
            for cid in joined:
                print(f"  ✅ {label}成功: {cid} 已連線")
        else:
            print(f"  ⚠️ {int(timeout)} 秒內沒有新設備連回")
            print("    - 確認 IP 正確、設備在線")
            print("    - 確認 UDP port 9000 與 TCP port 8000 沒被防火牆擋")
            print("    - 確認本機 IP 正確 (slave 要連回 ws://{}:{})".format(
                self.local_ip, self.config.get("ws_port", 8000)))
        return joined

    def _knock_recorded_devices(self, wait=10):
        """依 slave_map.json 的 IP 紀錄批量敲門 (unicast DISCOVER), 不發廣播。

        slave 的 IP 會因 DHCP 改變, 每次連上時 handle_client 已自動更新紀錄;
        操作者手動叫回設備時用這招點對點把它們全部叫回來 (master 不自動敲門,
        見 doc/03_notes/12_upload_wdt_diagnosis.md)。
        敲門後逐台回報: 誰連回、誰沒回來 (IP 可能已變, 需重新掃描更新紀錄)。
        """
        self.load_config()
        # 收集有 IP 紀錄的設備
        recorded = []
        for cid, info in self.config["mapping"].items():
            ip = (info or {}).get("ip", "")
            if ip:
                recorded.append((cid, ip))
        if not recorded:
            print("🔔 [Knock] slave_map.json 沒有記到任何 IP — 先掃描/定向連一次讓紀錄建立")
            return 0
        # IP 去重 (同 IP 多台 → 敲一次)
        ips = []
        for _, ip in recorded:
            if ip not in ips:
                ips.append(ip)

        before = set(self.slaves.keys())
        print(f"🔔 [Knock] 依記錄批量敲門 — 紀錄 {len(recorded)} 台 / 去重 {len(ips)} 個 IP:")
        for cid, ip in recorded:
            mark = "●在線" if cid in before else "○離線"
            pid = self.config["mapping"].get(cid, {}).get("play_id", "?")
            print(f"    {cid}  ip={ip:<15} PlayID={pid:<3} [{mark}]")

        self._send_unicast_discover(ips, label="[Knock] 批量 DISCOVER")
        if wait > 0:
            self._wait_connections(before, timeout=wait, label="敲門")

        # 批量結果: 逐台回報
        after = set(self.slaves.keys())
        missing = [(cid, ip) for cid, ip in recorded if cid not in after]
        print(f"\n📊 [Knock] 結果: 在線 {len(recorded) - len(missing)} / {len(recorded)}")
        if missing:
            print("   ✗ 未連回 (IP 可能已變, 用掃描 1=廣播 或 2=定向 更新紀錄):")
            for cid, ip in missing:
                print(f"      - {cid}  (舊紀錄 ip={ip})")
        else:
            print("   ✅ 全部設備已上線")
        return len(ips)

    def _broadcast_scan(self):
        """廣播 DISCOVER (0x1001) 到全域 + 子網廣播位址。"""
        print("\n[Scan] 正在廣播發現包...")
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

            # Refresh local IP
            self.local_ip = self.get_local_ip()

            port = self.config.get("ws_port", 8000)
            udp_port = self.config.get("upt_port", 9000)
            pkt = self._build_discover_packet(port)

            print(f"📡 Broadcasting DISCOVER to port {udp_port} (Server IP: {self.local_ip})")

            # 1. Send to Global Broadcast
            try:
                s.sendto(pkt, ('255.255.255.255', udp_port))
            except Exception as e:
                print(f"⚠️ Global broadcast failed: {e}")

            # 2. Send to Subnet Broadcast (Assuming /24)
            try:
                parts = self.local_ip.split('.')
                parts[-1] = '255'
                subnet_broadcast = '.'.join(parts)
                s.sendto(pkt, (subnet_broadcast, udp_port))
                print(f"📡 Subnet broadcast sent to {subnet_broadcast}:{udp_port}")
            except Exception as e:
                print(f"⚠️ Subnet broadcast failed: {e}")

            s.close()
            print("✅ 廣播已發送，請等待設備連線...")
        except Exception as e:
            print(f"❌ 廣播失敗: {e}")

    def _direct_scan(self):
        """定向掃描: 對指定 IP 逐一 unicast DISCOVER (0x1001), 不發廣播。

        公司網路擋廣播時, 用已知設備 IP 直接點對點握手:
        設備 UDP 收到 0x1001 後會依 ws_url 主動連回 master 的 WS 伺服器。
        """
        ips_input = input("\n👉 輸入設備 IP (逗號分隔, 例: 10.1.2.3,10.1.2.4): ").strip()
        if not ips_input:
            print("⚠️ 未輸入 IP")
            return
        ips = []
        for part in ips_input.replace('，', ',').split(','):
            ip = part.strip()
            if ip and not ip.startswith('255.'):
                ips.append(ip)
        if not ips:
            print("⚠️ 無有效 IP")
            return

        before = set(self.slaves.keys())
        self._send_unicast_discover(ips, label="[定向掃描] 點對點 DISCOVER")
        self._wait_connections(before, timeout=10, label="握手")

    @staticmethod
    def _parse_index_ranges(text, count):
        """解析「1,2,3-10」式編號輸入 → set of 0-based indices。

        支援: 逗號分隔 + a-b 範圍 (包含兩端, 例 "3-10" = 第 3 至第 10)。
        空白容忍; 非法片段/超出範圍/倒轉範圍 → 回 None (整筆輸入無效)。
        """
        if text is None:
            return None
        indices = set()
        try:
            for part in str(text).split(','):
                part = part.strip()
                if not part:
                    continue
                m = [p.strip() for p in part.split('-')]
                if len(m) == 1:
                    a = b = int(m[0])
                elif len(m) == 2:
                    a, b = int(m[0]), int(m[1])
                else:
                    return None
                if a < 1 or b < a or b > count:
                    return None
                for i in range(a - 1, b):
                    indices.add(i)
        except Exception:
            return None
        return indices if indices else None

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
        print(" - 輸入編號 (例: 1,3,5 或 1,2,3-10) 選擇/取消選擇")
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
            indices = self._parse_index_ranges(choice, len(sorted_ids))
            if indices is None:
                print("❌ 輸入無效 (例: 1,2,3-10 / a 全選 / c 清空)")
            else:
                current_set = set(self.selected_targets)
                for i in indices:
                    target = sorted_ids[i]
                    if target in current_set:
                        current_set.remove(target)
                    else:
                        current_set.add(target)
                # 保持排序順序
                self.selected_targets = [tid for tid in sorted_ids if tid in current_set]
                print(f"✅ 更新選擇: {len(self.selected_targets)} 個設備")
        
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
        """將 prepared_data 保存到 data/bins/ 目錄"""
        bins_dir = BINS_DIR
        os.makedirs(bins_dir, exist_ok=True)

        for pid, data in self.prepared_data.items():
            bin_path = os.path.join(bins_dir, f'pid_{pid}.bin')
            with open(bin_path, 'wb') as f:
                f.write(data)
            print(f"  💾 已保存 {bin_path} ({len(data)//1024} KB)")
            
        # Save Metadata
        try:
            with open(os.path.join(bins_dir, 'metadata.json'), 'w') as f:
                json.dump(self.pxld_metadata, f)
        except Exception as e:
            print(f"  ⚠️ Metadata save failed: {e}")

    def _load_metadata_only(self):
        """🔧 只載入 data/bins/metadata.json (不載 bin 資料)。

        讓工具重開後 (未跑 Step 2) 也能知道各 PlayID 的 total_frames/fps:
        - handle_client 註冊設備時面板 total_frames 正確 → 播放進度% 可顯示
        - 中途加入計算目標幀時 current_fps 正確
        """
        bins_dir = BINS_DIR
        meta_path = os.path.join(bins_dir, 'metadata.json')
        if not os.path.exists(meta_path):
            return
        try:
            with open(meta_path, 'r', encoding='utf-8') as f:
                loaded_meta = json.load(f)
            loaded = {int(k): v for k, v in loaded_meta.items()}
            self.pxld_metadata.update(loaded)
            print(f"  📋 Metadata (only) loaded ({len(loaded)} entries) — total_frames/fps 可用")
        except Exception as e:
            print(f"  ⚠️ Metadata (only) load failed: {e}")

    def _load_bins(self):
        """從 data/bins/ 目錄載入 bin 檔案到 prepared_data"""
        bins_dir = BINS_DIR
        needed_pids = {self.config["mapping"][tid].get("play_id") for tid in self.selected_targets}
        needed_pids.discard(None)

        self.prepared_data.clear()
        self.pxld_metadata.clear()
        
        # Load Metadata
        meta_path = os.path.join(bins_dir, 'metadata.json')
        if os.path.exists(meta_path):
            try:
                with open(meta_path, 'r') as f:
                    loaded_meta = json.load(f)
                    # Convert string keys to int
                    self.pxld_metadata = {int(k): v for k, v in loaded_meta.items()}
                print(f"  📋 Metadata loaded ({len(self.pxld_metadata)} entries)")
            except Exception as e:
                print(f"  ⚠️ Metadata load failed: {e}")

        loaded = 0
        missing = []
        for pid in needed_pids:
            bin_path = os.path.join(bins_dir, f'pid_{pid}.bin')
            if os.path.isfile(bin_path):
                with open(bin_path, 'rb') as f:
                    data = bytearray(f.read())
                # 🔧 PCA9685 點亮: 若每幀最後 16 顆的 W 通道全為 0 (來源 pxld
                #    沒提供 PWM 值), 把最後 16 顆的 W 填成前 660 顆的平均亮度,
                #    讓 PCA9685 有訊號可驅動。frame 大小 = num_pixels*4 (676*4)。
                self._fill_pca_w(data)
                self.prepared_data[pid] = data
                print(f"  📂 已載入 pid_{pid}.bin ({len(self.prepared_data[pid])//1024} KB)")
                loaded += 1
            else:
                missing.append(pid)

        if missing:
            print(f"  ⚠️ 缺少 PlayID: {missing}")

        return loaded, missing

    def _fill_pca_w(self, data):
        """把 data.bin 每幀「最後 16 顆」的 W 通道填上有值。

        前提: 每幀大小 = System.num_pixels * 4 (676*4 = 2704 bytes),
        最後 16 顆對應 PCA9685 的 16 通道。來源 pxld 常把這 16 顆 W 留 0,
        導致 PCA 收不到 PWM。此處把 W 填成「該幀前面燈的平均亮度」,
        若整幀全 0 (熄燈幀) 就維持 0。
        """
        frame = self._cfg_int("num_pixels", 0) * 4
        if frame <= 0 or len(data) < frame:
            # 不確定 frame 大小就 fallback: 用 676*4 (已知格式)
            frame = 2704
            if len(data) < frame:
                return
        n_frames = len(data) // frame
        for i in range(n_frames):
            base = i * frame
            # 前 660 顆的 W (index 3,7,11,...)
            front_end = base + 660 * 4
            front_ws = data[base + 3 : front_end : 4]
            avg = sum(front_ws) // len(front_ws) if front_ws else 0
            if avg <= 0:
                avg = 128  # 全黑幀也給個中性亮度, 讓 PCA 不滅
            # 最後 16 顆的 W (index 660..675)
            for k in range(16):
                idx = base + (660 + k) * 4 + 3
                if idx < len(data):
                    data[idx] = avg

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

        # 檢查 data/bins/ 是否有現成的 bin 檔案
        bins_dir = BINS_DIR
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
                    
                    # 🔧 PCA9685 點亮: 與 load_bins 一致, 把每幀最後 16 顆 W 填上有值
                    self._fill_pca_w(data)
                    self.prepared_data[pid] = data
                    # Update metadata with actual sliced frame count
                    sliced_frames = end_frame - start_frame
                    self.pxld_metadata[pid] = {"total_frames": sliced_frames, "fps": decoder.fps}
                    
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
        self.send_pkt(valid_tids, 0x2005, {"path": "/sd/data.bin"})
        
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
        choice = input("👉 輸入編號上傳 (例: 1,3,5 或 1-10) | 'a' 僅上傳不一致 | 'all' 全選: ").lower()
        
        final_targets = []
        if choice == 'all':
            final_targets = [item[0] for item in deploy_queue]
        elif choice == 'a':
            final_targets = [item[0] for item in deploy_queue if item[1] != item[2]]
        else:
            idxs = self._parse_index_ranges(choice, len(deploy_queue))
            if idxs is None:
                print("❌ 輸入錯誤 (例: 1,3,5 或 1-10)")
                self.panel.start()
                return
            final_targets = [deploy_queue[i][0] for i in sorted(idxs)]
        
        if not final_targets:
            print("ℹ️ 無設備被選中")
            time.sleep(1)
            self.panel.start()
            return
        
        self._transfer_begin()
        try:
            for tid in self.selected_targets:
                if tid not in final_targets:
                    self.panel.update_device(tid, status="待機", transfer_label="", upload_progress=100)
                else:
                    self.panel.update_device(tid, status="上傳中", transfer_label="上傳 data.bin", upload_progress=0)
            
            max_workers = self.config.get("max_workers", 50)
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(self._deploy_to_single_slave, tid): tid for tid in final_targets}
                
                for future in futures:
                    tid = futures[future]
                    try:
                        future.result()
                        self.panel.update_device(tid, status="待機", transfer_label="", upload_progress=100)
                    except Exception as e:
                        if str(e) == "已停止" or self.transfer_cancel.is_set():
                            self.panel.update_device(tid, status="已停止", transfer_label="", error_msg="")
                        else:
                            self.panel.update_device(tid, status="錯誤", transfer_label="", error_msg=str(e))
        finally:
            self._transfer_end()
        
        time.sleep(2)
        print("\n✅ 部署完成")
    
    def _deploy_to_single_slave(self, tid):
        node = self.slaves.get(tid)
        pid = self.config["mapping"][tid].get("play_id")
        data = self.prepared_data.get(pid)
        
        if not node or data is None:
            raise Exception("無數據或離線")

        local_sha = self._upload_bytes(tid, data, "/sd/data.bin", file_idx=1, total_files=1, file_id=1)
        # 🔧 播放數據常改, 不該進「3 次重啟自動回滾」保護帶: 上傳完立即 confirm
        #    (清 .bak + pending)。否則 /data.bin 的備份會留著, 3 次開機後被靜默還原成舊版。
        if not self._confirm_file(tid, "/sd/data.bin"):
            self._log_event("FAIL", "data.bin 確認失敗 (pending 未清, 可能 3 次重啟後回滾)", device_id=tid)
            self.panel.log("warn", f"⚠️ [{tid}] data.bin 上傳成功但確認失敗 (pending 未清)")
        self.config["mapping"][tid]["last_sha"] = local_sha.hex()
        self.save_config()
    
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
        
        if AUDIO_MODE is None:
            print("⚠️ 音訊模塊未安裝 (miniaudio/pygame) — MP3 無法播放,但仍可播放燈效 (靜音模式)")
        
        mp3_files = [f for f in os.listdir('.') if f.endswith('.mp3')]
        if not mp3_files:
            print("❌ 找不到 MP3 文件 (可選)")
        
        print(f"\n🎵 [音訊準備] 模式: {AUDIO_MODE}")
        print(f"  0. 不播放音訊 (僅觸發動畫)")
        for i, f in enumerate(mp3_files):
            print(f"  {i+1}. {f}")
        print("  q. 取消返回")
        print("  [Enter] 等同 0 (靜音模式)")
        
        selected_mp3 = None
        try:
            raw_choice = input("\n👉 選擇編號: ").strip().lower()
            if raw_choice == 'q':
                self.panel.start()
                return
            
            if raw_choice == '':
                choice = 0
            else:
                choice = int(raw_choice)
            if choice == 0:
                selected_mp3 = None
                print("✅ 已選擇: 靜音模式 (僅播放燈效)")
            elif 1 <= choice <= len(mp3_files):
                selected_mp3 = mp3_files[choice-1]
                if AUDIO_MODE is None:
                    print(f"⚠️ 音訊模塊未安裝,無法播放 {selected_mp3},改為靜音模式 (僅播放燈效)")
                    selected_mp3 = None
                else:
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
        
        print(f"\n⚙️ 正在預備設備...")
        
        for tid in self.selected_targets:
            self.panel.update_device(tid, status="待機")
            if tid in self.panel.monitors:
                self.panel.monitors[tid].reset_play_stats()
        
        # 🔧 play_mode: 0=播放一次, 1=循環 (播完自動重頭)
        self.current_play_mode = 1 if self.config.get("loop_play", 0) else 0
        self.send_pkt(self.selected_targets, 0x3009, {
            "file_name": "data.bin",
            "block_id": 0,
            "play_mode": self.current_play_mode
        })
        while True:
            loop_str = "開啟" if self.config.get("loop_play", 0) else "關閉"
            act_fps = self._cfg_int("active_sync_fps", 0)
            act_str = f"fps={act_fps}" if act_fps > 0 else "關閉"
            print("\n" + "!" * 50)
            print("     系統就緒,等待擊發")
            print(f"     延遲設定: {self.config.get('sync_delay_ms', 0)} ms  │  循環播放: {loop_str}  │  主動同步: {act_str}")
            print("     輸入 'go' 開始 | 't' 微調延遲 | 'l' 延遲測試並紀錄 | 'p' 切換循環 | 'a' 切換主動同步 | 'q' 取消")
            print("!" * 50)
            
            trigger = input("\n🚀 指令: ").lower().strip()
            
            if trigger == 'go':
                break
            elif trigger == 'q':
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
            elif trigger == 'l':
                # 🔧 延遲測試 + 手動紀錄 (CSV, 可加備註)
                self._latency_test_and_log(ask_note=True)
            elif trigger == 'a':
                # 🔧 切換主動同步幀率 (0 = 被動, 否則定時廣播 0x3001)
                cur = self._cfg_int("active_sync_fps", 0)
                if cur > 0:
                    self.config["active_sync_fps"] = 0
                    print("✅ 主動同步已關閉 (回到被動同步)")
                else:
                    try:
                        new_fps = int(input("👉 輸入要廣播的 fps (e.g. 40): ").strip())
                        if new_fps > 0:
                            self.config["active_sync_fps"] = new_fps
                            print(f"✅ 主動同步已開啟: 播放時每 {self.config.get('active_sync_interval_s', 10)}s 廣播 fps={new_fps}")
                        else:
                            print("❌ 無效 fps")
                    except ValueError:
                        print("❌ 輸入無效")
                self.save_config()
            elif trigger == 'p':
                # 🔧 切換循環播放 (重新發送 0x3009 讓 slave 更新 play_mode)
                self.config["loop_play"] = 1 - int(self.config.get("loop_play", 0))
                self.save_config()
                self.current_play_mode = 1 if self.config["loop_play"] else 0
                self.send_pkt(self.selected_targets, 0x3009, {
                    "file_name": "data.bin",
                    "block_id": 0,
                    "play_mode": self.current_play_mode
                })
                print(f"✅ 循環播放已{'開啟' if self.config['loop_play'] else '關閉'}")
            else:
                print("❌ 指令無效")

        for tid in self.selected_targets:
            self.panel.update_device(tid, status="播放中")
            # 🔧 補齊面板 total_frames (工具重開沒跑 Step 2 時, 這裡用已載入的 metadata)
            pid = self.config["mapping"][tid].get("play_id")
            tf = 0
            if pid is not None:
                tf = self.pxld_metadata.get(pid, {}).get("total_frames", 0) or 0
            self.panel.register_device(tid, pid, tf)
        
        self.panel.start(interactive=True)
        
        delay_ms = self.config.get("sync_delay_ms", 150)
        delay_sec = abs(delay_ms) / 1000.0
        
        # 記錄播放起始時間與 FPS，供中途加入使用
        self.playback_start_time = time.time()
        self.current_fps = 40 # Default
        if self.selected_targets:
            pid = self.config["mapping"][self.selected_targets[0]].get("play_id")
            if pid in self.pxld_metadata and "fps" in self.pxld_metadata[pid]:
                self.current_fps = self.pxld_metadata[pid]["fps"]
                if self.current_fps == 0: self.current_fps = 40
        
        # 🔧 播放模式: 0=一次, 1=循環 (中途加入沿用此值)
        self.current_play_mode = 1 if self.config.get("loop_play", 0) else 0
        
        # 🔧 播放會話開始: go → stop_all 之間持續有效 (音檔播完 ≠ 會話結束)。
        #    離線重連的自動續播 (mid-join) 依此旗標觸發。
        with self.play_lock:
            self.play_session_active = True
            self.audio_finished = False
            self.paused_since = None
            self.paused_total = 0.0
        self._dev_finished.clear()
        self._stop_was_manual = False   # 🔧 每次擊發重設: 手動停止 (s/q) 才會被設 True
        
        # 🔧 提前啟動 is_playing, 避免音訊執行緒啟動前的空窗期 (中途加入會漏掉)
        self.is_playing = True
        
        # 🔧 主動同步幀率: 若 config active_sync_fps > 0, 開始定時廣播 0x3001
        self._start_active_sync()
        
        if selected_mp3:
            if delay_ms >= 0:
                self._start_audio_stream(selected_mp3)
                if delay_ms > 0:
                    time.sleep(delay_sec)
                self.send_pkt(self.selected_targets, 0x300A, {"start_frame": 0})
            else:
                self.send_pkt(self.selected_targets, 0x300A, {"start_frame": 0})
                time.sleep(delay_sec)
                self._start_audio_stream(selected_mp3)
        else:
            # Silent mode: just trigger
            self.send_pkt(self.selected_targets, 0x300A, {"start_frame": 0})
        
        # 🔧 播放進度輪詢: 每秒向 slave 查 0x1101, 用 0x1102 更新面板進度
        self._start_progress_poll()
        
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
                            # 🔧 修復: 暫停/繼續用 0x3005 STREAM_PAUSE {pause}。
                            #    舊版誤用 0x3003 (Direct Mode, 需 pixel_data) 與
                            #    0x3004 (SEEK) — 按繼續會 seek 回第 0 幀重播!
                            with self.play_lock:
                                self.is_paused = not self.is_paused
                                if self.is_paused:
                                    self.paused_since = time.time()
                                    self.send_pkt(self.selected_targets, 0x3005, {"pause": 1})
                                    for tid in self.selected_targets:
                                        self.panel.update_device(tid, status="暫停")
                                else:
                                    if self.paused_since is not None:
                                        self.paused_total += time.time() - self.paused_since
                                        self.paused_since = None
                                    self.send_pkt(self.selected_targets, 0x3005, {"pause": 0})
                                    for tid in self.selected_targets:
                                        self.panel.update_device(tid, status="播放中")
                        
                        elif key == 's':
                            self.stop_all()
                            break
                        
                        elif key == 'q':
                            self.stop_all()
                            break
                        
                        elif key == 'l':
                            # 🔧 播放中量測延遲並紀錄 (觀察中途加入的延遲)
                            self._latency_test_and_log(note="during-play")
                        
                        elif key == '\x03': # Ctrl+C
                             self.stop_all()
                             break
                             
                    except Exception:
                        pass
                
                time.sleep(0.05)
        finally:
            # 確保退出播放循環時恢復原始模式
            input_handler.exit_raw_mode()
            # 🔧 停止主動同步廣播 / 進度輪詢
            self._stop_active_sync()
            self._stop_progress_poll()
            # 🔧 會話結束 (使用者停止 或 非循環模式音檔播完)
            with self.play_lock:
                self.play_session_active = False
            self._dev_finished.clear()

        # 🔧 自然播完 (非循環音檔結束, 非手動 s/q 停止) → 延遲 post_play_stop_delay_s
        #    後才送 0x3002 停止指令。slave 端在檔尾會保持最後一幀亮著, 這段延遲就是
        #    「最後姿勢定格」時間; 延遲一到才真正熄燈。手動停止時 stop_all() 已立即
        #    送過 0x3002, 這裡不重複送 (否則會誤關掉緊接著的下一段準備)。
        if (not self._stop_was_manual) and (self.current_play_mode != 1):
            delay = max(0.0, self._cfg_float("post_play_stop_delay_s", 10.0))
            self.panel.log("info", "🏁 播放自然結束, {} 秒後發送停止指令 (0x3002)...".format(delay))
            time.sleep(delay)
            if self.selected_targets:
                self.send_pkt(self.selected_targets, 0x3002, {})
                self.panel.log("ok", "🛑 已發送停止指令 (0x3002) — 熄燈")

        for tid in self.selected_targets:
            self.panel.update_device(tid, status="待機")
        
        time.sleep(1)

    # ==================== 主動同步幀率 (0x3001 STREAM_INFO 廣播) ====================
    def _start_active_sync(self):
        """啟動主動同步幀率廣播執行緒 (config: active_sync_fps / active_sync_interval_s)。

        定時廣播現有指令 0x3001 STREAM_INFO {fps}, slave 端只儲存原始 fps 不換算,
        RenderTask 偵測到變化時才換算一次節拍, 取代被動等待各設備用自己的 frame_interval_ms 播放。
        """
        self._stop_active_sync()
        fps = self._cfg_int("active_sync_fps", 0)
        if fps <= 0:
            return
        interval = self._cfg_float("active_sync_interval_s", 10.0)
        interval = max(1.0, interval)
        self._active_sync_stop.clear()

        def _loop():
            while not self._active_sync_stop.is_set() and self.is_playing:
                # 每次廣播重新讀取目標, 中途加入的設備也會被同步到
                targets = list(self.selected_targets)
                if targets:
                    self.send_pkt(targets, 0x3001, {
                        "total_blocks": 0,
                        "frames_per_block": 0,
                        "fps": fps
                    })
                self._active_sync_stop.wait(interval)

        self._active_sync_thread = threading.Thread(target=_loop, daemon=True)
        self._active_sync_thread.start()
        self.panel.log("info", f"🔁 [ActiveSync] 定時廣播 0x3001 fps={fps} (每 {interval:.0f}s)")

    def _stop_active_sync(self):
        """停止主動同步廣播執行緒。"""
        self._active_sync_stop.set()
        t = self._active_sync_thread
        if t:
            try:
                t.join(timeout=0.5)
            except Exception:
                pass
        self._active_sync_thread = None

    # ==================== 播放進度輪詢 (0x1101 → 0x1102) ====================
    def _start_progress_poll(self):
        """🔧 啟動播放進度輪詢執行緒。

        播放期間定期對每個目標發 0x1101 STATUS_GET, 用回覆的 0x1102 更新面板
        (frame/進度%/mem)。舊韌體只「被問才答」, 沒有這個輪詢 PC 端永遠收不到
        播放進度; 新韌體即使每秒主動推 0x1102, 輪詢也作為兜底與離線偵測。
        """
        self._stop_progress_poll()
        self._progress_poll_stop.clear()
        self._progress_poll_thread = threading.Thread(target=self._progress_poll_loop, daemon=True)
        self._progress_poll_thread.start()

    def _stop_progress_poll(self):
        self._progress_poll_stop.set()
        t = self._progress_poll_thread
        if t:
            try:
                t.join(timeout=0.5)
            except Exception:
                pass
        self._progress_poll_thread = None

    def _progress_poll_loop(self):
        interval = self._cfg_float("progress_poll_interval_s", 1.0)
        interval = max(0.5, interval)
        while not self._progress_poll_stop.is_set():
            if not self.play_session_active:
                break
            targets = list(self.selected_targets)
            for tid in targets:
                if self._progress_poll_stop.is_set() or not self.play_session_active:
                    break
                if tid not in self.slaves:
                    continue
                st = self.query_status(tid, timeout=1.0)
                if not st:
                    continue
                # 🔧 新舊韌體格式統一解析 (接口相容)
                cur, pos, active, mem_free, _rid = self._parse_status(st)
                self.panel.update_device(tid, current_frame=cur, mem_free=mem_free)
                mon = self.panel.monitors.get(tid)
                # 🔧 自然播完偵測: 裝置回報 stream_active=False 且已播過幀 →
                #    非循環播放已到檔尾; 標記後, 之後重連不再自動續播。
                #    (剛重啟的裝置 cur=0 且 stream_active=False, 不算播完,
                #     避免把「重啟後還沒接回」誤判成「播完」)
                if active is False:
                    if mon and mon.status in ("播放中", "暫停", "中途加入") and cur > 0:
                        self._dev_finished.add(tid)
                        self.panel.update_device(tid, status="播完")
                        print(f"🏁 [Play] {tid} 串流已自然播完 (frame {cur})")
                    continue
                # 🔧 進度校正: 播放中且回報進度與主控推算偏差過大 → SEEK 拉回。
                #    (新韌體有 stream_pos_frame 才做; 舊韌體 played_frames 是
                #    session 計數, 拿來比對會誤校正, 跳過)
                if pos is not None and mon and mon.status == "播放中":
                    total = self._device_total_frames(tid)
                    if total > 0:
                        expected = self._expected_frame(tid)
                        tol = max(30, int(total * 0.03))
                        if abs(pos - expected) > tol:
                            self._dev_drift[tid] = self._dev_drift.get(tid, 0) + 1
                            if self._dev_drift.get(tid, 0) >= 3:
                                self._dev_drift[tid] = 0
                                self.panel.log("warn", f"🩹 [Sync] {tid} 進度偏差 {abs(pos - expected)} 幀 (pos={pos}, expect={expected}) → SEEK 校正")
                                self.send_pkt([tid], 0x3004, {"target_block": 0, "target_frame": expected})
                        else:
                            self._dev_drift[tid] = 0
            self._progress_poll_stop.wait(interval)

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
                        
                        # 注意: miniaudio 的 PlaybackDevice 沒有 is_active 屬性,
                        # 用 running 判斷播放中; callback_generator 被清空 (stream 耗盡)
                        # 表示音檔已播完 (stop_callback 並非每次都觸發, 不可依賴)。
                        while device.running and self.running and self.is_playing:
                            while self.is_paused and self.is_playing:
                                time.sleep(0.1)
                            if device.callback_generator is None:
                                break
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
                # 🔧 音檔播完 ≠ 播放會話結束:
                #   - 循環播放: 燈效繼續循環, is_playing 保持 True, 主迴圈/中途加入照常
                #   - 非循環: 音檔結束代表整場結束 → is_playing=False 讓主迴圈退出
                self.audio_finished = True
                if self.current_play_mode != 1:
                    self.is_playing = False
        
        threading.Thread(target=_play_task, daemon=True).start()
    
    def stop_all(self):
        global mixer
        self._stop_was_manual = True   # 🔧 使用者手動停止 → 已在此立即送停止, 不需再補延遲停止
        self.is_playing = False
        self.is_paused = False
        # 🔧 播放會話結束 (離線重連不再自動續播)
        with self.play_lock:
            self.play_session_active = False
            self.paused_since = None
            self.paused_total = 0.0
        self._dev_finished.clear()
        # 🔧 停止主動同步幀率廣播 / 進度輪詢
        self._stop_active_sync()
        self._stop_progress_poll()
        
        if self.selected_targets:
            self.send_pkt(self.selected_targets, 0x3002, {})
            
            for tid in self.selected_targets:
                self.panel.update_device(tid, status="待機")
        
        if AUDIO_MODE == 'pygame' and mixer:
            try:
                if mixer.get_init():
                    mixer.music.stop()
            except:
                pass
        elif AUDIO_MODE == 'miniaudio':
            # 中斷 miniaudio 播放: is_playing=False 會讓 _play_task 迴圈退出
            # (device 由 with 區塊自動 close)
            self.audio_finished = True

    # ==================== Step 7: 配對模式 ====================
    def step_7_pairing(self):
        """配對模式: 讓 slave 播放(本地燈效 或 串流)供肉眼識別, 並更新 PlayID。

        兩種識別方式:
          1. 本地燈效 (0x3105) — 不需 data.bin, 播板上內建效果
          2. 串流播放 (0x3009 + 0x300A) — 需該設備已部署 data.bin
        """
        self.load_config()
        self.panel.stop()
        ConsoleUI.clear_screen()
        ConsoleUI.show_cursor()

        targets = self.selected_targets or list(self.slaves.keys())
        if not targets:
            print("⚠️ 無在線設備, 請先 Scan/Select")
            input("\n按 Enter 返回...")
            self.panel.start()
            return

        print("\n🔗 [Step 7] 配對模式 — 選擇識別方式")
        print(f"   處理設備數: {len(targets)}")
        print("   1. 本地燈效 (0x3105) — 不需 data.bin, 播板上內建效果")
        print("   2. 串流播放 (0x3009 + 0x300A) — 需已部署 data.bin")
        method = input("\n👉 請選擇 (1/2) [Enter=1]: ").strip()

        if method == '2':
            self._pairing_by_stream(targets)
        else:
            self._pairing_by_local_mode(targets)

        print("\n✅ 配對完成")
        input("\n按 Enter 返回...")
        self.panel.start()

    def _pairing_by_local_mode(self, targets):
        """識別方式 1: 逐一播放本地燈效 (0x3105 MODE_SET), 肉眼確認後更新 PlayID。"""
        print("\n🔗 [Step 7] 本地燈效識別")
        print("   (slave 需支援 0x31xx pixel 指令, 且已上傳 pixel/modes 本地燈效)")

        try:
            for cid in targets:
                node = self.slaves.get(cid)
                if not node:
                    print(f"\n❌ {cid}: 離線, 跳過")
                    continue

                old_pid = self.config["mapping"].get(cid, {}).get("play_id", "?")
                print(f"\n{'='*60}\n🎯 設備: {cid}  (目前 PlayID: {old_pid})")
                self.panel.update_device(cid, status="配對中")

                # 1. 停止串流 / 本地模式
                self.send_pkt([cid], 0x3002, {})
                self.send_pkt([cid], 0x3106, {"action": 1})

                # 2. 查詢本地燈效清單 (0x3101 + 0x3108 名稱)
                modes = self._query_modes(cid, timeout=2.0)
                mode_ids = [mid for mid, _ in modes]
                if not mode_ids:
                    print("   ⚠️ 即時查詢無模式 (slave 未實作 0x3101? 或 pixel/modes 為空)")
                    # 🔧 離線快取 fallback: 顯示 profile 已知模式, 不盲點
                    prof = self._load_profile(cid)
                    if prof and prof.get("modes"):
                        print("   📄 使用 profile 快取模式:")
                        for m in prof["modes"]:
                            print(f"     - mode {m['id']} ({m['name']})")
                else:
                    # 顯示模式名稱
                    print(f"   本地燈效 {len(mode_ids)} 個:")
                    for i, (mid, name) in enumerate(modes):
                        print(f"     {i+1:2d}. mode {mid:<3d} ({name})")

                    # 3. 逐一播放本地燈效, 讓使用者肉眼確認
                    for i, mid in enumerate(mode_ids):
                        print(f"\n   ▶ [{i+1}/{len(mode_ids)}] 播放 mode {mid} ...")
                        self.send_pkt([cid], 0x3105, {
                            "mode_type": mid >> 8,
                            "mode_id": mid & 0xFF,
                            "start_delay_ms": 0,
                            "brightness": 255
                        })
                        ch = input("      [Enter] 播下一個  [s] 停止此模式  [q] 結束配對: ").strip().lower()
                        if ch == 'q':
                            self.send_pkt([cid], 0x3106, {"action": 1})
                            print("\n🛑 配對結束")
                            return
                        if ch == 's':
                            self.send_pkt([cid], 0x3106, {"action": 1})

                # 4. 更新 PlayID
                self.send_pkt([cid], 0x3106, {"action": 1})
                new_pid = input(f"\n   ✏️ 輸入 {cid} 的新 PlayID (Enter=不變): ").strip()
                if new_pid:
                    try:
                        new_pid = int(new_pid)
                        if cid not in self.config["mapping"]:
                            self.config["mapping"][cid] = {"play_id": 0, "last_sha": ""}
                        self.config["mapping"][cid]["play_id"] = new_pid
                        self.save_config()
                        total_frames = self.pxld_metadata.get(new_pid, {}).get("total_frames", 0)
                        self.panel.register_device(cid, new_pid, total_frames)
                        print(f"   ✅ {cid} PlayID → {new_pid}")
                    except ValueError:
                        print("   ❌ 輸入無效, 保持不變")

                # 🔧 更新此設備 Profile (模式/狀態/延遲), 供日後離線查閱
                if self._save_profile(cid):
                    print(f"   💾 Profile 已更新: data/profiles/{cid.replace(':', '_')}.json")
        except KeyboardInterrupt:
            print("\n🛑 配對中斷")
        finally:
            # 確保所有設備熄燈
            for cid in targets:
                self.send_pkt([cid], 0x3106, {"action": 1})

    def _pairing_by_stream(self, targets):
        """識別方式 2: 對單一設備 0x3009+0x300A 串流播放 data.bin, 肉眼識別後更新 PlayID。

        前提: 該設備 SD 卡上已有 data.bin (串流是讀本地檔, 不是即時傳像素)。
        每次只播一台, 播完停掉再換下一台, 讓使用者能對照「哪塊板 = 哪個 ID」。
        """
        print("\n🔗 [Step 7] 串流播放識別 (0x3009 + 0x300A)")
        print("   (需該設備已部署 data.bin; 串流讀本地 SD 檔, 非即時傳像素)")

        # 先全部停止, 清掉殘留串流/本地模式
        self.send_pkt(targets, 0x3002, {})
        self.send_pkt(targets, 0x3106, {"action": 1})
        time.sleep(0.3)

        try:
            for cid in targets:
                node = self.slaves.get(cid)
                if not node:
                    print(f"\n❌ {cid}: 離線, 跳過")
                    continue

                old_pid = self.config["mapping"].get(cid, {}).get("play_id", "?")
                print(f"\n{'='*60}\n🎯 設備: {cid}  (目前 PlayID: {old_pid})")
                self.panel.update_device(cid, status="配對中")

                # 只對這一台準備 + 播放 data.bin; 等 READY (0x3008) 再開播,
                # 避免 0x300A 到時 slave 還在 LOADING 狀態而漏接。
                node["ready_event"].clear()
                self.send_pkt([cid], 0x3009, {"file_name": "data.bin", "block_id": 0, "play_mode": 0})
                if not node["ready_event"].wait(timeout=2.0):
                    print("   ⚠️ READY 逾時 (data.bin 可能未部署/開檔失敗), 仍嘗試開播")
                self.send_pkt([cid], 0x300A, {"start_frame": 0})
                print("   ▶ 串流播放中 (data.bin), 觀察燈效以識別此板...")

                new_pid = input(f"\n   ✏️ 輸入 {cid} 的新 PlayID (Enter=不變, q=停止並結束): ").strip().lower()
                if new_pid == 'q':
                    self.send_pkt([cid], 0x3002, {})
                    print("\n🛑 配對結束")
                    return
                if new_pid:
                    try:
                        new_pid = int(new_pid)
                        if cid not in self.config["mapping"]:
                            self.config["mapping"][cid] = {"play_id": 0, "last_sha": ""}
                        self.config["mapping"][cid]["play_id"] = new_pid
                        self.save_config()
                        total_frames = self.pxld_metadata.get(new_pid, {}).get("total_frames", 0)
                        self.panel.register_device(cid, new_pid, total_frames)
                        print(f"   ✅ {cid} PlayID → {new_pid}")
                    except ValueError:
                        print("   ❌ 輸入無效, 保持不變")

                # 停掉這台, 再換下一台
                self.send_pkt([cid], 0x3002, {})
        except KeyboardInterrupt:
            print("\n🛑 配對中斷")
        finally:
            # 確保所有設備熄燈/停止
            for cid in targets:
                self.send_pkt([cid], 0x3002, {})
                self.send_pkt([cid], 0x3106, {"action": 1})

    # ==================== Step 8: Profiles (每 id 一個 profile) ====================
    def step_8_profiles(self):
        """檢視每個設備的 Profile (可播放模式/狀態/檔案), 或批次備份全部設備。"""
        self.load_config()
        self.panel.stop()
        ConsoleUI.clear_screen()
        ConsoleUI.show_cursor()

        print("\n📚 [Step 8] Profiles — 每 id 一個 profile (對方有什麼模式可播放, 不用盲點)")
        print("  1. 顯示所有已快取 Profile (離線可看)")
        print("  2. 批次備份全部設備 (下載全部檔案 + 更新 Profile)")
        print("  q. 返回")

        choice = input("\n👉 請選擇: ").strip().lower()
        if choice == '1':
            profiles_dir = PROFILE_DIR
            if not os.path.isdir(profiles_dir):
                print("\n  (尚無 profile 快取 — 先執行批次備份或 Step 7 配對)")
            else:
                fns = sorted(f for f in os.listdir(profiles_dir) if f.endswith(".json"))
                if not fns:
                    print("\n  (尚無 profile 快取)")
                for fn in fns:
                    cid = fn[:-5]
                    print(f"\n{'─'*56}")
                    self._print_profile(cid, self._load_profile(cid))
            input("\n按 Enter 返回...")
        elif choice == '2':
            self._bulk_download_all()
        self.panel.start()

    # ==================== Step 9: PoE Restart (Cisco 交換器 PoE port 重啟) ====================
    def step_9_poe_restart(self):
        """呼叫 tools/PC/poe_restart.py — 遠端重啟 Cisco 3560 交換器的 PoE port。

        用子行程執行: 該腳本內部有 sys.exit()（取消/失敗退出），獨立行程才不會
        連帶結束主程式；netmiko 也只有這個工具需要，不必拖累主程式。
        """
        self.panel.stop()
        ConsoleUI.clear_screen()
        ConsoleUI.show_cursor()

        print("\n🔌 [Step 9] PoE Restart — 交換器 PoE 電源控制 (Cisco 3560)")
        print("  1. 正式執行 (可揀 重啟 / 關閉 PoE / 開啟 PoE)")
        print("  2. 模擬 Dry-run (只預覽指令, 不連線)")
        print("  q. 返回")

        choice = input("\n👉 請選擇 [Enter=1]: ").strip().lower() or "1"
        if choice == 'q':
            self.panel.start()
            return
        if choice not in ('1', '2'):
            print("❌ 無效選擇")
            time.sleep(1)
            self.panel.start()
            return

        script = os.path.join(SCRIPT_DIR, "poe_restart.py")
        cmd = [sys.executable, "-B", script]
        if choice == '2':
            cmd.append("--dry-run")
        print(f"\n執行: {' '.join(cmd)}\n")
        subprocess.run(cmd)
        input("\n按 Enter 返回...")
        self.panel.start()

    # ==================== Step I: Install Deps (專案虛擬環境 + 依賴安裝) ====================
    # 專案用到的第三方套件: (pip 套件名, import 模組名) — 統一裝進 .venv
    REQUIRED_DEPS = [
        ("miniaudio", "miniaudio"),   # 音訊播放 (主)
        ("pygame", "pygame"),         # 音訊備援
        ("netmiko", "netmiko"),       # poe_restart.py 交換器 PoE port 重啟
        ("pyserial", "serial"),       # test/protocol/rs485_probe_host.py
        ("mpremote", "mpremote"),     # mpremote 工具
        ("websockets", "websockets"), # tools/WebMaster/mock_slave.py
    ]

    def step_i_install_deps(self):
        """建立/檢查專案虛擬環境 (.venv) 並安裝缺失模組 (i 模式)。

        不用系統 pip: macOS Homebrew Python 受 PEP 668 保護, 系統環境容易弄壞;
        改用專案自己的 venv, Windows / macOS 通用。venv 建立後主程式會自動用它跑。
        """
        self.panel.stop()
        ConsoleUI.clear_screen()
        ConsoleUI.show_cursor()

        # --- 1. 確保 venv 存在 ---
        if not os.path.isfile(_venv_python()):
            print("\n📦 [i] Install Deps — 專案虛擬環境 (.venv) 尚未建立")
            print(f"  將使用系統 Python 建立: {_system_python()}")
            ans = input("要現在建立嗎? (yes/no): ").strip().lower()
            if ans != "yes":
                print("已取消。")
                input("\n按 Enter 返回...")
                self.panel.start()
                return
            print("\n建立虛擬環境 ...")
            r = subprocess.run([_system_python(), "-m", "venv", VENV_DIR])
            if r.returncode != 0 or not os.path.isfile(_venv_python()):
                print(f"❌ venv 建立失敗 (exit={r.returncode})")
                input("\n按 Enter 返回...")
                self.panel.start()
                return
            print("✅ 虛擬環境已建立")

        venv_py = _venv_python()
        print(f"虛擬環境 Python: {venv_py}")

        # --- 2. 用 venv python 檢查依賴 ---
        print("\n檢查依賴 ...")
        missing = []
        for pip_name, import_name in self.REQUIRED_DEPS:
            r = subprocess.run(
                [venv_py, "-B", "-c", f"import {import_name}"],
                capture_output=True, text=True,
            )
            ok = r.returncode == 0
            print(f"  {'✅' if ok else '❌'} {pip_name:<12} (import {import_name})")
            if not ok:
                missing.append(pip_name)
        if not missing:
            print("\n全部已安裝，不需要動作。")
            self._print_venv_hint()
            input("\n按 Enter 返回...")
            self.panel.start()
            return

        # --- 3. 用 venv pip 安裝缺失模組 ---
        print(f"\n以下 {len(missing)} 個模組缺失: {', '.join(missing)}")
        ans = input("要自動安裝嗎? (yes/no): ").strip().lower()
        if ans != "yes":
            print("已取消。可手動安裝:")
            for p in missing:
                print(f"  {venv_py} -m pip install {p}")
            input("\n按 Enter 返回...")
            self.panel.start()
            return

        # 先升級 venv 內的 pip, 避免舊版 pip 的問題 (失敗不阻斷)
        subprocess.run([venv_py, "-m", "pip", "install", "--upgrade", "pip"],
                       capture_output=True)
        for p in missing:
            print(f"\n安裝 {p} ...")
            r = subprocess.run([venv_py, "-m", "pip", "install", p])
            if r.returncode == 0:
                print(f"  ✅ {p} 安裝完成")
            else:
                print(f"  ❌ {p} 安裝失敗 (exit={r.returncode})")
        print("\n完成。")
        self._print_venv_hint()
        input("\n按 Enter 返回...")
        self.panel.start()

    def _print_venv_hint(self):
        """印出之後怎麼跑主程式 (venv 建好後, 直接跑也會自動切換)。"""
        print("\n💡 venv 建好後，直接照平常方式啟動主程式即可，程式會自動切到 .venv 執行:")
        if os.name == "nt":
            print("    python tools\\PC\\NetBusMaster.py")
        else:
            print("    python3 tools/PC/NetBusMaster.py")

    def _print_menu(self):
        self.panel.stop()
        ConsoleUI.clear_screen()
        ConsoleUI.show_cursor()
        
        print("\n" + "=" * 60)
        print(" 🎬 NetBus Master Control Panel")
        print("=" * 60)
        online, offline = self.device_manager.get_counts()
        print(" 0. Update Firmware    | 固件更新/配置修改")
        print(" 1. Scan Devices       | 掃描設備 (1=廣播 / 2=定向IP)")
        print(" 2. Select Devices     | 選擇目標設備 (已選/總數: {}/{})".format(len(self.selected_targets), online))
        print(" 3. Clear List         | 清除設備列表 (離線: {})".format(offline))
        print(" ----------------------------------------")
        print(" 4. Slice Animation    | 切分動畫數據")
        print(" 5. Deploy Data        | 部署到設備 (帶監控)")
        print(" 6. Sync Play          | 同步播放 (支持暫停/中途加入)")
        print(" 7. Pairing Mode       | 配對: 播放本地燈效並更新 PlayID")
        print(" 8. Profiles           | 每 id Profile (模式/狀態/批次備份)")
        print(" 9. PoE Restart        | 交換器 PoE 電源控制 (重啟/關閉/開啟, Cisco 3560)")
        print(" i. Install Deps       | 檢查/安裝缺失的 Python 模組")
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

    def main_loop(self):
        # 🔧 不再於啟動時自動敲門叫回設備: master 不主動發起重連 (自動敲門曾在
        #    「離線判斷 → 敲門 → slave 自我斷線重連」間造成抖動循環, 見
        #    doc/03_notes/12_upload_wdt_diagnosis.md)。要設備上線, 由操作者手動
        #    執行選單 1. Scan Devices (廣播 / 定向 IP / 依紀錄敲門)。
        self.load_config()
        if self.config["mapping"]:
            print("ℹ️ 已載入 {} 台設備紀錄 — 設備未上線時, 用選單 1 (掃描/敲門) 手動叫回".format(len(self.config["mapping"])))
        else:
            print("ℹ️ 尚無設備紀錄 (slave_map.json 為空) — 請用 Step 1 掃描/定向連線")

        self._print_menu()
        
        while self.running:
            # Revert to standard input to ensure reliability
            try:
                ch = self.input_with_refresh("\n👉 請選擇操作: ").lower().strip()
            except EOFError:
                break
            
            if not ch:
                continue
                
            if ch == '0':
                self.step_0_update_firmware()
                self._print_menu()
            elif ch == '1':
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
                self.step_7_pairing()
                self._print_menu()
            elif ch == '8':
                self.step_8_profiles()
                self._print_menu()
            elif ch == '9':
                self.step_9_poe_restart()
                self._print_menu()
            elif ch == 'i':
                self.step_i_install_deps()
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
    _auto_switch_to_venv()
    app = NetBusMaster()
    try:
        app.main_loop()
    except KeyboardInterrupt:
        print("\n\n🛑 用戶中斷")
    finally:
        app.panel.stop()
        ConsoleUI.show_cursor()
        print("\n再見! 👋")
