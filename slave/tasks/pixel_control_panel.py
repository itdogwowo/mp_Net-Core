"""
pixel_control_panel.py — Pixel 模式控制面板 Task

與 ControlPanelTask 對稱的面板裝置任務：
  ControlPanelTask       消費 bus.shared["_display_cmd"] → 廣播 0x1501 WTT_CTL
  PixelControlPanelTask  消費 bus.shared["_pixel_cmd"]    → 廣播 0x3105 MODE_SET

角色：面板裝置（LCD + encoder + 按鍵 + LVGL）。LVGL 頁面（ui/lvgl/page/pixel_controller.py）
只把操作寫進 bus.shared["_pixel_cmd"]（純狀態），本 task 消費一次（讀後清 None）後，
把模式 id 拆成 (mode_type, mode_id) 直接廣播 MODE_SET 給執行裝置。

「大家各自讀」：面板不驗證該模式是否存在（0x0200 是執行裝置/motor 的模式，面板
自己沒有），只負責把使用者的模式選擇「發送到對方」；執行裝置收到 MODE_SET 後，
由它自己的 pixel_actions/gmode 讀它自己的模式池去執行。

發送時機：每次 _pixel_cmd 有值就發（= 使用者操作的那一刻）。不做同值去重，與
ControlPanelTask 的「每次操作都是新的 request」語意一致。

_pixel_cmd 欄位（一次性 dict，消費後清 None）：
  {"mode": <16-bit id>}   發送 MODE_SET，id = (mode_type<<8)|mode_id（0x0200=SERVO mode0）
  {"stop": True}          發送 MODE_STOP（action=1 全關閉）
  {"brightness": 0-255}   本板亮度（寫 st_pixel，同 MODE_SET.brightness）
"""
import struct
from lib.sys.task import Task
from lib.sys.sys_bus import bus
from lib.sys.proto import Proto
from lib.sys.log_service import get_log

CMD_MODE_SET = 0x3105
CMD_MODE_STOP = 0x3106

# 可動（鐵打模式）：SERVO 組(mode_type=2) mode 0 → 16-bit id 0x0200。臨時應急。
MOVABLE_ID = 0x0200


class PixelControlPanelTask(Task):
    log_schema = []

    def __init__(self, name, ctx):
        super().__init__(name, ctx)
        self._now_bus = None
        self._st_pixel = None

    def on_start(self):
        super().on_start()
        # NowBus 由 NowTask/NetworkTask 建立(ESP-NOW 已 active)；st_pixel 由 pixel_drv 建立。
        self._now_bus = bus.get_service("NowBus")
        self._st_pixel = bus.get_service("st_pixel")
        get_log().info("[PixelCtl] online (now={}, st_pixel={})".format(
            "ok" if self._now_bus else "none",
            "ok" if self._st_pixel else "none"))

    def _ensure_services(self):
        if self._now_bus is None:
            self._now_bus = bus.get_service("NowBus")
        if self._st_pixel is None:
            self._st_pixel = bus.get_service("st_pixel")

    def _broadcast_mode_set(self, mid):
        """把 16-bit id 拆成 (mode_type, mode_id) → 廣播 MODE_SET。
        不驗證模式是否存在：執行裝置各自讀各自的模式池。"""
        mid = int(mid) & 0xFFFF
        mode_type = (mid >> 8) & 0xFF
        mode_id = mid & 0xFF
        payload = struct.pack("<BBHB", mode_type, mode_id, 0, 0xFF)  # delay=0, bri=不設置
        if self._now_bus is not None:
            self._now_bus.broadcast(Proto.pack(CMD_MODE_SET, payload))
        get_log().info("[PixelCtl][TX][0x3105] type={} id={} (0x{:04X})".format(
            mode_type, mode_id, mid))

    def _apply(self, cmd):
        """消費一筆 _pixel_cmd → 轉發成 ESP-NOW 指令。"""
        if not isinstance(cmd, dict):
            return
        if cmd.get("stop"):
            if self._now_bus is not None:
                self._now_bus.broadcast(Proto.pack(CMD_MODE_STOP, struct.pack("<B", 1)))
            get_log().info("[PixelCtl][TX][0x3106] stop action=1")
            return
        if "mode" in cmd:
            self._broadcast_mode_set(cmd["mode"])
        if "brightness" in cmd:
            v = max(0, min(255, int(cmd["brightness"])))
            if self._st_pixel is not None and hasattr(self._st_pixel, "set_brightness"):
                self._st_pixel.set_brightness(v)
                get_log().info("[PixelCtl] ● brightness={}".format(v))

    def _consume_pca_actions(self):
        """消費 LVGL PCA 頁的請求佇列：`_pca_actions` → 動作。

        目前只實作 {"action":"scan"}：掃描本板 I2C → 寫 bus.shared["_i2c_devices"]，
        再同步 `pca9685_ctl`（UI 控制表）→ 頁面的左欄清單會跟著增減（熱插拔）。

        ★ 掃描放在 task 而不是 LVGL 執行緒：`I2C.scan()` 最壞數百 ms，會卡畫面。
        ★ 頁面已改成「直接操作 `pca9685_ctl` 的 PCA9685 物件」：`_pca_actions`
          不再有 `start` / `stop`（那是舊版頁面留下來的，兩者本來就無人實作）。
          播放端 `pca9685_list` 不受掃描影響，熱插拔的板子只支援手動控制。
        """
        q = bus.shared.get("_pca_actions")
        if not q:
            return
        bus.shared["_pca_actions"] = None      # 一次性消費（讀後清）
        events = q if isinstance(q, list) else ()
        for ev in events:
            if not (isinstance(ev, dict) and ev.get("action") == "scan"):
                continue
            try:
                from driver.i2c_drv import scan_devices
                get_log().info("[PixelCtl] I2C scan: {}".format(scan_devices()))
            except Exception as e:
                get_log().error("[PixelCtl] i2c scan err: {}".format(e))
                continue
            try:
                from driver.pca9685_drv import refresh_pca_ctl
                refresh_pca_ctl(bus)
            except Exception as e:
                get_log().error("[PixelCtl] pca ctl refresh err: {}".format(e))

    def loop(self):
        if not self.running:
            return
        self._consume_pca_actions()
        cmd = bus.shared.get("_pixel_cmd")
        if not cmd:
            return
        bus.shared["_pixel_cmd"] = None   # 一次性消費（讀後清）
        self._ensure_services()
        try:
            self._apply(cmd)
            self.success += 1
        except Exception as e:
            get_log().error("[PixelCtl] apply err: {}".format(e))

    def on_stop(self):
        super().on_stop()
        self._now_bus = None
        self._st_pixel = None
