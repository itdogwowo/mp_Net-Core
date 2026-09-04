"""
pixel_drv.py — pixel 統一聚合層

將 apa1022_list + ws2812_list + pca9685_list + motor_list 合併成 pixel_list，
並建立 PixelStreamer (st_pixel)。

設定來源: 無（聚合下游 driver 結果）
產物:    bus.register_service("pixel_list", [...])
         bus.register_service("st_pixel", PixelStreamer)
"""
from lib.sys.sys_bus import bus
from lib.sys.log_service import get_log
from lib.sw.PixelController import PixelStreamer


def init_pixel(sysbus=None):
    sysbus = sysbus or bus
    apa_list = sysbus.get_service("apa1022_list") or []
    ws_list = sysbus.get_service("ws2812_list") or []
    pca_list = sysbus.get_service("pca9685_list") or []
    motor_list = sysbus.get_service("motor_list") or []

    pixel_list = apa_list + ws_list + pca_list + motor_list
    sysbus.register_service("pixel_list", pixel_list)

    try:
        st = PixelStreamer(pixel_list)
        st.show_all()
        sysbus.register_service("st_pixel", st)
    except Exception as e:
        get_log().error("st_pixel init error: {}".format(e))

    return pixel_list


def gpios(sysbus=None):
    # pixel 本身不佔 GPIO
    return {}
