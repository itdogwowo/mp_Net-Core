#!/usr/bin/env python3
# poe_restart.py — Cisco 3560 PoE port 電源工具（重啟 / 關閉 / 開啟）
#   重啟 = 斷電 → 等待 → 恢復供電; 關閉 = 只斷電; 開啟 = 只供電
# 跨平台：macOS / Windows。需要 netmiko（腳本會自動偵測並提議安裝）。
# 模擬模式：python3 poe_restart.py --dry-run（只印指令，不連線）
import sys
import time

# ============================================================
# 設定區 — 要改就改這裡
# ============================================================
SWITCHES = {
    "1": {"name": "Light-SW-01", "host": "192.168.8.254"},
    "2": {"name": "Light-SW-02", "host": "192.168.8.253"},
}
USERNAME = "admin"               # 有帳號的登入才會用到
PASSWORD = "Zion4794"
SECRET = "Zion4794"              # enable 密碼（與登入密碼相同）
PROTECTED_PORTS = {46, 47, 48}   # 46=電腦 47=switch互連 48=router，永遠不動
MAX_PORT = 48
CONTROL_MIN, CONTROL_MAX = 1, 45
OFF_DELAY = 5                    # 斷電後等幾秒恢復供電
ON_RETRIES = 3                   # 恢復供電失敗時的重試次數
IFACE_PREFIX = "GigabitEthernet0/"  # 模組化機型改 "GigabitEthernet1/0/"
RANGE_GROUP_SIZE = 5             # 一條 interface range 最多幾段（舊 IOS 上限）


# ============================================================
# 純邏輯（可獨立測試，不碰網路）
# ============================================================
def parse_ports(text):
    """'3,5,10-15' → {3,5,10,11,12,13,14,15}；格式錯誤丟 ValueError。"""
    ports = set()
    if not text or not text.strip():
        raise ValueError("輸入是空的")
    for piece in text.split(","):
        piece = piece.strip()
        if not piece:
            raise ValueError("有多餘的逗號")
        if "-" in piece:
            a, b = piece.split("-", 1)
            start, end = int(a), int(b)
            if start > end:
                raise ValueError(f"範圍顛倒了: {piece}")
            if start < 1 or end > MAX_PORT:
                raise ValueError(f"port 要在 1-{MAX_PORT} 之間: {piece}")
            ports.update(range(start, end + 1))
        else:
            n = int(piece)
            if n < 1 or n > MAX_PORT:
                raise ValueError(f"port 要在 1-{MAX_PORT} 之間: {piece}")
            ports.add(n)
    return ports


def filter_protected(ports):
    """回傳 (可操作的 sorted list, 被保護而剔除的 sorted list)。"""
    allowed = sorted(p for p in ports if p not in PROTECTED_PORTS)
    skipped = sorted(p for p in ports if p in PROTECTED_PORTS)
    return allowed, skipped


def compress_ranges(ports):
    """[1,2,3,7] → [(1,3),(7,7)] 連續段壓縮。"""
    runs = []
    for p in sorted(ports):
        if runs and p == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], p)
        else:
            runs.append((p, p))
    return runs


def fmt_ports(ports):
    """[1,2,3,7] → '1-3, 7'（給人看的格式）。"""
    return ", ".join(
        str(a) if a == b else f"{a}-{b}" for a, b in compress_ranges(ports)
    )


def build_iface_entries(ports, prefix=IFACE_PREFIX):
    """port 集合 → interface range 用的段落字串。"""
    entries = []
    for start, end in compress_ranges(ports):
        if start == end:
            entries.append(f"{prefix}{start}")
        else:
            entries.append(f"{prefix}{start} - {end}")
    return entries


def build_power_cmds(ports, state, prefix=IFACE_PREFIX, group_size=RANGE_GROUP_SIZE):
    """產生整段 config 指令。state 是 'never'（斷電）或 'auto'（供電）。"""
    entries = build_iface_entries(ports, prefix)
    cmds = []
    for i in range(0, len(entries), group_size):
        cmds.append("interface range " + " , ".join(entries[i:i + group_size]))
        cmds.append(f"power inline {state}")
    return cmds


# ============================================================
# 連線層
# ============================================================
class DryRunConnection:
    """模擬連線：把會送出的指令印出來，什麼都不做。"""

    def __init__(self, name):
        self.name = name

    def enable(self):
        pass

    def send_config_set(self, cmds):
        print(f"[DRY-RUN {self.name}] 以下指令只是預覽，沒有真的送出:")
        for c in cmds:
            print(f"    {c}")
        return ""

    def disconnect(self):
        pass


import os
import subprocess


