import gc
import time
from lib.sys_bus import bus
from lib.log_service import get_log


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

        log = get_log()
        log.register_metric("core0_idle_pct")
        log.register_metric("core0_tick_us")
        log.register_metric("core0_loops_per_sec")
        log.register_metric("core1_idle_pct")
        log.register_metric("core1_tick_us")
        log.register_metric("core1_loops_per_sec")

        bus.register_service("task_manager", self)

    @property
    def boot_phase(self):
        if self._boot_done:
            return "running"
        return self._boot_layer

    def advance_to_running(self):
        if not self._boot_done:
            self._boot_done = True
            log = get_log()
            log.info("⚙ [TM] Boot → running (forced)")

    def enable_layer(self, layer):
        self._layer_enabled[layer] = True

    def disable_layer(self, layer):
        self._layer_enabled[layer] = False

    def is_layer_enabled(self, layer):
        return self._layer_enabled.get(layer, True)

    def register_task(self, name, task_cls, default_affinity=(0, 0), layer=0, run_once=False):
        self.task_classes[name] = task_cls
        self.config[name] = default_affinity
        self.layers[name] = int(layer)
        if int(layer) > self._max_layer and int(layer) >= 0:
            self._max_layer = int(layer)
        self._run_once_flags[name] = run_once

        log = get_log()
        log.info("Task [{}] L{} affinity {}".format(name, int(layer), default_affinity))

        schema = getattr(task_cls, "log_schema", None)
        if schema:
            for m in schema:
                log.register_metric(m)

        log.register_metric("t_{}_avg_us".format(name))
        log.register_metric("t_{}_max_us".format(name))
        log.register_metric("t_{}_count".format(name))

    def set_affinity(self, name, affinity):
        log = get_log()
        if affinity == (1, 1):
            log.error("Task [{}] cannot run on both cores simultaneously.".format(name))
            return False
        self.config[name] = affinity
        log.info("Task [{}] affinity → {}".format(name, affinity))
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
                            self.tasks[name] = new_task
                        except Exception as e:
                            log.error("❌ [Core {}] Failed to instantiate {}: {}".format(core_id, name, e))
                            continue
                    else:
                        log.warn("⚠️ [Core {}] Task class for {} not found!".format(core_id, name))
                        continue

                task = self.tasks[name]
                log.info("[Core {}] Starting task: {}".format(core_id, name))
                try:
                    task.on_start()
                    self.active_tasks[core_id][name] = task
                except Exception as e:
                    log.error("❌ [Core {}] Failed to start {}: {}".format(core_id, name, e))

            elif not should_run and is_running:
                task = self.active_tasks[core_id][name]
                log.info("[Core {}] Stopping task: {}".format(core_id, name))
                try:
                    task.on_stop()
                except Exception as e:
                    log.error("❌ [Core {}] Error stopping {}: {}".format(core_id, name, e))
                del self.active_tasks[core_id][name]

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
                log.info("⚙ [TM] Boot complete → running")
        else:
            self._boot_layer += 1
            log.info("⚙ [TM] Boot layer {}".format(self._boot_layer))

    def _snapshot_task_perf(self, core_id, now_ms):
        if time.ticks_diff(now_ms, self._perf_snapshot_ms[core_id]) < 2000:
            return
        self._perf_snapshot_ms[core_id] = now_ms

        log = get_log()
        for name, task in self.active_tasks[core_id].items():
            snap = task.perf_snapshot()
            task.perf_reset()
            log.set_metric("t_{}_avg_us".format(name), snap.get("loop_avg_us", 0))
            log.set_metric("t_{}_max_us".format(name), snap.get("loop_max_us", 0))
            log.set_metric("t_{}_count".format(name), snap.get("loop_count", 0))

    def runner_loop(self, core_id):
        log = get_log()
        log.info("🚀 [Core {}] Task Runner Started".format(core_id))

        time.sleep_ms(100 if core_id == 0 else 500)

        loop_count = 0
        start_time = time.ticks_ms()
        busy_total_us = 0

        _perf_enabled = True
        _perf_refresh_ms = 0
        _engine_run = True
        _engine_refresh_ms = 0

        while True:
            now_ms = time.ticks_ms()

            if time.ticks_diff(now_ms, _engine_refresh_ms) > 500:
                _engine_run = bus.shared.get("engine_run", True)
                _engine_refresh_ms = now_ms

            if not _engine_run:
                break

            if time.ticks_diff(now_ms, _perf_refresh_ms) > 2000:
                _perf_enabled = bus.shared.get("perf_enabled", True)
                _perf_refresh_ms = now_ms

            if _perf_enabled:
                t0 = time.ticks_us()

            self._update_tasks(core_id)

            if not self.active_tasks[core_id]:
                time.sleep_ms(100)
                loop_count = 0
                start_time = time.ticks_ms()
                busy_total_us = 0
                continue

            current_tasks = list(self.active_tasks[core_id].items())

            for name, task in current_tasks:
                try:
                    if _perf_enabled:
                        t_task0 = time.ticks_us()
                    task.loop()
                    if _perf_enabled:
                        t_task1 = time.ticks_us()
                        elapsed = time.ticks_diff(t_task1, t_task0)
                        task.perf["loop_us"] = elapsed
                        task.perf["loop_count"] += 1
                        task.perf["loop_total_us"] += elapsed
                        if elapsed > task.perf["loop_max_us"]:
                            task.perf["loop_max_us"] = elapsed

                    if getattr(task, 'run_once', False):
                        log.info("[Core {}] One-shot task {} finished. Stopping.".format(core_id, name))
                        try:
                            task.on_stop()
                        except Exception:
                            pass
                        del self.active_tasks[core_id][name]
                        self.config[name] = (0, 0)

                except Exception as e:
                    log.error("❌ [Core {}] Task {} Loop Error: {}".format(core_id, task.name, e))
                    time.sleep_ms(1000)

            time.sleep_ms(0)
            if _perf_enabled:
                t1 = time.ticks_us()
                busy_total_us += time.ticks_diff(t1, t0)
                loop_count += 1
                duration = time.ticks_diff(now_ms, start_time)

                if duration >= 2000 and loop_count > 0:
                    avg_tick_us = busy_total_us // loop_count
                    elapsed_total_us = duration * 1000
                    idle_pct = max(0, 100 - (busy_total_us * 100 // elapsed_total_us))
                    hz = (loop_count * 1000) // duration

                    log.set_metric("core{}_tick_us".format(core_id), avg_tick_us)
                    log.set_metric("core{}_idle_pct".format(core_id), idle_pct)
                    log.set_metric("core{}_loops_per_sec".format(core_id), hz)

                    self._snapshot_task_perf(core_id, now_ms)

                    loop_count = 0
                    start_time = now_ms
                    busy_total_us = 0

        log.info("🛑 [Core {}] Runner Stopped".format(core_id))
