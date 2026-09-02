"""
pixel_task.py — pixel 子系統統一管理任務（PixelTask）

四層資料：
  1. effects  : pixel/effects/（effects.json + effects.py 生成器，py 優先）
  2. mapping  : pixel/map/*.json（每套一套群組，自帶 id/name）
  3. modes    : pixel/modes/*.json（模式 = 效果 × 群組配對 + 播放參數）
  4. registry : pixel/registry.json（播放清單 + auto_play）

on_start 依序初始化：硬體（st_pixel）→ effects → mapping（PixelLayout）→ modes → registry。
loop() = 播放端：大隊列（registry.list）依序播放，show 循環；mode 的播放參數
（play_count / play_interval）控制每輪出現與否，單位全用 frame。
同一個 mode 連續播放（重複播放）時重用現有生成器（restart），不剷除重建。

硬體 order/counts 一律從播放器（PixelStreamer.controllers）推導，不自己設定
（硬體真值）。registry.json 只用來選擇「播什麼 / 開不開自動播放」。

指令介面（bus.shared，指令層寫入、本任務消費）：
  pixel_play   → 開始/重啟 show
  pixel_stop   → 停止（熄燈）
  pixel_pause  → 暫停 / 恢復
"""

import json
import time
from lib.sys.task import Task
from lib.sys.sys_bus import bus
from lib.sys.log_service import get_log
from lib.sw.pixel_layout import PixelLayout

EFFECTS_JSON = "/pixel/effects/effects.json"
MAP_DIR = "/pixel/map"
MODES_DIR = "/pixel/modes"
REGISTRY_JSON = "/pixel/registry.json"

# 硬體 controller 型別 → registry 統一 key（單一真源）
TYPE_MAP = {"APA102": "apa102", "WS2812": "ws2812", "i2c_pixel": "pca9685",
            "uartMotor1": "uartMotor1"}

WRITE_WHITELIST = ("r", "g", "b", "w", "ww", "rgb", "rgbw", "wwww")


def _list_json(d):
    """目錄下 *.json 檔的完整路徑清單（目錄不存在 → []）。"""
    import os
    try:
        return [d.rstrip("/") + "/" + f for f in os.listdir(d) if f.endswith(".json")]
    except OSError:
        return []