def ensure_netmiko():
    """檢查 netmiko；沒裝就提示安裝指令並問要不要自動安裝。"""
    try:
        import netmiko  # noqa: F401
        return
    except ImportError:
        pass
    pip_cmd = "pip install netmiko" if os.name == "nt" else "pip3 install netmiko"
    print("📦 這個腳本需要 Netmiko 程式庫，但目前的 Python 環境還沒安裝。")
    print(f"手動安裝指令: {pip_cmd}")
    ans = input("要現在自動幫你安裝嗎? (yes/no): ").strip().lower()
    if ans == "yes":
        r = subprocess.run([sys.executable, "-m", "pip", "install", "netmiko"])
        if r.returncode == 0:
            try:
                import netmiko  # noqa: F401
                print("✅ 安裝完成，繼續執行。\n")
                return
            except ImportError:
                pass
        print("❌ 自動安裝失敗。")
    print(f"請手動執行: {pip_cmd}")
    sys.exit(1)


def connect_real(sw):
    """先試 Telnet（快），失敗改試 SSH。回傳已 enable 的連線。"""
    from netmiko import ConnectHandler
    from netmiko.exceptions import (
        NetmikoAuthenticationException,
        NetmikoTimeoutException,
    )

    errors = []
    for device_type, label in (("cisco_ios_telnet", "Telnet"), ("cisco_ios", "SSH")):
        try:
            print(f"🔌 連線 {sw['name']} ({sw['host']}) — {label} ...")
            conn = ConnectHandler(
                device_type=device_type,
                host=sw["host"],
                username=USERNAME,
                password=PASSWORD,
                secret=SECRET,
                conn_timeout=15,
                timeout=60,
            )
            conn.enable()
            print(f"✅ 已連上（{label}）")
            return conn
        except NetmikoAuthenticationException as e:
            errors.append(f"{label}: 帳號或密碼錯誤 — {e}")
            print(f"⚠️ {label} 登入被拒（帳號/密碼錯誤）")
        except NetmikoTimeoutException as e:
            errors.append(f"{label}: 連不到裝置（沒有回應）— {e}")
            print(f"⚠️ {label} 連不上（沒有回應）")
        except Exception as e:
            errors.append(f"{label}: {e}")
            print(f"⚠️ {label} 連不上")
    raise ConnectionError(
        f"{sw['name']} ({sw['host']}) Telnet 和 SSH 都連不上:\n  " + "\n  ".join(errors)
    )


def power_on_with_retry(sw, conn, ports, dry_run):
    """恢復供電；失敗就重連重試。全部失敗 → 大聲警告後退出。回傳目前的連線。"""
    cmds = build_power_cmds(ports, "auto")
    print(f"🔌 [{sw['name']}] 恢復供電 ...")
    for attempt in range(1, ON_RETRIES + 1):
        try:
            conn.send_config_set(cmds)
            return conn
        except Exception as e:
            print(f"⚠️ 恢復供電失敗（第 {attempt}/{ON_RETRIES} 次）: {e}")
            if dry_run or attempt == ON_RETRIES:
                break
            try:
                conn.disconnect()
            except Exception:
                pass
            try:
                conn = connect_real(sw)
            except Exception as ce:
                print(f"⚠️ 重新連線也失敗: {ce}")
    print("!" * 60)
    print(f"❌ 嚴重: {sw['name']} 的 port {fmt_ports(ports)} 可能仍在斷電狀態!")
    print(f"請立刻手動登入 {sw['host']}，對這些 port 執行: power inline auto")
    print("!" * 60)
    sys.exit(2)


def run_switch_action(sw, ports, action, dry_run):
    """單一交換器的電源動作:
      restart = 斷電 → 等待 OFF_DELAY → 恢復供電
      off     = 只斷電 (關閉 PoE, 處理完嘢再自己開返)
      on      = 只恢復供電
    """
    conn = DryRunConnection(sw["name"]) if dry_run else connect_real(sw)
    if action == "off":
        print(f"\n🔌 [{sw['name']}] 關閉 PoE port: {fmt_ports(ports)}")
        conn.send_config_set(build_power_cmds(ports, "never"))
        conn.disconnect()
        print(f"✅ [{sw['name']}] 完成，PoE 已關閉 (要用時請再執行「開啟 PoE」)")
        return
    if action == "on":
        print(f"\n🔌 [{sw['name']}] 開啟 PoE port: {fmt_ports(ports)}")
        conn = power_on_with_retry(sw, conn, ports, dry_run)
        conn.disconnect()
        print(f"✅ [{sw['name']}] 完成，PoE 已開啟")
        return
    # restart
    print(f"\n🔌 [{sw['name']}] 斷電 port: {fmt_ports(ports)}")
    conn.send_config_set(build_power_cmds(ports, "never"))
    if dry_run:
        print(f"[DRY-RUN] （這裡會等待 {OFF_DELAY} 秒）")
    else:
        print(f"⏳ 等待 {OFF_DELAY} 秒 ...")
        time.sleep(OFF_DELAY)
    conn = power_on_with_retry(sw, conn, ports, dry_run)
    conn.disconnect()
    print(f"✅ [{sw['name']}] 完成，已恢復供電")


