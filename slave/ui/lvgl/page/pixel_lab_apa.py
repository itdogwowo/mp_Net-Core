# ui/lvgl/page/pixel_lab_apa.py — PixelLab APA 頁
from ui.lvgl.registry import register
from ui.lvgl.page.pixel_lab_common import SimplePage

_page = SimplePage("apa", "PixelLab APA", "APA102", 0x6D5BD0)


@register(id="pixel_lab_apa", title="PixelLab APA", icon="sun",
          desc="APA102 · 呼吸/選中", order=2, accent=0x6D5BD0)
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
