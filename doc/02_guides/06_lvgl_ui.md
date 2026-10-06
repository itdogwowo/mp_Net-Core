# LVGL UI 最新使用指南（2026-08 驗證版）

> **用途**：`slave new/` 的 LVGL 本地 UI 層（`ui/lvgl/` + `ui_test_tool.py`）使用指南——架構、啟動方式、螢幕方向、config 設定、字型生成、導覽框架、踩坑記錄。
> **分類**：使用教學（02_guides）
> **最後更新**：2026-08-18
> **舊參考**：`mp_LVGL/ui/`（設計稿來源，只參考 UI 層面，內部數據機制不照抄）

---

## 1. 檔案對應

```
slave new/
├── ui/lvgl/
│   ├── lvgl_init.py     # LVGL display 一次初始化 + bus reuse（對齊 i80_drv/tft_drv）
│   ├── ui_common.py     # palette / 字型 / widget builder + mk_list/mk_led/mk_arc 等 helper
│   ├── registry.py      # @register 動態註冊表
│   ├── app.py           # 平台解耦路由器（build_all 預建 screen + go 沿用）
│   ├── launcher.py      # 動態首頁（讀 registry 產生卡片）
│   ├── nav.py           # ★共用三層導覽狀態機（class Nav）
│   ├── board.py         # 板上對接層（has_lcd 閘門 + 輸入 + 主迴圈）
│   └── page/
│       ├── __init__.py  # 集中 import（容錯：單頁失敗不拖垮其他）
│       ├── control_panel.py  # 控制面板（模式/亮度/倒數/拍攝·可動旗標）
│       ├── pca9685.py        # PCA9685 I2C 檢查器
│       └── settings.py       # 系統設定
├── driver/enc_drv.py    # ★硬體編碼器 driver（config ENC 區塊）
├── ui_test_tool.py      # ★獨立測試入口（import 即用，旋鈕+按鈕操作）
└── config.json          # TFT / ENC / PIN(encC,btn)
```

---

## 2. 啟動方式

### 正式啟動（board.run）
- 唯一前置條件：`bus.has_lcd()`（boot.py 的 `init_tft` 成功）。
- LVGL 與 JPEG player 共用同一塊 LCD、互斥，由你手動決定跑哪個（不再由 config 自動切換）。
- 用法：
  ```python
  import ui.lvgl.board
  ui.lvgl.board.run()
  ```

### 獨立測試入口（ui_test_tool）
- 強制起 UI（測試用，仍需 LCD 存在），import 即用（對齊 tft_test_tool 慣例）：
  ```python
  import ui_test_tool
  # 直接進主迴圈，實體旋鈕 + encC(確認) + btn(離開) 操作
  # Ctrl-C 回 REPL，LVGL 留 bus reuse
  ```
- REPL 除錯 API：`pages()`、`goto("settings")`、`cur()`、`peek("control_panel")`、`set("control_panel", {...})`、`frame(n)`、`run()`。

---

## 3. 架構原則

### 分層（對齊 slave new driver → bus → 應用）
- **硬體一律由 driver 初始化**，UI 只從 bus 取用，不自己 `machine.Encoder()`/`Pin()`。
  - encoder → `driver/enc_drv.py`（`bus.get_service("enc_list")`）
  - 確認鍵/離開鍵 → `driver/pin_drv.py`（`bus.get_service("pin_by_label")["encC"/"btn"]`）
  - LCD → `driver/tft_drv.py`（`bus.get_service("lcd")`）
- **LVGL display 一次初始化 + bus reuse**：`lvgl_init.get_platform()` 先查 `bus.service("lvgl_disp")`，已存在就重用。soft-reboot 後 LVGL C 層狀態殘留，重複 `lv.init()`/`display_create()` 會要配置數百 MB garbage（見踩坑 §8.1）。
- **頁面數據從 bus 讀**：`update()` 自己 `bus.shared.get(...)`（對齊 `jpeg_player` 慣例）。

### 導覽框架（nav.py）
三層狀態機 class，頁面只宣告「項清單 + 每項 kind + 回呼」，框架自動處理 enc/confirm/exit：

