---
name: mp-netcore
description: >
  在 mp_Net-Core 專案的 MicroPython slave 中新增功能模組 (command/action/task/config)。
  每當使用者提到「新增指令」「新增 action」「新增 task」「新增功能模組」「新增 schema」、
  或者要在 slave/ 底下加入任何新功能、或者 mp_Net-Core / mp_Net-Light 專案的 slave 端開發時，
  就使用這個 skill。它涵蓋了 schema JSON 定義、action handler 撰寫、registry 註冊、task 建立、
  以及 config.json 欄位新增的完整流程與慣例。
---

# mp_Net-Core Slave 開發技能

此 skill 涵蓋在 `mp_Net-Core/slave/` 中新增功能模組的完整規範。

## 專案架構總覽

```
slave/
├── app.py                  # 裝配層：SchemaStore + Dispatcher + 註冊所有 action
├── boot.py                 # 硬體初始化：SPI/I2C/LED/Network/SD 註冊到 SysBus
├── main.py                 # 入口：TaskManager 註冊 tasks，啟動雙核心 Runner
├── config.json             # 系統/硬體/緩衝/網路設定 (無損更新)
├── action/                 # 行為層 (常改)：每個 <group>_actions.py 對應一個功能模組
│   ├── registry.py         # 統一註冊入口：import 各 action 模組並呼叫 register(app)
│   ├── sys_actions.py
│   ├── status_actions.py
│   ├── heartbeat_actions.py
│   ├── file_actions.py
│   ├── stream_actions.py
│   └── ram_bench_actions.py
├── schema/                 # 協議定義：每個 <group>.json 定義該模組的 cmd 與 payload
│   ├── sys.json
│   ├── status.json
│   ├── heartbeat.json
│   ├── file.json
│   ├── stream.json
│   └── ram_bench.json
└── tasks/                  # 任務層：雙核心 Runner 調度的背景任務
    ├── network.py          # NetworkTask: TCP/UDP/WS 收發 + Heartbeat + Supply Chain
    ├── bus_decode.py       # BusDecodeTask: 封包解析 + Dispatch
    ├── render.py           # RenderTask: LED 渲染 (Core 1)
    ├── web_ui.py           # WebUITask: HTTP 管理頁面
    ├── dp_manager_task.py  # DpManagerTask: Display Manager
    ├── jpeg_decode_task.py # JpegDecodeTask: JPEG 解碼
    ├── dp_buffer_task.py   # DpBufferTask: Display Buffer
    └── display_task.py     # DisplayTask: 顯示輸出
```

## 核心設計原則

### SysBus 三級存儲
```python
from lib.sys_bus import bus

# Services: 單例服務對象 (Buffer Hub、驅動、網路管理器)
bus.register_service("pixel_stream", hub)
hub = bus.get_service("pixel_stream")

# Providers: 動態健康度回報 (lambda 延遲計算)
bus.register_provider("fps", lambda: led_driver.get_fps())

# Shared: 輕量級狀態同步 dict
bus.shared["brightness"] = 128
```

### 雙核心分工
| Core | 角色 | 典型任務 |
|------|------|---------|
| Core 0 | 網路 + 控制 | NetworkTask, BusDecodeTask, WebUITask, DpManagerTask |
| Core 1 | 渲染 + 顯示 | RenderTask, JpegDecodeTask, DpBufferTask, DisplayTask |

- Core 0 寫入 `pixel_stream` hub 的寫緩衝，Core 1 讀取讀緩衝
- 避免兩核心同時修改同一個 `dict` key
- 使用 `AtomicStreamHub` 的三緩衝狀態機實現零拷貝數據交換

## 新增 Command (最常見的擴展)

新增一個指令需要修改 **4 個位置**：

### Step 1: 決定 CMD 編號

使用 16-bit hex 編號，按功能域劃分：
| 範圍 | 功能 | 範例 |
|------|------|------|
| 0x10xx | 系統發現 | DISCOVER, ANNOUNCE |
| 0x11xx | 狀態管理 | STATUS_GET, STATUS_RSP |
| 0x12xx | 心跳 | HEARTBEAT |
| 0x13xx | 檔案系統 | FS_TREE_GET |
| 0x18xx | 效能測試 | RAM_BENCH |
| 0x20xx | 檔案傳輸 | FILE_BEGIN/CHUNK/END |
| 0x30xx | LED 串流 | STREAM_FRAME, STREAM_PLAY |