# ============================================================
# 互動流程
# ============================================================
def choose(prompt, valid, default=None):
    """選擇提示; default 非 None 時直接按 Enter = 預設值。"""
    while True:
        ans = input(prompt).strip()
        if ans == "" and default is not None:
            return default
        if ans in valid:
            return ans
        if default is not None:
            print(f"  ❌ 請輸入 {' 或 '.join(sorted(valid))}（直接 Enter = {default}）\n")
        else:
            print(f"  ❌ 請輸入 {' 或 '.join(sorted(valid))}\n")


def ask_ports():
    while True:
        raw = input("輸入要操作的 port（例: 3,5,10-15，範圍 1-{}）: ".format(MAX_PORT))
        try:
            return parse_ports(raw)
        except ValueError as e:
            print(f"  ❌ 格式不對: {e}，請再輸入一次\n")


ACTION_INFO = {
    "1": ("重啟", f"斷電 {OFF_DELAY} 秒後恢復供電"),
    "2": ("關閉 PoE", "只斷電，不恢復（處理完嘢再執行「開啟 PoE」）"),
    "3": ("開啟 PoE", "只恢復供電"),
}
ACTION_MODE = {"1": "restart", "2": "off", "3": "on"}


def main():
    dry_run = "--dry-run" in sys.argv
    print()
    print("=" * 60)
    print("  🔌 Cisco 3560 PoE Port 電源工具" + ("   [DRY-RUN 模擬模式]" if dry_run else ""))
    print("=" * 60)
    if not dry_run:
        ensure_netmiko()
        print()

    # ── 動作 ──
    print("─" * 56)
    print("  動作")
    print("    1. 重啟       斷電 → 等待 {}s → 恢復供電".format(OFF_DELAY))
    print("    2. 關閉 PoE   只斷電（處理完嘢再開返）")
    print("    3. 開啟 PoE   只恢復供電")
    print("─" * 56)
    action = choose("👉 請選擇 [Enter=1]: ", {"1", "2", "3"}, default="1")

    # ── 交換器 ──
    print()
    print("─" * 56)
    print("  交換器")
    for k, s in SWITCHES.items():
        print("    {}. {}   ({})".format(k, s["name"], s["host"]))
    print("    3. 兩台都要")
    print("─" * 56)
    sw_choice = choose("👉 請選擇 [Enter=3]: ", {"1", "2", "3"}, default="3")
    targets = list(SWITCHES.values()) if sw_choice == "3" else [SWITCHES[sw_choice]]

    # ── Port 範圍 ──
    print()
    print("─" * 56)
    print("  Port 範圍")
    print("    1. 全部 ({}-{})".format(CONTROL_MIN, CONTROL_MAX))
    print("    2. 部分（自訂, 例: 3,5,10-15）")
    print("─" * 56)
    scope = choose("👉 請選擇 [Enter=1]: ", {"1", "2"}, default="1")
    ports = (
        set(range(CONTROL_MIN, CONTROL_MAX + 1)) if scope == "1" else ask_ports()
    )

    allowed, skipped = filter_protected(ports)
    if skipped:
        print(f"注意: port {fmt_ports(skipped)} 受保護（電腦/互連/router），已自動跳過")
    if not allowed:
        print("剔除受保護 port 之後沒有剩下任何 port，不執行。")
        sys.exit(0)

    action_name, action_detail = ACTION_INFO[action]
    print()
    print("=" * 56)
    print("  請確認")
    print("─" * 56)
    print("  目標: {}".format(" + ".join(s['name'] + ' (' + s['host'] + ')' for s in targets)))
    print("  動作: {}（{}）".format(action_name, action_detail))
    print("  Port: {}".format(fmt_ports(allowed)))
    print("=" * 56)
    if input("確定執行? 輸入 yes 才會執行: ").strip().lower() != "yes":
        print("已取消，什麼都沒做。")
        sys.exit(0)

    for sw in targets:
        run_switch_action(sw, allowed, ACTION_MODE[action], dry_run)
    print("\n全部完成。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 已中斷")
        sys.exit(130)
    except ConnectionError as e:
        # 🔧 連線失敗不印 traceback, 與主程式風格一致 (❌ + 提示 + 按 Enter 返回)
        print(f"\n❌ {e}")
        input("按 Enter 返回...")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 未預期的錯誤: {e}")
        input("按 Enter 返回...")
        sys.exit(1)
