"""
uart_motor.py — UART 電機控制器（單一物件管理全部）

對齊 PixelController 的集中處理風格：
  - 初始化時設定 version（指令方法標記）與 addresses（我控制的全部台）
  - 內部統一緩衝區 buffer，show_all() 一次 uart.write 推送整幀
  - version 分派：協定由外部維護、硬體可能混合版本，
    依 version 選用對應的 frame 編碼方式。對方改協定時，
    註冊新編碼器 + 改 config 的 version 即可切換，不碰核心代碼。

行程模式（疊加在速度控制之上，無回饋、估算用）：
  - 位置用整數 0..4095 表達全程：0 = 全收，4095 = 全伸
  - 速度用整數 0..128：128 = 全速、64 = 中速（= 舊 FWD/REV）、0 = 停
  - 每台記錄啟動位置/時間/速率，update() 用時間差閉式重算（非阻塞、無 sleep）
  - 速度→位移換算：rate = rate_full * speed / 128，rate_full 由「全速全程時間」得出，
    可 calibrate() 記錄實測值做多段速度校準

用法:
    from lib.hw.uart_motor import UartMotor

    motor = UartMotor({
        'version': 1,          # 指令方法標記
        'addresses': [1, 2, 3],# 我控制的全部台
        'uart': uart,          # 共用同一條 UART
        # 校準（選項）：{address: {speed: 全程ms}}，正反同值，缺的台/速度用線性補
        'calib': {
            1: {128: 3000},                       # 只給全速一項
            2: {24: 24000, 64: 12000, 128: 6000}, # 低/中/高三點
        },
    })

    motor.set(2, FWD)          # 更新緩衝（不發送）
    motor.show_all()           # FF 00 80 40 80 FE — 一次 uart.write 推送全部
    motor.send(3, REV)         # set + 立即發送單台
    motor.stop_all()           # 全部停止

    # 行程模式（速度控制之上的位置估算）
    motor.move_to(1, 2048, SPEED_MED)  # 中速伸到半程（只改 buffer，方向自動）
    motor.show_all()                    # 送出速度幀
    # ... 之後週期呼叫 update() 結算、到達即停
    pos = motor.position(1)             # 0..4095
"""

try:
    import micropython
    _MP = True
except ImportError:
    micropython = None
    _MP = False

import time as _time

try:
    _default_clock_ms = _time.ticks_ms
    _default_clock_diff = _time.ticks_diff
except AttributeError:
    # CPython 沒有 ticks_ms；用 monotonic 毫秒（單調不回繞，差值即相減）
    def _default_clock_ms():
        return _time.monotonic_ns() // 1_000_000

    def _default_clock_diff(a, b):
        return a - b

# === 協定常數（v1 = 參考文件 UART-412 格式）===
HEADER = 0xFF
ENDING = 0xFE

STOP   = 0x80   # 停止
FWD    = 0x40   # 正轉中速（0x00 全速 ~ 0x7F 停止）
REV    = 0xC0   # 反轉中速（0x80 停止 ~ 0xFF 全速）
FWD_FS = 0x00   # 正轉全速
REV_FS = 0xFF   # 反轉全速

# === 行程模式常數 ===
SPEED_MAX = 128   # 全速（byte 0x00 正轉 / 0xFF 反轉）
SPEED_MED = 64    # 中速（等於舊 FWD/REV）
SPEED_MIN = 1     # 最慢可用速度；以下（含 0）視為停止
SPEED_STOP = 0

POS_MAX = 0x0FFF  # 4095，滿行程
_Q      = 12      # 定點小數位數（對外位置 = 內部值 >> _Q，得 0..4095）

DEFAULT_T_FULL_MS = 3000  # 全速走完全程的預設 ms（需依實物校準）


def speed_to_byte(speed, direction):
    """speed 0..128 + 方向（>=0 伸 / <0 縮）→ UART 速度 byte（v1 格式）。"""
    speed = int(speed)
    if speed <= 0:
        return STOP
    if speed > SPEED_MAX:
        speed = SPEED_MAX
    if direction >= 0:
        b = 0x80 - speed           # 128→0x00 全速正轉、64→0x40 中速
        return b if b >= 0 else 0x00
    b = 0x80 + speed               # 128→0xFF 全速反轉（截斷上限）、64→0xC0
    return b if b <= 0xFF else 0xFF