### Step 2: 定義 Schema

在對應的 `/schema/<group>.json` 的 `cmds` 陣列中新增 cmd 定義。若是全新模組，建立新的 json 檔。

```json
{
  "group": "<group>",
  "cmds": [
    {
      "cmd": "0xXXXX",
      "name": "CMD_NAME",
      "payload": [
        {"name": "field1", "type": "u8"},
        {"name": "field2", "type": "u32"},
        {"name": "data", "type": "bytes_rest"}
      ]
    }
  ]
}
```

**支援的 payload 類型**：

| 類型 | 說明 | 佔用位元組 |
|------|------|-----------|
| `u8` | uint8 | 1 |
| `u16` | uint16 LE | 2 |
| `u32` | uint32 LE | 4 |
| `i16` | int16 LE | 2 |
| `i32` | int32 LE | 4 |
| `str_u16len` | 字串，前綴 2B 長度 | 2 + len |
| `bytes_fixed` | 固定長度 bytes (需指定 `"len": N`) | N |
| `bytes_rest` | 吃掉剩餘所有 bytes | 剩餘全部 |

### Step 3: 撰寫 Action Handler

在 `/action/<group>_actions.py` 中撰寫 handler 函數與 register 函數。

**Handler 簽名固定為**：
```python
def on_xxx(ctx, args):
    # ctx: {"app": App, "send": send_func, "transport": str, ...}
    # args: dict，由 schema 自動解碼
    pass
```

**註冊函數**：
```python
def register(app):
    app.disp.on(0xXXXX, on_xxx)
    print("✅ [Action] <Group> actions registered")
```

**發送回覆封包的標準寫法**：
```python
def on_xxx(ctx, args):
    app = ctx["app"]

    # 建立回覆 payload
    cmd_def = app.store.get(0xYYYY)  # 回覆用的 cmd
    payload = SchemaCodec.encode(cmd_def, {
        "field1": value1,
        "field2": value2,
    })
    pkt = Proto.pack(0xYYYY, payload)

    if "send" in ctx:
        ctx["send"](pkt)
```

### Step 4: 註冊到 Registry

在 `/action/registry.py` 中 import 新的 action 模組並呼叫其 `register(app)`：

```python
from action import <group>_actions

def register_all(app):
    # ... 已有模組 ...
    <group>_actions.register(app)
```

### 完整範例：新增一個「系統資訊查詢」指令

**schema/sys.json** (新增 cmd)：
```json
{
  "cmd": "0x1003",
  "name": "SYS_INFO_GET",
  "payload": []
}
```

**action/sys_actions.py** (新增 handler)：
```python
import gc, os
from lib.sys_bus import bus

def on_sys_info_get(ctx, args):
    gc.collect()
    stat = os.statvfs('/')
    print(f"ℹ️ RAM Free: {gc.mem_free()//1024}KB, FS Free: {(stat[0]*stat[3])//1024}KB")
```

並在該檔案的 `register(app)` 中加入：
```python
app.disp.on(0x1003, on_sys_info_get)
```

## 新增 Task

Task 是雙核心 Runner 調度的背景任務。新增步驟：

### Step 1: 建立 Task 類別

在 `/tasks/<name>.py` 建立繼承 `Task` 的類別：

```python
import time
from lib.task import Task
from lib.sys_bus import bus

class MyTask(Task):
    def __init__(self, name, ctx):
        super().__init__(name, ctx)
        # 從 ctx 取得需要的服務
        self.app = ctx['app']
        self.hub = bus.get_service("pixel_stream")

    def on_start(self):
        super().on_start()
        # 初始化邏輯：註冊 Provider、設定計時器等
        bus.register_provider("my_metric", lambda: self._count)
        print("✅ [MyTask] Started")

    def loop(self):
        if not self.running:
            return
        # 主要邏輯
        # ⚠️ 不要在 loop 裡放 sleep，讓 TaskManager 控制調度

    def on_stop(self):
        super().on_stop()
        # 清理邏輯
        print("🛑 [MyTask] Stopped")
```

### Step 2: 在 main.py 註冊 Task

```python
from tasks.my_task import MyTask

# 在 launcher() 中，tm.register_task(...) 區塊：
tm.register_task("my_task", MyTask, default_affinity=(1, 0))  # Core 0
# 或
tm.register_task("my_task", MyTask, default_affinity=(0, 1))  # Core 1
```