```python
from ui.lvgl.nav import Nav, ITEM_LIST, ITEM_SLIDER, ITEM_BUTTON
nav = Nav()
def build():
    nav.reset()
    nav.add(_mode_list, ITEM_LIST, on_change=_sel_mode_delta)   # confirm 進編輯，enc 上下選
    nav.add(_bright_sl, ITEM_SLIDER, on_change=_adj_bright)     # confirm 進編輯，enc 調值
    nav.add(btn, ITEM_BUTTON, on_change=_toggle)                # confirm 觸發
def on_enc(d):    nav.enc(d)
def on_confirm(): nav.confirm()
def on_exit():    return nav.exit()   # True=消耗(編輯中先退編輯)；False=回 launcher
```

項型別：`ITEM_INFO`（唯讀聚焦）/ `ITEM_SWITCH` / `ITEM_ENUM` / `ITEM_SLIDER` / `ITEM_BUTTON` / `ITEM_LIST`（新增，可編輯態）。

---

## 4. 螢幕方向（重要）

- **LCD 橫屏 320×240，但 config 是直向 240×320**。
- 關鍵：LVGL 自己送 MADCTL 0x60（`lvgl_init._MADCTL`），讓 ST7789 framebuffer 旋轉；`show()` 用 **bus adapter 的 `set_window`**（繞過 `ST7789.set_window` 在 rotation 90/270 的 x/y swap）。
- `config.json` 的 `TFT.rotation` 必須維持 `0`（driver 不送 MADCTL、不 swap），否則跟 LVGL 送的 MADCTL **double-rotate**。
- 要改直屏：`lvgl_init._MADCTL` 改 `0x00`，但頁面佈局也要改回直向。

---

## 5. config 設定

```json
"ENC": {
    "enable": 1,
    "list": [ { "id": 0, "GPIO": { "a": 18, "b": 8 } } ]   // 硬體編碼器 A/B
},
"PIN": {
    "list": [
        { "GPIO": 17, "label": "encC", "mode": "IN", "pull": "UP" },  // 確認鍵
        { "GPIO": 42, "label": "btn",  "mode": "IN", "pull": "UP" }   // 離開鍵
    ]
}
```

- **encoder A/B 不能放 PIN 段**：`machine.Encoder` 是硬體周邊，PIN 段會把它建成普通 GPIO Pin 衝突。放獨立 `ENC` 區塊（enc_drv 處理）。
- encoder 用 GPIO 18/8 需 `UART.enable = 0`（UART1 原本佔用 18/8）。

---

## 6. 字型生成（補缺字）

中文字體 `slave/ui/lvgl/src/zh_hant_16.bin` 是 `lv_font_conv` 產生的 binfont（`--no-compress`）。
它是**子集**字型，而且這顆的 `fallback = None`（實測）—— **不在子集裡的字不會退到別的字型，
就是畫不出來**（空白／方塊）。UI 新增中文卻忘了重跑生成 = 靜默的視覺缺陷。

### 6.1 一行做完

```bash
python -B temp/gen_font.py            # 只算，不寫檔（先看數字）
python -B temp/gen_font.py --write    # 真的產生
python -B temp/font_audit.py          # 上板驗證：還有沒有缺字
```

`gen_font.py` 會：掃 `slave/**/*.py` 的**字串常量** → 過濾出 TTF 真的有的碼點 →
呼叫 `lv_font_conv`（走 npx 快取，離線可跑）→ 跟舊檔比對 `glyf` 前段確認來源 TTF 一致。

### 6.2 手動版（參數要對）

```bash
LVFC=~/.npm/_npx/*/node_modules/.bin/lv_font_conv
"$LVFC" --font "/Library/Fonts/Arial Unicode.ttf" --size 16 \
  --format bin --bpp 4 --no-compress \
  -r "0x20-0x7E,<所有碼點>" \
  -o slave/ui/lvgl/src/zh_hant_16.bin        # ★ 路徑含 slave/，舊版文件漏了
```

