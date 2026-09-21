# -*- coding: utf-8 -*-
"""AtomicStreamHub / alloc_dma / free_dma 單元測試

目的
  驗證 lib/buffer_hub.py 的核心契約：
    1. alloc_dma / free_dma —— 全專案唯一 heap_caps 入口；CPython 下 fallback
       bytearray（is_dma=False），MicroPython + heap_caps 下配內部 SRAM。
    2. AtomicStreamHub SPSC 狀態機 —— IDLE→READY→READING→IDLE 三態正確流轉，
       寫滿 / 讀空回傳值正確，指標循環推進。
    3. copy 模式（write_from / read_into）與 view 模式（get_write_view/commit、
       get_read_view/release_read）兩條資料路徑一致。
    4. try_dma 參數：不論可用與否都能建構、dirty/get_fill_level 正確、close 安全。
    5. 回歸防線：bounce 相關符號（DmaBounceBuf / try_bounce / bounce_into /
       has_bounce）已被移除，不得再出現。

可攜性
  採專案既有慣例（參考 test/circuit_bus_file_test.py）：IS_MICROPYTHON 旗標分
  流。CPython 下於匯入 buffer_hub 前注入 micropython mock（const / native / viper
  no-op），讓純邏輯可在 PC 跑；heap_caps 不存在故 alloc_dma 走 fallback 分支，
  正好驗證降級路徑。裝置上（MicroPython + heap_caps）則一併測真實 DMA 分配。

判讀
  逐項印 ✅ PASS / ❌ FAIL（附數值），最後總結。任何 FAIL 即代表契約被破壞。

用法
  PC（CPython）：   python test\\test_buffer_hub.py
  裝置（REPL）：    soft reboot 後執行 import test_buffer_hub; test_buffer_hub.run()
"""
import sys

IS_MICROPYTHON = (getattr(sys, "implementation", None)
                  and sys.implementation.name == "micropython")

# --- CPython 下 mock micropython，讓 buffer_hub 得以匯入 -------------------------
# buffer_hub 在匯入時即使用 micropython.const 與 @micropython.native 裝飾器，
# 兩者在 CPython 皆不存在。這層 mock 僅在 PC 跑邏輯測試時生效；裝置上用真的。
if not IS_MICROPYTHON:
    import types
    if "micropython" not in sys.modules:
        _mp = types.ModuleType("micropython")
        _mp.const = lambda x: x

        def _noop_decorator(*args):
            # 支援 @native 與 @native(f0,f1) 兩種呼叫形式
            if len(args) == 1 and callable(args[0]):
                return args[0]
            def wrap(fn):
                return fn
            return wrap
        _mp.native = _noop_decorator
        _mp.viper = _noop_decorator
        sys.modules["micropython"] = _mp

# 把 slave/ 加入路徑，使 from lib.sys.buffer_hub import ... 成立
# 僅 CPython 需要：裝置上 lib/ 已在根目錄，直接可 import；且 MicroPython 無 os.path。
# （lib/ 三級分類重構後：buffer_hub 在 lib/sys/，舊路徑 lib.buffer_hub 已不存在）
if not IS_MICROPYTHON:
    import os
    _HERE = os.path.dirname(os.path.abspath(__file__))
    _ROOT = os.path.dirname(os.path.dirname(_HERE))
    _SLAVE = os.path.join(_ROOT, "slave")
    if os.path.isdir(_SLAVE) and _SLAVE not in sys.path:
        sys.path.insert(0, _SLAVE)

from lib.sys.buffer_hub import AtomicStreamHub, alloc_dma, free_dma  # noqa: E402

# --- 測試公用 -----------------------------------------------------------------
_failed = 0
_passed = 0


def _check(label, cond, extra=""):
    """記錄並印出一項檢查結果。"""
    global _failed, _passed
    mark = "✅ PASS" if cond else "❌ FAIL"
    if not cond:
        _failed += 1
    else:
        _passed += 1
    print("  {} — {}{}".format(mark, label, ("  " + extra) if extra else ""))