`default_affinity` 格式為 `(core0_enable, core1_enable)`，`1` 表示允許在該核心執行。

## 修改 config.json

設定值載入後存在 `bus.shared` 中，使用 `ConfigManager` 無損更新：

```python
from lib.ConfigManager import cfg_manager

# 修改記憶體中的值
bus.shared["System"]["refresh_rate_ms"] = 2

# 無損寫回檔案 (僅替換指定 key，保留其他格式)
cfg_manager.save_from_bus(update_key="System.refresh_rate_ms")
```

**新增頂層 key** 時會觸發全檔重寫，但會保留鍵值順序。

## 使用 lib 核心模組

以下是行動層最常使用的 lib 模組：

| 模組 | 用途 | 常用 API |
|------|------|---------|
| `lib.proto` | 封包打包/解析 | `Proto.pack(cmd, payload)`, `StreamParser` |
| `lib.schema_codec` | Payload 編解碼 | `SchemaCodec.encode(cmd_def, obj)` |
| `lib.schema_loader` | Schema 載入 | `app.store.get(cmd_int)` |
| `lib.dispatch` | 指令分發 | `app.disp.on(cmd, handler)` |
| `lib.sys_bus` | 三級數據總線 | `bus.get_service()`, `bus.register_provider()`, `bus.shared` |
| `lib.buffer_hub` | 三緩衝狀態機 | `hub.get_write_view()`, `hub.commit()`, `hub.get_read_view()` |
| `lib.task` | Task 基類 | `Task.on_start()`, `Task.loop()`, `Task.on_stop()` |
| `lib.task_manager` | 任務調度 | `tm.register_task()`, `tm.runner_loop(core)` |
| `lib.fs_manager` | 檔案系統管理 | `fs.begin_write()`, `fs.write_chunk()`, `fs.end_write()` |
| `lib.ConfigManager` | 設定檔管理 | `cfg_manager.save_from_bus(update_key=...)` |

## 常見錯誤與修正

- **Schema JSON 語法錯誤**：檢查是否有尾逗號、註解 (`//` 不合法)
- **Handler 沒被呼叫**：確認已在 `action/<group>_actions.py` 的 `register(app)` 中呼叫 `app.disp.on(cmd, handler)`
- **忘記 import action 模組**：確認已在 `action/registry.py` 中 import 並呼叫 `register(app)`
- **Payload 解碼失敗**：確認 schema 中欄位類型與順序正確，`bytes_rest` 必須放在最後
- **Handler 收到空的 args**：檢查 cmd 編號是否與 schema 定義一致（hex 字串 vs int）
- **雙核 Race Condition**：避免兩個核心同時修改同一個 `bus.shared` key

## 新增功能模組的檢查清單

當你要新增一個完整的功能模組 (例如 `gpio`、`sensor`) 時，請確認以下步驟：

- [ ] `/schema/<group>.json`：定義所有 cmd 與 payload 欄位
- [ ] `/action/<group>_actions.py`：撰寫所有 handler + `register(app)` 函數
- [ ] `/action/registry.py`：import 新模組並呼叫 `register(app)`
- [ ] (可選) `/tasks/<task_name>.py`：若需要背景任務
- [ ] (可選) `main.py`：若新增 Task，在 `tm.register_task()` 區塊註冊
- [ ] (可選) `config.json`：若需要新設定欄位

## 快速參照

當你需要具體的實作範例時，直接在專案中閱讀這些檔案（相對專案根目錄）：
- **簡單指令** (請求→回覆)：參考 `slave/action/status_actions.py`
- **多指令模組** (含 State)：參考 `slave/action/stream_actions.py`
- **含內部狀態的模組**：參考 `slave/action/ram_bench_actions.py`
- **檔案操作模組**：參考 `slave/action/file_actions.py`
- **Task 範例**：參考 `slave/tasks/render.py` (Core 1) 或 `slave/tasks/network.py` (Core 0)
- **Boot 初始化**：參考 `slave/boot.py`

若有 `mp_Net-Light` 專案在同層目錄，也可參考：
- `../mp_Net-Light/doc/AI_CONTEXT.md` — 完整架構文件
- `../mp_Net-Light/doc/ADD_NEW_CMD_FLOW.md` — 新增指令流程文件