# === 指令方法（version → frame 編碼器）===
#
# 編碼器介面（frame 由 UartMotor 預建、每幀重複用，避免每幀分配）:
#   broadcast(frame, buffer, n)  — 把 buffer[0..n-1] 填進廣播 frame 含 HEADER/ENDING
#   single(frame, addr, value)   — 把 4-byte 單台 frame 填好
# 需 viper 加速的版本放 device 上跑；PC 測試走 python fallback。

_COMMAND_METHODS = {}


if _MP:

    @micropython.viper
    def _build_v1_broadcast(frame, buffer, n: int):
        """FF 00 V1 V2 ... VN FE"""
        pf: ptr8 = ptr8(frame)
        pb: ptr8 = ptr8(buffer)
        pf[0] = 0xFF
        pf[1] = 0x00
        i: int = 0
        while i < n:
            pf[2 + i] = pb[i]
            i += 1
        pf[2 + n] = 0xFE

    @micropython.viper
    def _build_v1_single(frame, addr: int, value: int):
        """FF addr value FE"""
        pf: ptr8 = ptr8(frame)
        pf[0] = 0xFF
        pf[1] = addr
        pf[2] = value
        pf[3] = 0xFE

else:

    def _build_v1_broadcast(frame, buffer, n):
        frame[0] = 0xFF
        frame[1] = 0x00
        for i in range(n):
            frame[2 + i] = buffer[i]
        frame[2 + n] = 0xFE

    def _build_v1_single(frame, addr, value):
        frame[0] = 0xFF
        frame[1] = addr
        frame[2] = value
        frame[3] = 0xFE


def register_command_method(version, build_broadcast, build_single):
    """註冊一個新的指令方法，供協定改版時切換。

    對方改協定 → 用這個註冊新編碼器，之後初始化時把 version 指過去即可，
    同一台 device 可同時跑不同 version 的 UartMotor（混合硬體版本）。
    """
    _COMMAND_METHODS[int(version)] = {
        "broadcast": build_broadcast,
        "single": build_single,
    }


register_command_method(1, _build_v1_broadcast, _build_v1_single)


