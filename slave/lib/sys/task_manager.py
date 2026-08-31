import gc
import time
from lib.sys.sys_bus import bus
from lib.sys.log_service import get_log, _viper_write_i32, _viper_read_i32
from lib.sys import watchdog as _wd

_FIXED = 5
_FIXED_BYTES = _FIXED * 4


class TaskManager:
    def __init__(self, ctx):
        self.ctx = ctx
        self.tasks = {}
        self.task_classes = {}
        self.config = {}
        self.layers = {}
        self._layer_enabled = {}
        self.active_tasks = {0: {}, 1: {}}
        self._boot_layer = 0
        self._boot_done = False
        self._max_layer = -1
        self._run_once_flags = {}
        self._perf_snapshot_ms = {0: 0, 1: 0}

        self._core_buf = bytearray(24)
        self._prealloc = {}

        # ── 效能優化:dirty flag + active_list 物化 ──
        # _dirty: per-core，標記該核是否需要重新評估啟停。
        #   runner_loop 只在 dirty 時才呼叫 _update_tasks，
        #   穩態下(無 affinity/layer 變更)完全跳過全表遍歷。
        # _active_list: per-core 的穩定 list，主循環直接遍歷它，
        #   不再每輪 tuple(active_tasks.items()) 重新分配。
        #   由 _update_tasks 在實際增刪任務時維護。
        self._dirty = {0: True, 1: True}
        self._active_list = {0: [], 1: []}

        bus.register_service("task_manager", self)

    @property
    def boot_phase(self):
        if self._boot_done:
            return "running"
        return self._boot_layer

    def _mark_dirty(self):
        """標記兩核都需要重新評估啟停(affinity/layer 變更後呼叫)。"""
        self._dirty[0] = True
        self._dirty[1] = True

    def advance_to_running(self):
        if not self._boot_done:
            self._boot_done = True
            self._mark_dirty()
            log = get_log()
            log.info("\u2699 [TM] Boot \u2192 running (forced)")

    def enable_layer(self, layer):
        self._layer_enabled[layer] = True
        self._mark_dirty()

    def disable_layer(self, layer):
        self._layer_enabled[layer] = False
        self._mark_dirty()

    def is_layer_enabled(self, layer):
        return self._layer_enabled.get(layer, True)

    def _check_affinity(self, name, task_cls, affinity):
        """硬體歸屬防呆：宣告 hw=("lcd",) 的 task 禁止排到 core1。

        lcd_bus 的 DMA queue 不是 thread-safe，LVGL 若跟
        core1 的任務同時碰 SPI1 會直接崩潰，所以這裡直接擋下。"""
        hw = tuple(getattr(task_cls, "hw", ()))
        if "lcd" in hw and affinity[1] == 1:
            log = get_log()
            log.error(
                "\u26d4 Task [{}] requires LCD/SPI(core0 only) — "
                "affinity {} rejected, forcing (1,0)".format(name, affinity)
            )
            return (1, 0)
        return affinity

    def register_task(self, name, task_cls, default_affinity=(0, 0), layer=0, run_once=False):
        # 硬體歸屬檢查：LCD task 強制 core0（見 _check_affinity）
        default_affinity = self._check_affinity(name, task_cls, default_affinity)
        self.task_classes[name] = task_cls
        self.config[name] = default_affinity
        self.layers[name] = int(layer)
        if int(layer) > self._max_layer and int(layer) >= 0:
            self._max_layer = int(layer)
        self._run_once_flags[name] = run_once
        self._mark_dirty()

        log = get_log()
        log.info("Task [{}] L{} affinity {}".format(name, int(layer), default_affinity))

    def finalize(self):
        log = get_log()
        from lib.sys.sys_bus import bus
        bus.shared["_core_buf"] = self._core_buf

        self._task_schema_idx = {}
        task_bufs = {}
        for name, cls in self.task_classes.items():
            lbuf = bytearray(_FIXED_BYTES)
            task_bufs[name] = lbuf

            schema = getattr(cls, "log_schema", None)
            if schema:
                n = len(schema)
                lbuf_ex = bytearray(n * 4)
                idx_map = {}
                for i, m in enumerate(schema):
                    log.register_slot(m, lbuf_ex, i * 4)
                    idx_map[m] = i
                self._task_schema_idx[name] = idx_map
            else:
                lbuf_ex = None

            self._prealloc[name] = (lbuf, lbuf_ex)

        bus.shared["_task_bufs"] = task_bufs

    def _alloc_task_bufs(self, name, task):
        pre = self._prealloc.get(name)
        if pre is not None:
            task._lbuf, task._lbuf_ex = pre

    def set_affinity(self, name, affinity):
        log = get_log()
        if affinity == (1, 1):
            log.error("Task [{}] cannot run on both cores simultaneously.".format(name))
            return False
        # 硬體歸屬檢查：LCD task 禁止排到 core1（見 _check_affinity）
        task_cls = self.task_classes.get(name)
        if task_cls is not None:
            affinity = self._check_affinity(name, task_cls, affinity)
        self.config[name] = affinity
        self._mark_dirty()
        log.info("Task [{}] affinity \u2192 {}".format(name, affinity))
        return True

    def get_status(self):
        rows = []
        for name in self.config:
            affinity = self.config.get(name, (0, 0))
            layer = self.layers.get(name, -1)
            running_core = None
            for core in (0, 1):
                if name in self.active_tasks[core]:
                    running_core = core
                    break
            rows.append({
                "name": name,
                "layer": layer,
                "affinity": list(affinity),
                "running_core": running_core,
                "running": running_core is not None,
            })
        return {
            "boot_phase": self.boot_phase,
            "boot_layer": self._boot_layer,
            "boot_done": self._boot_done,
            "tasks": rows,
        }

    def get_registered_task_names(self):
        return list(self.config.keys())

    def _task_eligible_for_boot(self, name):
        layer = self.layers.get(name, 0)
        if layer == -1:
            return False
        if self._boot_done:
            return self.is_layer_enabled(layer)
        return layer <= self._boot_layer

    def _update_tasks(self, core_id):
        current_config = list(self.config.items())
        log = get_log()
        active_list = self._active_list[core_id]

        for name, affinity in current_config:
            should_run = (affinity[core_id] == 1)
            if should_run and not self._task_eligible_for_boot(name):
                should_run = False

            is_running = name in self.active_tasks[core_id]

            if should_run and not is_running:
                if name not in self.tasks:
                    if name in self.task_classes:
                        try:
                            new_task = self.task_classes[name](name, self.ctx)
                            run_once = self._run_once_flags.get(name, False)
                            new_task.run_once = run_once
                            self._alloc_task_bufs(name, new_task)
                            self.tasks[name] = new_task
                        except Exception as e:
                            log.error("\u274c [Core {}] Failed to instantiate {}: {}".format(core_id, name, e))
                            continue
                    else:
                        log.warn("\u26a0\ufe0f [Core {}] Task class for {} not found!".format(core_id, name))
                        continue

                task = self.tasks[name]
                log.info("[Core {}] Starting task: {}".format(core_id, name))
                try:
                    task.on_start()
                    self.active_tasks[core_id][name] = task
                    active_list.append(task)
                except Exception as e:
                    log.error("\u274c [Core {}] Failed to start {}: {}".format(core_id, name, e))

            elif not should_run and is_running:
                task = self.active_tasks[core_id][name]
                log.info("[Core {}] Stopping task: {}".format(core_id, name))
                try:
                    task.on_stop()
                except Exception as e:
                    log.error("\u274c [Core {}] Error stopping {}: {}".format(core_id, name, e))
                del self.active_tasks[core_id][name]
                try:
                    active_list.remove(task)
                except ValueError:
                    pass

        if not self._boot_done:
            self._check_boot_layer_done()

    def _check_boot_layer_done(self):
        log = get_log()
        layer = self._boot_layer
        all_ok = True
        for name, cfg in self.config.items():
            if self.layers.get(name) != layer:
                continue
            if layer == -1:
                continue
            if cfg[0] == 1 and name not in self.active_tasks[0]:
                all_ok = False
                break
            if cfg[1] == 1 and name not in self.active_tasks[1]:
                all_ok = False
                break
            if name == "fs_scan" and not bus.shared.get("fs_scan_done"):
                if bus.shared.get("fs_scan_requested"):
                    all_ok = False
                    break
        if not all_ok:
            return

        if layer >= self._max_layer or self._max_layer < 0:
            if not self._boot_done:
                self._boot_done = True
                log.info("\u2699 [TM] Boot complete \u2192 running")
        else:
            self._boot_layer += 1
            # layer 推進: 下一層任務可能變成 eligible, 需重新評估
            self._mark_dirty()
            log.info("\u2699 [TM] Boot layer {}".format(self._boot_layer))

    def _snapshot_task_perf(self, core_id, perf_enabled=True):
        for name, task in self.active_tasks[core_id].items():
            b = task._lbuf
            if b is None:
                continue
            p = task.perf
            if perf_enabled:
                _viper_write_i32(b, 0, p["loop_total_us"] // max(p["loop_count"], 1))
                _viper_write_i32(b, 4, p["loop_max_us"])
                _viper_write_i32(b, 8, p["loop_count"])
            _viper_write_i32(b, 12, task.touch)
            _viper_write_i32(b, 16, task.success)
            p["loop_count"] = 0
            p["loop_total_us"] = 0
            p["loop_max_us"] = 0
            task.touch = 0
            task.success = 0

    def runner_loop(self, core_id):
        log = get_log()
        log.info("\U0001f680 [Core {}] Task Runner Started".format(core_id))

        time.sleep_ms(100 if core_id == 0 else 500)

        loop_count = 0
        start_time = time.ticks_ms()

        # ── 分批驗證鉤子注入 (core0 only) ──
        #   全檔 SHA 讀取迴圈 (fs_manager) 每 ~256KB 讓步一次; 若 WDT 已建立,
        #   讓步 = 餵狗 + sleep_ms(0), 避免大檔驗證同步阻塞卡死 core0 超過
        #   WDT timeout 被 TWDT 復位。WDT 未建立 (enable=0) → 不注入, 鉤子維持
        #   no-op, fs_manager 模組零 WDT 耦合 (由啟動方決定餵狗策略)。
        if core_id == 0:
            try:
                _wdt0 = bus.get_service("wdt")
                if _wdt0 is not None:
                    from lib.sys.fs_manager import set_yield_cb

                    def _fs_yield():
                        _wdt0.feed()
                        time.sleep_ms(0)

                    set_yield_cb(_fs_yield)
            except Exception:
                pass

        _need_task_perf = False
        _need_core_metrics = False
        _log_cfg_refresh_ms = 0
        _perf_interval_ms = 1000
        _engine_run = True
        _engine_refresh_ms = 0

        while True:
            now_ms = time.ticks_ms()

            # ── WDT：主線程（core0）直接餵狗 + 測試模式 re-arm 檢查 ──
            # 同執行緒建立/餵（lib/sys/watchdog.py），無跨核心、無額外執行緒、
            # 無獨立任務（poll_rearm 就是大循環的一步，每圈執行一次）。
            # 系統卡死（本 runner 停止）→ 不餵 → WDT 重置；
            # 測試模式（enable=0）沉默逾時 → poll_rearm 存 enable=1 + 重啟。
            if core_id == 0:
                try:
                    _wdt = bus.get_service("wdt")
                    if _wdt is not None:
                        _wdt.feed()
                    _wd.poll_rearm()
                except Exception:
                    pass

            if time.ticks_diff(now_ms, _engine_refresh_ms) > 500:
                _engine_run = bus.shared.get("engine_run", True)
                _engine_refresh_ms = now_ms

            if not _engine_run:
                break

            if time.ticks_diff(now_ms, _log_cfg_refresh_ms) > 500:
                names = bus.shared.get("log_subscribe", [])
                if names == "__list__" or names is None:
                    names = []
                if not isinstance(names, (list, tuple)):
                    names = []
                interval = bus.shared.get("log_print_interval_ms", 1000)
                try:
                    interval = int(interval or 1000)
                except Exception:
                    interval = 1000
                if interval <= 0:
                    interval = 1000
                _perf_interval_ms = interval
                cfg = self.config
                need_task_perf = False
                need_core_metrics = False
                for n in names:
                    if n in cfg:
                        need_task_perf = True
                    if n == "cpu0" or n == "cpu1":
                        need_core_metrics = True
                    if isinstance(n, str) and (n.startswith("core0_") or n.startswith("core1_")):
                        need_core_metrics = True
                _need_task_perf = need_task_perf
                _need_core_metrics = need_core_metrics
                _log_cfg_refresh_ms = now_ms

            # ── dirty flag: 只在 affinity/layer 變更時才重新評估啟停 ──
            # 穩態下完全跳過 _update_tasks, 消除每輪全表遍歷開銷。
            # 先清後跑: 讓 _check_boot_layer_done 推進 layer 時能重設 dirty。
            if self._dirty[core_id]:
                self._dirty[core_id] = False
                self._update_tasks(core_id)

            # ── 每個 runner 週期都計數 ──
            if _need_core_metrics:
                loop_count += 1
                duration = time.ticks_diff(now_ms, start_time)
                if duration >= _perf_interval_ms:
                    off = core_id * 12
                    _viper_write_i32(self._core_buf, off, loop_count)
                    loop_count = 0
                    start_time = now_ms

            active_list = self._active_list[core_id]
            if not active_list:
                time.sleep_ms(0)
                continue

            # 直接遍歷物化的 active_list, 不再每輪 tuple(items()) 分配。
            # 複製一份快照供本輪遍歷(run_once 完成時會修改 active_list)。
            current_tasks = list(active_list)

            for task in current_tasks:
                try:
                    if _need_task_perf:
                        t_task0 = time.ticks_us()
                    task.loop()
                    if _need_task_perf:
                        t_task1 = time.ticks_us()
                        elapsed = time.ticks_diff(t_task1, t_task0)
                        task.perf["loop_us"] = elapsed
                        task.perf["loop_count"] += 1
                        task.perf["loop_total_us"] += elapsed
                        if elapsed > task.perf["loop_max_us"]:
                            task.perf["loop_max_us"] = elapsed
                    task.touch += 1

                    if task.run_once:
                        name = task.name
                        log.info("[Core {}] One-shot task {} finished. Stopping.".format(core_id, name))
                        try:
                            task.on_stop()
                        except Exception:
                            pass
                        if name in self.active_tasks[core_id]:
                            del self.active_tasks[core_id][name]
                        try:
                            active_list.remove(task)
                        except ValueError:
                            pass
                        self.config[name] = (0, 0)

                except Exception as e:
                    log.error("\u274c [Core {}] Task {} Loop Error: {}".format(core_id, task.name, e))
                    time.sleep_ms(1000)

            time.sleep_ms(0)

            if time.ticks_diff(now_ms, self._perf_snapshot_ms[core_id]) >= _perf_interval_ms:
                self._perf_snapshot_ms[core_id] = now_ms
                self._snapshot_task_perf(core_id, _need_task_perf)

        log.info("\U0001f6d1 [Core {}] Runner Stopped".format(core_id))
