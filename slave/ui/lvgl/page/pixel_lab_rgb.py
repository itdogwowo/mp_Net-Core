# ui/lvgl/page/pixel_lab_rgb.py — PixelLab RGB (WS2812) 頁
from ui.lvgl.registry import register
from ui.lvgl.page.pixel_lab_common import SimplePage

_page = SimplePage("rgb", "PixelLab RGB", "WS2812", 0x0E9F6E)


@register(id="pixel_lab_rgb", title="PixelLab RGB", icon="lightbulb",
          desc="WS2812", order=2, accent=0x0E9F6E)
def build():
    return _page.build()


def on_enter():
    _page.on_enter()


def on_leave():
    _page.on_leave()


def on_enc(d):
    _page.on_enc(d)


def on_confirm():
    return _page.on_confirm()


def on_exit():
    return _page.on_exit()


def update(run):
    _page.update(run)
