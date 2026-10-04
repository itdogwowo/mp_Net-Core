# -*- coding: utf-8 -*-
"""ESP32-P4 上載極限網速基準測試 (MicroPython)

配對檔: tools/net_bench_pc.py (PC 端)。

測試 ESP32-P4 原生 RMII 乙太網 (IP101 PHY) 往 PC 的「持續上載」吞吐上限。
全程裸 TCP socket,不過 NetBus / SchemaCodec / WebSocket / 任何 slave task
—— 那些協議層的 per-frame 框架與 unmask 會壓低吞吐,要測極限就必須繞過。

善用緩存 / 零 GC (熱路徑零分配,三招):
  1. alloc_dma() 取內部 SRAM 收發 buffer (專案唯一 DMA 入口, 規則一)
  2. payload 預填一次, 迴圈內只送同一塊 memoryview 切片
  3. sock.send(mv[off:]) + sleep_ms(0) 讓 GIL (復刻 lib/net_bus.py::_send_all)
  絕不用 recv()/sendall(bytes_obj) (每次新分配)。
  每 run 前後 gc.collect() + gc.mem_free() 對比, mem_delta==0 即客觀證零分配。

自動連線: 優先沿用 boot 已連好的 WiFi; 否則從 config.json 讀 RMII 參數起
network.LAN。之後 UDP 監聽 discovery_port 等 PC 廣播 beacon, 拿 PC IP 後 TCP connect。

用法 (於 slave 根目錄):
  exec(open("test/bench_net.py").read())     # 完整矩陣, 預設值
  bench_net.run()                            # 同上
  bench_net.run(total_kb=2048)               # 每筆 2 MB
  bench_net.run(chunk_sizes=(4096, 16384))   # 只測指定 chunk
  bench_net.run(runs=1)                      # 每 chunk 只跑 1 次

跑前確認: 需已有可用網路 — boot 連上 WiFi, 或 config.json 的 Network.lan.enable=1。
"""

import gc
import socket
import struct
import time

try:
    import ujson as json
except ImportError:
    import json

try:
    import ubinascii as binascii
    _HAVE_CRC32 = True
except ImportError:
    try:
        import binascii
        _HAVE_CRC32 = True
    except ImportError:
        _HAVE_CRC32 = False

try:
    import machine
    import network
except ImportError:
    machine = None
    network = None

# NC4 協議模組 (Proto.pack 封裝 + StreamParser 拆幀), 走 lib.proto — 同 bench_sd 慣例。
# 用來測「真實指令傳輸」成本: 每個 chunk 經 Proto.pack (含 9B header + CRC32 + framing)
# 封裝, 收端用 StreamParser.feed/pop 拆解 (含 CRC 驗證)。完全模擬 slave Action 系列
# 的封包路徑, 但用虛擬 cmd 0x18F0, 不註冊到任何 dispatcher, 不碰生產碼。
try:
    from lib.sys.proto import Proto, StreamParser
    _HAVE_PROTO = True
except Exception:
    _HAVE_PROTO = False

# 虛擬測試 cmd (0x18xx = 效能測試範圍, 0x18F0 不衝突 bench 的 0x1811-14)
_CMD_BENCH_PROTO = 0x18F0

# 直接用 heap_caps 申請緩存 (參考 health.py + jpeg_player_task 的 PSRAM 例外路徑)。
# 依 buffer-conventions 規則一, 一般路徑應走 lib.buffer_hub.alloc_dma;
# 但本測試要對比 SRAM(DMA) vs PSRAM(SPIRAM) 兩種, 必須直接碰 heap_caps,
# 這正是 buffer-conventions 為 framebuffer 開的同一條例外。
try:
    import heap_caps as _heap_caps
    _HAVE_HEAP_CAPS = True
    _CAP_DMA = _heap_caps.CAP_DMA
    _CAP_SPIRAM = _heap_caps.CAP_SPIRAM
    _hc_malloc = _heap_caps.malloc
    _HC_FREE = _heap_caps.free
except Exception:
    _HAVE_HEAP_CAPS = False
    _CAP_DMA = None
    _CAP_SPIRAM = None
    _hc_malloc = None
    _HC_FREE = None

# ── 測試參數 (預設值; run() 可覆寫) ──
DEFAULT_CHUNK_SIZES = (2048, 4096, 8192, 16384, 32768)
DEFAULT_TOTAL_KB = 4 * 1024          # 每筆 4 MB
DEFAULT_RUNS = 3                     # 每 chunk 重複次數 (取最佳)
DEFAULT_DISC_PORT = 9000             # 對齊 config System.discovery_port
DEFAULT_DATA_PORT = 5001             # PC 資料 TCP server 埠
LINK_WAIT_S = 30                     # 等乙太網 link up / DHCP 最長秒數

_EAGAIN = 11     # POSIX EAGAIN
_EWOULDBLOCK = 35  # ESP32 lwip EWOULDBLOCK

# socket 接收方法探測結果快取 (單元素 list 當可變全域; MicroPython 無 nonlocal 跨函式乾淨寫法)。
# _recv_into_all 首次呼叫時探測 'recv_into'/'readinto'/'recv', 之後熱迴圈直接重用。
_RECV_FN_G = [None]


# ═══════════════════════════════════════════════════════════════
#  helper
# ═══════════════════════════════════════════════════════════════

_CHIP_NAME = None   # 首次偵測後快取


def _chip_name():
    """偵測晶片型號 (MicroPython os.uname().machine), 供標題顯示。

    例: ESP32-P4 / ESP32-S3 / ESP32-C3 等; PC 上跑則回 'ESP'。
    """
    global _CHIP_NAME
    if _CHIP_NAME is not None:
        return _CHIP_NAME
    name = "ESP"
    try:
        import os
        m = getattr(os.uname(), "machine", "") or ""
        for kw in ("ESP32P4", "ESP32S3", "ESP32S2", "ESP32C6", "ESP32C3",
                   "ESP32", "RP2040", "STM32", "MIMXRT"):
            if kw in m:
                name = kw.replace("ESP32", "ESP32-")
                break
    except Exception:
        pass
    if name == "ESP":
        try:
            import sys
            p = getattr(sys, "platform", "esp32")
            name = "ESP32" if "esp32" in str(p).lower() else "ESP"
        except Exception:
            pass
    _CHIP_NAME = name
    return name


def _fmt_bytes(n):
    if n >= 1048576:
        return "{:.1f} MB".format(n / 1048576)
    if n >= 1024:
        return "{:.0f} KB".format(n / 1024)
    return "{} B".format(n)


def _mb_s(total_bytes, elapsed_ms):
    """MB/s (1 MB = 1048576 B)。elapsed_ms<=0 視為 1 避免除零。"""
    if elapsed_ms <= 0:
        elapsed_ms = 1
    return (total_bytes * 1000.0) / (elapsed_ms * 1048576)


def _speed_bar(mb_s):
    if mb_s >= 22:
        return "  ████████"
    if mb_s >= 16:
        return "  ██████"
    if mb_s >= 10:
        return "  ████"
    if mb_s >= 4:
        return "  ██"
    return ""


def _alloc_buf(size, kind="sram"):
    """依 kind 申請發送 buffer。回傳 (buf, kind_actual, heap_caps?)。

    kind:
      "sram"  → heap_caps CAP_DMA (內部 SRAM, 乙太網 MAC DMA 偏好); 不可用 fallback bytearray
      "psram" → heap_caps CAP_SPIRAM (外部 PSRAM); 不可用 fallback bytearray
                (PSRAM 路徑依 buffer-conventions 規則一的 jpeg_player_task 例外,
                 直接用 heap_caps.malloc(CAP_SPIRAM), 不走 alloc_dma)

    kind_actual 回傳實際拿到什麼 ("sram"/"psram"/"bytearray"),
    第 3 元素 True 表示需用 heap_caps.free 釋放。
    """
    if _HAVE_HEAP_CAPS:
        cap = _CAP_SPIRAM if kind == "psram" else _CAP_DMA
        try:
            buf = _hc_malloc(size, cap)
            if buf is not None:
                return buf, kind, True
        except Exception:
            pass
    return bytearray(size), "bytearray", False


def _free_buf(buf, from_hc):
    """釋放 buffer。heap_caps 配的用 heap_caps.free; bytearray 交給 GC。"""
    if from_hc and _HC_FREE is not None and buf is not None:
        try:
            _HC_FREE(buf)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
#  記憶體偵測 (參考 health.py 風格)
# ═══════════════════════════════════════════════════════════════

def _hc_sz(b):
    """heap_caps 容量人類可讀。"""
    for u in ("B", "KB", "MB"):
        if b < 1024:
            return "{}{}".format(b, u)
        b //= 1024
    return "{}MB".format(b)


