# thread_lcd_rule.py — 驗證「LCD/SPI(lcd_bus) 只能單核碰」規則的必要性
#
# ⚠️⚠️ 破壞性測試 ⚠️⚠️
#   預期結果是「畫面壞掉」甚至「整板崩潰重啟」。只有在你想要確認
#   目前使用的 lcd_bus 版本有沒有內建鎖（有的版本加了）才跑。
#
# 目的：
#   證明 TaskManager 的 hw=("lcd",) 防呆（LCD task 禁止排 core1）
#   是有根據的 — 兩顆核同時碰同一個 lcd_bus SPIBus（DMA queue 非
#   thread-safe）會炸。
#
# 方法：
#   core1(_thread) 對 lcd adapter 狂寫整幀黑畫面（模擬 jpeg_player
#   誤放 core1 的行為），core0 同時也對同一 adapter 寫（模擬 LVGL
#   渲染）。數到每核 N 次或崩潰為止。
#
# 判讀：
#   崩潰/畫面壞 → 規則必要，維持 hw=("lcd",) 防呆，jpeg_player 只能
#                 與 lvgl 互斥二選一（都在 core0）。
#   居然沒崩     → 你的 lcd_bus 版本有內建鎖，可以考慮放寬
#                 （但要再驗證 DMA 排程與畫質，別急著放寬）。
#
# 用法（soft reboot 後，boot.py 已完成 LCD 初始化）：
#   import thread_lcd_rule
#   thread_lcd_rule.run(confirm=True, n=2000)   # confirm 必填，防手滑

import time, _thread
from lib.sys.sys_bus import bus

_CHUNK = 32768


def _get_adapter():
    lcd = bus.get_service("lcd")
    if lcd is None:
        print("❌ lcd not on bus — run boot.py first")
        return None
    adapter = getattr(lcd, "_bus", None)
    if adapter is None:
        print("❌ lcd service missing _bus (adapter)")
        return None
    return adapter


def _write_loop(adapter, buf, n, counter):
    """對 adapter 狂寫（set_window + async DMA + flush）"""
    w = int(bus.shared.get("tft_width", 240))
    h = int(bus.shared.get("tft_height", 320))
    for _ in range(n):
        adapter.set_window(0, 0, w - 1, h - 1)
        try:
            adapter.write_data_async(buf)
            adapter.flush()
        except Exception:
            pass
        counter[0] += 1


def run(confirm=False, n=2000):
    if not confirm:
        print("⚠️ 這是破壞性測試（預期崩潰/畫面壞）。要跑請: run(confirm=True)")
        return

    adapter = _get_adapter()
    if adapter is None:
        return

    # 每個 core 各準備一塊 frame buffer（避免共用同一塊被 DMA 讀寫撕裂）
    import heap_caps
    bufs = []
    for _ in range(2):
        try:
            b = heap_caps.malloc(_CHUNK, heap_caps.CAP_DMA)
            bufs.append(b if b is not None else bytearray(_CHUNK))
        except Exception:
            bufs.append(bytearray(_CHUNK))

    c1 = [0]        # core1 寫入次數
    c0 = [0]        # core0 寫入次數

    print("=" * 62)
    print("⚠️  LCD 跨核規則驗證（破壞性）— 兩核同時寫 lcd_bus")
    print("    預期：崩潰 / 畫面壞。跑了才知道你的 lcd_bus 有沒有內建鎖")
    print("=" * 62)

    _thread.stack_size(16 * 1024)
    th = _thread.start_new_thread(_write_loop, (adapter, bufs[0], n, c1))

    t0 = time.ticks_ms()
    while c1[0] < n and time.ticks_diff(time.ticks_ms(), t0) < 60000:
        _write_loop(adapter, bufs[1], 1, c0)
        time.sleep_ms(1)

    print("core0 wrote {}x, core1 wrote {}x".format(c0[0], c1[0]))
    if c1[0] >= n:
        print("=" * 62)
        print("⚠️  竟然沒崩 — 你的 lcd_bus 版本可能內建鎖")
        print("    但請檢查畫面是否正常；維持 hw=(\"lcd\",) 防呆仍是安全做法")
    else:
        print("=" * 62)
        print("✅ 崩潰/卡死發生在 core1 寫入 {} 次 — 規則必要".format(c1[0]))
        print("   維持：LCD task 只能 core0；jpeg_player 與 lvgl 互斥二選一")
    print("done.")
