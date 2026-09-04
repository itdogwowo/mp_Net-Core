"""
pca9685_drv.py — PCA9685 PWM pixel 管理 (走 I2C)

設定來源: bus.shared["PCA9685"]  ({enable, list})
         list item: {"i2c": <i2c_list index>, "address": ["0x40"]}
產物:    bus.register_service("pca9685_list", [...])

address 特殊值:
  "0xFF" (255) — 真·廣播 (LED All Call, datasheet Fig.25):
                i2c.scan() 找實體位址(排除 0x70) → 逐一對實體位址喚醒/開
                ALLCALL → 建立「單一」0x70 廣播 controller。每幀對 0x70 寫
                一筆 = 所有板子同步收到相同 16 通道 (12-bit)。
  "0x70" (112) — 同上, 但不掃描、不逐一喚醒, 直接以 0x70 建立廣播 controller。
                僅當確定線上晶片 ALLCALL 已開啟時使用。
"""
from lib.sys.log_service import get_log
from lib.hw.pca9685 import PCA9685
from lib.sys.sys_bus import bus

# PCA9685 ALLCALL 廣播位址 (112 = 0x70)。所有 writeto_mem 寫到 0x70 都是
# 廣播: 匯流排上所有啟用 ALLCALL 的 PCA9685 同時收到。因此 0x70 不該當
# 一般裝置註冊 — scan 掃到要排除, 只有明確要用「單一廣播」才註冊它。
_PCA_ALL_CALL = 0x70
# config address 清單中的魔法值: 自動掃描註冊匯流排上全部 PCA9685。
_PCA_AUTO_SCAN = 0xFF


def _make_controller(pca, item):
    """用 PCA9685 實體建立 PixelController (與串流渲染格式一致)。"""
    from lib.sw.PixelController import PixelController
    return PixelController("i2c_pixel", {
        "pixel_IO": pca,
        "Q": 16,
        "order": "W",
        "dStay": item.get("dStay", 0),
    })


def _register_pca(i2c, addr, item, pca_list):
    """建立單一 PCA9685(位址 addr)並加入 pca_list。回傳 True/False。

    addr == 0x70 → ALLCALL 廣播位址 (broadcast controller): 每幀對 0x70 寫一筆
    = 所有啟用 ALLCALL 的板子同時收到。其餘位址為一般單一裝置, 個別寫入。
    """
    try:
        pca = PCA9685(i2c, address=addr)
        pca.freq(1000)
    except Exception as e:
        get_log().error("PCA9685@{} init error: {}".format(hex(addr), e))
        return False
    pca_list.append(_make_controller(pca, item))
    return True


def _scan_and_register(i2c, item, pca_list):
    """0xFF: 真·廣播 (LED All Call, datasheet Fig.25)。

    掃描找到實體位址 (排除 0x70 自身) 後:
      1. 逐一對「每個實體位址」初始化 (PCA9685.freq 保留 ALLCALL 位元), 把
         晶片從 Sleep 喚醒、開 auto-increment、並確保 ALLCALL=1。
      2. 建立「單一」0x70 廣播 controller。每幀 show() 對 0x70 寫一筆 64
         bytes, 所有板子同時收到並各自寫入自己的 LED0~LED15 (16 通道 / 12-bit
         完全獨立, 等同控制一顆晶片; 只是每顆板子內容相同)。

    廣播要生效的關鍵 (社群踩坑): 每個晶片的 ALLCALL(MODE1 bit0) 都必須是 1,
    但重刷韌體通常不斷電, 舊程式若寫過 MODE1=0x00/0x10 就會把它關掉。因此
    這裡一定要「先逐一對實體位址寫入重設」, 之後 0x70 才會被 ACK; 若直接對
    0x70 初始化, 在 ALLCALL 已被關掉的板子上會靜默失敗 (ENODEV / 不亮燈)。

    線上完全沒板子時不建 0x70, 避免每幀對不存在的位址刷 ENODEV。
    """
    try:
        found = [a for a in i2c.scan() if a != _PCA_ALL_CALL]
    except Exception as e:
        get_log().error("PCA9685 scan error: {}".format(e))
        return
    get_log().info("I2C Scan (excl 0x70): {}".format([hex(a) for a in found]))
    if not found:
        get_log().warn("PCA9685: no physical device found — skip 0x70 broadcast")
        return
    # 1. 逐一喚醒實體板 (保留 ALLCALL), 不註冊成 controller
    for addr in found:
        try:
            pca = PCA9685(i2c, address=addr)
            pca.freq(1000)
        except Exception as e:
            get_log().error("PCA9685@{} init error: {}".format(hex(addr), e))
    # 2. 單一 0x70 廣播 controller: 寫一次 = 所有板子同步收到
    if _register_pca(i2c, _PCA_ALL_CALL, item, pca_list):
        get_log().info("PCA9685: 0x70 broadcast controller ({} physical board(s))".format(len(found)))


def init_pca9685(sysbus=None):
    sysbus = sysbus or bus
    cfg = sysbus.shared.get("PCA9685") or {}
    if not cfg.get("enable"):
        return []

    from lib.sw.PixelController import PixelController
    i2c_list = sysbus.get_service("i2c_list") or []
    pca_list = []
    for item in cfg.get("list", []):
        i2c_idx = item.get("GPIO", {}).get("i2c", 0)
        if i2c_idx < 0 or i2c_idx >= len(i2c_list):
            get_log().error("PCA9685: i2c index {} not found".format(i2c_idx))
            continue
        i2c = i2c_list[i2c_idx]
        addrs = item.get("address", [])
        if not addrs:
            # 無 address → 沿用舊 fallback: 掃描並排除 0x70
            try:
                addrs = [a for a in i2c.scan() if a != _PCA_ALL_CALL]
                get_log().info("I2C Scan: {}".format([hex(a) for a in addrs]))
            except Exception as e:
                get_log().error("PCA9685 scan error: {}".format(e))
                continue
        for addr in addrs:
            if isinstance(addr, str):
                addr = int(addr, 16)
            if addr == _PCA_AUTO_SCAN:
                _scan_and_register(i2c, item, pca_list)
            else:
                _register_pca(i2c, addr, item, pca_list)
    sysbus.register_service("pca9685_list", pca_list)
    get_log().info("PCA9685: {} device(s)".format(len(pca_list)))
    return pca_list


def gpios(sysbus=None):
    # PCA9685 走 I2C，無獨立 GPIO
    return {}
