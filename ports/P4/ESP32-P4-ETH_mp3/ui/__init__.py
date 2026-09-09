# ui/ — slave new 統一 UI 區塊
#
# 子區塊(執行邏輯各自獨立,只是資源歸在同一層):
#   ui/web/    靜態網頁 UI(WebUITask 服務,web_root="ui/web")
#   ui/lvgl/   LVGL 本地 UI(由 ui.lvgl.board.run() 啟動,跑在 LCD 上)
