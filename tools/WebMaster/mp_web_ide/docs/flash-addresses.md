# Flash 位址對照表（權威設定參考）

> 用途：燒錄時「邊個檔案寫入邊個位址」。以 **ESP-IDF 預設單一 factory 分割**
> （無 OTA）為準；來源：Espressif 官方文件（見文末連結）。
> 若你哋嘅 build 有自訂分割表（例如 OTA 或 custom partition.csv），請以
> 你哋自己嘅 partition table 為準——**你哋 Python esptool 嘅現行指令就係真理**。

## MicroPython 官方 .bin（你哋主要燒錄對象）

| 映像 | 位址 | 說明 |
|---|---|---|
| ESP32 經典（ESP32_GENERIC-*.bin，v1.2x 或更早文件） | 0x1000 | 舊慣例（bootloader@0x1000） |
| **ESP32-S3 / S2 / C3 等新 build（v1.2x 起，IDF 新版）** | **0x0** | **新 bootloader 起點係 0x0**；燒 0x1000 會出 `invalid header` |

> 點解會中伏：MicroPython 官網舊教學寫 0x1000，但新 S3 build 已經唔同。
> 參考：[micropython discussion #16417（0x00 vs 0x1000）](https://github.com/orgs/micropython/discussions/16417)、
> [#16158（S3 invalid header 個案）](https://github.com/orgs/micropython/discussions/16158)。
> 判別口訣：**S3 一律試 0x0；開唔到機先試 0x1000**（反之亦然，總之唔好整片清除咗先亂試）。

## 出廠整包（factory merged，含 bootloader + partition + app）

| 內容 | 位址 |
|---|---|
| 整包 .bin（bootloader+partition+app 合併，通常由 `esptool merge_bin` 或 CI 產生） | **0x0** |

## 逐晶片預設（散件分開燒時）

| 晶片 | bootloader.bin | partition-table.bin | app（factory） | 備註 |
|---|---|---|---|---|
| ESP32 | 0x1000 | 0x8000 | 0x10000 | 經典 layout |
| ESP32-S2 | 0x1000 | 0x8000 | 0x10000 | |
| **ESP32-S3** | **0x0** | **0x8000** | **0x10000** | 你哋主力板；bootloader 由 0x0 起 |
| ESP32-C3 | 0x0 | 0x8000 | 0x10000 | |
| ESP32-C2 / C6 / H2 | 0x0 | 0x8000 | 0x10000 | 沿用同系慣例（C5/C61/P4 待官方文件核實後再補） |
| ESP8266 | —（無獨立 bootloader） | — | 0x0（non-OTA） | OTA 佈局請查官方 docs |

## 救命指令（Python esptool，瀏覽器冇揸住埠時）

```bash
# 散件重燒（ESP32-S3 例）
esptool.py --chip esp32s3 --port COM27 write_flash \
    0x0 bootloader.bin \
    0x8000 partition-table.bin \
    0x10000 app.bin

# 成粒擦乾淨（謹慎：bootloader 都會冇埋）
esptool.py --chip esp32s3 --port COM27 erase_flash

# 整包
esptool.py --chip esp32s3 --port COM27 write_flash 0x0 factory.bin
```

## 新手防呆規則（mp_web_ide UI 已/將實作）

1. **整包 merged → 一律 0x0**；單一 app → 依偵測晶片帶入（上表 app 欄）
2. **「整片清除」係高風險操作**：會擦走 bootloader；淨 app 燒錄時唔好㨂，
   或者㨂之前確認 bin 含 bootloader（UI 已有警告）
3. 位址錯嘅典型症狀：開機 ROM 報 `invalid header: 0xffffffff`（0x0 附近係空）
   → 用整包 0x0 重燒即可
4. WebSerial／Python 唔可以同時揸住同一個 COM 埠：燒完（或失敗後）關閉瀏覽器
   分頁／按「釋放序列埠」，Python 先連得入

## 來源

- ESP-IDF Partition Tables（ESP32-S3）：
  https://docs.espressif.com/projects/esp-idf/en/latest/esp32s3/api-guides/partition-tables.html
- esptool Basic Commands（內含各晶片 flash 位址慣例，例如 ESP32-C3 頁）：
  https://docs.espressif.com/projects/esptool/en/latest/esp32c3/esptool/basic-commands.html
- esptool flash-modes / flash 位址說明（GitHub）：
  https://github.com/espressif/esptool/blob/master/docs/en/esptool/flash-modes.rst
- 個案：erase 後 invalid header 0xFFFFFFFF 與修復討論：
  https://esp32.com/viewtopic.php?p=154466
