# Microchip ATtiny1616 (tinyAVR 1-series) — 查證報告

所有數字均取自可點擊的來源。主要一手來源：

- **[ATtiny1614/1616/1617 Complete Datasheet, DS40002204A](https://ww1.microchip.com/downloads/en/DeviceDoc/ATtiny1614-16-17-DataSheet-DS40002204A.pdf)**（以下稱「DS40002204A」；本文所有「Features」章節數字與表 2-1、表 7-2、表 6-4、第 24/28/30/31 章皆出自此份 PDF）
- **[ATtiny1616 產品頁 (microchip.com)](https://www.microchip.com/en-us/product/ATtiny1616)**

> 註：我曾嘗試抓取 Microchip [onlinedocs HTML 版](https://onlinedocs.microchip.com/oxy/GUID-9175774B-9B45-45D7-A62D-28F07E411073-en-US-4/GUID-8FD014F0-1B05-4B34-BFD6-EE4FC885DFEE.html)，該站回 HTTP 503，故本文全部改用官方 PDF（DS40002204A）作為引註。引用時請以「DS40002204A-page N」標註頁碼。

---

## 1. Flash / SRAM / EEPROM

- Flash：**16 KB** in-system self-programmable Flash
- SRAM：**2 KB**
- EEPROM：**256 bytes**
- 耐久度：Flash **10,000** cycles；EEPROM **100,000** cycles；資料保存 **40 年 @ 55°C**
- User Row：**32 bytes**（chip erase 後仍保留，UPDI 可在鎖定裝置上寫入）
- EEPROM/Flash 之外的「額外」非揮發空間：SIGROW（簽章列，見第 5 節）

來源：DS40002204A Features / 表 2-1，[PDF](https://ww1.microchip.com/downloads/en/DeviceDoc/ATtiny1614-16-17-DataSheet-DS40002204A.pdf)（Introduction 與 p.1–2、p.11–12）；User Row 見同份 p.21、NVMCTRL 第 9 章 p.60

---

## 2. 最高時脈 vs 電壓（Speed Grades）

官方 Features 條列（一字不差）：

- **0–5 MHz @ 1.8V – 5.5V**
- **0–10 MHz @ 2.7V – 5.5V**
- **0–20 MHz @ 4.5V – 5.5V**

重點：**3.3V 不在任何一格的保證範圍內**。以保證值而言，3.3V 只落在 2.7–5.5V 這一格，即 **10 MHz**。5V（4.5–5.5V）才保證 20 MHz。

時脈來源（CLKCTRL）：

- 內部 **16/20 MHz low-power internal RC oscillator**（可選 16 MHz 或 20 MHz）
- 內部 **32.768 kHz ULP RC oscillator**（OSCULP32K）
- **32.768 kHz 外部晶體**（XOSC32K，接 TOSC1/TOSC2）
- **External clock input**（EXTCLK 腳）

> ⚠️ 關於「高頻外部晶體」：ATtiny1616 **沒有**給主時脈用的高頻（MHz 級）外部晶體振盪器。唯一的晶體振盪器是 **32.768 kHz** 的 XOSC32K，且它主要是給 RTC／低功耗時基使用。若要外部高速時脈，只能用 **EXTCLK 外部時脈輸入**（由外部元件/振盪器產生訊號灌入），不是「接晶體」。20 MHz 是靠內部 RC 振盪器達成的。

來源：DS40002204A Features（Speed Grades、Clock options）、第 10 章 CLKCTRL（p.1–2、p.32 附近），[PDF](https://ww1.microchip.com/downloads/en/DeviceDoc/ATtiny1614-16-17-DataSheet-DS40002204A.pdf)

---

## 3. USART：數量、FIFO、baud、DMA、RS-485

- USART 數量：**1 個**（USART0；有 fractional baud rate generator、auto-baud、start-of-frame detection、Master SPI mode、LIN Slave 支援）
- **TX/RX FIFO：沒有真正的 FIFO。** 官方原文：「The transmitter consists of a **single-write buffer**, a Shift register... The receiver consists of a **two-level receive buffer** and a Shift register.」
  - 也就是：TX = 1 byte 寫入緩衝；RX = 2 級接收緩衝（= 1 byte 可讀 + 1 byte 在後備），**沒有 TX FIFO，沒有可程式化深度的 FIFO**。
  - 全份 datasheet 中 “FIFO” 字樣只出現一次，且是描述 **SPI Master Buffer mode** 的 two-entry RX FIFO，不是 USART。（搜尋 `FIFO` 全文中僅 2 行，位於第 25 章 SPI）
- 最高 baud：datasheet **未給單一 absolute max baud 數字**，只給限制式：`fBAUD ≤ fCLK_PER`、BAUD 暫存器最小值 64（S=16 normal mode）→ 上限即 **fCLK_PER/16**；Double-Speed mode（RXMODE=0x01，S=8）加倍為 **fCLK_PER/8**。以 20 MHz 周邊時脈推得 normal ≈ **1.25 Mbps**、double-speed ≈ **2.5 Mbps**，但此為我由公式推導，**非 datasheet 直接標示的保證值**。
  - datasheet 有明列數字的只有 **UPDI** 介面：**0.9 Mbps @ VDD = 2.7–5.5V（TA = 0–50°C）**、450 kbps @ 2.2–5.5V、225 kbps @ 1.8–5.5V。
- **DMA：沒有。** 全份 datasheet 完全沒有 “DMA” 字樣（0 次命中）。
- **硬體 RS-485 / DE 控制：有。** RS-485 mode 由 `USARTn.CTRLA.RS485[1:0]` 啟用；`RS485[0]=1` 時 USART 自動驅動 **XDIR pin**（Transmit enable for RS-485）：在資料移位輸出前 **1 個 baud clock cycle** 拉高，在整個 frame（含 Stop bit）期間保持高，傳完再拉低。XDIR 為硬體自動控制，不需軟體 toggle GPIO。

來源：DS40002204A 第 24 章 USART（Features p.316、Overview p.316、fractional baud p.319、RS-485 p.325 附近）與第 25 章 SPI（p.349–350）、UPDI 第 33 章表 33-1 與表 36-37，[PDF](https://ww1.microchip.com/downloads/en/DeviceDoc/ATtiny1614-16-17-DataSheet-DS40002204A.pdf)

---

## 4. 周邊彙總（ATtiny1616）

| 項目 | 數量 / 規格 |
|---|---|
| SPI | **1** 個 master/slave SPI；Master 模式 **fSCK max 10 MHz**（電性表） |
| TWI (I2C) | **1** 個，dual address match；Sm 100 kHz / Fm 400 kHz / **Fm+ 1 MHz** |
| TCA | **1 個 16-bit** Timer/Counter type A（3 個 compare channel，split mode 可當 2×8-bit） |
| TCB | **2 個 16-bit** Timer/Counter type B（input capture、8-bit PWM） |
| TCD | **1 個 12-bit** Timer/Counter type D（控制應用、fault handling、half/full-bridge） |
| RTC | **1 個 16-bit**（外部晶體／外部時脈／內部 RC） |
| WDT | 1 個，11 段 timeout（8 ms–8 s），含 Window mode |
| CCL | **1 個 instance，2 個 programmable LUT**；3 輸入任意真值表 + 內建 sequencer（D-FF / JK-FF / D-latch / RS-latch）；可選 synchronizer / filter / edge detector |
| Event System | **6 channels**（其中 **最多 4 條 asynchronous** + **最多 2 條 synchronous** 並行） |
| ADC | **2 個 10-bit ADC**，115 ksps；ATtiny1616 的 ADC 通道 = **12 single-ended + 8 differential**（表 2-1 寫法 `12+8`） |
| DAC | **有，3 個 8-bit DAC**（每顆 350 ksps；**只有 DAC0 有外部輸出腳**，DAC1/DAC2 只能內部饋入 AC/ADC） |
| AC | **3 個** Analog Comparator |
| PTC | 1 個；ATtiny1616 支援 **12 self-cap**、**36 mutual-cap** 通道 |
| CRCSCAN | **1 個，僅 CRC-16-CCITT**（見第 10 節，**沒有 CRC-32**） |
| GPIO | 18 條（20-pin 封裝）；全部 GPIO 都可外部中斷 |

來源：DS40002204A 表 2-1 Peripheral Summary（p.11–12）、Features（p.1–2）、SPI/TWI 電性表、第 28 章 CCL Features，[PDF](https://ww1.microchip.com/downloads/en/DeviceDoc/ATtiny1614-16-17-DataSheet-DS40002204A.pdf)

---

## 5. 晶片唯一序號（Unique ID）— **有**

- **有 10-byte 出廠唯一序號**：`SIGROW.SERNUM[9:0]`，位於 Signature Row offset **0x03–0x0C**。
- datasheet 原文：「Each device has an individual serial number, representing a **unique ID**. This can be used to identify a specific device in the field. The serial number consists of **ten bytes**: SIGROW.SERNUM[9:0].」
- 另外還有 3-byte Device ID（`SIGROW.DEVICEID[2:0]`，offset 0x00–0x02），識別型號／記憶體大小／pin count／die revision。**ATtiny1616 的 signature bytes = `0x1E 0x94 0x21`**。（ATtiny1614 = `1E 94 22`、ATtiny1617 = `1E 94 20`）
- Signature Row 另含工廠校正資料：TEMPSENSE[1:0] (0x20–0x21)、OSC16ERR3V/5V、OSC20ERR3V/5V (0x22–0x25)。
- 因此**不需要**用 DEVICEID 充當序號、也**不需要**使用者自己燒 EEPROM 來當唯一 ID — 硬體已提供 10 bytes 唯一序號。
- 注意：鎖定（locked）裝置下只有 System Information Block 可讀；不過 SERNUM 本身是唯讀出廠值。

來源：DS40002204A §6.7 Signature Bytes + 表 6-4 Device ID（p.21）、§6.10.1 Signature Row Summary 與 §6.10.2.2 Serial Number Byte n（p.26–28）、NVMCTRL §9.1 Features「Serial number for each device」（p.60），[PDF](https://ww1.microchip.com/downloads/en/DeviceDoc/ATtiny1614-16-17-DataSheet-DS40002204A.pdf)

---

## 6. Bootloader / Flash 自我編程

- NVMCTRL Features 明列：**Self-Programming and Boot Loader Support**；可設定三段式寫入保護：**BOOT section / APPCODE section / APPDATA section**。
- Flash 頁面（page）粒度：**256 bytes**（BOOTEND/APPEND 以 256 bytes 為單位；ATtiny1616 Flash = 16 KB = 64 頁）。
- 分區由 fuse 決定，原文：「The fuses select the section sizes in **blocks of 256 bytes**. The BOOT section stretches from the start of the Flash until BOOTEND. The APPCODE section runs from BOOTEND until APPEND. ... If BOOTEND and APPEND are written to '0', the entire Flash is regarded as the BOOT section.」
  - 所以 **BOOT section 大小可選值 = n × 256 bytes**（BOOTEND = 0x01…0x40，最大 16 KB；BOOTEND=0x00 代表整塊 Flash 都是 BOOT）。
  - datasheet 舉例：「If FUSE.BOOTEND is written to 0x04 and FUSE.APPEND is written to 0x08, the first 4*256 bytes ...」
- 由 BOOT 區執行的碼可以寫 APPCODE 與 APPDATA；**CPU 永遠不能寫 BOOT section**。`NVMCTRL.CTRLB` 的 **APCWP / BOOTLOCK** lock bit 可鎖住對應區塊直到下次 Reset。
- 自我編程需 **CCP**（Configuration Change Protection）：寫入 **SPM signature `0x9D`** 到 `CCP` 暫存器後，緊接著執行寫 NVMCTRL command register，順序錯誤或中途存取 Flash/NVMCTRL/EEPROM 會立即失效。
- 中斷向量**可選擇放在 Application section 或 Boot loader section**（CPUINT Features 明列）→ 這是做 bootloader 時的重要彈性。
- **現成 UART bootloader：有。**
  - **Optiboot_x**（Optiboot 的 tinyAVR 分支）：megaTinyCore 支援，0/1-series 已上線；安裝 bootloader 需要用 UPDI 燒一次。
  - **urboot**：AVRDUDE v7.1+ 以 `-c urclock` 支援；官方 README 說多數 bootloader 可塞進 **256 bytes**（無 EEPROM 讀寫）、**384 bytes**（含 EEPROM 讀寫）、**512 bytes**（雙啟動 + EEPROM + chip erase）。
  - 注意 0/1-series 的 UX 限制（megaTinyCore README 明講）：0/1-series 沒有可用於自動 reset 的硬體 reset pin，因此要用 bootloader 上傳只能 (a) 關掉 UPDI（之後改 fuse/bootloader 需 HV 燒錄器），或 (b) 保留 UPDI 但**上電後 8 秒內**開始上傳。

來源：DS40002204A §8.5.7 CCP + `0x9D SPM`（p.49 附近）、§9 NVMCTRL（Features p.60、§9.2.2 分區表與舉例）、§7.2 CPUINT Features「Interrupt Vectors Optionally Placed in the Application Section or the Boot Loader Section」（p.105 附近），[PDF](https://ww1.microchip.com/downloads/en/DeviceDoc/ATtiny1614-16-17-DataSheet-DS40002204A.pdf)；[megaTinyCore README](https://github.com/SpenceKonde/megaTinyCore)、[urboot README](https://github.com/stefanrueger/urboot/blob/main/README.md)

---

## 7. 燒錄介面 (UPDI) 與工具鏈

- 燒錄/除錯介面：**單腳 UPDI**（Unified Program and Debug Interface），支援 on-chip debug（2 個硬體斷點）。
- UPDI pin 可經 fuse 設定為 UPDI / 一般 I/O / Reset。
- 官方工具：Microchip **MPLAB X IDE / MPLAB PICkit 4 / SNAP / Atmel-ICE**、舊稱 Atmel Studio；**pymcuprog**（Microchip 維護的 Python 工具，支援單電阻/二極體 USB-serial 上傳）。
- 低成本替代：**SerialUPDI**（megaTinyCore 內建，基於 pymcuprog）、**jtag2updi**（用經典 Arduino 當燒錄器；megaTinyCore 已將其標為 deprecated）。
- Arduino：**[megaTinyCore](https://github.com/SpenceKonde/megaTinyCore)**（SpenceKonde），支援 ATtiny3216/1616/816/416 等 1-series；透過 Boards Manager 安裝會自動帶入所需 toolchain（目前為 Azduino5 build，含更新過的 ATpacks）。
- 我**未**在本輪查到可引註的 PlatformIO board id / platform 頁面，以及 avr-libc 加入 tinyAVR 0/1-series header 的確切版本與官方 pack 版本號 → 列入第 11 節未查證清單。

來源：[ATtiny1616 產品頁](https://www.microchip.com/en-us/product/ATtiny1616)、DS40002204A §33 UPDI（p.465 起，UPDI 腳位與 fuse 設定）、[megaTinyCore README](https://github.com/SpenceKonde/megaTinyCore)

---

## 8. 中斷向量、延遲、硬體乘法器

- **中斷向量數量：31 個（vector number 0–30）**（RESET=0 … NVMCTRL_EE=30）。表 7-2 完整列出，每個週邊 source 一個向量；含 1 個 NMI（CRCSCAN_NMI）。
- 中斷控制器：**two-level** — NMI + 兩級優先權（0 Normal / 1 High，可指定 1 個為 level-1），Normal 級可選 round-robin；支援 **Compact Vector Table (CVT)**。
- **中斷延遲（最小響應時間）**，官方表 13-1：
  - Finish ongoing instruction: **1 cycle**
  - Store PC to stack: **2 cycles**
  - Jump to interrupt handler: **3 cycles (jmp)** — 因為 ATtiny1616 Flash > 8 KB；Flash ≤ 8 KB 的型號用 `rjmp` 只要 **2 cycles**
  - → 最小總延遲 **6 cycles**（16 KB 型號）。從 sleep 醒來額外 **+5 cycles**（再加所選 sleep mode 的 start-up time）。
  - `RETI`：**4–5 cycles**（視 PC 大小）。
- **硬體乘法器：有**，**2-cycle** hardware multiplier，支援 signed/unsigned integer 與 fractional（8×8 → 16-bit）。Features 直接寫「Two-cycle hardware multiplier」。

來源：DS40002204A 表 7-2 Interrupt Vector Mapping（p.45–46）、第 13 章 CPUINT（Features p.103、表 13-1 Minimum Interrupt Response Time p.104）、§8.4.1 Hardware Multiplier（p.52 附近），[PDF](https://ww1.microchip.com/downloads/en/DeviceDoc/ATtiny1614-16-17-DataSheet-DS40002204A.pdf)

---

## 9. 同系列 Flash / SRAM 差異簡表

### tinyAVR 1-series（同一份 DS40002204A 的 ATtiny1614/1616/1617）

| 型號 | 腳數 | Flash | SRAM | EEPROM | Max freq |
|---|---|---|---|---|---|
| ATtiny1614 | 14 | 16 KB | 2 KB | 256 B | 20 MHz |
| **ATtiny1616** | **20** | **16 KB** | **2 KB** | **256 B** | **20 MHz** |
| ATtiny1617 | 24 | 16 KB | 2 KB | 256 B | 20 MHz |

→ 1614/1616/1617 **記憶體完全相同**，只差 pin 數與 ADC/PTC/AC 通道數、GPIO 數。

### ATtiny3216 / 3217（32 KB 版本）

- **ATtiny3216：32 KB Flash、2 KB SRAM、256 B EEPROM**（來源：[ATtiny3216/3217 Automotive DS40002212A](https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/ATtiny3216-17-Auto-DataSheet-DS40002212A.pdf)，Features 明載）。
  - ⚠️ 該份是 **Automotive** 版本，標示 **up to 16 MHz**、16 MHz 內部 RC。工業級 ATtiny3216 的正式 datasheet（DS40002147）與其 max frequency 我**未取得可引註來源**，故不寫數字。
  - → 與 ATtiny1616 的差異重點：**Flash 32 KB vs 16 KB，SRAM 同為 2 KB**。

### tinyAVR 2-series（ATtiny1626 等）

- **ATtiny1624/1626/1627：16 KB Flash、2 KB SRAM、256 B EEPROM、32 B User Row**；20 MHz；**2 個 USART**、**4 個 CCL LUT**、12-bit 差動 ADC + PGA、**沒有 TCD**、**沒有 DAC/AC1-2/ADC1**（來源：[ATtiny1624/1626/1627 DS40002234A](https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/ATtiny1624-26-27-DataSheet-DS40002234A.pdf)，表 1 與表 2）。
- 整個 2-family 記憶體階梯（同表 1）：4 KB/512 B、8 KB/1 KB、**16 KB/2 KB**、32 KB/3 KB（Flash/SRAM）；EEPROM 128 B（4/8 KB 型號）或 256 B（16/32 KB 型號）。

---

## 10. CRC32 與 SHA-256（8-bit AVR）— 部分未查證

**已查證（有來源）**

- **ATtiny1616 的 CRC 周邊（CRCSCAN）只有 CRC-16-CCITT**：Features 寫「**CRC-16-CCITT**」，多項式 `x16 + x12 + x5 + 1`。**沒有 CRC-32**。用途是開機時掃描 Flash（全 Flash / Boot section / Boot+App），失敗可發 NMI；也可由軟體啟用。CRC 以 **每 3 個 main clock cycle 讀 1 個 16-bit word**（priority mode）的速率掃描。→ **不能**用它來算 CRC-32 資料校驗。
  來源：DS40002204A 第 27 章 CRCSCAN（p.392 起），[PDF](https://ww1.microchip.com/downloads/en/DeviceDoc/ATtiny1614-16-17-DataSheet-DS40002204A.pdf)
- **確有 AVR 型號提供硬體 CRC-32 周邊：AVR-DA 系列**（AVR128DA28/32/48/64）。
  - AVR-DA 的 CRCSCAN Features 明列：「**CRC-16-CCITT or CRC-32 (IEEE 802.3)**」，且 SRC 可選全 Flash / App / Boot，失敗可 NMI，可在 reset initialization 期間掃描。
  - SYSCFG0 fuse 位 `CRCSRC` 的選項亦明列 `0x1 CRC32 — CRC-32 (IEEE 802.3)`（對照 ATtiny1616 同欄位只有 FLASH/BOOT/BOOTAPP/NOCRC）。
  - **AVR-DB 亦具 CRC 周邊**（搜尋結果指向 [AVR128DB28/32/48/64 DS40002247](https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/AVR128DB28-32-48-64-DataSheet-DS40002247.pdf)，但我未逐頁確認其多項式清單）。
  來源：[AVR128DA28/32/48/64 Data Sheet DS40002183E](https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/AVR128DA28-32-48-64-Data-Sheet-DS40002183.pdf)（第 28 章 CRCSCAN Features，p.450；SYSCFG0 CRCSRC 位元說明）

**未查證（本輪時間內未取得可引註的一手數字，請勿引用我未標註的數字）**

- CRC32 **軟體**實作在 AVR 上的 cycles/byte：**未查證**。
- 256-entry（1 KB）查表版本佔用的 flash：可推得 **256 × 4 B = 1024 bytes**（純算術），但**未找到明確標示此數字的可引註來源**。
- 是否有其他 AVR 家族（DB/DD/EA/DU 等）具 CRC 周邊與其寬度：僅確認 **DA 有 CRC-16 + CRC-32**；其餘未逐一查證。
- **AVR 上 SHA-256 的 flash/RAM 佔用與速度**：**完全未查證**。（唯一線索是 IACR ePrint 2012/156 "Fast Embedded Software Hashing" 有 AVR 量測，但我未開啟確認數字。）
- 是否有任何 AVR 具 SHA 硬體加速：**未查證**。

---

## 11. 不確定 / 查不到清單（誠實標記）

1. **USART 最高 baud 的官方絕對數字**：datasheet 沒有給單一 max baud，只有 `fBAUD ≤ fCLK_PER` 與 BAUD ≥ 64 的限制。我推得 20 MHz 下 normal ≈1.25 Mbps / double-speed ≈2.5 Mbps，但**這是我的推導，非 datasheet 標示值**。datasheet 唯一明列的高速數字是 **UPDI 的 0.9 Mbps @ 2.7–5.5V**。
2. **ATtiny3216（工業級，非 Automotive）datasheet DS40002147 的 max frequency**：未取得可引註來源。Automotive 版標 16 MHz，與常見說法 20 MHz 不同，這點需要另行確認再引用。
3. **AVR-DB** 的 CRC 多項式清單：只知道「有 CRC」，未逐頁確認是否含 CRC-32。
4. **CRC32 軟體在 AVR 上的每 byte cycles、查表 flash 佔用**：未找到可引註的一手量測來源。1024 bytes 是算術推導。
5. **SHA-256 在 AVR 的 flash/RAM/速度**：完全未查證。
6. **是否有任何 AVR 具 SHA 硬體加速**：未查證。
7. **PlatformIO** 對 ATtiny1616 的官方 board id / platform 頁面：未查證（只知道 megaTinyCore 支援；PlatformIO 論壇有 attiny1616 + jtag2updi 的討論串，但非官方支援聲明）。
8. **avr-libc 加入 tinyAVR 0/1-series header（iotn1616.h）的確切版本**、**Microchip 官方 ATtiny_DFP pack 版本號**、**`-mmcu=attiny1616` 的官方出處**：未查證。
9. **Microchip onlinedocs HTML 版**（`onlinedocs.microchip.com/oxy/GUID-9175774B-...`）在查證時回應 **HTTP 503**；本報告所有 datasheet 引註改用官方 PDF DS40002204A。若需要網頁版引用，需稍後重試。
10. **SPI 在 slave 模式的最高 fSCK**、以及 TWI 在 1 MHz (Fm+) 下的完整時序保證：未逐條查證（只確認電性表 Master fSCK max = 10 MHz）。

---

# 附錄：追加查證（NC4 小節點 + 迷你檔案系統用途）

> **來源取得狀況**：ATtiny3226 專屬 datasheet（DS40002321）在 `ww1.microchip.com` **三次嘗試皆回 HTTP 403**；Microchip onlinedocs（`onlinedocs.microchip.com/oxy/GUID-ACE76F82-...`）**回 HTTP 503**。故本附錄以 **[ATtiny1624/1626/1627 DS40002234A](https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/ATtiny1624-26-27-DataSheet-DS40002234A.pdf)** 為一手來源（該份 datasheet 表 1 明確把 ATtiny3224/3226/3227 列為同 family 的 32 KB 成員）。

## A. tinyAVR 2-series 的 USART

### A1. 是否真的有 2 個 USART？— **是，2 個**
- 表 2 Peripheral Overview（ATtiny1624/1626/1627 欄）明列：**`USART/SPI master  2  2  2`**。
- Features 明列：「**Two** Universal Synchronous Asynchronous Receiver Transmitter (**USART**) with fractional baud rate...」
- 週邊地址圖同時有 `0x0800 USART0` 與 `0x0820 USART1`；中斷向量表有 USART0（17/18/19）與 USART1（26/27/28）兩組 RXC/DRE/TXC。
- 對照 1-series ATtiny1616：**只有 1 個 USART**（表 2-1 與中斷向量表都只有 USART0）。
- 來源：[DS40002234A](https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/ATtiny1624-26-27-DataSheet-DS40002234A.pdf) 表 2、Features、Peripheral Address Map、中斷向量表；[DS40002204A](https://ww1.microchip.com/downloads/en/DeviceDoc/ATtiny1614-16-17-DataSheet-DS40002204A.pdf) 表 2-1、表 7-2

### A2. 同樣有 fractional baud rate generator 嗎？— **有**
- 2-series Features 原文與 1-series **逐字相同**：「Fractional Baud Rate Generator: Can generate the desired baud rate from any peripheral clock frequency / No need for an external oscillator」。
- 兩份 datasheet 的 USART 都在第 24 章，且小節編號同為 `24.3.2.2.1 The Fractional Baud Rate Generator`；BAUD 公式、`S=16 / S=8`、BAUD 最小值 64 的敘述一致。
- 來源：[DS40002234A](https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/ATtiny1624-26-27-DataSheet-DS40002234A.pdf) §24.1 / §24.3.2.2.1

### A3.（重點）硬體 RS-485 / XDIR 自動方向控制？— **有，2 個 USART 都有，與 1-series 相同**
- 2-series USART Features 明列 Half-Duplex Operation: **One-Wire mode / RS-485 mode**。
- Block diagram 訊號表明列 **`XDIR  Output  Transmit enable for RS-485`**。
- Pinout/mux 表把 **XDIR 列為 USART0 與 USART1 的可選輸出腳**（PA4 可為 `XDIR(4)`/`XDIR`、PB0 為 `XDIR`、PC3 為 `XDIR(4)`）；`USARTROUTEA` 暫存器分別選擇 USART1[1:0] / USART0[1:0] 的 TxD/RxD/XCK/**XDIR** 腳位。
- → 對 NC4 小節點的意涵：**RS-485 半雙工的 DE 方向控制由硬體自動處理**，不需軟體 toggle GPIO；且 2-series 有 2 個 USART，可同時接 RS-485 匯流排與一個 console/除錯埠。
- 來源：[DS40002234A](https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/ATtiny1624-26-27-DataSheet-DS40002234A.pdf) §24.1、§24.2.2 Signal Description、§5.1、§15（USARTROUTEA）

### A4.（重點）RX 緩衝深度有升級嗎？— **沒有，仍是 2 級**
- 2-series Overview 原文：「The transmitter consists of a **single-write buffer**, a Shift register... The receiver consists of a **two-level receive buffer** and a Shift register.」→ 與 1-series **逐字相同**。
- RXDATAH 說明再次確認：「The receive buffer consists of a **two-level buffer**」；溢位條件明寫「a buffer overflow occurs when the receive buffer is full (**two characters**)」。
- 全份 2-series datasheet 中 “FIFO” 同樣**不出現在 USART**（僅 SPI Master Buffer mode 的 two-entry RX FIFO）。
- → **結論：2-series 的 USART 沒有任何 RX FIFO 深度升級。** 高速 baud 下「邊寫 NVM 邊收封包」只有約 2 個字元時間的餘裕，這是硬體層級硬限制。
- 來源：[DS40002234A](https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/ATtiny1624-26-27-DataSheet-DS40002234A.pdf) §24.2 Overview、§24.3.2.4.4 RXDATAH

### A5. 1-series vs 2-series USART 主要差異（可查證者）

| 項目 | 1-series (ATtiny1616) | 2-series (ATtiny1626/3226/1627) |
|---|---|---|
| USART 數量 | **1** | **2**（USART0 + USART1） |
| Fractional baud generator | 有 | 有（同設計） |
| RS-485 / XDIR 硬體方向控制 | 有 | 有（每個 USART 皆有 XDIR） |
| TX 緩衝 | single-write buffer | single-write buffer（相同） |
| RX 緩衝 | two-level | two-level（相同） |
| 訊框/錯誤檢測/IRCOM/LIN slave/auto-baud | 有 | 有（Features 逐字相同） |
| 腳位路由 | `PORTMUX` 個別控制 | `USARTROUTEA`（USART0/USART1 各 2 bit） |

來源：同 A1–A4。

**A. 未查證**：ATtiny3226/3227 專屬 datasheet（DS40002321）取不到（403/503），故「32 KB 版本本身」的 USART 與記憶體表我未直接讀到；上列數字來自同 family 共同 datasheet DS40002234A。若需精確引用 3226 本體，請另行取得該 PDF。

---

## B. Flash / EEPROM 寫入行為

### B1. Page size 與頁數

**1-series ATtiny1616**（[DS40002204A](https://ww1.microchip.com/downloads/en/DeviceDoc/ATtiny1614-16-17-DataSheet-DS40002204A.pdf) 表 6-1 / 6-3）：

| 項目 | Flash | EEPROM |
|---|---|---|
| Size | 16 KB | 256 B |
| **Page size** | **64 B** | **32 B** |
| **Number of pages** | **256** | **8** |
| Start addr（data space） | 0x8000 | 0x1400 |

**2-series ATtiny1624/1626/1627**（[DS40002234A](https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/ATtiny1624-26-27-DataSheet-DS40002234A.pdf) 表 7-1 / 7-3）：**與 1-series 完全相同** → Flash **64 B/page、256 pages**；EEPROM **32 B/page、8 pages**；SRAM 2 KB @0x3800。

> **ATtiny3226（32 KB）的 Flash page size：未查證**（DS40002321 取不到）。32 KB ÷ 256 pages 暗示可能 128 B/page，但**無來源，請勿引用此推測**。

### B2. 一次 page erase+write 要多久

兩份 datasheet 的 Programming Times 表**逐項相同**（1-series 表 36-38；2-series 表 33-34）：

| 符號 | 典型時間 |
|---|---|
| Page Buffer Clear (PBC) | **7 個 CLK_CPU cycles** |
| Page Write (WP) | **2 ms** |
| Page Erase (ER) | **2 ms** |
| **Page Erase-Write (ERWP)** | **4 ms** |
| Chip Erase (CHER) | 4 ms |
| EEPROM Erase (EEER) | 4 ms |

- → 寫一頁 Flash（64 B）：已抹除時 **WP = 2 ms**；一般情況 **ERWP = 4 ms**。換算 **4 ms / 64 B ≈ 62.5 µs per byte**，期間 CPU 停擺。
- 來源：[DS40002204A](https://ww1.microchip.com/downloads/en/DeviceDoc/ATtiny1614-16-17-DataSheet-DS40002204A.pdf) §36.22；[DS40002234A](https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/ATtiny1624-26-27-DataSheet-DS40002234A.pdf) §33.20

### B3.（重點）寫 flash 期間 CPU 是否被 halt？RWW 規則

**1-series 與 2-series 文字完全相同**（1-series §9.3.2.4.x、2-series §10.3.2.4.x）：

- **Write Page (WP)**：「**If the write is to the Flash, the CPU will stop executing code as long as the Flash is busy with the write operation.** If the write is to the EEPROM, **the CPU can continue executing code** while the operation is ongoing.」
- **Erase Page (ER)**：「For erasing the Flash... **The CPU will be halted while the erase is ongoing.** For the EEPROM... **The CPU can continue running code** while the operation is ongoing.」
- **Erase and Write Page (ERWP)**：「**When executed on the Flash, the CPU will be halted** when the operations are ongoing. When executed on EEPROM, the CPU can continue executing code.」
- Page Buffer Clear：CPU 停 **7 個 CPU cycles**。EEPROM Erase (EEER)：CPU 停。

→ **結論：對 Flash 做 WP / ER / ERWP，CPU 一律被 halt 整個操作期間（2 ms 或 4 ms）。**

**RWW（read-while-write）**：
- **兩份 datasheet 全文都沒有 “read-while-write” / “RWW” 字樣**，也沒有任何「一面寫 flash 一面從 flash 取指」的支援敘述；相反的敘述是明確的 CPU halt。2-series 記憶體章節另明寫「**It is not possible to execute code from SRAM.**」→ flash 忙時不能取指、又不能改從 SRAM 跑，實質上**沒有 RWW**。
- **「BOOT section 的 RWW 特別規則」：查無此敘述**（datasheet 對 BOOT 區的規則是**寫入權限**與 BOOTLOCK/APCWP，不是 RWW 例外）→ **標為未查證/不適用**。
- 對中斷的實務影響（**由 datasheet 敘述推得，非原文**）：halt 期間中斷不會被服務，但**週邊硬體仍在跑**；USART 是非同步週邊、有自己的移位與 2 級接收緩衝，所以 2–4 ms 的 flash 寫入期間位元仍會收進 shift register / 2 級緩衝，只是沒人讀 RXDATA。以 115200 baud 為例一字元約 87 µs，2 級緩衝 ≈ 174 µs 就滿 → **該次寫入期間必然掉資料**。這是 NC4 小節點最關鍵的硬限制。
- 來源：[DS40002204A](https://ww1.microchip.com/downloads/en/DeviceDoc/ATtiny1614-16-17-DataSheet-DS40002204A.pdf) §9.3.2.4；[DS40002234A](https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/ATtiny1624-26-27-DataSheet-DS40002234A.pdf) §10.3.2.4、§7.3

### B4.（重點）從 Application section 能不能自我編程？

**兩份 datasheet 的 “Inter-Section Write Protection” 逐字相同**：

> - The code in the **BOOT section can write to APPCODE and APPDATA**
> - The code in **APPCODE can write to APPDATA**
> - The code in **APPDATA cannot write to Flash or EEPROM**
>
> The CPU **can never write to the BOOT section**.

→ **答案：可以，但要看你寫哪一區。**
1. 從 **APPCODE 執行**：**可以**寫 **APPDATA**（= 資料區／迷你檔案系統），**不能**改寫自己的 APPCODE 程式碼區。
2. 從 **BOOT section 執行**：可以寫 APPCODE 與 APPDATA（= 典型 bootloader 更新韌體的能力）。
3. **APPDATA 裡的程式不能**寫 Flash 或 EEPROM。
4. **任何人都不能寫 BOOT section**（連 bootloader 自己也不行）。

關鍵陷阱：**若 `FUSE.BOOTEND = 0x00` 且 `FUSE.APPEND = 0x00`，整塊 Flash 被視為 BOOT section**（原文：「A value of 0x00 defines the whole Flash as the BOOT section. When both FUSE.APPEND and FUSE.BOOTEND are 0x00, the entire Flash is the BOOT section.」）→ 此時 **CPU 無法對 Flash 自我編程**（BOOT 不可寫）。要用 APPDATA 就必須先定義 BOOT section。

自我編程前置條件（兩系列相同）：寫 NVM 命令前先對 `CPU.CCP` 寫入 **SPM signature `0x9D`**，並在**接下來 4 個指令內**寫 `NVMCTRL.CTRLA.CMD`；且需先確認 `NVMCTRL.STATUS` 的 FBUSY/EBUSY 為 0。

- 來源：[DS40002204A](https://ww1.microchip.com/downloads/en/DeviceDoc/ATtiny1614-16-17-DataSheet-DS40002204A.pdf) §9.3.1.1、§9.2.2（p.60–64）、§8.5.7（CCP / SPM 0x9D）；[DS40002234A](https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/ATtiny1624-26-27-DataSheet-DS40002234A.pdf) §10.3.1、§6.5.6.2、§7.3

### B5. EEPROM 寫入時間與是否 halt
- **EEPROM 寫入不 halt CPU**：WP / ER / ERWP 在 EEPROM 上皆明寫「the CPU can continue executing code / running code while the operation is ongoing」。
- **但整顆 EEPROM 抹除（EEER = 4 ms）會 halt CPU。**
- EEPROM 為 **byte granularity on erase/write**：「Within one page, only the bytes marked to be updated will be erased/written.」→ 寫 1 byte 不會抹掉整頁 32 B。
- **datasheet 沒有給「單 byte EEPROM 寫入」的獨立毫秒數**（Programming Times 表只有 EEPROM Erase = 4 ms）→ **未查證**。
- → 對迷你檔案系統的意涵：**EEPROM 是唯一「寫入時不停 CPU」的選項**，但只有 256 B、耐久 100 k cycles，不適合當檔案系統主體（此為設計取捨，非 datasheet 數字）。
- 來源：同 B2/B3。

### B6. Flash 抹寫耐久

| | 1-series (ATtiny1616) | 2-series (ATtiny1624/26/27) |
|---|---|---|
| **Flash** | **10,000 cycles** | **10,000 cycles** |
| **EEPROM** | **100,000 cycles** | **100,000 cycles** |
| 資料保存 | 40 years @ 55°C | 40 years @ 55°C |

兩系列 Features 逐項相同。**ATtiny3226 的耐久數字：未查證**（DS40002321 取不到）。
來源：[DS40002204A](https://ww1.microchip.com/downloads/en/DeviceDoc/ATtiny1614-16-17-DataSheet-DS40002204A.pdf) Features；[DS40002234A](https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/ATtiny1624-26-27-DataSheet-DS40002234A.pdf) Features

**B. 未查證**：ATtiny3226/3227 的 Flash page size／頁數／耐久；單 byte EEPROM 寫入時間；「無 RWW」是否在 datasheet 明文否定（我只確認全文無該字樣，屬反證式結論）；USART 在 flash halt 期間的硬體行為（無明文，我的推論基於非同步週邊 + 2 級緩衝）。

---

## C. ATtiny3226 供貨 / 單價概況（查詢日期：**2026-10-11**）

- **LCSC `ATTINY3226-SF`（SOIC-20-300mil, C3234894）**：現貨 **32 pcs**；階梯價 **$2.1516 @1、$2.1012 @10、$2.0670 @30、$2.0345 @100**（USD，未稅）。來源：[LCSC ATTINY3226-SF](https://www.lcsc.com/product-detail/Microcontrollers-MCU-MPU-SOC_Microchip-Tech-ATTINY3226-SF_C3234894.html)（直接抓取該頁 JSON，非目錄標價）
- **LCSC `ATTINY3226-SU`**：階梯價 **$2.9533 @1 → $2.5106**（@10，後續更低）。來源同上頁面之同頁變體資料。
- **Unikeyic `ATTINY3226-SF`**：標示 **Out of Stock**（需詢價）；同頁規格 32 KB Flash / 3 KB RAM / 256 B EEPROM / 20 MHz / 20-SOIC。來源：[Unikeyic ATTINY3226-SF](https://www.unikeyic.com/products/microcontrollers/attiny3226-sf/33812026.html)
- **DigiKey / Mouser 報價頁取不到數字**：兩者皆**回 HTTP 403**（反爬）。來源（僅證明料號存在）：[DigiKey ATTINY3226-SF](https://www.digikey.com/en/products/detail/microchip-technology/ATTINY3226-SF/15520471)、[Mouser ATtiny3224/3226/3227 專區](https://www.mouser.com/c/semiconductors/embedded-processors-controllers/microcontrollers-mcu/?marcom=170428930)
- **ATtiny3227（24-pin）供貨/單價：未查證**。
- ⚠️ 價格與庫存為**查詢當日快照**，會變動；引用時請標註日期。
