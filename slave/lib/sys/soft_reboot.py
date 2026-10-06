# lib/sys/soft_reboot.py
# Soft reboot 之後的資源回收 —— 開機最前面跑一次
#
# ══════════════════════════════════════════════════════════════════════════
#  為什麼需要
# ══════════════════════════════════════════════════════════════════════════
#  軟重開機（friendly REPL 的 Ctrl-D／`mpremote` 預設收尾）只重來 MicroPython
#  自己：`gc_init + mp_init + machine_pins_init`。C 層的靜態資料、以及其他
#  allocator 手上的記憶體**都還在**，但 Python 端指向它們的物件全沒了
#  → 變成**孤兒**。
#
#  這不是某個子系統的問題，是**每個「配置在 GC heap 之外」的東西**共通的：
#
#    lib/sys/buffer_hub.alloc_dma()
#        → heap_caps.malloc(size, CAP_DMA)   ← 內部 SRAM，soft reboot 不釋放
#    （Skills/buffer-conventions 規則一：DMA 緩衝的唯一入口）
#
#  `mp_heap_caps` 就是為此設計的：它用一個 BSS 靜態陣列追蹤配置過的指標
#  （`HEAP_CAPS_MAX_TRACKED` = 128 筆，跨 soft reboot 存活），
#  `heap_caps.reset()` = `untrack_all()` 把它們全部 free 掉。
#
#  ⚠️ 但本專案**從來沒有呼叫過 `heap_caps.reset()`**（2026-10 才發現）。
#     也就是說：任何走 `alloc_dma()` 的緩衝，只要在 soft reboot 時還沒被
#     正常 free，那塊內部 SRAM 就再也回不來了。
#
# ══════════════════════════════════════════════════════════════════════════
#  真機實測（11401，2026-10）
# ══════════════════════════════════════════════════════════════════════════
#  刻意造出孤兒（配置 4 x 16KB CAP_DMA 後不 free）再軟重開機，重複兩輪：
#
#                                 internal_free
#    A 基準                          118,023      ← 第 1 輪
#    B 配置後(未 free)                68,859   −49,164
#    C 軟重開機後                    106,359      ← 回來了（只少 ~11.7KB）
#    D heap_caps.reset()             106,359      ← 回收 0（C 已經沒東西可收）
#
#    第 2 輪同形狀：−65,552 → 軟重開機後只剩 −10,464 → reset 回收 0
#
#  ⇒ **在這個 build 上，孤兒在 soft reboot 時就自己回來了** —— 推測與
#    `mp_init` → `gc_init` 的 heap 重置有關（GC heap 會用到內部 SRAM，
#    重來時把那塊一起還給系統）。所以 `heap_caps.reset()` 目前是 no-op。
#
#  ⚠️ 但**不能因此說它沒用**，三件事要分開講清楚：
#    1. 那 ~10KB 的殘差**每輪都在累積**（118,023 → 106,359 → 95,895）——
#       確實有東西跨 soft reboot 沒還，但**不是 heap_caps 記帳的那些**。
#       來源未定（可能是中斷打斷 main.py 造成的驅動重複配置），沒有結論。
#    2. `heap_caps.reset()` 在「同一輪裡還有追蹤項」時是**真的會 free**
#       （實測 8 x 16KB 追蹤項全部回收）。放在開機最前面是確保
#       「本輪剛配好的不會被誤殺」。
#    3. `mp_heap_caps` 提供這個 API 就是為了這個用途；現行 config 剛好沒踩到
#       （沒有任何地方傳 `try_dma=True`，SD 也沒起來）。
#
#  一句話：**這是保險，不是本輪問題的解**。真正的難題（LVGL 的死指標）
#  不在這裡，見 ui/lvgl/lvgl_init.py 的 `_soft_reboot_recover()`。
#
#  ⚠️ 注意：只追蹤 128 筆。超過的配置不受 `reset()` 保護。
#     那是 mp_heap_caps 的限制，不是這裡能解的。
#
# ══════════════════════════════════════════════════════════════════════════
#  呼叫時機
# ══════════════════════════════════════════════════════════════════════════
#  **boot.py 的第一件事**，在任何 driver / 子系統配置之前。
#  這個順序是必要的：`heap_caps.reset()` 會 free「所有還被追蹤的配置」，
#  如果先讓本輪的 hub 配好了再呼叫，就會把**還在用**的緩衝一起 free 掉。
#
#  用 `reclaim_all()` 而不是直接叫 `heap_caps.reset()`，是為了留一個
#  「其他子系統也能掛自己的回收步驟」的位置 —— 但**現在不預先抽象**：
#  等第二個真的出現再加，介面就是這個形狀。

_ran = False


def _reclaim_heap_caps(trace=False):
    """回收被 soft reboot 遺棄的 heap_caps 緩衝。回傳 True 代表真的有做。"""
    try:
        import heap_caps
    except ImportError:
        return False
    if not hasattr(heap_caps, "reset"):
        return False
    try:
        heap_caps.reset()
        return True
    except Exception as e:
        if trace:
            print("[soft_reboot] heap_caps.reset() 失敗:", e)
        return False


def reclaim_all(trace=False):
    """開機最前面呼叫一次：把上一個 session 遺棄的資源收回來。

    只在**第一次**呼叫時動作（同一輪開機重複呼叫是 no-op）。
    """
    global _ran
    if _ran:
        return
    _ran = True

    if trace:
        try:
            import heap_caps
            print("[soft_reboot] internal free =",
                  heap_caps.get_free_size(heap_caps.CAP_INTERNAL))
        except Exception:
            pass

    if _reclaim_heap_caps(trace):
        if trace:
            try:
                import heap_caps
                print("[soft_reboot] after reset  =",
                      heap_caps.get_free_size(heap_caps.CAP_INTERNAL))
            except Exception:
                pass