class PixelTask(Task):
    """pixel 管理 + 播放端：初始化四層資料，並執行大隊列自動播放。"""

    def __init__(self, name, ctx):
        super().__init__(name, ctx)
        self._st = None
        self._lay = None
        self._hub = None
        self._gens = {}
        self._modes = {}
        self._show = {"auto_play": False, "list": []}
        self._show_list = []
        self._orig_show_list = None   # 遠端單模式播放前的原始 show list (結束後還原)
        self._playing = False
        self._paused = False
        self._pass = 1       # 目前 show 的輪次（第 1 輪起）
        self._mode_idx = 0
        self._cur = None
        self._cur_mode = None   # _cur 對應的 mode（判斷下一個是否同一 mode → 重用生成器）
        self._cur_repeat = 1    # 目前 mode 本次出現已連播次數（play_count 用）
        self._cur_frames = 0    # 本次播放已 commit 幀數（maxF 用）
        self._appears = {}      # mode id → 已出現次數（play_loop 總次數用）

    # ── 啟動：依序初始化 ──────────────────────────
    def on_start(self):
        super().on_start()
        try:
            self._init_hw()
            self._init_effects()
            self._init_layout()
            self._init_modes()
            self._init_show()
            if self._show["auto_play"] and self._show_list:
                self._start()
        except Exception as e:
            get_log().error("[Pixel] 初始化失敗: {}".format(e))

    def _init_hw(self):
        """確保 st_pixel 存在（播放器 = 硬體真值）；連到 pixel_stream hub 供 RenderTask 播放。"""
        st = bus.get_service("st_pixel")
        if st is None:
            from driver.pixel_drv import init_pixel
            try:
                init_pixel(bus)
                st = bus.get_service("st_pixel")
            except Exception as e:
                get_log().error("[Pixel] init_pixel 失敗: {}".format(e))
        if st is None:
            get_log().warn("[Pixel] 無 st_pixel（pixel 硬體未接）— 僅載入設定")
            return
        self._st = st

        # 播放 hub：既有 pixel_stream（Core_Manager 建立，RenderTask 消費）。
        # 本 task（計算核）scatter 進 hub slot，RenderTask（播放核）read_into 推硬體。
        hub = bus.get_service("pixel_stream")
        if hub is None:
            try:
                from lib.sys.buffer_hub import AtomicStreamHub
                hub = AtomicStreamHub(st.total_bytes)
                bus.register_service("pixel_stream", hub)
            except Exception as e:
                get_log().error("[Pixel] pixel_stream hub 建立失敗: {}".format(e))
        self._hub = hub
        # 節奏完全由 RenderTask（core0）依 System.frame_interval_ms 控制；
        # 本 task（計算核）全力算幀，hub 滿即 drop（不推進生成器，同一幀重試）。

    def _init_effects(self):
        """py register + 載 effects.json → bus.shared["pixel_gens"]（存 cls/params，播放時建 Effect）。

        衝突檢查：載入後呼叫 effects.check_conflicts()，把 id/name 衝突列印成警告
        （對齊 boot.py 的 GPIO 檢查；不 raise，人肉判斷修正）。
        """
        from pixel.effects import effects
        try:
            with open(EFFECTS_JSON) as f:
                effects.load_json(json.load(f).get("effects", []))
        except OSError:
            get_log().warn("[Pixel] 找不到 {}，僅用 py 效果".format(EFFECTS_JSON))
        except Exception as e:
            get_log().error("[Pixel] 載入 {} 失敗: {}".format(EFFECTS_JSON, e))

        gens = {}
        for name, eid in effects.dump().items():
            if eid is None:
                # py 有類別但 json 沒給 id/params → 不播放（check_conflicts() 已警告）
                continue
            gens[name] = {
                "id": eid,
                "name": name,
                "cls": effects.resolve(eid),
                "params": effects.get_params(eid),
            }
        self._gens = gens
        bus.shared["pixel_gens"] = gens
        # 開機預計算波表：掩蓋首次播放的計算成本（同 effect 之後零重算）
        n_wave = effects.warm_up()
        get_log().info("[Pixel] effects: {} 個（波表預算 {} 個）".format(len(gens), n_wave))
        # 啟動檢查：id/name 衝突警告（對齊 boot GPIO 檢查，人肉判斷修正）
        for line in effects.check_conflicts():
            get_log().warn("[Pixel] " + line)

    def _init_layout(self):
        """從播放器推導 order/counts，載入 map/*.json 註冊全部 mapping → pixel_layout。"""
        st = self._st
        order = []
        counts = {}
        if st:
            for c in st.controllers:
                t = TYPE_MAP.get(getattr(c, "pixel_type", ""))
                if t is None:
                    get_log().warn("[Pixel] 未知 controller 型別: {!r}".format(c.pixel_type))
                    continue
                if t not in counts:
                    counts[t] = 0
                    order.append(t)
                counts[t] += c.num_pixels
        if not order:
            get_log().warn("[Pixel] 播放器無可辨識 controller — order 為空")

        lay = PixelLayout(order, counts)
        for fn in _list_json(MAP_DIR):
            try:
                with open(fn) as f:
                    m = json.load(f)
                self._warn_missing_types(lay, m)
                lay.register_mapping(m["id"], m["name"], m.get("groups", []))
                get_log().info("[Pixel] mapping {}（{}）: {} group(s)".format(
                    m["id"], m["name"], len(m.get("groups", []))))
            except ValueError as e:
                get_log().warn("[Pixel] 跳過 mapping {}: {}".format(fn, e))
            except Exception as e:
                get_log().warn("[Pixel] 載入 {} 失敗: {}".format(fn, e))

        self._lay = lay
        bus.shared["pixel_layout"] = lay

    @staticmethod
    def _warn_missing_types(lay, m):
        """warn 群組引用到未接硬體的型別（誠實反映為空段）。"""
        for g in m.get("groups", []):
            for seg in g.get("sel", []):
                t = seg["type"]
                if isinstance(t, int):
                    continue  # 以 order index 引用，無法預檢
                if t not in lay.counts:
                    get_log().warn("[Pixel] mapping {} group {} 引用無硬體型別 {!r}（空段）".format(
                        m["name"], g["name"], t))

    def _init_modes(self):
        """載入 modes/*.json → bus.shared["pixel_maps"]。解析失敗只 warn 跳過該項。"""
        modes = {}
        for fn in _list_json(MODES_DIR):
            try:
                with open(fn) as f:
                    d = json.load(f)
                self._parse_mode(d, modes)
            except Exception as e:
                get_log().warn("[Pixel] 載入 {} 失敗: {}".format(fn, e))
        self._modes = modes
        bus.shared["pixel_maps"] = modes
        get_log().info("[Pixel] modes: {} 個".format(len(modes)))

    def _parse_mode(self, d, modes):
        mid = int(d["id"])
        name = d["name"]
        if mid in modes:
            get_log().warn("[Pixel] mode id 重複 {}（{}）— 跳過".format(mid, name))
            return
        for other in modes.values():
            if other["name"] == name:
                get_log().warn("[Pixel] mode name 重複 {} — 跳過".format(name))
                return
        lay = self._lay
        if lay is None:
            get_log().warn("[Pixel] mode {} 載入前無 pixel_layout — 跳過".format(name))
            return

        default_map = d.get("mapping")
        entries = []
        seen = set()
        for it in d.get("map", []):
            gref = it["group"]
            mref = default_map
            if isinstance(gref, str) and "." in gref:
                mref, gref = gref.split(".", 1)
            try:
                rmid, rgid = lay.resolve_group(mref, gref)
            except (KeyError, ValueError, TypeError):
                get_log().warn("[Pixel] mode {} 引用未知群組 {!r} — 跳過該項".format(name, it["group"]))
                continue
            # 播放範圍（群組內 slice，可選）：同一群組可拆多段配不同效果
            offs = None
            rng = it.get("range")
            if rng is not None:
                try:
                    offs = lay.sub_offsets(rmid, rgid, rng)
                except Exception:
                    get_log().warn("[Pixel] mode {} 引用無效 range {!r} — 跳過該項".format(name, rng))
                    continue
                if not offs:
                    get_log().warn("[Pixel] mode {} range {!r} 為空 — 跳過該項".format(name, rng))
                    continue
            key = (rmid, rgid, str(rng))
            if key in seen:
                get_log().warn("[Pixel] mode {} 群組 {!r} range {!r} 重複 — 只保留第一項".format(
                    name, it["group"], rng))
                continue
            seen.add(key)

            eff = self._find_effect(it["effect"])
            if eff is None:
                get_log().warn("[Pixel] mode {} 引用未知效果 {!r} — 跳過該項".format(name, it["effect"]))
                continue
            write = it["write"]
            if write not in WRITE_WHITELIST:
                get_log().warn("[Pixel] mode {} 未知寫法 {!r} — 跳過該項".format(name, write))
                continue
            entries.append({
                "mref": rmid, "gref": rgid, "write": write, "offs": offs,
                "cls": eff["cls"], "name": eff["name"], "params": eff["params"],
            })

        modes[mid] = {
            "id": mid,
            "name": name,
            "index": d.get("index", mid),
            # 播放語意（3 欄分工）：
            #   play_loop     = 總共 loop/出現幾次循環（0=不播; N=最多 N 次; -1=常駐每輪）
            #   play_count    = 同一個 loop 中播放幾次（1..N=連播 N 次; -1=無限連播）
            #   play_interval = 相隔多少個循環播一次（0=每個循環都播; 1=隔 1 循環）
            "play_loop": self._parse_loop(d),
            "play_count": self._parse_count(d),
            "play_interval": max(0, int(d.get("play_interval", 0) or 0)),
            "maxF": max(0, int(d.get("maxF", 0) or 0)),   # 每次播放最大幀數；0=不限制
            "entries": entries,
            # 燈效綁定音效（M5）：原樣攜帶 audio 段（gmode 解析後扇出給 DjTask；
            # PixelTask 自己不碰播放邏輯）。無此段 = 純燈效。
            "audio": d.get("audio"),
        }

    @staticmethod
    def _parse_loop(d):
        """play_loop：總出現次數。0=不播; N=最多 N 次; 負數(-1)=常駐每輪。預設 -1。"""
        try:
            v = int(d.get("play_loop", -1))
        except (TypeError, ValueError):
            v = -1
        return -1 if v < 0 else v

    @staticmethod
    def _parse_count(d):
        """play_count：同一個 loop 中連播幾次。0/缺省→1; 負數(-1)=無限連播。"""
        try:
            v = int(d.get("play_count", 1) or 1)
        except (TypeError, ValueError):
            v = 1
        return -1 if v < 0 else max(1, v)

    def _find_effect(self, ref):
        if isinstance(ref, int):
            for g in self._gens.values():
                if g["id"] == ref:
                    return g
            return None
        return self._gens.get(ref)

    def _init_show(self):
        """載入 registry.json（播放清單 + auto_play）→ bus.shared["pixel_show"]。"""
        show = {"auto_play": False, "list": []}
        try:
            with open(REGISTRY_JSON) as f:
                d = json.load(f)
            show["auto_play"] = bool(d.get("auto_play", False))
            show["list"] = d.get("list", [])
        except OSError:
            get_log().warn("[Pixel] 找不到 {} — 關閉自動播放".format(REGISTRY_JSON))
        except Exception as e:
            get_log().error("[Pixel] 載入 {} 失敗: {}".format(REGISTRY_JSON, e))
        self._show = show
        bus.shared["pixel_show"] = show

        lst = []
        for ref in show["list"]:
            m = self._find_mode(ref)
            if m is None:
                get_log().warn("[Pixel] 播放清單引用未知 mode {!r} — 跳過".format(ref))
                continue
            lst.append(m)
        self._show_list = lst
        get_log().info("[Pixel] show: auto_play={} list={} 個".format(show["auto_play"], len(lst)))

    def _find_mode(self, ref):
        if isinstance(ref, int):
            return self._modes.get(ref)
        for m in self._modes.values():
            if m["name"] == ref:
                return m
        return None

    # ── 播放端：大隊列 show ───────────────────────
    def _start(self):
        self._playing = True
        self._paused = False
        self._pass = 1
        self._mode_idx = 0
        self._cur_repeat = 1
        self._cur_frames = 0
        self._appears = {}
        self._release_player(self._cur)
        self._cur = None
        self._cur_mode = None
        # 通知播放核（RenderTask）開始取幀；串流播放中不搶渲染旗標
        if not bus.shared.get("stream_active"):
            # 🔧 切換效果前清空 hub：上一個效果/檔案串流的舊幀還堆在 ring 裡，
            #    RenderTask 會先把舊幀逐幀渲染完才輪到新幀（約 10 幀 ≈ 330ms），
            #    看起來就是「轉換慢 / 沒生效」。先 flush 讓新幀立即排到最前。
            if self._hub is not None:
                self._hub.flush()
            bus.shared["is_streaming"] = True
            bus.shared["is_ready"] = True
            bus.shared["is_paused"] = False
        get_log().info("[Pixel] ▶ show 開始（{} mode(s)）".format(len(self._show_list)))

    def _stop(self):
        self._playing = False
        self._release_player(self._cur)
        self._cur = None
        self._cur_mode = None
        if not bus.shared.get("stream_active"):
            # 停止/熄燈：填中性值（燈=0 熄滅，motor=0x80 死區停），
            # 不能全清 0 —— UART-412 的 0 = 全速正轉！
            # 串流播放中不碰渲染旗標/不熄燈（那些是串流的）
            bus.shared["is_streaming"] = False
            bus.shared["is_ready"] = False
            # 🔧 先清 hub 再熄燈：否則殘留幀會在 clear_all 之後被 RenderTask
            #    重新讀出、把熄掉的燈又點亮（轉換時閃一下舊畫面）。
            if self._hub is not None:
                self._hub.flush()
            if self._st:
                self._st.clear_all()
        get_log().info("[Pixel] ■ show 停止")

    def _consume_cmds(self):
        if bus.shared.pop("pixel_stop", None) is not None:
            self._stop()
        if bus.shared.pop("pixel_play", None) is not None:
            self._start()
        if bus.shared.pop("pixel_pause", None) is not None:
            self._paused = not self._paused
            if not bus.shared.get("stream_active"):
                # 同步給播放核（RenderTask）：暫停時電機填中性值歸位（is_paused 分支）
                bus.shared["is_paused"] = self._paused
            get_log().info("[Pixel] ⏸ paused={}".format(self._paused))
        # 遠端停止本地模式 (0x3106 MODE_STOP) → 熄燈並還原 show list。
        # 先處理：可以取消尚未到期的延遲 MODE_SET（清掉 pending）。
        if bus.shared.pop("pixel_remote_stop", None) is not None:
            bus.shared.pop("pixel_remote_set", None)
            bus.shared.pop("pixel_remote_start_at", None)
            if self._orig_show_list is not None:
                self._show_list = self._orig_show_list
                self._orig_show_list = None
            self._stop()
            get_log().info("[Pixel] ■ remote stop (show list restored)")
        # 遠端指定播放單一本地模式 (0x3105 MODE_SET → pixel_actions 寫入)
        rid = bus.shared.pop("pixel_remote_set", None)
        if rid is not None:
            at = bus.shared.pop("pixel_remote_start_at", 0)
            if at and time.ticks_diff(time.ticks_ms(), at) < 0:
                # start_delay_ms 未到 → 放回，下一輪再檢查（非阻塞延遲播放）
                bus.shared["pixel_remote_set"] = rid
                bus.shared["pixel_remote_start_at"] = at
                return
            try:
                mode = self._modes.get(int(rid))
            except (TypeError, ValueError):
                mode = None
            if mode:
                if self._orig_show_list is None:
                    self._orig_show_list = self._show_list
                self._show_list = [mode]
                self._start()
                get_log().info("[Pixel] ▶ remote play mode {}".format(rid))
            else:
                get_log().warn("[Pixel] remote mode {} 不存在".format(rid))

    def _should_play(self, mode):
        """這一輪（循環）這個 mode 是否要播。

        play_loop     : 總共出現幾次循環（0=不播; N=最多 N 次; -1=常駐每輪）
        play_interval : 相隔多少個循環播一次（0=每個循環都播; 1=隔 1 循環=每 2 循環一次）
        """
        pl = mode["play_loop"]
        iv = mode["play_interval"]
        if pl == 0:
            return False                       # 永遠不播
        if (self._pass - 1) % (iv + 1) != 0:
            return False                       # 相隔循環
        if pl > 0 and self._appears.get(mode["id"], 0) >= pl:
            return False                       # 總出現次數已滿
        return True

    def _find_next(self, prev_mode=None):
        """掃一圈找下一個要播的 mode；沒有 → _cur 清空（空輪，pass 已推進）。

        prev_mode：剛播完的 mode。若下一個要播的正是同一個 mode（同一物件，
        例如播放清單連續放同一個 mode），重用現有播放器（restart 生成器），
        不釋放不重建 —— 避免「剷除 → 重建」在重複播放時造成卡頓。
        """
        lst = self._show_list
        for _ in range(len(lst)):
            mode = lst[self._mode_idx]
            self._mode_idx += 1
            if self._mode_idx >= len(lst):
                self._mode_idx = 0
                self._pass += 1
            if not self._should_play(mode):
                continue
            if (prev_mode is not None and mode is prev_mode
                    and self._cur is not None and self._restart_player(self._cur)):
                # 下一個與剛播完的是同一個 mode → 重用生成器（不剷除、不重建）
                pass
            else:
                self._release_player(self._cur)
                self._cur = self._make_player(mode)
            self._cur_mode = mode
            self._cur_repeat = 1
            self._cur_frames = 0
            self._appears[mode["id"]] = self._appears.get(mode["id"], 0) + 1
            return
        # 掃一圈沒找到要播的 → 空輪
        self._release_player(self._cur)
        self._cur = None
        self._cur_mode = None
        self._cur_repeat = 1
        self._cur_frames = 0

    def _make_player(self, mode):
        """mode → 播放器：每個 entry 一個 fresh generator（換 mode 時才重建）。

        同一個 mode 連續播放（重複播放）不經過這裡 —— _find_next 會直接
        restart 重用，避免「釋放 → 重建」造成卡頓。
        """
        return [{
            "mref": e["mref"], "gref": e["gref"], "write": e["write"],
            "offs": e.get("offs"),
            "gen": self._instantiate(e["cls"], e["name"], e["params"]),
            "done": False,
            "run_done": False,   # 已至少跑完一次完整效果（短效果循環結束判斷用）
        } for e in mode["entries"]]

    @staticmethod
    def _restart_player(player):
        """重用播放器：restart 每個 entry 的生成器，重置 done。

        全部 entry 都可 restart → 回 True（重用成功）；任何一個不支援
        restart（例如原生 generator 物件）→ 回 False，呼叫端改走剷除重建。
        """
        for e in player:
            gen = e.get("gen")
            if gen is None or not hasattr(gen, "restart"):
                return False
        for e in player:
            e["gen"].restart()
            e["done"] = False
            e["run_done"] = False
        return True

    @staticmethod
    def _instantiate(cls, name, params):
        """依 json 參數建立 Effect 實例（有 __next__/restart/seek）。"""
        return cls(name, params)

    @staticmethod
    def _release_player(player):
        """off 即丟：釋放每個 entry 的 effect 波緩衝（Effect.release()）。"""
        if player:
            for e in player:
                gen = e.get("gen")
                if gen is not None and hasattr(gen, "release"):
                    gen.release()

    def _tick_player(self, player):
        """播放器推進一幀。回傳 True = 還在播；False = 本次循環結束。

        短效果自己循環：entry 生成器耗盡 → restart 重播（長效果繼續播），
        直到「全部 entry 都至少跑完一次」= 長效果結束，本次循環才結束。
        生成器不支援 restart → 耗盡即定格（done，保持最後一幀）。
        maxF 達上限 → 強制結束本次循環。

        只負責「計算」：scatter 進 pixel_stream hub 的 slot 後 commit()，不碰硬體。
        硬體輸出由 RenderTask（core0）以固定 fps（20ms @ 50fps）節奏從 hub 取幀播放。
        """
        hub = self._hub
        if hub is None:
            return False
        view = hub.get_write_view()
        if view is None:
            # hub 滿（播放端還沒消化）→ 這幀跳過（drop），不阻塞計算核
            return True
        lay = self._lay
        alive = False
        for e in player:
            if e["done"]:
                continue
            try:
                vals = next(e["gen"])
            except StopIteration:
                e["run_done"] = True
                gen = e["gen"]
                if not hasattr(gen, "restart"):
                    e["done"] = True
                    continue
                try:
                    gen.restart()
                    vals = next(gen)
                except Exception:
                    e["done"] = True
                    continue
            offs = e.get("offs")
            if offs is not None:
                lay.scatter_offs(view, offs, vals, e["write"])
            else:
                lay.scatter(view, e["mref"], e["gref"], vals, e["write"])
            alive = True
        if not alive:
            return False
        if all(e.get("run_done") or e.get("done") for e in player):
            # 全部 entry 都至少跑完一次（= 長效果結束）→ 本次循環結束
            return False
        hub.commit()
        # maxF：本次播放已 commit 幀數達上限 → 強制結束本次循環
        maxf = (self._cur_mode or {}).get("maxF", 0) or 0
        if maxf > 0:
            self._cur_frames += 1
            if self._cur_frames >= maxf:
                for e in player:
                    e["done"] = True
        return True

    def loop(self):
        if not self.running:
            return
        self._consume_cmds()
        # ── 串流優先：stream_active=True（串流載入/播放中）→ 本地模式讓位，
        #    但保持 _playing 狀態不停止；串流結束（stream_active=False）自動恢復。
        if bus.shared.get("stream_active"):
            return
        # 串流結束自動恢復：本地模式還在播、但渲染旗標被串流清掉 → 重新宣告，
        # 讓 RenderTask 恢復取幀（配合 _consume_cmds 的 pixel_remote_set 流程）。
        if (self._playing and not self._paused
                and bus.shared.get("is_streaming") is not True):
            bus.shared["is_streaming"] = True
            bus.shared["is_ready"] = True
        if not self._playing or self._paused or self._st is None or self._hub is None:
            return
        # 計算核：全力算幀。hub 滿（播放核來不及消化）→ get_write_view 回 None，
        # _tick_player 直接 drop 該幀，不阻塞。播放節奏完全由 RenderTask（core0）控制。
        if self._cur is None:
            self._find_next()
            if self._cur is None:
                return
        if not self._tick_player(self._cur):
            # 本次循環結束 → play_count 未滿（或 -1 無限）則重播，否則找下一個
            mode = self._cur_mode or {}
            rep = mode.get("play_count", 1)
            if rep < 0 or self._cur_repeat < rep:
                if rep > 0:
                    self._cur_repeat += 1
                self._cur_frames = 0
                if not self._restart_player(self._cur):
                    # 生成器不支援 restart → 剷除重建
                    self._cur = self._make_player(mode)
            else:
                self._find_next(self._cur_mode)

    def on_stop(self):
        super().on_stop()
        self._playing = False
        self._orig_show_list = None
        self._release_player(self._cur)
        self._cur = None
        self._cur_mode = None