# --- 測試 1：alloc_dma / free_dma ---------------------------------------------
def test_alloc_dma():
    print("\n[1] alloc_dma / free_dma")
    buf, is_dma = alloc_dma(1024)
    _check("回傳的 buf 非 None", buf is not None)
    _check("buf 長度 == 1024", len(buf) == 1024, "len={}".format(len(buf)))
    # is_dma 在裝置上應為 True（heap_caps 可用）；PC 上恆為 False（fallback）
    _check("is_dma 型別為 bool", isinstance(is_dma, bool))
    if IS_MICROPYTHON:
        _check("裝置上 is_dma 應為 True（heap_caps 可用）", is_dma is True)
    else:
        _check("PC 上 is_dma 應為 False（fallback bytearray）", is_dma is False,
               "(這是預期，PC 無 heap_caps)")
    # writeable 且內容可讀寫
    buf[0] = 0xAB
    buf[-1] = 0xCD
    _check("buf 可寫入頭尾", buf[0] == 0xAB and buf[-1] == 0xCD)
    # free_dma 對兩種旗標都不應丟例外
    try:
        free_dma(buf, is_dma)
        _check("free_dma(buf, is_dma) 不丟例外", True)
    except Exception as e:
        _check("free_dma(buf, is_dma) 不丟例外", False, str(e))
    # free_dma 對 None / bytearray 應為 no-op
    try:
        free_dma(None, False)
        free_dma(bytearray(8), False)
        _check("free_dma(None/bytearray, False) 為 no-op", True)
    except Exception as e:
        _check("free_dma(None/bytearray, False) 為 no-op", False, str(e))


# --- 測試 2：copy 模式（write_from / read_into）------------------------------
def test_copy_mode():
    print("\n[2] copy 模式 write_from / read_into")
    N, SLOTS = 64, 3
    hub = AtomicStreamHub(N, num_buffers=SLOTS)
    src = bytearray(range(N))            # 0..63
    dst = bytearray(N)

    _check("初始 dirty 為 False", hub.dirty is False)
    _check("初始 fill_level == 0", hub.get_fill_level() == 0)

    ok = hub.write_from(src)
    _check("write_from 首筆成功", ok is True)
    _check("寫後 dirty 為 True", hub.dirty is True)
    _check("寫後 fill_level == 1", hub.get_fill_level() == 1)

    ok = hub.read_into(dst)
    _check("read_into 成功", ok is True)
    _check("資料位元組一致", bytes(dst) == bytes(src))
    _check("讀後 dirty 回 False", hub.dirty is False)
    _check("讀後 fill_level == 0", hub.get_fill_level() == 0)

    hub.close()


# --- 測試 3：view 模式（零拷貝）----------------------------------------------
def test_view_mode():
    print("\n[3] view 模式 get_write_view/commit + get_read_view/release_read")
    N, SLOTS = 32, 3
    hub = AtomicStreamHub(N, num_buffers=SLOTS)

    wv = hub.get_write_view()
    _check("get_write_view 回傳非 None（有空槽）", wv is not None)
    _check("write view 長度 == N", wv is not None and len(wv) == N)
    # 直接寫入 view（模擬 2-byte 長度前綴 + payload 的 hub 寫入路徑）
    wv[0], wv[1] = 0x05, 0x00
    wv[2:7] = b"HELLO"
    hub.commit()
    _check("commit 後 dirty 為 True", hub.dirty is True)

    rv = hub.get_read_view()
    _check("get_read_view 回傳非 None", rv is not None)
    _check("長度前綴 == 5 (LE)", rv is not None and rv[0] == 0x05 and rv[1] == 0x00)
    _check("payload == HELLO", rv is not None and bytes(rv[2:7]) == b"HELLO")
    hub.release_read()
    _check("release_read 後 dirty 回 False", hub.dirty is False)

    hub.close()


# --- 測試 4：寫滿 / 讀空 行為 -------------------------------------------------
def test_overflow_underflow():
    print("\n[4] 寫滿拒絕 / 讀空回 False")
    N, SLOTS = 16, 3
    hub = AtomicStreamHub(N, num_buffers=SLOTS)
    src = bytearray(b"A" * N)

    # 3 個槽全部填滿
    for i in range(SLOTS):
        _check("write_from #{} 成功".format(i + 1), hub.write_from(src) is True)
    _check("fill_level == SLOTS（全滿）", hub.get_fill_level() == SLOTS)
    _check("第 4 筆寫入應被拒（滿）", hub.write_from(src) is False)
    _check("滿時 get_write_view 回 None", hub.get_write_view() is None)

    # 讀光
    dst = bytearray(N)
    for i in range(SLOTS):
        _check("read_into #{} 成功".format(i + 1), hub.read_into(dst) is True)
    _check("讀空後 read_into 回 False", hub.read_into(dst) is False)
    _check("讀空後 get_read_view 回 None", hub.get_read_view() is None)
    _check("讀空後 fill_level == 0", hub.get_fill_level() == 0)

    hub.close()


# --- 測試 5：指標循環推進（多輪後仍正確）-------------------------------------
def test_wraparound():
    print("\n[5] 指標循環推進（連續多輪）")
    N, SLOTS = 8, 2
    hub = AtomicStreamHub(N, num_buffers=SLOTS)
    seen = set()
    ok_all = True
    for k in range(SLOTS * 5):          # 跑 5 圈
        payload = bytes([k & 0xFF]) * N
        # 確保寫得進（讀端同步消耗）
        if not hub.write_from(payload):
            ok_all = False
            break
        dst = bytearray(N)
        if not hub.read_into(dst):
            ok_all = False
            break
        if bytes(dst) != payload:
            ok_all = False
            break
        seen.add(k)
    _check("連續 {} 輪 write/read 無遺失".format(SLOTS * 5), ok_all,
           "seen={} 筆".format(len(seen)))
    _check("迴圈後指標歸位、dirty False", hub.dirty is False)
    hub.close()