def detect_memory():
    """偵測並印出系統各類記憶體現況 (DRM/DMA/PSRAM)。

    回傳 dict 供後續決策:
      {"have_heap_caps": bool, "have_sram": bool, "have_psram": bool,
       "sram_free": int, "psram_free": int}
    先 heap_caps.reset() + gc.collect() 清乾淨再量 (同 health.py 順序)。
    """
    info = {"have_heap_caps": _HAVE_HEAP_CAPS,
            "have_sram": False, "have_psram": False,
            "sram_free": 0, "psram_free": 0}

    print("── 記憶體偵測 ──")
    if _HAVE_HEAP_CAPS:
        try:
            _heap_caps.reset()       # 釋放追蹤中 buffer (含前 session 漏 free)
        except Exception:
            pass
    gc.collect()

    print("  GC free: {} KB".format(gc.mem_free() // 1024))

    if not _HAVE_HEAP_CAPS:
        print("  (無 heap_caps 模組 → 只能用 bytearray, 無法對比 SRAM/PSRAM)")
        print()
        return info

    # 列出各類記憶體 total / free / 最大連續 block
    regions = [
        ("DRAM",   _heap_caps.CAP_8BIT | _heap_caps.CAP_INTERNAL),
        ("DMA",    _heap_caps.CAP_DMA),
        ("PSRAM",  _heap_caps.CAP_8BIT | _heap_caps.CAP_SPIRAM),
    ]
    for label, cap in regions:
        try:
            total = _heap_caps.get_total_size(cap)
        except Exception:
            total = 0
        if total == 0:
            print("  {:6s}: 不存在".format(label))
            continue
        try:
            free = _heap_caps.get_free_size(cap)
        except Exception:
            free = 0
        try:
            largest = _heap_caps.get_largest_free_block(cap)
        except Exception:
            largest = 0
        used = total - free
        pct = (used * 100 // total) if total else 0
        print("  {:6s}: {:>7s} total, {:>7s} free, {:>5s} 連續  (used {}%)".format(
            label, _hc_sz(total), _hc_sz(free), _hc_sz(largest), pct))
        if label == "DMA":
            info["have_sram"] = free > 0
            info["sram_free"] = free
        elif label == "PSRAM":
            info["have_psram"] = free > 0
            info["psram_free"] = free

    print()
    return info


# ═══════════════════════════════════════════════════════════════
#  起網 — 只讀 config.json 的 lan 區段, 不起 NetworkManager (它會掃 WiFi)
# ═══════════════════════════════════════════════════════════════

def _load_config():
    """讀 config.json。先試 slave 根目錄 (本檔慣例), 再試 CWD。"""
    for path in ("config.json", "slave/config.json", "../slave/config.json"):
        try:
            with open(path, "r") as f:
                return json.load(f), path
        except OSError:
            continue
    return None, None


_PHY_MAP = {
    "IP101": "PHY_IP101",
    "LAN8720": "PHY_LAN8720",
    "LAN8710": "PHY_LAN8710",
    "DP83848": "PHY_DP83848",
}


def _resolve_phy_type(name):
    """把 config 字串 (如 'IP101') 對應到 network 模組常數。"""
    if network is None:
        return None
    if not isinstance(name, str):
        return name  # 已是常量直接回
    key = str(name).upper()
    const = _PHY_MAP.get(key)
    if const and hasattr(network, const):
        return getattr(network, const)
    return None


def bring_up_ethernet(cfg):
    """依 config.json 的 Network.lan 起 RMII LAN。回傳 (lan_obj, ip_str) 或 (None, None)。"""
    if network is None or machine is None:
        print("❌ 非 MicroPython / 無 network 模組, 無法起乙太網")
        return None, None

    net_cfg = (cfg or {}).get("Network", {}) or {}
    lan_cfg = net_cfg.get("lan", {}) or {}
    if not lan_cfg.get("enable", 0):
        print("❌ config.json 的 Network.lan.enable = 0")
        print("   請改成 1 再跑 (這是 ESP32-P4 RMII 乙太網開關)。")
        return None, None

    g = lan_cfg.get("GPIO", {}) or {}
    for k in ("mdc", "mdio", "ref_clk"):
        if g.get(k) is None:
            print("❌ config Network.lan.GPIO 缺 {} 腳位".format(k))
            return None, None

    driver = str(lan_cfg.get("driver", "RMII")).upper()
    if driver != "RMII":
        print("⚠️ 本測試只支援 RMII 原生 ETH (config driver={})".format(driver))
        print("   SPI/W5500 路徑請改用專案 NetworkManager, 本檔不涵蓋。")
        return None, None

    phy_type = _resolve_phy_type(lan_cfg.get("phy_type", "IP101"))
    if phy_type is None:
        print("⚠️ 無法解析 phy_type={!r}, 回退 PHY_LAN8720".format(lan_cfg.get("phy_type")))
        phy_type = network.PHY_LAN8720

    print("🔌 起動 RMII LAN  mdc={} mdio={} ref_clk={} phy_addr={} phy_type={}".format(
        g["mdc"], g["mdio"], g["ref_clk"], lan_cfg.get("phy_addr", 1),
        lan_cfg.get("phy_type", "IP101")))

    lan = network.LAN(
        mdc=machine.Pin(g["mdc"]),
        mdio=machine.Pin(g["mdio"]),
        ref_clk=machine.Pin(g["ref_clk"]),
        phy_addr=int(lan_cfg.get("phy_addr", 1)),
        phy_type=phy_type,
    )
    lan.active(True)

    # ── 等乙太網就緒, 分兩階段印進度方便診斷 ──
    # 注意: lan.status()==2 是 WiFi 的 LINK_UP 語意, 對乙太網不可靠;
    # 乙太網可靠路徑是 isconnected() + ifconfig() 拿到非 0.0.0.0 的 IP。
    deadline = time.ticks_add(time.ticks_ms(), LINK_WAIT_S * 1000)
    ip = None
    link_up = False
    last_status = None
    while time.ticks_ms() < deadline:
        # 1) link up 判定: isconnected() 為主, 退一步看 status()
        if not link_up:
            try:
                link_up = bool(lan.isconnected())
            except Exception:
                pass
            if not link_up:
                try:
                    last_status = lan.status()
                except Exception:
                    last_status = "?"
            if link_up:
                print("✓ PHY link up")
        # 2) 拿 IP (DHCP): link up 後才有意義
        if link_up:
            try:
                ifc = lan.ifconfig()
                ip = ifc[0]
            except Exception:
                ip = None
            if ip and ip != "0.0.0.0":
                break
        time.sleep(0.5)

    if not link_up:
        print("❌ PHY link 一直沒起來 ({}s)。最後 status={!r}".format(LINK_WAIT_S, last_status))
        print("   檢查: 網線兩端插好? PHY(IP101) 電源? mdc/mdio/ref_clk 腳位對?")
        return None, None
    if not ip or ip == "0.0.0.0":
        print("❌ link 已 up 但 DHCP 取 IP 超時 ({}s)".format(LINK_WAIT_S))
        print("   檢查: 上層 switch/router 有開 DHCP? 或改用固定 IP。")
        return None, None

    print("✓ 乙太網就緒 | IP: {}".format(ip))
    return lan, ip


def _wifi_disable_pm():
    """關閉 WiFi 省電 (根治「PC 要先 ping 才連得上」)。

    ESP32 預設省電時射頻會休眠, ARP/單播回應延遲數百 ms 甚至掉包,
    PC 的 TCP connect 因此 ETIMEDOUT, 但 ping 一下 (強制喚醒) 就好。
    關掉省電即可讓 ESP 對連入隨時回應。失敗靜默 (某些韌體不支援 pm)。
    """
    if network is None:
        return
    try:
        sta = network.WLAN(network.STA_IF)
        for attr in ("PM_NONE", "PM_PERFORMANCE"):
            if hasattr(sta, attr):
                try:
                    sta.config(pm=getattr(sta, attr))
                    print("   WiFi 省電: 已關閉 ({})".format(attr))
                    return
                except Exception:
                    continue
    except Exception:
        pass


def _wifi_ip():
    """若 boot 已連上 WiFi STA, 回傳其 IP; 否則 None。"""
    if network is None:
        return None
    try:
        sta = network.WLAN(network.STA_IF)
        if sta.active() and sta.isconnected():
            ip = sta.ifconfig()[0]
            if ip and ip != "0.0.0.0":
                return ip
    except Exception:
        pass
    return None


def bring_up_network(cfg):
    """優先沿用 boot 已連好的網路 (WiFi STA); 否則照 config 起 RMII LAN。

    回傳 (ip_str, link_name) 或 (None, None)。
    與 health.py 同哲學: 網路若已由 boot 建好, 就直接讀狀態, 不重複初始化。
    """
    # 1. boot 已連的 WiFi (裝置走 WiFi 的常見路徑)
    ip = _wifi_ip()
    if ip:
        _wifi_disable_pm()
        print("✓ 沿用 boot 已連線 WiFi | IP: {}".format(ip))
        return ip, "WIFI"

    # 2. 依 config 起 RMII 乙太網 (僅 lan.enable=1 時)
    net_cfg = (cfg or {}).get("Network", {}) or {}
    lan_cfg = net_cfg.get("lan", {}) or {}
    if lan_cfg.get("enable", 0):
        lan, ip = bring_up_ethernet(cfg)
        if lan is not None:
            return ip, "LAN"

    print("❌ 找不到可用網路: WiFi 未連線, 且 Network.lan.enable 未開啟")
    print("   請先讓 boot 連上 WiFi, 或把 config.json 的 Network.lan.enable 改成 1 起 RMII LAN。")
    return None, None


# ═══════════════════════════════════════════════════════════════
#  自動連線 — UDP 監聽 PC 廣播 beacon, 拿 PC IP 後 TCP connect
# ═══════════════════════════════════════════════════════════════

def _recv_line(sock, timeout_ms=2000):
    """從 TCP 讀一行 (以 \\n 結束)。用最小狀態, 僅控制路徑用 (非熱路徑)。"""
    sock.settimeout(0.2)
    line = bytearray()
    deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
    while time.ticks_ms() < deadline:
        try:
            b = sock.recv(1)
        except OSError:
            continue
        if not b:
            break
        line.extend(b)
        if b == b"\n":
            break
    return bytes(line)


def wait_for_beacon(disc_port, timeout_s=60):
    """UDP 監聽 disc_port, 等 PC 廣播 beacon。回傳 (pc_ip, pc_data_port, params) 或 None。

    params (來自 beacon JSON, 全可缺, 缺則用預設): total_kb / chunk_sizes / runs / data_port
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", disc_port))
    except OSError as e:
        print("❌ UDP bind {} 失敗: {} (該埠可能被 slave network task 佔用)".format(disc_port, e))
        s.close()
        return None
    s.settimeout(0.5)

    print("📡 監聽 PC beacon @ UDP :{} (最多等 {}s)...".format(disc_port, timeout_s))
    deadline = time.ticks_add(time.ticks_ms(), timeout_s * 1000)
    while time.ticks_ms() < deadline:
        try:
            data, addr = s.recvfrom(512)
        except OSError:
            continue
        pc_ip = addr[0]
        if not data:
            continue
        # beacon 是一行 JSON: {"data_port":5001,"total_kb":4096,...}
        params = {}
        try:
            obj = json.loads(data.decode())
            if obj.get("type") != "netbench":
                continue
            params = obj
        except Exception:
            continue

        data_port = int(params.get("data_port", DEFAULT_DATA_PORT))
        print("✓ 收到 PC beacon  {}  → 資料埠 {}".format(pc_ip, data_port))
        s.close()
        return pc_ip, data_port, params

    print("❌ 等待 PC beacon 超時")
    s.close()
    return None


# ═══════════════════════════════════════════════════════════════
#  零 GC 上載核心
# ═══════════════════════════════════════════════════════════════

def _send_all(sock, mv, sub_cap=0):
    """把整段 memoryview 送完 (分段重試 + EAGAIN/EWOULDBLOCK 容忍 + 讓 GIL)。
    復刻 lib/net_bus.py::_send_all。回傳送出位元組數。

    sub_cap > 0 時, 每次 sock.send() 最多只送 sub_cap bytes (用 memoryview 切片限制)。
    用途: lwIP TCP_SND_BUF (~5.7KB) 限制了單次 send 的非阻塞量; chunk > 該值時
    send() 會阻塞等 ACK, 期間 ESP 沒驅動 lwIP → TX 停滯 → 8KB+ 懸崖。
    把每次 send 量壓在 TCP_SND_BUF 以下, 讓 send() 不阻塞、lwIP 持續被驅動。"""
    ln = len(mv)
    off = 0
    retry = 0
    while off < ln:
        seg = mv[off:] if sub_cap <= 0 else mv[off:off + sub_cap]
        try:
            n = sock.send(seg)
            if n is None:
                n = 0
            if n > 0:
                off += n
                retry = 0
                continue
        except OSError as e:
            code = e.args[0] if e.args else None
            if code not in (_EAGAIN, _EWOULDBLOCK):
                raise
        retry += 1
        if retry >= 64:
            raise OSError("send retry exhausted (off {}/{})".format(off, ln))
        try:
            time.sleep_ms(0)   # 讓 GIL (thread_gil_split.py 驗證過的技巧)
        except Exception:
            time.sleep(0)
    return off


def upload_burst(sock, tx_mv, total_bytes, send_cap=0, crc=False):
    """零 GC 洪流上載: 把 tx_mv (一個 chunk) 重複送滿 total_bytes。
    回傳 (sent_bytes, elapsed_ms, mem_delta, crc_val)。

    send_cap > 0 時, _send_all 內每次 send() 最多送 send_cap bytes (見 _send_all 說明)。
      實測: send_cap=4096 (低於 TCP_SND_BUF) 能解除 8KB 懸崖。

    crc=True 時, 「送端」的 CRC 在計時區間外預算 (送端內容固定, 行內算會拖慢送出,
      污染上載吞吐計時)。PC 收端會在計時內算 CRC 驗證收到的內容 — 那才是「帶驗證
      的接收成本」。回傳第 4 元素為送端預算的 CRC, 供 PC 比對。"""
    chunk = len(tx_mv)
    gc.collect()
    mem0 = gc.mem_free()
    t0 = time.ticks_ms()

    sent = 0
    while sent < total_bytes:
        rem = total_bytes - sent
        seg = tx_mv if rem >= chunk else tx_mv[:rem]
        sent += _send_all(sock, seg, sub_cap=send_cap)

    t1 = time.ticks_ms()
    gc.collect()
    mem1 = gc.mem_free()
    elapsed_ms = time.ticks_diff(t1, t0)
    mem_delta = mem0 - mem1   # >0 = 有分配 (GC 沒接住); ==0 = 零分配

    # 送端 CRC 在計時外預算 (不污染吞吐量測); 與 PC 收端串接演算法一致
    crc_val = 0
    if crc and _HAVE_CRC32:
        s = 0
        while s < total_bytes:
            rem = total_bytes - s
            seg = tx_mv if rem >= chunk else tx_mv[:rem]
            crc_val = binascii.crc32(seg, crc_val)
            s += len(seg)
    return sent, elapsed_ms, mem_delta, crc_val & 0xFFFFFFFF


def _recv_into_all(sock, rx_mv):
    """把 rx_mv 收滿 (零分配, 寫進預分配 buffer 的切片)。

    socket 接收 API 跨 MicroPython 移植版不一致 — ESP32-P4 本移植版的
    socket 沒有 recv_into, 只有 readinto。照 lib/net_bus.py 的三級階梯探測:
      recv_into(rx_mv) → readinto(rx_mv) → recv(n) + slice 賦值 (會分配, 最後手段)
    探測只做一次 (模組級快取), 之後熱迴圈直接呼叫綁定的方法。

    EAGAIN/EWOULDBLOCK 容忍 + 讓 GIL。n==0 (對端關閉) 直接 raise 中止。"""
    recv_fn = _RECV_FN_G[0]   # 模組級快取: 'recv_into' / 'readinto' / 'recv'
    if recv_fn is None:
        if hasattr(sock, "recv_into"):
            recv_fn = "recv_into"
        elif hasattr(sock, "readinto"):
            recv_fn = "readinto"
        else:
            recv_fn = "recv"
        _RECV_FN_G[0] = recv_fn   # 記下供後續呼叫重用

    ln = len(rx_mv)
    off = 0
    while off < ln:
        try:
            if recv_fn == "recv":
                raw = sock.recv(ln - off)
                n = len(raw) if raw else 0
                if n > 0:
                    rx_mv[off:off + n] = raw   # 落到預分配 buffer (raw 本身會 GC)
            elif recv_fn == "readinto":
                n = sock.readinto(rx_mv[off:])
            else:  # recv_into
                n = sock.recv_into(rx_mv[off:])
        except OSError as e:
            code = e.args[0] if e.args else None
            if code not in (_EAGAIN, _EWOULDBLOCK):
                raise
            try:
                time.sleep_ms(0)
            except Exception:
                time.sleep(0)
            continue
        if n is None:
            continue
        if n == 0:
            raise OSError("peer closed (recv off {}/{})".format(off, ln))
        off += n
    return off


def _recv_some(sock, rx_mv):
    """讀「可用」數據 (不要求填滿 rx_mv), 回傳實際讀到的 n (>0)。

    協議模式專用。為何不用 readinto/recv_into: 這顆移植版的 socket.readinto
    會「填滿整個 buffer 才返回」;協議幀流每幀多 15B 開銷 (9 header + 2 seq + 4 CRC),
    總長不是 chunk 的整數倍 — 最後一段永遠填不滿, 卡到 socket timeout (ETIMEDOUT)。
    (實證: 2KB 幀流總長 4225024 恰被 2048 整除→成功; 4KB+ 不整除→全掛。)

    改用 recv() 讀多少算多少, 由 StreamParser 用內部緩衝自行對齊幀邊界。
    recv 每次建臨時 bytes (之後 slice 賦值回 rx_mv 交 GC), 協議模式本就含複製, 可接受。"""
    while True:
        try:
            raw = sock.recv(len(rx_mv))
        except OSError as e:
            code = e.args[0] if e.args else None
            if code not in (_EAGAIN, _EWOULDBLOCK):
                raise
            try:
                time.sleep_ms(0)
            except Exception:
                time.sleep(0)
            continue
        if not raw:
            raise OSError("peer closed")
        n = len(raw)
        rx_mv[:n] = raw
        return n


def download_burst(sock, rx_mv, total_bytes, crc=False):
    """零 GC 洪流下載: 用 recv_into 把 rx_mv (一個 chunk) 反覆收滿 total_bytes。
    回傳 (recv_bytes, elapsed_ms, mem_delta, crc_acc)。

    crc=True 時, 每收到一個 chunk 算一次 binascii.crc32 (對齊專案 proto.py
      StreamParser 收到 payload 後驗 CRC 的行為)。"""
    chunk = len(rx_mv)
    gc.collect()
    mem0 = gc.mem_free()
    t0 = time.ticks_ms()

    crc_val = 0
    received = 0
    while received < total_bytes:
        rem = total_bytes - received
        seg = rx_mv if rem >= chunk else rx_mv[:rem]
        received += _recv_into_all(sock, seg)
        if crc and _HAVE_CRC32:
            crc_val = binascii.crc32(seg, crc_val)   # 串接: 與 PC 送端一致, 可比對

    t1 = time.ticks_ms()
    gc.collect()
    mem1 = gc.mem_free()
    elapsed_ms = time.ticks_diff(t1, t0)
    mem_delta = mem0 - mem1
    return received, elapsed_ms, mem_delta, crc_val & 0xFFFFFFFF


# ═══════════════════════════════════════════════════════════════
#  協議模式 — 完全模擬 NC4 指令傳輸 (Proto.pack 封裝 + StreamParser 拆幀)
#  不碰生產碼, 用虛擬 cmd 0x18F0, 測的是「真實上線協議」的吞吐成本
# ═══════════════════════════════════════════════════════════════

def _make_proto_payload(seq, data_mv):
    """模擬 NC4 schema payload: u16 seq + bytes_rest data (像 BENCH_DATA)。
    用 struct.pack 前綴 + data 切片, 不依賴 SchemaCodec 的 viper 編譯。
    回傳 bytes (Proto.pack 會再包成完整幀)。"""
    # <H seq (2B) + data。這模仿 schema 的 {"seq":u16, "data":bytes_rest}
    return struct.pack("<H", seq & 0xFFFF) + bytes(data_mv)


# 預分配的 pack 緩衝 (快版 pack 重複用, 不每幀建新 bytes)
_pack_buf = None
_pack_mv = None
_pack_cap = 0


def pack_into_fast(cmd, seq, data_mv, addr=0xFFFF):
    """快版 NC4 封裝 — 直接寫進預分配 buffer, 不做 header+payload+crc 拼接。

    與 Proto.pack 的差異 (這是 74.7% 瓶頸的解法):
      Proto.pack: header + payload + crc_bytes → 三段拼接建一個新大 bytes (複製 payload)
      pack_into_fast: struct.pack_into 寫 header → mv 切片賦值寫 payload → pack_into 寫 crc
                      全程寫進同一塊預分配 buf, 零大 bytes 建立, 零 payload 複製

    payload 結構同 _make_proto_payload: u16 seq + data (內嵌, 省一次拼接)。
    回傳 memoryview (指向預分配 buf 的前 total_len bytes), 呼叫端直接送。"""
    global _pack_buf, _pack_mv, _pack_cap
    data_len = len(data_mv)
    payload_len = 2 + data_len        # seq(2) + data
    total_len = 9 + payload_len + 4   # header + payload + crc
    # 惰性配 / 不夠大才重配 (正常只配一次)
    if _pack_buf is None or _pack_cap < total_len:
        _pack_cap = total_len + 256   # 預留成長空間
        _pack_buf = bytearray(_pack_cap)
        _pack_mv = memoryview(_pack_buf)

    b = _pack_mv
    # 1. header (9B): SOF + ver + addr + cmd + payload_len
    struct.pack_into("<2sBHHH", b, 0, b"NC", 4, addr, cmd, payload_len)
    # 2. payload: seq(2) + data (直接寫, 不建新 bytes)
    struct.pack_into("<H", b, 9, seq & 0xFFFF)
    b[11:11 + data_len] = data_mv
    # 3. CRC32 (ver..payload_end, 同 Proto.pack: header[2:])
    if _HAVE_CRC32:
        crc = binascii.crc32(b[2:9 + payload_len], 0) & 0xFFFFFFFF
    else:
        crc = 0
    struct.pack_into("<I", b, 9 + payload_len, crc)
    return b[:total_len]   # memoryview 切片, 零分配


# v2: 預算 header 固定前綴的 CRC, 每幀只串接算變動部分 (payload_len + seq + data)
# CRC32 串接性: crc32(B, crc32(A,0)) == crc32(A+B, 0) — 所以固定前綴可預算
_pack_hdr_prefix_crc = None   # 預算的 crc32(ver+addr+cmd, 0), 5B 固定
_pack_hdr_prefix_cmd = None   # 記住是哪個 cmd 算的 (cmd 變要重算)


def pack_into_fast_v2(cmd, seq, data_mv, addr=0xFFFF):
    """快版 v2 — 預算 header 固定前綴 CRC, 每幀省掉 5B 重算。

    原理: CRC32 可串接。header = SOF(2B固定) + ver(1B固定) + addr(2B固定) + cmd(2B固定)
    + payload_len(2B變動)。CRC 範圍是 header[2:] (ver..payload_end), 即:
      ver+addr+cmd (5B固定) + payload_len(2B變) + seq(2B變) + data(nB變)
    固定的 5B (ver+addr+cmd) 預算一次: hdr_prefix_crc = crc32(ver+addr+cmd, 0)
    每幀只串接算變動部分: crc = crc32(len+seq+data, hdr_prefix_crc)
    收端一次算完整段, 結果相同 (CRC32 串接數學保證)。"""
    global _pack_buf, _pack_mv, _pack_cap, _pack_hdr_prefix_crc, _pack_hdr_prefix_cmd
    data_len = len(data_mv)
    payload_len = 2 + data_len
    total_len = 9 + payload_len + 4
    if _pack_buf is None or _pack_cap < total_len:
        _pack_cap = total_len + 256
        _pack_buf = bytearray(_pack_cap)
        _pack_mv = memoryview(_pack_buf)
        _pack_hdr_prefix_crc = None   # buffer 換了要重算

    b = _pack_mv
    # 1. header
    struct.pack_into("<2sBHHH", b, 0, b"NC", 4, addr, cmd, payload_len)
    # 2. payload
    struct.pack_into("<H", b, 9, seq & 0xFFFF)
    b[11:11 + data_len] = data_mv
    # 3. CRC32: 預算固定前綴 (ver+addr+cmd = b[2:7]), 只串接算變動部分 (b[7:9+payload_len])
    if _HAVE_CRC32:
        # 預算/快取 header 固定前綴的 CRC (cmd 變或 buffer 換才重算)
        if _pack_hdr_prefix_crc is None or _pack_hdr_prefix_cmd != cmd:
            _pack_hdr_prefix_crc = binascii.crc32(b[2:7], 0) & 0xFFFFFFFF
            _pack_hdr_prefix_cmd = cmd
        # 串接算變動部分: payload_len(2) + seq(2) + data(n) = b[7:9+payload_len]
        crc = binascii.crc32(b[7:9 + payload_len], _pack_hdr_prefix_crc) & 0xFFFFFFFF
    else:
        crc = 0
    struct.pack_into("<I", b, 9 + payload_len, crc)
    return b[:total_len]


def upload_burst_proto(sock, tx_mv, total_bytes, send_cap=0):
    """NC4 協議上載: 每個 chunk 經 Proto.pack 封裝 (9B header + CRC32 + framing) 再送。
    完全模擬 slave Action 送封包的路徑 (Proto.pack 是 action 回報封包的標準寫法)。

    回傳 (sent_bytes, elapsed_ms, mem_delta, frame_count)。
    sent_bytes = 實際 payload data 累計 (不含協議 header/CRC 開銷)。
    frame_count = 送出的 NC4 幀數 (每個 chunk 一幀)。"""
    chunk = len(tx_mv)
    gc.collect()
    mem0 = gc.mem_free()
    t0 = time.ticks_ms()

    sent = 0
    seq = 0
    frames = 0
    while sent < total_bytes:
        rem = total_bytes - sent
        seg = tx_mv if rem >= chunk else tx_mv[:rem]
        # 模擬 action 送封包: payload 編碼 → Proto.pack (含 CRC) → 送出
        payload = _make_proto_payload(seq, seg)
        pkt = Proto.pack(_CMD_BENCH_PROTO, payload)
        _send_all(sock, pkt, sub_cap=send_cap)
        sent += len(seg)
        seq = (seq + 1) & 0xFFFF
        frames += 1

    t1 = time.ticks_ms()
    gc.collect()
    mem1 = gc.mem_free()
    elapsed_ms = time.ticks_diff(t1, t0)
    mem_delta = mem0 - mem1
    return sent, elapsed_ms, mem_delta, frames


def upload_burst_proto_fast(sock, tx_mv, total_bytes, send_cap=0):
    """NC4 協議上載 (快版 pack): 用 pack_into_fast 封裝, 不做 bytes 拼接。
    對比 upload_burst_proto (慢版 Proto.pack), 量化 pack 優化的效果。
    回傳 (sent_bytes, elapsed_ms, mem_delta, frame_count)。"""
    chunk = len(tx_mv)
    gc.collect()
    mem0 = gc.mem_free()
    t0 = time.ticks_ms()

    sent = 0
    seq = 0
    frames = 0
    while sent < total_bytes:
        rem = total_bytes - sent
        seg = tx_mv if rem >= chunk else tx_mv[:rem]
        pkt_mv = pack_into_fast(_CMD_BENCH_PROTO, seq, seg)   # 快版: 寫進預分配 buf
        _send_all(sock, pkt_mv, sub_cap=send_cap)
        sent += len(seg)
        seq = (seq + 1) & 0xFFFF
        frames += 1

    t1 = time.ticks_ms()
    gc.collect()
    mem1 = gc.mem_free()
    elapsed_ms = time.ticks_diff(t1, t0)
    mem_delta = mem0 - mem1
    return sent, elapsed_ms, mem_delta, frames


# framed 直讀的緩衝 (串流式 resync, 惰性配一次, 跨 run 重用)
_framed_buf = None
_framed_cap = 0


def download_burst_proto(sock, rx_mv, total_bytes):
    """NC4 協議下載 (串流式 framed 直讀 + resync, 不走 StreamParser)。

    TCP 是位元組串流, 沒有幀邊界保證 — 上一幀尾部 / 握手殘留都可能讓「讀固定
    9B header」失步。這裡用滾動緩衝 + 讀取指標 (start/end): 只補足到「至少 9B」
    就開始找 SOF("NC") 定位真正幀開頭, 找不到就往後跳 1 byte 重同步; 定位後讀
    長度 → 補足整幀 → 就地 CRC(對 memoryview 切片, 零複製) → 消費, 未消費的
    位元組留在緩衝給下一幀。緊湊只在「讀取指標走太遠」時才做一次, 不每幀搬移。

    header 欄位 (對齊 Proto.pack): [0:2]=SOF"NC", [2]=ver, [3:5]=addr, [5:7]=cmd,
    [7:9]=payload_len。CRC 範圍 = header[2:] .. payload 尾 (不含 SOF)。

    回傳 (recv_bytes, elapsed_ms, mem_delta, frame_count)。recv_bytes 已扣每幀 seq 2B。
    """
    global _framed_buf, _framed_cap
    chunk = len(rx_mv)
    # payload = seq(2B) + data(chunk), 所以單幀 = header(9) + (chunk+2) + crc(4)
    frame_max = 9 + (chunk + 2) + 4
    # 容 8 幀 + 餘量: recv 一次能收大塊, 緊湊頻率降到 1/8, 且 slice 緊湊是 memmove 級
    need = frame_max * 8 + 64
    if _framed_buf is None or _framed_cap < need:
        _framed_buf = bytearray(need)
        _framed_cap = need
    buf = _framed_buf
    bmv = memoryview(buf)
    start = 0   # 第一個未消費位元組
    end = 0     # 有效位元組尾 (exclusive)

    gc.collect()
    mem0 = gc.mem_free()
    t0 = time.ticks_ms()

    received = 0
    frames = 0

    def _compact():
        """把 [start, end) 搬回開頭, 騰出尾部空間 (僅 buffer 快滿時呼叫)。

        用 slice assignment (C 層 memmove, 一次搬完), 絕不逐 byte Python 迴圈。"""
        nonlocal start, end
        n = end - start
        if n > 0:
            buf[:n] = bmv[start:end]
        start = 0
        end = n

    def _topup(n):
        """補足到未消費段至少 n 位元組。"""
        nonlocal end
        while end - start < n:
            if end >= _framed_cap:
                _compact()
            end += _recv_some(sock, bmv[end:])

    while received < total_bytes:
        # 1) 補到至少 9B
        _topup(9)

        # 2) 找 SOF "NC" (失步往後跳 1 byte)
        while start + 1 < end and not (buf[start] == 0x4E and buf[start + 1] == 0x43):
            start += 1
        _topup(9)
        if buf[start] != 0x4E or buf[start + 1] != 0x43:
            # 沒找到 SOF (理論上 _topup 已補夠, 但保守重試)
            start += 1
            continue

        # 3) 驗 ver + 讀長度
        if buf[start + 2] != 4:
            start += 1
            continue
        ln = buf[start + 7] | (buf[start + 8] << 8)
        if ln < 2 or ln > chunk + 2:   # payload 合法範圍: seq(2) + data(chunk)
            start += 1
            continue
        total_len = 9 + ln + 4

        # 4) 補足整幀
        _topup(total_len)

        # 5) 就地 CRC 驗證 (memoryview 切片, 零複製)
        crc_calc = binascii.crc32(bmv[start + 2:start + 9 + ln], 0) & 0xFFFFFFFF
        crc_recv = (buf[start + 9 + ln] | (buf[start + 9 + ln + 1] << 8)
                    | (buf[start + 9 + ln + 2] << 16) | (buf[start + 9 + ln + 3] << 24))
        if crc_calc != crc_recv:
            start += 1   # CRC 錯 → 重同步
            continue

        # 6) 消費這幀 (payload 是 bmv[start+9:start+9+ln] 的 memoryview, 零複製)
        received += ln - 2   # 扣 seq 2B 前綴
        frames += 1
        start += total_len
        # 緩衝完全消費完 → 直接歸零 (免費, 不需搬移); 否則交給 _topup 在 buffer 滿時
        # 用 slice 緊湊。絕不每幀緊湊 (那會讓 Python 逐 byte 搬移拖垮吞吐)。
        if start == end:
            start = 0
            end = 0

    t1 = time.ticks_ms()
    gc.collect()
    mem1 = gc.mem_free()
    elapsed_ms = time.ticks_diff(t1, t0)
    mem_delta = mem0 - mem1
    return received, elapsed_ms, mem_delta, frames


# ═══════════════════════════════════════════════════════════════
#  瓶頸分析 — 拆解 NC4 協議各環節的 CPU 成本 (不傳輸, 純記憶體操作計時)
# ═══════════════════════════════════════════════════════════════

def profile_proto(chunk_size=4096, frames=500):
    """量測 NC4 協議各環節的 CPU 成本, 找出 -77% 裡哪個最貴。

    每個環節處理 frames 個 chunk (各 chunk_size bytes), 量 ticks_us。
    環節:
      A. bytes(mv) 複製        — pack 內部把 payload 從 mv 複製成 bytes 的成本
      B. struct.pack 前綴       — _make_proto_payload 的 seq 前綴
      C. crc32 單獨             — 純 CRC32 計算 (pack 和 parser 都會算)
      D. Proto.pack 完整封裝    — header + CRC + 組幀 (= B+C+組裝)
      E. StreamParser feed+pop  — 拆幀 + CRC 驗 (= 掃SOF + C + 建 payload)
      F. SchemaCodec.decode     — dispatch 內 payload 解碼 (若有 store)
    回傳 dict {環節: 微秒/幀}。"""
    if not _HAVE_PROTO:
        print("❌ 無 lib.proto, 無法做瓶頸分析")
        return None

    # 準備測試 buffer
    buf = bytearray(chunk_size)
    for i in range(chunk_size):
        buf[i] = i & 0xFF
    mv = memoryview(buf)

    results = {}

    # A. bytes(mv) 複製
    gc.collect()
    t0 = time.ticks_us()
    for _ in range(frames):
        _b = bytes(mv)   # 模擬 pack 內部的 payload 複製
    results["A. bytes(mv)複製"] = (time.ticks_diff(time.ticks_us(), t0)) // frames

    # B. struct.pack 前綴
    gc.collect()
    t0 = time.ticks_us()
    for seq in range(frames):
        _p = struct.pack("<H", seq & 0xFFFF)
    results["B. struct.pack前綴"] = (time.ticks_diff(time.ticks_us(), t0)) // frames

    # C. crc32 單獨 (對 chunk)
    if _HAVE_CRC32:
        gc.collect()
        t0 = time.ticks_us()
        for _ in range(frames):
            _c = binascii.crc32(mv)
        results["C. crc32(chunk)"] = (time.ticks_diff(time.ticks_us(), t0)) // frames

    # D. Proto.pack 完整封裝 (慢版, 現狀)
    gc.collect()
    t0 = time.ticks_us()
    for seq in range(frames):
        payload = struct.pack("<H", seq & 0xFFFF) + bytes(mv)
        _pkt = Proto.pack(_CMD_BENCH_PROTO, payload)
    results["D. Proto.pack完整(慢)"] = (time.ticks_diff(time.ticks_us(), t0)) // frames

    # D2. pack_into_fast 快版封裝 (驗證優化效果)
    gc.collect()
    t0 = time.ticks_us()
    for seq in range(frames):
        _pkt_mv = pack_into_fast(_CMD_BENCH_PROTO, seq, mv)
    results["D2.pack_into_fast(快)"] = (time.ticks_diff(time.ticks_us(), t0)) // frames

    # D3. pack_into_fast_v2 (預算 header CRC, 省固定部分重算)
    gc.collect()
    t0 = time.ticks_us()
    for seq in range(frames):
        _pkt_mv = pack_into_fast_v2(_CMD_BENCH_PROTO, seq, mv)
    results["D3.pack_v2(預算CRC)"] = (time.ticks_diff(time.ticks_us(), t0)) // frames

    # E. StreamParser feed+pop — 逐幀 pack→feed→pop, 不預先組大 buffer。
    #    原版一次組 frames 幀 (500×~4KB≈2MB) 會爆 RAM;
    #    逐幀處理記憶體只剩單幀, 且更貼近真實串流用法。
    parser = StreamParser(max_len=65536)
    gc.collect()
    t0 = time.ticks_us()
    cnt = 0
    for seq in range(frames):
        payload = struct.pack("<H", seq & 0xFFFF) + bytes(mv)
        pkt = Proto.pack(_CMD_BENCH_PROTO, payload)
        parser.feed(pkt)
        for _v, _a, _c, _p in parser.pop():
            cnt += 1
    results["E. StreamParser拆幀"] = (time.ticks_diff(time.ticks_us(), t0)) // frames

    # F. SchemaCodec.decode (若有 store; 模擬 dispatch 的解碼成本)
    try:
        from lib.sys.schema_codec import SchemaCodec
        from lib.sys.schema_loader import SchemaStore
        store = SchemaStore()
        # 找一個有 bytes_rest 的現成 cmd 做解碼成本取樣 (用 bench DATA 0x1812)
        cmd_def = store.get(0x1812)
        if cmd_def:
            test_payload = bytes(mv)  # BENCH_DATA: payload 就是純 data(bytes_rest)
            gc.collect()
            t0 = time.ticks_us()
            for _ in range(frames):
                _args = SchemaCodec.decode(cmd_def, test_payload, store)
            results["F. SchemaCodec.decode"] = (time.ticks_diff(time.ticks_us(), t0)) // frames
    except Exception as e:
        results["F. SchemaCodec.decode"] = "skip({})".format(e)

    # 印報告
    print("\n── NC4 協議瓶頸分析 (chunk={}B, {}幀) ──".format(chunk_size, frames))
    print("  {:>22s}  {:>10s}  {}".format("環節", "μs/幀", "佔比"))
    print("  " + "─" * 48)
    total_us = 0
    num_results = {}
    for k, v in results.items():
        if isinstance(v, int):
            num_results[k] = v
            total_us += v
    for k, v in results.items():
        if isinstance(v, int) and total_us > 0:
            print("  {:>22s}  {:>8d} μs  {:>5.1f}%".format(k, v, v * 100 / total_us))
        else:
            print("  {:>22s}  {:>10s}".format(k, str(v)))
    print("  " + "─" * 48)
    if total_us > 0:
        print("  {:>22s}  {:>8d} μs".format("合計", total_us))
    print()
    return results


# ═══════════════════════════════════════════════════════════════
#  主測試
# ═══════════════════════════════════════════════════════════════

def _send_line(sock, text):
    """送一行控制訊息 (非熱路徑)。"""
    sock.send(text.encode() + b"\n")


# buffer 類型 → 顯示標籤
_KIND_LABEL = {"sram": "SRAM(DMA,內部)", "psram": "PSRAM(SPIRAM)", "bytearray": "bytearray(普通RAM)"}


def _run_matrix_once(sock, buf_kind, max_chunk, chunk_sizes, runs, total_bytes,
                     direction="up", send_cap=0, crc=False, proto=False, fast_pack=False):
    """用指定 buffer 類型跑完整 chunk 矩陣一次。

    direction:
      "up" — 上載 (ESP→PC): ESP 洪流 send, PC recv_into 收。控制行 BEGIN/RESULT/SKIP
      "dl" — 下載 (PC→ESP): PC 洪流 send, ESP recv_into 收。控制行 DL_BEGIN/DL_RESULT/SKIP
    proto=True 時改走 NC4 協議 (Proto.pack 封裝 + StreamParser 拆幀), 控制行加 P_ 前綴。
    回傳 results = [(chunk, best_ms, best_mem_delta), ...] (只含成功的 chunk)。
    每輪用同一塊 buffer (最大 chunk), 小 chunk 用 memoryview 切片重用, 全程零 churn。
    """
    tx_buf, actual_kind, from_hc = _alloc_buf(max_chunk, kind=buf_kind)
    label = _KIND_LABEL.get(actual_kind, actual_kind)
    if direction == "dl":
        pass
    else:
        for i in range(max_chunk):
            tx_buf[i] = i & 0xFF

    verb = "下載" if direction == "dl" else "上載"
    mode = " (NC4 協議)" if proto else ""
    print("── {}{} buffer: {} ({}) ──".format(verb, mode, _fmt_bytes(max_chunk), label))
    if actual_kind != buf_kind:
        print("  ⚠️ 想要 {} 但拿到 {} (該類記憶體不足或不存在)".format(buf_kind, actual_kind))

    results = []
    for chunk_size in chunk_sizes:
        if chunk_size <= 0:
            continue
        seg_mv = memoryview(tx_buf)[:chunk_size]

        best_ms = None
        best_mem = 0
        ok = True
        for r in range(runs):
            # 協議模式用 P_ 前綴控制行; 裸 TCP 用原控制行
            if proto:
                begin_tag = "P_DL_BEGIN" if direction == "dl" else "P_BEGIN"
                result_tag = "P_DL_RESULT" if direction == "dl" else "P_RESULT"
            else:
                begin_tag = "DL_BEGIN" if direction == "dl" else "BEGIN"
                result_tag = "DL_RESULT" if direction == "dl" else "RESULT"
            _send_line(sock, "{} {} {}".format(begin_tag, chunk_size, total_bytes))
            try:
                if proto:
                    if direction == "dl":
                        nbytes, ms, mem_delta, _extra = download_burst_proto(sock, seg_mv, total_bytes)
                    else:
                        if fast_pack:
                            nbytes, ms, mem_delta, _extra = upload_burst_proto_fast(sock, seg_mv, total_bytes, send_cap=send_cap)
                        else:
                            nbytes, ms, mem_delta, _extra = upload_burst_proto(sock, seg_mv, total_bytes, send_cap=send_cap)
                else:
                    if direction == "dl":
                        nbytes, ms, mem_delta, _extra = download_burst(sock, seg_mv, total_bytes, crc=crc)
                    else:
                        nbytes, ms, mem_delta, _extra = upload_burst(sock, seg_mv, total_bytes, send_cap=send_cap, crc=crc)
            except OSError as e:
                print("  ❌ chunk={} run={} {}中斷: {}".format(chunk_size, r, verb, e))
                ok = False
                break
            # 以下為共用 (量校驗 + 回報)
            if nbytes != total_bytes:
                print("  ⚠️ chunk={} run={} 量不符: got={} expected={}".format(
                    chunk_size, r, nbytes, total_bytes))
                ok = False
                break
            resp_tag = result_tag
            # RESULT 行第 6 欄: 裸 TCP 帶 CRC (PC 比對驗證); 協議模式帶 frame_count
            # (協議模式的 CRC 驗證已內建在 StreamParser.pop, PC 用 frame_count 驗收到的幀數)
            _send_line(sock, "{} {} {} {} {} {}".format(resp_tag, chunk_size, nbytes, ms, mem_delta, _extra))
            if best_ms is None or ms < best_ms:
                best_ms = ms
                best_mem = mem_delta

        if not ok:
            try:
                _send_line(sock, "SKIP {}".format(chunk_size))
            except Exception:
                pass
            continue

        mb = _mb_s(total_bytes, best_ms)
        if best_mem == 0:
            gc_tag = "✅"
        elif best_mem < 0:
            gc_tag = "♻️"   # 淨回收 (這輪 GC 回收 > 分配, 多為前一輪臨時物件這輪回收)
        else:
            gc_tag = "⚠️+{}".format(best_mem)
        print("  {:>8s}  {:>8s}  {:>7d} ms  {:>7.2f} MB/s  GC {}{}".format(
            _fmt_bytes(chunk_size), _fmt_bytes(total_bytes), best_ms, mb, gc_tag,
            _speed_bar(mb)))
        results.append((chunk_size, best_ms, best_mem))

    _free_buf(tx_buf, from_hc)
    print()
    return results


def _print_compare_generic(all_results, total_bytes, title):
    """多組結果並排對比表 (通用)。只在 ≥1 種有結果時印; <2 種只印單欄。
    all_results: {label: [(chunk, best_ms, best_mem), ...]}"""
    kinds = [k for k, v in all_results.items() if v]
    if not kinds:
        return

    print("╔" + "=" * 60 + "╗")
    print("║" + title.center(54) + "║")
    print("╚" + "=" * 60 + "╝")
    hdr = "  {:>8s}".format("chunk")
    for k in kinds:
        hdr += " │ {:>13s}".format(k)
    print(hdr)
    print("  " + "─" * (9 + len(kinds) * 16))

    # 收集所有 chunk (取聯集)
    all_chunks = sorted({r[0] for k in kinds for r in all_results[k]})
    for ch in all_chunks:
        row = "  {:>8s}".format(_fmt_bytes(ch))
        for k in kinds:
            found = next((r for r in all_results[k] if r[0] == ch), None)
            if found:
                row += " │ {:>10.2f} MB/s".format(_mb_s(total_bytes, found[1]))
            else:
                row += " │ {:>13s}".format("-")
        print(row)
    print("  " + "─" * (9 + len(kinds) * 16))
    print()


def _print_report(total_bytes, up, dl, up_crc, dl_crc, up_proto, dl_proto, up_proto_fast, up_kind, dl_kind):
    """最終報告: 雙向表 + 雙向+CRC 表 + NC4 協議表 + pack 優化對比表 + 摘要。
    各參數: [(chunk, best_ms, best_mem), ...] 各方向的結果 (空 list = 未測)。
    """
    print("\n" + "╔" + "═" * 62 + "╗")
    print("║" + ("  {} 雙向吞吐報告".format(_chip_name())).center(48) + "║")
    print("╚" + "═" * 62 + "╝")

    # ── 報告表 1: 雙向 (純傳輸, 上載分段 vs 下載) ──
    _print_compare_generic(
        {"上載 ESP→PC": up, "下載 PC→ESP": dl},
        total_bytes, "雙向吞吐 (純傳輸, 上載{}/下載{})".format(up_kind, dl_kind))

    # ── 報告表 2: 雙向 + CRC32 校驗 ──
    _print_compare_generic(
        {"上載+CRC32": up_crc, "下載+CRC32": dl_crc},
        total_bytes, "雙向吞吐 + CRC32 校驗 (對齊 proto.py)")

    # ── 報告表 3: 裸 TCP vs NC4 協議 (真實指令傳輸成本) ──
    if up_proto or dl_proto:
        _print_compare_generic(
            {"上載 NC4協議": up_proto, "下載 NC4協議": dl_proto},
            total_bytes, "NC4 協議傳輸 (Proto.pack+StreamParser, 真實指令路徑)")

    # ── 報告表 4: pack 優化對比 (慢版 Proto.pack vs 快版 pack_into_fast) ──
    if up_proto_fast:
        _print_compare_generic(
            {"NC4慢版pack": up_proto, "NC4快版pack": up_proto_fast},
            total_bytes, "pack 優化對比 (Proto.pack vs pack_into_fast, 上載)")

    # ── 摘要: 各方向峰值 + 各層開銷 ──
    def _peak(res):
        if not res:
            return None
        return max((_mb_s(total_bytes, r[1]) for r in res), default=None)

    up_p, dl_p = _peak(up), _peak(dl)
    up_c_p, dl_c_p = _peak(up_crc), _peak(dl_crc)
    up_pr_p, dl_pr_p = _peak(up_proto), _peak(dl_proto)
    up_pr_f_p = _peak(up_proto_fast)

    print("─" * 62)
    print("摘要:")
    if up_p:
        print("  上載峰值 (裸TCP): {:.2f} MB/s ({:.1f} Mbit/s)".format(up_p, up_p * 8))
    if dl_p:
        print("  下載峰值 (裸TCP): {:.2f} MB/s ({:.1f} Mbit/s)".format(dl_p, dl_p * 8))
    if up_p and dl_p:
        print("  雙向對稱性: 上載/下載 = {:.2f}x".format(up_p / dl_p))
    print()
    if up_p and up_c_p:
        print("  上載 CRC32 開銷: {:.2f} → {:.2f} MB/s (-{:.0f}%)".format(
            up_p, up_c_p, (1 - up_c_p / up_p) * 100))
    if dl_p and dl_c_p:
        print("  下載 CRC32 開銷: {:.2f} → {:.2f} MB/s (-{:.0f}%)".format(
            dl_p, dl_c_p, (1 - dl_c_p / dl_p) * 100))
    print()
    if up_p and up_pr_p:
        print("  上載 NC4慢版pack: {:.2f} → {:.2f} MB/s (-{:.0f}%)".format(
            up_p, up_pr_p, (1 - up_pr_p / up_p) * 100))
    if up_p and up_pr_f_p:
        print("  上載 NC4快版pack: {:.2f} → {:.2f} MB/s (-{:.0f}%)".format(
            up_p, up_pr_f_p, (1 - up_pr_f_p / up_p) * 100))
    if up_pr_p and up_pr_f_p:
        print("  pack 優化提升: {:.2f} → {:.2f} MB/s (+{:.0f}%)".format(
            up_pr_p, up_pr_f_p, (up_pr_f_p / up_pr_p - 1) * 100))
    if dl_p and dl_pr_p:
        print("  下載 NC4 協議開銷: {:.2f} → {:.2f} MB/s (-{:.0f}%)".format(
            dl_p, dl_pr_p, (1 - dl_pr_p / dl_p) * 100))
    print("─" * 62)


def run(total_kb=DEFAULT_TOTAL_KB,
        chunk_sizes=DEFAULT_CHUNK_SIZES,
        runs=DEFAULT_RUNS,
        disc_port=DEFAULT_DISC_PORT,
        data_port=DEFAULT_DATA_PORT,
        cfg=None,
        skip=()):
    """完整上載基準矩陣。

    參數:
      total_kb    每筆上載總量 (KB)
      chunk_sizes 要掃的 chunk 大小 tuple
      runs        每 chunk 重複次數 (取最佳值, 消首次冷啟噪音)
      disc_port   UDP beacon 監聽埠 (對齊 config System.discovery_port)
      data_port   預設 PC 資料 TCP 埠 (beacon 會帶實際值覆寫)
      cfg         已載入的 config dict; None 則本檔自己讀
      skip        要跳過的階段 (tuple/set 字串), 可選:
                    "profile"    NC4 協議瓶頸分析 (純 CPU)
                    "cliff"      上載基線 + 分段 (懸崖驗證)
                    "dl"         裸 TCP 下載
                    "crc"        上載/下載 + CRC32 校驗
                    "ram"        RAM 對比 (SRAM vs PSRAM)
                    "proto"      NC4 協議模式 (上載 + 下載)
                    "proto_up"   僅 NC4 協議上載
                    "proto_dl"   僅 NC4 協議下載
                    "fastpack"   快版 pack 上載
                  例: run(skip=("profile","cliff","dl","crc","ram","proto_up","fastpack"))
                  只跑 NC4 協議下載
    """
    skip = set(skip or ())

    # 各階段結果先初始化, 跳過的階段維持空 list, 最終報告不會 NameError
    up_baseline = []
    up_subcapped = []
    dl_results = []
    up_crc = []
    dl_crc = []
    up_proto = []
    dl_proto = []
    up_proto_fast = []

    print("\n" + "╔" + "=" * 60 + "╗")
    print("║" + ("  {} → PC  上載極限吞吐 (裸 TCP / 零 GC)".format(_chip_name())).center(60) + "║")
    print("╚" + "=" * 60 + "╝")

    # ── 1. 讀 config + 起乙太網 ──
    if cfg is None:
        cfg, cfg_path = _load_config()
        if cfg is None:
            print("❌ 找不到 config.json (試過 config.json / slave/config.json)")
            return
        print("✓ 讀取設定: {}".format(cfg_path))

    my_ip, _link_name = bring_up_network(cfg)
    if my_ip is None:
        return

    # 對齊 config 的 discovery_port (beacon 監聽同一埠)
    sys_cfg = (cfg.get("System", {}) or {})
    disc_port = int(sys_cfg.get("discovery_port", disc_port))

    # ── 1b. NC4 協議瓶頸分析 (純 CPU, 不需網路; 在傳輸測試前先量化各環節成本) ──
    if "profile" not in skip:
        profile_proto(chunk_size=4096, frames=500)

    # ── 2. 等 PC beacon ──
    found = wait_for_beacon(disc_port)
    if not found:
        return
    pc_ip, pc_data_port, params = found

    # beacon 可帶參數覆寫 (PC 端 CLI 設定)
    if "total_kb" in params:
        total_kb = int(params["total_kb"])
    if "runs" in params:
        runs = int(params["runs"])
    if "chunk_sizes" in params:
        chunk_sizes = tuple(int(x) for x in params["chunk_sizes"])

    total_bytes = total_kb * 1024

    # ── 3. TCP connect PC ──
    print("🔌 TCP connect {}:{} ...".format(pc_ip, pc_data_port))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5)
    try:
        sock.connect((pc_ip, pc_data_port))
    except OSError as e:
        print("❌ TCP connect 失敗: {}".format(e))
        return
    # 送資料用阻塞 + 超時 (洪流期間靠 send() 自然背壓)
    sock.settimeout(15)
    print("✓ 已連線\n")

    # ── 4. 握手: 等 PC 的 HELLO ──
    hello = _recv_line(sock, timeout_ms=3000)
    if b"HELLO" not in hello:
        print("⚠️ 沒收到 PC HELLO (got {!r}), 仍繼續嘗試".format(hello))

    # _recv_line 把 timeout 改成 0.2s; 洪流期靠 send() 自然背壓, 但 socket buffer 滿時
    # send 會阻塞等 PC 收資料 — 若 timeout 太短會 ETIMEDOUT 中斷洪流。這裡重設為夠長。
    sock.settimeout(15)

    # ── 5. 記憶體偵測, 決定要測哪些 buffer 類型 ──
    mem_info = detect_memory()
    # 上載用 sram 為主 (之前測過 sram≈psram, 對比懸崖只需一種); 若無 sram 用 bytearray
    up_kind = "sram" if mem_info["have_sram"] else "bytearray"
    # 下載用 psram 為主 (大 buffer 收得穩); 若無 psram 用 sram
    dl_kind = "psram" if mem_info["have_psram"] else ("sram" if mem_info["have_sram"] else "bytearray")
    max_chunk = max(c for c in chunk_sizes if c > 0)

    def _set_nodelay(on):
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1 if on else 0)
        except Exception:
            pass

    # ════════════════════════════════════════════════════════════
    #  5a. 上載 — 懸崖根因驗證: 分段發送 (sub_cap=4KB)
    #      假設: 8KB 懸崖是 lwIP TCP_SND_BUF(~5.7KB) 限制 — chunk > 該值時
    #      send() 阻塞等 ACK, 期間 ESP 沒驅動 lwIP → TX 停滯。
    #      解法: 不管邏輯 chunk 多大, 每次只送 ≤4KB, 讓 send() 不阻塞。
    #      若分段的 8KB+ 回升到 ~9, 根因確認 + 懸崖解除。
    # ════════════════════════════════════════════════════════════
    if "cliff" not in skip:
        print("╔" + "=" * 60 + "╗")
        print("║" + "  上載 (ESP→PC) — 懸崖根因驗證".center(48) + "║")
        print("╚" + "=" * 60 + "╝")

        print("── 基線: 一次送整個 chunk (現狀, 有懸崖) ──")
        up_baseline = _run_matrix_once(
            sock, up_kind, max_chunk, chunk_sizes, runs, total_bytes, direction="up")
        time.sleep(0.3)
        sock.settimeout(15)
        print("── 實驗: 分段發送, 每次 send ≤ 4KB (低於 TCP_SND_BUF) ──")
        up_subcapped = _run_matrix_once(
            sock, up_kind, max_chunk, chunk_sizes, runs, total_bytes, direction="up", send_cap=4096)

    # ════════════════════════════════════════════════════════════
    #  5b. 下載 (PC→ESP) — 補成雙向; Nagle 預設 (off)
    # ════════════════════════════════════════════════════════════
    if "dl" not in skip:
        _set_nodelay(False)
        print("╔" + "=" * 60 + "╗")
        print("║" + "  下載 (PC→ESP)".center(54) + "║")
        print("╚" + "=" * 60 + "╝")
        dl_results = _run_matrix_once(
            sock, dl_kind, max_chunk, chunk_sizes, runs, total_bytes, direction="dl")

    # ════════════════════════════════════════════════════════════
    #  5c. CRC32 校驗開銷 — 分段上載 + 每段算 CRC32 (對齊 proto.py)
    #      量測「加上 CRC32 校驗」比純傳輸下降多少。
    # ════════════════════════════════════════════════════════════
    if "crc" not in skip:
        time.sleep(0.3)
        sock.settimeout(15)
        crc_tag = "有" if _HAVE_CRC32 else "無 binascii"
        print("╔" + "=" * 60 + "╗")
        print("║" + "  上載分段 + CRC32 校驗 ({}模組)".format(crc_tag).center(46) + "║")
        print("╚" + "=" * 60 + "╝")
        up_crc = _run_matrix_once(
            sock, up_kind, max_chunk, chunk_sizes, runs, total_bytes,
            direction="up", send_cap=4096, crc=True)

    # ════════════════════════════════════════════════════════════
    #  5d. 下載 + CRC32 — 配對上載+CRC, 湊齊雙向+CRC 報告
    # ════════════════════════════════════════════════════════════
    if "crc" not in skip:
        time.sleep(0.3)
        sock.settimeout(15)
        print("╔" + "=" * 60 + "╗")
        print("║" + "  下載 (PC→ESP) + CRC32 校驗".center(48) + "║")
        print("╚" + "=" * 60 + "╝")
        dl_crc = _run_matrix_once(
            sock, dl_kind, max_chunk, chunk_sizes, runs, total_bytes,
            direction="dl", crc=True)

    # ════════════════════════════════════════════════════════════
    #  5g. RAM 對比 — 上載/下載各跑 SRAM(DMA) 與 PSRAM 兩種 buffer
    #      (health.py 同款「兩種 RAM 都測」; 上載走分段 ≤4KB 最佳路徑)
    # ════════════════════════════════════════════════════════════
    up_by_ram = {}
    dl_by_ram = {}
    if "ram" not in skip:
        buf_kinds = []
        if mem_info["have_sram"]:
            buf_kinds.append("sram")
        if mem_info["have_psram"]:
            buf_kinds.append("psram")
        if not buf_kinds:
            buf_kinds.append("bytearray")

        _ram_short = {"sram": "SRAM", "psram": "PSRAM", "bytearray": "bytearray"}

        print("╔" + "=" * 60 + "╗")
        print("║" + "  RAM 對比 — SRAM(DMA) vs PSRAM(SPIRAM)".center(44) + "║")
        print("╚" + "=" * 60 + "╝")

        for kind in buf_kinds:
            time.sleep(0.3)
            sock.settimeout(15)
            up_by_ram[kind] = _run_matrix_once(
                sock, kind, max_chunk, chunk_sizes, runs, total_bytes,
                direction="up", send_cap=4096)
            time.sleep(0.3)
            sock.settimeout(15)
            dl_by_ram[kind] = _run_matrix_once(
                sock, kind, max_chunk, chunk_sizes, runs, total_bytes,
                direction="dl")

        if len(buf_kinds) >= 2:
            _print_compare_generic(
                {"上載 " + _ram_short.get(k, k): up_by_ram[k] for k in buf_kinds},
                total_bytes, "上載 RAM 對比 (分段 ≤4KB)")
            _print_compare_generic(
                {"下載 " + _ram_short.get(k, k): dl_by_ram[k] for k in buf_kinds},
                total_bytes, "下載 RAM 對比")

    # ════════════════════════════════════════════════════════════
    #  5e. NC4 協議模式 — 真實指令傳輸 (Proto.pack 封裝 + StreamParser 拆幀)
    #      完全模擬 slave Action 的封包路徑, 用虛擬 cmd 0x18F0, 不碰生產碼。
    #      對比裸 TCP vs 協議棧, 量化「上線 NC4」的實際成本。
    # ════════════════════════════════════════════════════════════
    if "proto" not in skip and _HAVE_PROTO:
        time.sleep(0.3)
        sock.settimeout(15)
        print("╔" + "=" * 60 + "╗")
        print("║" + "  NC4 協議模式 (Proto.pack + StreamParser)".center(40) + "║")
        print("╚" + "=" * 60 + "╝")
        if "proto_up" not in skip:
            print("── 上載 (協議封裝, 分段 ≤4KB) ──")
            up_proto = _run_matrix_once(
                sock, up_kind, max_chunk, chunk_sizes, runs, total_bytes,
                direction="up", send_cap=4096, proto=True)
            time.sleep(0.3)
            sock.settimeout(15)
        if "proto_dl" not in skip:
            print("── 下載 (協議拆幀) ──")
            dl_proto = _run_matrix_once(
                sock, dl_kind, max_chunk, chunk_sizes, runs, total_bytes,
                direction="dl", proto=True)
    elif "proto" not in skip:
        print("\n⚠️ 無 lib.proto 模組, 跳過 NC4 協議模式測試")

    # ════════════════════════════════════════════════════════════
    #  5f. 快版 pack 上載 — pack_into_fast (預分配 buffer, 零拼接)
    #      對比 5e 的慢版 Proto.pack, 量化 pack 優化的實際傳輸效果
    # ════════════════════════════════════════════════════════════
    if "fastpack" not in skip and _HAVE_PROTO:
        time.sleep(0.3)
        sock.settimeout(15)
        print("╔" + "=" * 60 + "╗")
        print("║" + "  快版 pack 上載 (pack_into_fast, 零拼接)".center(36) + "║")
        print("╚" + "=" * 60 + "╝")
        up_proto_fast = _run_matrix_once(
            sock, up_kind, max_chunk, chunk_sizes, runs, total_bytes,
            direction="up", send_cap=4096, proto=True, fast_pack=True)

    # ── 6. 收尾 ──
    try:
        _send_line(sock, "ENDALL")
    except Exception:
        pass
    try:
        sock.close()
    except Exception:
        pass

    if not (up_baseline or up_subcapped or dl_results or up_crc or dl_crc or up_proto or dl_proto or up_proto_fast):
        print("\n❌ 沒有成功的量測")
        return

    # ── 最終報告: 雙向表 + 雙向+CRC 表 + NC4 協議表 + pack 優化表 + 摘要 ──
    _print_report(total_bytes,
                  up=up_subcapped, dl=dl_results,
                  up_crc=up_crc, dl_crc=dl_crc,
                  up_proto=up_proto, dl_proto=dl_proto,
                  up_proto_fast=up_proto_fast,
                  up_kind=up_kind, dl_kind=dl_kind)

    # ── 全局最佳 ──
    print("─" * 60)
    best = None
    best_tag = None
    for tag, res in (("上載分段", up_subcapped),
                     ("下載", dl_results),
                     ("上載+CRC32", up_crc),
                     ("下載+CRC32", dl_crc),
                     ("上載 NC4", up_proto),
                     ("下載 NC4", dl_proto),
                     ("上載 NC4快pack", up_proto_fast)):
        for row in res:
            mb = _mb_s(total_bytes, row[1])
            if best is None or mb > best[0]:
                best = (mb, row)
                best_tag = tag
    print("🏆 全局最佳: [{}] chunk={} → {:.2f} MB/s ({:.1f} Mbit/s)".format(
        best_tag, _fmt_bytes(best[1][0]), best[0], best[0] * 8))
    all_rows = up_subcapped + dl_results + up_crc + dl_crc
    all_zero_gc = all(r[2] == 0 for r in all_rows)
    print("🧹 零 GC: {}".format("✅ 全程 mem_delta=0" if all_zero_gc else "⚠️ 有分配, 見上表"))
    print("─" * 60)


# if __name__ == "__main__":
#     run()