- **掃整個 `slave/`**（不手列檔案），否則漏檔 → 缺字。多收無害，漏收才變方塊。
- 符號要補：`▲▼◀▶▽△↕↑↓→←°℃·±×÷—…（）`。

### 6.3 ⚠️ 四個會讓人做出錯誤結論的坑（都踩過）

1. **`lv_font_conv` 遇到一個字型沒有的碼點就整個中止**，不會跳過：
   ```
   Font "..." doesn't have any characters included in range 0x2139-0x2139
   ```
   → 必須先讀來源 TTF 的 cmap，只把**它真的有的**碼點餵進去。

2. **`ast.Constant` 連 docstring 一起抓** —— 而 docstring 不會被顯示。
   把註解裡的 `⚠️` 當成缺字去補，字型會白胖一圈。
   只算「真的會被畫出來的字串」（`font_audit.py` 的 `literals_only()`）。

3. **私用區 `0xE000–0xF8FF` 不是中文字型的事** —— 那是 `icons_16.bin`（icon 字型）的
   地盤，`mk_icon()` 會明確 `set_style_text_font(icon_font)`。
   把它們算成缺字永遠補不完。

4. **驗證的 oracle 一定要先拿對照組驗過**。`font.get_glyph_width(ch)` 在這塊固件上
   **對每個字都丟 `TypeError`**（它是 unbound 風格，簽名不同），而我的結果解析器
   又只認 `MISSING` 不看 `ERRORS` → 印出「640 個字全部都有」。
   **實際上 203 個沒有。** 正確形式是：
   ```python
   d = lv.font_glyph_dsc_t()
   ok = f.get_glyph_dsc(f, d, codepoint, 0)   # 要自己把 font 傳進去
   ```
   驗 oracle 用的對照組：陽性 `A`／`1`／空白，陰性 U+E000／U+10FFFD／emoji。
   陰性若回 True，這個 oracle 就不能用。

### 6.4 為什麼不能「反正掃註解也收」

會爆。2026-10 實測：`slave/` 全部 `.py` 的**字串常量**就有 1166 個非 ASCII 碼點，
字型從 73716 B 長到 136616 B（+85%）。再收註解只會更大，而畫面上一個字都不會多。

### 6.5 動態文字（掃不到的那種）

`@mode.*` 的模式名稱、節點名稱這類**執行期才从 JSON 進來**的字，靜態掃描看不到。
真的要支援就必須：
- 收一整段常用字（Big5 一級字 5401 字 ≈ 690 KB），或
- 改文案讓它只用 ASCII，或
- 換一顆有 `fallback` 的字型（LVGL 支援 `--lv-fallback`）。

目前**沒有做**，所以模式名稱請用英文/數字。

---

## 7. 頁面數據對接（control_panel 與協議）

control_panel 頁與 `tasks/action_task_1.py` 共享 mode byte：
- `bus.shared["_display_mode"]` = mode byte：Bit7=拍攝模式(0x80)、Bit6=可動模式(0x40)、Bit5-0=模式值
- `bus.shared["_display_brightness"]`、`bus.shared["_display_time"]`（0-255）
- 切換旗標用 XOR（`_display_mode ^ 0x80`），只改旗標不動 mode 低 6 bit。

---

## 8. 踩坑記錄（開發技巧）

