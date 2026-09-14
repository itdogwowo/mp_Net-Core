# test_poe_restart.py — assert-based, no third-party deps. Run: python3 test_poe_restart.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from poe_restart import (
    parse_ports,
    filter_protected,
    compress_ranges,
    fmt_ports,
    PROTECTED_PORTS,
    build_iface_entries,
    build_power_cmds,
)


def test_parse_single_and_list():
    assert parse_ports("5") == {5}
    assert parse_ports("3,5,7") == {3, 5, 7}


def test_parse_range_and_mixed():
    assert parse_ports("10-13") == {10, 11, 12, 13}
    assert parse_ports("3, 5, 10-12") == {3, 5, 10, 11, 12}


def test_parse_rejects_bad_input():
    for bad in ("", "abc", "0", "49", "15-10", "1-100", "5,,x"):
        try:
            parse_ports(bad)
        except ValueError:
            continue
        raise AssertionError(f"parse_ports({bad!r}) should raise ValueError")


def test_protected_ports_are_46_47_48():
    assert PROTECTED_PORTS == {46, 47, 48}


def test_filter_protected():
    allowed, skipped = filter_protected({45, 46, 47, 48, 1})
    assert allowed == [1, 45]
    assert skipped == [46, 47, 48]


def test_filter_protected_none_skipped():
    allowed, skipped = filter_protected({1, 2, 3})
    assert allowed == [1, 2, 3]
    assert skipped == []


def test_compress_ranges():
    assert compress_ranges([1, 2, 3, 7, 10, 11]) == [(1, 3), (7, 7), (10, 11)]
    assert compress_ranges([5]) == [(5, 5)]


def test_fmt_ports():
    assert fmt_ports([1, 2, 3, 7]) == "1-3, 7"
    assert fmt_ports([4]) == "4"


def test_build_iface_entries():
    assert build_iface_entries([10, 11, 12, 15]) == [
        "GigabitEthernet0/10 - 12",
        "GigabitEthernet0/15",
    ]


def test_build_power_cmds_single_group():
    assert build_power_cmds([1, 2, 3], "never") == [
        "interface range GigabitEthernet0/1 - 3",
        "power inline never",
    ]


def test_build_power_cmds_groups_of_five():
    # 6 個不連續 port → 6 段 → 拆成 5 段 + 1 段兩條 interface range
    cmds = build_power_cmds([1, 3, 5, 7, 9, 11], "auto")
    assert cmds == [
        "interface range GigabitEthernet0/1 , GigabitEthernet0/3 , "
        "GigabitEthernet0/5 , GigabitEthernet0/7 , GigabitEthernet0/9",
        "power inline auto",
        "interface range GigabitEthernet0/11",
        "power inline auto",
    ]


def test_dry_run_end_to_end():
    """互動流程整條跑一次（dry-run，零網路）。輸入: 重啟 / SW-02 / 部分 / 10-15,47 / yes"""
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    proc = subprocess.run(
        [sys.executable, os.path.join(here, "poe_restart.py"), "--dry-run"],
        input="1\n2\n2\n10-15,47\nyes\n",
        capture_output=True,
        text=True,
        timeout=30,
    )
    out = proc.stdout
    assert proc.returncode == 0, f"exit={proc.returncode}\n{out}\n{proc.stderr}"
    assert "interface range GigabitEthernet0/10 - 15" in out
    assert "power inline never" in out
    assert "power inline auto" in out
    assert "47" in out and "跳過" in out      # protected port warning shown
    assert "GigabitEthernet0/47" not in out   # protected port never in commands


def test_auto_args_parse():
    from poe_restart import parse_auto_args
    assert parse_auto_args(["--dry-run"]) is None            # 只有 --dry-run → 互動模式
    assert parse_auto_args([]) is None
    o = parse_auto_args(["--switches", "both", "--action", "restart",
                         "--ports", "all", "--yes"])
    assert o == {"dry_run": False, "switches": "both", "action": "restart",
                 "ports": "all", "yes": True}


def test_auto_args_rejects_bad_input():
    from poe_restart import parse_auto_args
    for bad in (["--switches"], ["--bogus"], ["--action"]):
        try:
            parse_auto_args(bad)
        except SystemExit:
            continue
        raise AssertionError(f"parse_auto_args({bad!r}) should raise SystemExit")


def test_auto_mode_dry_run_end_to_end():
    """非互動模式（網頁一鍵呼叫的路徑）: 零 stdin、零網路、兩台全 port。"""
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    proc = subprocess.run(
        [sys.executable, os.path.join(here, "poe_restart.py"),
         "--dry-run", "--switches", "both", "--action", "restart",
         "--ports", "all", "--yes"],
        input="",
        capture_output=True,
        text=True,
        timeout=30,
    )
    out = proc.stdout
    assert proc.returncode == 0, f"exit={proc.returncode}\n{out}\n{proc.stderr}"
    assert "非互動模式" in out
    assert "Light-SW-01" in out and "Light-SW-02" in out
    assert "interface range GigabitEthernet0/1 - 45" in out
    assert "GigabitEthernet0/46" not in out and "GigabitEthernet0/48" not in out
    assert "EOF" not in out and "EOF" not in proc.stderr


def test_auto_mode_bad_action_and_ports():
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    for args in (["--action", "nope"], ["--ports", "99"], ["--switches", "9"]):
        proc = subprocess.run(
            [sys.executable, os.path.join(here, "poe_restart.py"), "--dry-run"] + args,
            input="", capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode != 0, f"{args} should fail\n{proc.stdout}"
        assert "EOF" not in proc.stderr


def run():
    fails = 0
    names = sorted(n for n in globals() if n.startswith("test_"))
    for name in names:
        try:
            globals()[name]()
            print(f"PASS  {name}")
        except AssertionError as e:
            fails += 1
            print(f"FAIL  {name}: {e}")
    print(f"\n{len(names) - fails}/{len(names)} passed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    run()
