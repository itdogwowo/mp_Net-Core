"""WebMaster PoE 電源控制 — 重用 tools/PC/poe_restart.py 嘅純邏輯。

唔重抄邏輯:透過 importlib 動態載入 PC 端 poe_restart.py(佢 top-level 只定義
函式/常數,main 有 __main__ 守衛唔會跑),再呼叫佢嘅 run_switch_action。
輸出用 redirect_stdout 捉返嚟,經 /api/poe 俾 UI 顯示。
"""
import io
import os
import importlib.util
from contextlib import redirect_stdout


def _load_mod():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.abspath(os.path.join(here, "..", "PC", "poe_restart.py"))
    spec = importlib.util.spec_from_file_location("poe_restart_tool", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_poe(action, switch_names, ports_text, dry_run):
    """執行 PoE 電源動作。回傳 (output_text, ok)。action: restart/off/on。"""
    mod = _load_mod()
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            if action not in ("restart", "off", "on"):
                return "❌ 未知動作: {}".format(action), False
            # 未指定 port = 全部可控制 port
            if ports_text and ports_text.strip():
                ports = mod.parse_ports(ports_text)
            else:
                ports = set(range(mod.CONTROL_MIN, mod.CONTROL_MAX + 1))
            allowed, skipped = mod.filter_protected(ports)
            if skipped:
                print("注意: port {} 受保護（電腦/互連/router），已自動跳過".format(mod.fmt_ports(skipped)))
            if not allowed:
                print("剔除受保護 port 之後冇任何 port，不執行。")
                return buf.getvalue(), True
            # 未揀交換器 = 兩台都要
            if not switch_names:
                switch_names = [s["name"] for s in mod.SWITCHES.values()]
            by_name = {s["name"]: s for s in mod.SWITCHES.values()}
            targets = [by_name[n] for n in switch_names if n in by_name]
            if not targets:
                print("❌ 未揀到有效交換器")
                return buf.getvalue(), False
            if not dry_run:
                try:
                    import netmiko  # noqa: F401
                except ImportError:
                    return "❌ 真正執行需要 netmiko（pip install netmiko）；想預覽請用 Dry-run。", False
            print("🏭 目標: {}".format(" + ".join(t['name'] + ' (' + t['host'] + ')' for t in targets)))
            print("🎯 動作: {} | Port: {}".format(action, mod.fmt_ports(allowed)))
            if dry_run:
                print("[DRY-RUN] 以下只係預覽，唔會真係斷電/供電。\n")
            for sw in targets:
                mod.run_switch_action(sw, allowed, action, dry_run)
            print("\n全部完成。")
            return buf.getvalue(), True
        except Exception as e:
            # redirect_stdout 已捉咗前面輸出, 呢度加錯誤進去
            print("❌ 未預期的錯誤: {}".format(e))
            return buf.getvalue(), False