1. **LVGL 在軟重開機後一定不能沿用 —— 交給 `ui/lvgl/soft_reboot_guard.py`**。
   soft-reboot 後 C 層殘留，`lv.init()` + `lv.display_create()` 會
   `MemoryError` 要求數百 MB（**那個數字是指標，不是尺寸**）。

   **真根因（2026-10 查到底，C 源碼 + ELF 確認）**：
   `lv_malloc_core()` → `gc_alloc()`（`ext_mod/lvgl/mem_core.c`），
   所以 **LVGL 的每一筆配置都在 MicroPython 的 GC heap 上**；
   而 binding 的 root pointer `mp_lv_roots` 是**普通 C 全域**（`.bss`，
   軟重開機不會清），指到的 `lv_global_t` 卻在舊 heap 上
   → 整棵樹變死指標。且 `static bool mp_lv_roots_initialized` 也是常駐，
   所以 `mp_lv_init_gc()` 再也不會重建 `lv_global`。

   ⚠️ **歷史教訓（本檔與 `todo/08` 前兩版都寫錯過）**：
   - ❌「`deinit()` 是元兇，改成沿用既有 display」→ 能沿用的不是堪用的
     display，是**一整棵死指標樹**；實測 `lv.obj()` / `lv.screen_load()` /
     `lv.deinit()` 全部**直接打死板子**。
   - ❌「用 `lv.is_initialized()` 判斷有沒有殘留」→ 這個 binding 在
     **import `lvgl` 時就會做掉 C 層初始化**，乾淨開機時它也可能是 `True`；
     拿它判斷會誤判、把自己的 UI 擋掉。**要看 `display_get_default()`。**

   ✅ **正確做法**：`ui/lvgl/soft_reboot_guard.py` —— 掛在
   `lvgl_init.get_platform()`（LVGL 唯一入口），進來先探測
   `lvgl.display_get_default() is not None`，是殘留就 `machine.reset()`
   （硬重置會把 C 層與 heap 一起歸零），用 `/lvgl_state` 標記檔防無窮重置。
   實測硬重置後 `display_create()` 成功、UI 正常起來。

   ⚠️ **不要把它塞進 `boot.py`** —— 那是硬體初始化，子系統的 soft-reboot
   復原該掛在自己的入口（這也對齊 I80 計畫書的 M2：
   「`make_new` 偵測殘留 → `esp_restart()`」）。

   詳見 `todo/08_lvgl_reinit.md` 與 changelog §38（§37 是已被證偽的版本）。
2. **MADCTL 只能一邊送**：driver rotation 與 LVGL 自送 MADCTL 只能擇一，否則 double-rotate 花屏。
3. **declare/build 時機**：頁面 `@register` 在 import 時跑、`build()` 在 `build_all()` 跑；依賴「build 後才有的 widget 資料」的邏輯要放對時機。
4. **switch binding API 差異**：`add_state`/`clear_state`/`has_state` 各 binding 名稱不一，用 `ui_common.sw_set/sw_get` wrapper 防護。
5. **MicroPython `json.dumps` 不吃 kwargs**：`ensure_ascii`/`indent` 在板上會 `TypeError`，用無 kwargs 版本 + 自製縮排。
6. **page import 容錯**：`page/__init__.py` 每個 import 包 try/except + `if pid in PAGES` 守護，單頁刪除/壞檔不拖垮其他頁。
7. **字型缺字**：新中文字 → 重跑字型生成（§6），方塊字消失。
   2026-10 實測：UI 用到的 640 個非 ASCII 字裡 **203 個沒有 glyph**
   （連「遙」都沒有 —— 遙控器一直顯示成「⬜控器」）。
   修完之後**要跑 `temp/font_audit.py` 驗**，不要憑感覺。
8. **encoder 是硬體周邊**：不能放 PIN 段，獨立 `enc_drv`。

---

## 9. 常見操作

- 啟動測試：`import ui_test_tool`（旋鈕選/調，encC 確認，btn 返回，Ctrl-C 回 REPL）。
- 跳頁：`ui_test_tool.goto("pca9685")`。
- 看/設 bus 值：`ui_test_tool.peek("_display_mode")`、`ui_test_tool.set("_display_time", 120)`。
- 加新頁：建 `page/xxx.py`（`@register` + `nav`）+ `__init__.py` 加兩行（import + `_PAGES_MOD`）。
- 換螢幕方向：改 `lvgl_init._MADCTL`（0x60 橫 / 0x00 直）。

## 相關文件

- `05_tft_usage.md` — TFT + lcd_bus 使用（LVGL 底層顯示依賴）
- `09_cores.md` — cores 核心實例（Core_LVGL 獨立核心）
- `01_protocol/02_command_index.md` — 完整指令索引（control_panel 的 mode byte 由 UART 幀協定定義）