class UartMotor:
    """UART 電機控制器 — 單一物件管理全部電機。

    初始化即鎖定 version（指令方法）與 addresses（控制台數），
    內部集中 buffer，硬體輸出統一走 show_all() 的一次 uart.write。

    行程模式（move_to / move / position / update / calibrate）疊在速度控制之上：
    全程以整數 0..4095 表達，速度以 0..128 表達（方向由目標自動決定），
    無回饋估算位置；只改 buffer，實際輸出仍統一走 show_all()。
    """

    def __init__(self, cfg):
        self.version = int(cfg.get("version", 1))
        self.uart = cfg.get("uart")
        if self.uart is None:
            raise ValueError("UartMotor: 需要 uart 實例")

        raw = cfg.get("addresses")
        if raw is None:
            raise ValueError("UartMotor: 需要 addresses（我控制的全部台）")
        if isinstance(raw, int):
            raw = [raw]
        addresses = sorted({int(a) for a in raw})
        if not addresses:
            raise ValueError("UartMotor: addresses 不能為空")
        for a in addresses:
            if not 1 <= a <= 255:
                raise ValueError("UartMotor: address 必須在 1-255: {}".format(a))
        self.addresses = addresses
        self._address_set = set(addresses)

        method = _COMMAND_METHODS.get(self.version)
        if method is None:
            raise ValueError(
                "UartMotor: 未知 version {}（可用: {}）。協定由外部維護，"
                "改版請用 register_command_method() 註冊後再切換。".format(
                    self.version, sorted(_COMMAND_METHODS)))
        self._build_broadcast = method["broadcast"]
        self._build_single = method["single"]

        # 廣播欄位數 = 最大 address（參考文件 8 台 = addr 1..8）。
        # 未控制的 position 固定 STOP（0x80），安全預設。
        self.num_devices = max(addresses)
        self.buffer = bytearray(self.num_devices)
        self._tx_broadcast = bytearray(self.num_devices + 3)
        self._tx_single = bytearray(4)
        self.set_all(STOP)

        # 中性值（停止/歸零時回到的數值）：config 的 dStay（default Stay, 12-bit）
        # → big_buffer 8-bit。預設 2048 = 0x80 死區停（UART-412 的 0 = 全速正轉！）。
        dstay = int(cfg.get("dStay", self.DEFAULT_DSTAY))
        self.neutral_value = (dstay >> 4) & 0xFF

        # ── 行程模式狀態（每台一份，索引 addr-1，與 buffer 同構）──
        # 全部整數；_pos/_pos0/_target 為 Q 定點，對外位置 = 值 >> _Q（0..4095）
        self._pos    = [0] * self.num_devices
        self._target = [None] * self.num_devices
        self._pos0   = [0] * self.num_devices
        self._t0     = [0] * self.num_devices
        self._rate   = [0] * self.num_devices   # 正=伸、負=縮、0=停（Q 定點，格/ms）

        # 時鐘可注入（PC 測試用假時鐘；預設 MicroPython ticks_ms / CPython monotonic）
        self._clock = cfg.get("clock", _default_clock_ms)
        self._clock_diff = cfg.get("clock_diff", _default_clock_diff)

        # ── 校準狀態（每台一份，key = address；不同推桿各自校準）──
        # t_full / rate_full 帶 address 維度；speed 校準表也按 address 分開。
        self._t_full_fwd_ms = {}
        self._t_full_rev_ms = {}
        self._rate_full_fwd = {}
        self._rate_full_rev = {}
        self._rate_fwd = {}   # address → {speed: rate}
        self._rate_rev = {}

        t_default = int(cfg.get("t_full_ms", DEFAULT_T_FULL_MS))

        def _resolve(v, default):
            """int → 全部台同值；dict {addr: ms} → 逐台覆蓋，缺漏用 default。"""
            if v is None:
                return {a: default for a in addresses}
            if isinstance(v, dict):
                return {a: int(v.get(a, default)) for a in addresses}
            val = int(v)
            return {a: val for a in addresses}

        self._t_full_fwd_ms = _resolve(cfg.get("t_full_fwd_ms"), t_default)
        self._t_full_rev_ms = _resolve(cfg.get("t_full_rev_ms"), t_default)
        for a in addresses:
            self._rate_full_fwd[a] = (POS_MAX << _Q) // self._t_full_fwd_ms[a]
            self._rate_full_rev[a] = (POS_MAX << _Q) // self._t_full_rev_ms[a]
            self._rate_fwd[a] = {}
            self._rate_rev[a] = {}

        self._apply_calib_config(cfg)

    # ── 初始化輔助：解析 'calib' 校準配置（選項）──

    def _apply_calib_config(self, cfg):
        """套用 cfg 的 'calib' 校準資料（可選）。

        固定格式：{address: {speed: 全程ms}}，正反同值。
        每台可給任意速度點（例如只給 {128:3000} 或低/中/高三點），
        speed=128 順便同步該台全速全程時間（t_full）。
        """
        calib = cfg.get("calib")
        if calib is None:
            return
        if not isinstance(calib, dict):
            raise ValueError("UartMotor: calib 格式應為 {address: {speed: 全程ms}}")
        for addr, table in calib.items():
            addr = int(addr)
            if addr not in self._address_set:
                raise ValueError(
                    "UartMotor: calib address {} 不在控制列表 {}".format(
                        addr, self.addresses))
            if not isinstance(table, dict):
                raise ValueError(
                    "UartMotor: calib[{}] 格式應為 {{speed: 全程ms}}".format(addr))
            for s, ms in table.items():
                s = int(s)
                ms = int(ms)
                # 正反同值；speed=128 同步 t_full（calibrate 只填表，不設 rate_full）
                self.calibrate(addr, 1, s, ms)
                self.calibrate(addr, -1, s, ms)
                if s == SPEED_MAX:
                    self.set_t_full(addr, 1, ms)
                    self.set_t_full(addr, -1, ms)

    # ── 緩衝區操作（只更新 buffer，不碰硬體）──

    def set(self, addr, value):
        """設定單台目標值（不發送）。addr 必須在初始化指定的 addresses 內。"""
        addr = int(addr)
        if addr not in self._address_set:
            raise ValueError(
                "UartMotor: address {} 不在控制列表 {}".format(addr, self.addresses))
        self.buffer[addr - 1] = value & 0xFF

    def set_all(self, value):
        """全部設備設為同一個值（不發送）。"""
        v = value & 0xFF
        for i in range(self.num_devices):
            self.buffer[i] = v

    def set_many(self, values):
        """批量設定。
        dict  → {addr: value} 只更新指定台
        list  → 長度必須等於 num_devices，逐位填（含未控制位置）
        """
        if isinstance(values, dict):
            for addr, v in values.items():
                self.set(addr, v)
        else:
            if len(values) != self.num_devices:
                raise ValueError(
                    "UartMotor: set_many 長度 {} != num_devices {}".format(
                        len(values), self.num_devices))
            for i, v in enumerate(values):
                self.buffer[i] = v & 0xFF

    def get_write_view(self):
        """零拷貝入口：外部（如 hub read_into / action）直接寫 buffer。"""
        return memoryview(self.buffer)

    # ── 硬體輸出（統一發送）──

    # 死區（停）值：UART-412 的 updateMotor 中 value=0 → PWM 254（全速正轉！），
    # value=128(0x80) → 兩腳 PWM 都 0（死區停）。所以「停/歸零」用 0x80（中性值），
    # 由 render 的 clear_all()/stop_motors() 明確寫入再推幀；效果寫 "w" 時 0x00
    # 是有效命令（全速收），st_load_and_convert 原樣收下、不改寫。
    # dStay（default Stay，12-bit，config 可覆寫）：停止/歸零時回到的數值。
    DEFAULT_DSTAY = 2048   # = 0x80 死區（12-bit 語義，>>4 = 0x80）

    def show_all(self):
        """單台 frame 串接：一次 write 發所有 address 的 frame（FF addr value FE × N）。

        UART-412 廣播模式受 MAX_DEVICE=32 限制（原碼 while i < MAX_DEVICE+2），
        address > 32 的設備廣播收不到；故一律用單台 frame 串接，一次過發射。
        依 version 用對應的 _build_single 編碼（v1=FF/addr/value/FE，可註冊其他版本）。
        """
        n = len(self.addresses)
        need = n * 4
        if len(self._tx_broadcast) < need:
            self._tx_broadcast = bytearray(need)
        frame = self._tx_broadcast
        for k, addr in enumerate(self.addresses):
            seg = memoryview(frame)[k * 4:(k + 1) * 4]
            self._build_single(seg, addr & 0xFF, self.buffer[addr - 1])
        self.uart.write(memoryview(frame)[:need])

    def send(self, addr, value):
        """set + 立即發送單台 frame（同時更新 buffer，保持 buffer 為權威狀態）。"""
        self.set(addr, value)
        self._build_single(self._tx_single, addr & 0xFF, value & 0xFF)
        self.uart.write(self._tx_single)

    def send_all(self, value):
        """set_all + show_all 立即廣播全部同值。"""
        self.set_all(value)
        self.show_all()

    def stop_all(self):
        """全部停止（set_all(STOP) + show_all）。"""
        self.send_all(STOP)

    # ── 行程模式（位置估算，疊在速度控制之上；只改 buffer，輸出統一走 show_all）──

    def _lookup_rate(self, addr, speed, direction):
        """某台 + speed + 方向 → 速率（Q 定點，格/ms）。

        校準表優先，採分段線性：
          - 命中量測點 → 直接查表
          - 落於兩點之間 → 線性內插
          - 全速端點（128 → rate_full）永遠存在，供「最高量測點～全速」內插
          - 低於最低量測點 → 死區回 0（最低點即「最低可用速度」）
          - 只有全速端點（= 只輸入一項）→ 從原點線性正比（無死區）
        """
        speed = int(speed)
        if speed <= 0:
            return 0
        if speed > SPEED_MAX:
            speed = SPEED_MAX
        if direction >= 0:
            table = self._rate_fwd[addr]
            base = self._rate_full_fwd[addr]
        else:
            table = self._rate_rev[addr]
            base = self._rate_full_rev[addr]
        # 量測點 + 全速端點（128 → rate_full）。全速端點永遠存在，
        # 使 speed 落在「最高量測點與全速之間」時能正確內插，而非被 clamp。
        points = dict(table)
        if SPEED_MAX not in points:
            points[SPEED_MAX] = base
        if speed in points:
            return points[speed]
        ks = sorted(points)
        if len(ks) == 1:
            # 只有全速端點（= 只輸入一項）→ 從原點線性正比
            return points[ks[0]] * speed // ks[0]
        if speed < ks[0]:
            return 0                        # 低於最低量測點 → 死區
        if speed > ks[-1]:
            return points[ks[-1]]           # 不會發生（speed ≤ 128 = 最大端點）
        for i in range(len(ks) - 1):
            lo, hi = ks[i], ks[i + 1]
            if lo <= speed <= hi:
                rlo, rhi = points[lo], points[hi]
                return rlo + (rhi - rlo) * (speed - lo) // (hi - lo)
        return points[ks[-1]]               # 理論上不會到這

    def _recompute(self, addr):
        """時間差閉式重算一台的當前位置；到達目標/邊界即停（改 buffer）。

        同一段等速移動內只有一次乘法，沒有逐 tick 累加的捨入誤差。
        """
        i = addr - 1
        rate = self._rate[i]
        if rate == 0:
            return
        elapsed = self._clock_diff(self._clock(), self._t0[i])
        pos = self._pos0[i] + rate * elapsed
        target = self._target[i]
        pos_fix = POS_MAX << _Q
        if rate > 0:
            if target is not None and pos >= target:
                pos, rate = target, 0
            elif pos >= pos_fix:
                pos, rate = pos_fix, 0
        else:
            if target is not None and pos <= target:
                pos, rate = target, 0
            elif pos <= 0:
                pos, rate = 0, 0
        if rate == 0:
            self.set(addr, STOP)
        self._pos[i] = pos
        self._rate[i] = rate

    def move_to(self, addr, target, speed=SPEED_MAX):
        """以指定速度移動到絕對位置（0..4095），方向由 target 自動決定。

        只更新 buffer，不立即發送；配合 show_all() 一次推送。
        speed=0 或已到目標 → 直接停。
        """
        addr = int(addr)
        if addr not in self._address_set:
            raise ValueError(
                "UartMotor: address {} 不在控制列表 {}".format(addr, self.addresses))
        target = int(target)
        if target < 0:
            target = 0
        elif target > POS_MAX:
            target = POS_MAX
        speed = int(speed)
        self._recompute(addr)              # 先結算當前位置，避免跨段誤差
        i = addr - 1
        cur = self._pos[i] >> _Q
        if cur == target or speed <= 0:
            self._rate[i] = 0
            self.set(addr, STOP)
            return
        direction = 1 if target > cur else -1
        self._pos0[i] = self._pos[i]       # 以結算後位置為起點
        self._t0[i] = self._clock()
        self._target[i] = target << _Q
        self._rate[i] = self._lookup_rate(addr, speed, direction) * direction
        self.set(addr, speed_to_byte(speed, direction))

    def move(self, addr, delta, speed=SPEED_MAX):
        """以指定速度相對位移（delta 單位 = 全程的 1/4095，可正可負）。"""
        self._recompute(addr)
        cur = self._pos[addr - 1] >> _Q
        return self.move_to(addr, cur + int(delta), speed)

    def position(self, addr):
        """讀取一台的估算位置（0..4095），讀前先結算。"""
        addr = int(addr)
        if addr not in self._address_set:
            raise ValueError(
                "UartMotor: address {} 不在控制列表 {}".format(addr, self.addresses))
        self._recompute(addr)
        return self._pos[addr - 1] >> _Q

    def update(self):
        """週期呼叫：重算全部電機位置，到達目標/邊界的台自動停（改 buffer）。

        之後再 show_all() 把停止幀推送出去。非阻塞，不含 sleep / timer。
        """
        for addr in self.addresses:
            self._recompute(addr)

    # ── 校準（無回饋，半自動 + 人手確認）──

    def set_t_full(self, addr, direction, ms):
        """設定某台全速走完全程的 ms（direction >=0 伸 / <0 縮）。"""
        addr = int(addr)
        if addr not in self._address_set:
            raise ValueError(
                "UartMotor: address {} 不在控制列表 {}".format(addr, self.addresses))
        ms = int(ms)
        if ms <= 0:
            raise ValueError("UartMotor: t_full 必須 > 0")
        if direction >= 0:
            self._t_full_fwd_ms[addr] = ms
            self._rate_full_fwd[addr] = (POS_MAX << _Q) // ms
        else:
            self._t_full_rev_ms[addr] = ms
            self._rate_full_rev[addr] = (POS_MAX << _Q) // ms

    def calibrate(self, addr, direction, speed, elapsed_ms):
        """記錄某台某速度下實測的「走完全程 ms」，寫入該台校準表。

        elapsed_ms 越小 → 速率越快；重複呼叫覆蓋同 speed 舊值。
        低速若實測推不動（死區），給極大 elapsed_ms 等效停。
        """
        addr = int(addr)
        if addr not in self._address_set:
            raise ValueError(
                "UartMotor: address {} 不在控制列表 {}".format(addr, self.addresses))
        speed = int(speed)
        elapsed_ms = int(elapsed_ms)
        if not 1 <= speed <= SPEED_MAX:
            raise ValueError("UartMotor: speed 必須在 1..{}".format(SPEED_MAX))
        if elapsed_ms <= 0:
            raise ValueError("UartMotor: elapsed_ms 必須 > 0")
        rate = (POS_MAX << _Q) // elapsed_ms
        if direction >= 0:
            self._rate_fwd[addr][speed] = rate
        else:
            self._rate_rev[addr][speed] = rate

    def __len__(self):
        return self.num_devices

    # ── pixel 系統相容介面（走 big_buffer，讀 W 通道）─────────────
    # UartMotor 可作為 PixelStreamer 的 controller（像 PCA9685 的 i2c_pixel）：
    #   PixelTask scatter 效果輸出 → big_buffer（RGBW 4 bytes/顆）
    #   PixelStreamer.show_all() → st_load_and_convert() 提取 W 通道（8-bit）
    #                              → st_show() 一次過組 UART frame 發射
    # 效果輸出用 write:"w"（或 rgbw）→ big_buffer W 通道 = 速度 byte（0x80=停）。

    @property
    def pixel_type(self):
        """registry 統一 key（對齊 pixel_task TYPE_MAP 的 uartMotor1）。"""
        return "uartMotor1"

    @property
    def num_pixels(self):
        return self.num_devices

    @property
    def frame_size(self):
        """big_buffer 佔用：每顆 RGBW 4 bytes。"""
        return self.num_devices * 4

    def st_init(self):
        """PixelStreamer.init() 呼叫；motor 不需額外初始化（UartMotor 建構已設 STOP）。"""

    def st_load_and_convert(self, source_buffer, offset):
        """從 big_buffer 提取 W 通道（每顆第 4 byte）填進 motor buffer —— 原樣 raw byte。

        motor 效果（如 uart_motor_sine）走 write:"w"，直接在 W 通道給 8-bit raw：
        0x00 全速收 … 0x80 停 … 0xFF 全速伸，0 也是有效命令 → 不作保護改寫。
        停止/熄燈不靠「W=0」表達：render 的 clear_all() / stop_motors() 會把 W
        明確填成 neutral_value（0x80 死區停）再推一幀。
        """
        n = self.num_devices
        for i in range(n):
            self.buffer[i] = source_buffer[offset + (i << 2) + 3]

    def st_show(self):
        """組廣播 frame（FF 00 V1..VN FE）一次過 uart.write。"""
        self.show_all()


