# ui/lvgl/nav.py — 共用導覽狀態機框架
#
# 兩層模型(對齊使用者設計):
#   第一層 = 頁內選區域(焦點列表):encoder 移動焦點,confirm 進入該項。
#   第二層 = 調值/改選項:slider 用 encoder 調值、enum 循環選項、switch 切換。
#
# 頁面只負責「註冊項的清單」,框架自動處理 enc/confirm/exit 狀態轉移 + 焦點視覺。
#
# 用法:
#   from ui.lvgl.nav import Nav, ITEM_INFO, ITEM_SWITCH, ITEM_ENUM, ITEM_SLIDER, ITEM_BUTTON
#   nav = Nav()
#   def build():
#       nav.reset()
#       nav.add(widget1, ITEM_INFO)                       # 唯讀聚焦
#       nav.add(sw, ITEM_SWITCH, on_change=_toggle)       # confirm 切換
#       nav.add(box, ITEM_ENUM, on_change=_cycle)         # confirm 循環選項
#       nav.add(slider, ITEM_SLIDER, on_change=_set_val)  # confirm 進編輯,enc 調值
#       nav.add(btn, ITEM_BUTTON, on_change=_trigger)     # confirm 觸發
#       nav.paint()
#   def on_enc(d):     nav.enc(d)
#   def on_confirm():  nav.confirm()
#   def on_exit():     return nav.exit()
#
# 支援的項型別常數:
#   ITEM_INFO    唯讀聚焦,confirm 無動作
#   ITEM_SWITCH  confirm → on_change() 切換開關
#   ITEM_ENUM    confirm → on_change() 循環選項
#   ITEM_SLIDER  confirm → 進/出編輯態;編輯態 enc → on_change(delta)
#   ITEM_BUTTON  confirm → on_change() 觸發動作

# 項型別常數
ITEM_INFO = 0
ITEM_SWITCH = 1
ITEM_ENUM = 2
ITEM_SLIDER = 3
ITEM_BUTTON = 4
ITEM_LIST = 5     # confirm 進編輯(選擇),enc 上下移選項,confirm/exit 退出

# 可編輯態:confirm 進出編輯,enc 在編輯態呼叫 on_change(delta)
_EDITABLE = (ITEM_SLIDER, ITEM_LIST)


class Nav:
    """單頁導覽狀態機。每個 page 建一個實例,在 build() 註冊項。"""

    def __init__(self):
        self._items = []      # [{w, kind, on_change, on_focus}]
        self._fi = 0          # 焦點索引
        self._editing = False

    # ── 註冊(頁面 build 時呼叫) ──

    def reset(self):
        """清空項清單(build 開頭呼叫,重建頁時重來)。"""
        # 離開舊焦點視覺
        for it in self._items:
            self._focus_widget(it["w"], False, False)
        self._items = []
        self._fi = 0
        self._editing = False

    def add(self, w, kind, on_change=None, on_focus=None):
        """加入一個項。
        w         該項的 widget(畫焦點框用)
        kind      ITEM_INFO / ITEM_SWITCH / ITEM_ENUM / ITEM_SLIDER / ITEM_BUTTON
        on_change 該項被操作時的回呼:
                    SWITCH/ENUM/BUTTON → on_change() 無參(頁面自己切換/循環/觸發)
                    SLIDER 編輯態 enc → on_change(delta) delta=累加格數(±N)
        on_focus  焦點變化回呼 on_focus(focused: bool)(選用,框架預設畫焦點框)"""
        self._items.append({
            "w": w, "kind": kind,
            "on_change": on_change, "on_focus": on_focus,
        })

    # ── 焦點視覺 ──

    def paint(self):
        """重繪所有焦點框。"""
        from ui.lvgl import ui_common as u
        if not self._items:
            return
        self._fi = max(0, min(self._fi, len(self._items) - 1))
        for i, it in enumerate(self._items):
            foc = i == self._fi
            self._focus_widget(it["w"], foc, self._editing and foc)
            if it["on_focus"]:
                try:
                    it["on_focus"](foc)
                except Exception:
                    pass

    def _focus_widget(self, w, foc, editing):
        from ui.lvgl import ui_common as u
        try:
            u.set_focus(w, foc, editing)
        except Exception:
            pass

    # ── 輸入事件(頁面 on_enc/on_confirm/on_exit 轉發) ──

    def enc(self, d):
        """encoder 事件。
        導覽態:移動焦點(_fi)。
        編輯態:呼叫當前項 on_change(delta)(slider 調值)。"""
        if not self._items:
            return
        if self._editing:
            it = self._items[self._fi]
            if it["on_change"]:
                try:
                    # 傳原始 delta(累加格數,可能 ±N):不夾成 ±1,快速轉動不掉格
                    it["on_change"](d)
                except Exception as e:
                    print("[nav] enc_edit err:", e)
            return
        self._fi = (self._fi + d) % len(self._items)
        self.paint()

    def confirm(self):
        """confirm 事件。依當前項 kind 分派:
        SLIDER → 進/出編輯態;其他 → on_change()。"""
        if not self._items:
            return
        it = self._items[self._fi]
        kind = it["kind"]
        if kind in _EDITABLE:
            # 進/出編輯態
            self._editing = not self._editing
            self.paint()
            return
        if it["on_change"]:
            try:
                it["on_change"]()
            except Exception as e:
                print("[nav] confirm err:", e)

    def exit(self):
        """exit 事件。回 True=消耗(留在頁,例如退出編輯);False=回 launcher。"""
        if self._editing:
            self._editing = False
            self.paint()
            return True
        return False

    # ── 查詢 ──

    def is_editing(self):
        return self._editing

    def count(self):
        return len(self._items)

    def current_kind(self):
        if not self._items:
            return None
        return self._items[self._fi]["kind"]
