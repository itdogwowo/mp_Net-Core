# ports/ — 專案 delta 層(文件管理架構)

```
mp_Net-Core/
├── slave/                    ← 乾淨基礎框架(不綁任何實際項目)
│     • 通用韌體程式、lib、schedule/vBus 框架、基礎 config.json
│     • pixel 只留基礎效果 + 一支馬達正弦波效果範本（uart_motor_sine / motor_sine）
│     • registry.json: auto_play=false（開機不自動播放，純框架）
│
└── ports/                    ← 每個實際項目/板子的「delta 層」
      └── <板子或專案名>/
            ├── config.json          ← 該板 config（覆蓋 slave/config.json）
            └── ... 任何「相對 slave/ 同名同路徑」的檔案 = delta
                 例：
                   pixel/modes/xxx.json   ← 該項目自訂模式
                   schedule.json          ← 該項目開機排程（schedule 任務讀 /schedule.json）
                   pixel/effects/xxx.py   ← 項目專用效果
```

## 規則

1. **slave/ 永遠保持可單獨使用的基礎**: 不加項目專用內容。
2. **實際項目要跑什麼**,一律以 delta 形式放 `ports/<板子|專案名>/`,檔案的
   相對路徑 = 燒錄到裝置上的路徑(slave 根 = 裝置根)。
3. 燒錄 = 先上傳完整 `slave/`,再以 ports 內的 delta 檔**覆蓋**同名路徑
   (最後覆蓋者勝)。
4. 不同項目各自一個資料夾,互不干擾;基礎升級 = 改 slave,項目 = 只改自己的
   ports delta。

## 範例(S3 電機 bench 板)

`ports/S3/ESP32-S3-1_18/`:
- `config.json` — uartMotor(addr 18, uart list[1] = id2 @9600 GPIO12)、
  UART/RS485、Schedule 等該板設定。
- `README.md` — 該板說明與 delta 清單。

## 快速燒錄(以 mpremote 為例)

```bash
# 1) 上傳基礎（裝置已接 COM28）
python -m mpremote connect COM28 fs cp -r slave :/        # 依 mpremote 版本支援 -r；不支援就逐檔上傳
# 2) 套用 delta（覆蓋）
python -m mpremote connect COM28 fs cp ports/S3/ESP32-S3-1_18/config.json :/config.json
python -m mpremote connect COM28 fs cp ports/S3/ESP32-S3-1_18/pixel/modes/xxx.json :/pixel/modes/xxx.json
# 3) RESET
```

> 之後可加一支小工具 `tools/PC/deploy_port.py <COM> <port_name>` 自動化
> 「全量 slave + delta 覆蓋」，避免手動逐檔。
