# ports/S3/ESP32-S3-1_18 — S3 電機 bench 板(實測成功組合)

## 板子
ESP32-S3 (UID 90DA724962C0 / cID 62C0,COM28):UART 電機(UART-412 推桿)bench。
開機流程(已實測成功,50fps):
1. **auto**:`motor_home` 歸位一次(A收 全速 2s → 停) [registry]
2. **schedule(vBus)**:+8s `MODE_SET id2` → `motor_sine` **呼吸一週期**(B伸5s→停1s→A縮5s→停1s = 12s)
3. **+21s `MODE_STOP`** → 全停,**保持中性值 2048**(0x80,電機死區停,不再動)

## delta 檔案清單(相對 slave/ 覆蓋,一次過上傳)
| 檔案 | 內容 |
|---|---|
| `config.json` | uartMotor addr18@UART list[1](id2 9600 GPIO12)、RS485 id1 115200 |
| `pixel/effects/effects.py` | slave 基礎 + `uart_motor_sine` + bench 行程類別(motor_home / motor_test_cycle) |
| `pixel/effects/effects.json` | effects id:1-6 基礎、7 uart_motor_sine、8 motor_home、9 motor_test_cycle |
| `pixel/modes/motor_home.json` | mode id3(開機 auto 歸位) |
| `pixel/modes/motor_test_cycle.json` | mode id4(伸10s/收12s 一次) |
| `pixel/registry.json` | `auto_play:true, list:["motor_home"]` |
| `schedule.json` | 開機 +8s 經 vBus 發 `MODE_SET 00 04 00 00 FF`(id4) |
| `README.md` | 本檔 |

> motor_sine(mode id2,正弦波設定方法)在 slave 基礎內,不需放此。

## MODE_SET payload 速查(type=0 本地 id)
| id | mode | payload |
|---|---|---|
| 2 | motor_sine(循環) | `00 02 00 00 FF` |
| 3 | motor_home(歸位一次) | `00 03 00 00 FF` |
| 4 | motor_test_cycle(伸縮一次) | `00 04 00 00 FF` |
| MODE_STOP | — | `00`… `0x3106 payload 01` |

## 上傳
```bash
# 1) 先上傳 slave/ 基礎(全量)
# 2) delta 覆蓋(依序):
python -m mpremote connect COM28 fs cp ports/S3/ESP32-S3-1_18/config.json :/config.json
python -m mpremote connect COM28 fs cp ports/S3/ESP32-S3-1_18/pixel/effects/effects.py :/pixel/effects/effects.py
python -m mpremote connect COM28 fs cp ports/S3/ESP32-S3-1_18/pixel/effects/effects.json :/pixel/effects/effects.json
python -m mpremote connect COM28 fs cp ports/S3/ESP32-S3-1_18/pixel/modes/motor_home.json :/pixel/modes/motor_home.json
python -m mpremote connect COM28 fs cp ports/S3/ESP32-S3-1_18/pixel/modes/motor_test_cycle.json :/pixel/modes/motor_test_cycle.json
python -m mpremote connect COM28 fs cp ports/S3/ESP32-S3-1_18/pixel/registry.json :/pixel/registry.json
python -m mpremote connect COM28 fs cp ports/S3/ESP32-S3-1_18/schedule.json :/schedule.json
# 3) RESET（開機馬達會自動歸位一次，之後 schedule 會再觸發一次伸縮）
```