# === PC 快速 demo（FakeUART + FakeClock，不依賴硬體）===
if __name__ == "__main__":

    class _FakeUART:
        def __init__(self):
            self.writes = []

        def write(self, data):
            self.writes.append(bytes(data))
            return len(data)

    class _FakeClock:
        def __init__(self):
            self.t = 0

        def __call__(self):
            return self.t

        @staticmethod
        def diff(a, b):
            return a - b

    # 原始模式
    uart = _FakeUART()
    motor = UartMotor({"version": 1, "addresses": [1, 2, 3], "uart": uart})

    motor.send(1, FWD_FS)
    motor.send(2, REV)
    motor.set(3, STOP)
    motor.show_all()
    motor.stop_all()

    for w in uart.writes:
        print("TX -> [{}]".format(w.hex(" ")))

    # 行程模式（t_full_ms=4095 → 全速約 1 格/ms，方便看）
    clock = _FakeClock()
    uart2 = _FakeUART()
    m2 = UartMotor({
        "version": 1,
        "addresses": [1, 2],
        "uart": uart2,
        "clock": clock,
        "clock_diff": _FakeClock.diff,
        "t_full_ms": 4095,
    })

    m2.move_to(1, 1000, SPEED_MAX)   # 全速伸到 1000
    m2.move_to(2, 4095, SPEED_MED)   # 中速伸到底
    m2.show_all()

    clock.t += 500
    print("pos1 @500ms =", m2.position(1), "(期望 500)")
    print("pos2 @500ms =", m2.position(2), "(期望 250)")

    m2.update()
    m2.show_all()
    clock.t += 500
    m2.update()
    m2.show_all()
    print("pos1 @1000ms =", m2.position(1), "(已到目標 1000，應停)")
    print("byte1 =", hex(m2.buffer[0]), "(停 = 0x80)")

    for w in uart2.writes:
        print("TX2 -> [{}]".format(w.hex(" ")))
