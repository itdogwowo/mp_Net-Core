"""
mp3_tf_16p.py — DFPlayer Mini (MP3-TF-16P) 串列驅動
MicroPython 用，透過 UART 發送 10-byte 指令幀。

硬體接線:
    VCC  → 5V (或 3.3V，依模組規格)
    GND  → GND
    RX   → ESP32 TX
    TX   → ESP32 RX (可不接，不回讀狀態)
"""

import time

# ── 指令碼 ──
_CMD_NEXT       = 0x01
_CMD_PREV       = 0x02
_CMD_PLAY_TRACK = 0x03
_CMD_VOL_UP     = 0x04
_CMD_VOL_DOWN   = 0x05
_CMD_SET_VOL    = 0x06
_CMD_SET_EQ     = 0x07
_CMD_LOOP_TRACK = 0x08
_CMD_SEL_DEVICE = 0x09
_CMD_SLEEP      = 0x0A
_CMD_WAKE       = 0x0B
_CMD_RESET      = 0x0C
_CMD_PLAY       = 0x0D
_CMD_PAUSE      = 0x0E
_CMD_PLAY_FOLDER= 0x0F
_CMD_VOL_ADJUST = 0x10
_CMD_STOP       = 0x16
_CMD_REPEAT     = 0x11

# ── 儲存裝置參數 ──
DEVICE_U   = 1   # USB (U 碟)
DEVICE_TF  = 1   # SD / TF 卡  (DFPlayer 手冊: 0x01=TF, 部分版本 0x02=USB/SD)
DEVICE_SD  = 2
DEVICE_FLASH = 4

class MP3TF16P:
    """DFPlayer Mini 串列控制"""

    def __init__(self, uart):
        """
        uart: machine.UART 實例，需已初始化 (baud=9600)
        """
        self._uart = uart

    def _send_cmd(self, cmd, param=0, feedback=0):
        """發送 10-byte 指令幀"""
        param_h = (param >> 8) & 0xFF
        param_l = param & 0xFF
        checksum = 0xFFFF - (0xFF + 0x06 + cmd + feedback + param_h + param_l) + 1
        chk_h = (checksum >> 8) & 0xFF
        chk_l = checksum & 0xFF

        frame = bytes([
            0x7E,       # 起始
            0xFF,       # 版本
            0x06,       # 長度
            cmd,        # 指令
            feedback,   # 回饋 (0=無, 1=有)
            param_h,
            param_l,
            chk_h,
            chk_l,
            0xEF,       # 結束
        ])
        self._uart.write(frame)
        # 幀間延遲 — DFPlayer 在 9600 baud 下約需 ~10ms
        time.sleep_ms(15)

    # ═══ 公開方法 ═══

    def switch_drive(self, device):
        """
        選擇儲存裝置。
        device: DEVICE_U/TF (1), DEVICE_SD (2), DEVICE_FLASH (4)
        """
        self._send_cmd(_CMD_SEL_DEVICE, param=device)
        time.sleep_ms(200)

    def play_track(self, track):
        """
        播放指定編號曲目 (1-based)。
        track: 1 ~ 3000
        """
        self._send_cmd(_CMD_PLAY_TRACK, param=track)

    def loop_track(self, track):
        """
        循環播放指定編號曲目。
        track: 1 ~ 3000
        """
        self._send_cmd(_CMD_LOOP_TRACK, param=track)

    def stop(self):
        """停止播放"""
        self._send_cmd(_CMD_STOP)

    def pause(self):
        """暫停"""
        self._send_cmd(_CMD_PAUSE)

    def play(self):
        """繼續播放 (從暫停恢復)"""
        self._send_cmd(_CMD_PLAY)

    def next(self):
        """下一首"""
        self._send_cmd(_CMD_NEXT)

    def prev(self):
        """上一首"""
        self._send_cmd(_CMD_PREV)

    def set_volume(self, vol):
        """
        設定音量。
        vol: 0 ~ 30
        """
        v = max(0, min(30, vol))
        self._send_cmd(_CMD_SET_VOL, param=v)

    def volume_up(self):
        """音量 +1"""
        self._send_cmd(_CMD_VOL_UP)

    def volume_down(self):
        """音量 -1"""
        self._send_cmd(_CMD_VOL_DOWN)

    def set_eq(self, eq):
        """
        設定等化器模式。
        eq: 0=Normal, 1=Pop, 2=Rock, 3=Jazz, 4=Classic, 5=Bass
        """
        self._send_cmd(_CMD_SET_EQ, param=eq)

    def reset(self):
        """重設模組"""
        self._send_cmd(_CMD_RESET)
        time.sleep_ms(1500)

    def sleep(self):
        """進入休眠 (低功耗)"""
        self._send_cmd(_CMD_SLEEP)

    def wake(self):
        """從休眠喚醒"""
        self._send_cmd(_CMD_WAKE)

    def repeat(self, enable):
        """
        設定循環模式。
        enable: True=循環播放當前曲目, False=停止循環
        """
        self._send_cmd(_CMD_REPEAT, param=1 if enable else 0)

    def play_folder(self, folder, track):
        """
        播放指定資料夾中指定曲目。
        folder: 資料夾編號 (01~99)
        track:  曲目編號 (1~255)
        """
        param = (folder << 8) | (track & 0xFF)
        self._send_cmd(_CMD_PLAY_FOLDER, param=param)