# --- 測試 6：try_dma 參數（不論可用與否都能用）--------------------------------
def test_try_dma():
    print("\n[6] try_dma 參數")
    hub = AtomicStreamHub(128, num_buffers=3, try_dma=True)
    _check("try_dma=True 建構成功", True)
    # dma_count 反映實際配到的 DMA 槽數：裝置上 >0，PC 上 == 0（fallback）
    if IS_MICROPYTHON:
        _check("裝置上 dma_count > 0", hub.dma_count > 0,
               "dma_count={}".format(hub.dma_count))
    else:
        _check("PC 上 dma_count == 0（無 heap_caps）", hub.dma_count == 0,
               "dma_count={}".format(hub.dma_count))
    # 不論 DMA 與否，資料路徑都要能用
    ok = hub.write_from(bytearray(b"Z" * 128))
    _check("try_dma hub 的 write_from 可用", ok is True)
    dst = bytearray(128)
    ok = hub.read_into(dst)
    _check("try_dma hub 的 read_into 可用", ok is True)
    _check("try_dma hub 資料一致", bytes(dst) == bytes(b"Z" * 128))
    # close 不丟例外（含 DMA 釋放）
    try:
        hub.close()
        _check("try_dma hub close() 安全", True)
    except Exception as e:
        _check("try_dma hub close() 安全", False, str(e))


# --- 測試 7：flush 歸零 -------------------------------------------------------
def test_flush():
    print("\n[7] flush")
    N, SLOTS = 32, 3
    hub = AtomicStreamHub(N, num_buffers=SLOTS)
    hub.write_from(bytearray(N))
    hub.write_from(bytearray(N))
    _check("flush 前 fill_level == 2", hub.get_fill_level() == 2)
    hub.flush()
    _check("flush 後 fill_level == 0", hub.get_fill_level() == 0)
    _check("flush 後 dirty == False", hub.dirty is False)
    _check("flush 後 get_read_view 回 None", hub.get_read_view() is None)
    _check("flush 後可重新寫入 slot 0", hub.write_from(bytearray(N)) is True)
    hub.close()


# --- 測試 8：回歸防線 —— bounce 相關符號必須已移除 ----------------------------
def test_bounce_removed():
    """整合時已刪除 DmaBounceBuf / try_bounce / bounce_into / has_bounce。
    這些符號若重新出現，代表有人違反 buffer-conventions 技能，測試要抓出來。"""
    print("\n[8] 回歸：bounce 符號已移除")
    import lib.sys.buffer_hub as bh

    _check("buffer_hub 無 DmaBounceBuf", not hasattr(bh, "DmaBounceBuf"))
    _check("AtomicStreamHub 無 bounce_into 方法",
           not hasattr(AtomicStreamHub, "bounce_into"))
    _check("AtomicStreamHub 無 has_bounce 屬性",
           not hasattr(AtomicStreamHub, "has_bounce"))

    # 檢查 __init__ 不含 try_bounce 參數。CPython 用 inspect；MicroPython 無 inspect，
    # 改偵測建構時傳 try_bounce= 是否被接受（應為 TypeError：unexpected keyword）。
    if not IS_MICROPYTHON:
        import inspect
        sig = str(inspect.signature(AtomicStreamHub.__init__))
        _check("AtomicStreamHub.__init__ 無 try_bounce 參數", "try_bounce" not in sig,
               "sig={}".format(sig))
    else:
        rejected = False
        try:
            AtomicStreamHub(8, try_bounce=True)
        except TypeError:
            rejected = True
        except Exception:
            pass
        _check("AtomicStreamHub.__init__ 無 try_bounce 參數", rejected,
               "(以 try_bounce=True 應被拒)")


def run():
    print("=" * 60)
    print("AtomicStreamHub / alloc_dma 測試")
    print("環境：{}".format("MicroPython (裝置)" if IS_MICROPYTHON else "CPython (PC)"))
    print("=" * 60)
    test_alloc_dma()
    test_copy_mode()
    test_view_mode()
    test_overflow_underflow()
    test_wraparound()
    test_try_dma()
    test_flush()
    test_bounce_removed()
    print("\n" + "=" * 60)
    print("結果：✅ {} 通過 / ❌ {} 失敗".format(_passed, _failed))
    print("=" * 60)
    return _failed == 0


if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)
