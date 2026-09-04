# ui/lvgl/registry.py — 動態註冊表
PAGES = {}


def register(id, title, icon="", desc="", order=0, accent=0x1A73E8, status=""):
    def deco(fn):
        PAGES[id] = {
            "id": id, "title": title, "icon": icon, "desc": desc,
            "order": order, "accent": accent, "status": status, "build": fn,
        }
        return fn
    return deco


def ordered():
    return [PAGES[k] for k in sorted(PAGES, key=lambda k: PAGES[k]["order"])]


def get(page_id):
    return PAGES.get(page_id)
