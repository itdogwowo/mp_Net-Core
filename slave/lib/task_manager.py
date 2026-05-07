import gc
import time
from lib.sys_bus import bus

class TaskManager:
    def __init__(self, ctx):
        self.ctx = ctx
        self.tasks = {}
        self.task_classes = {}
        self.config = {}
        self.layers = {}
        self._layer_enabled = {}
        self.active_tasks = {0: {}, 1: {}}
        self._cached_tasks = {0: [], 1: []}
        self._tasks_dirty = {0: True, 1: True}
        self._last_update_ms = {0: 0, 1: 0}
        self._boot_layer = 0
        self._boot_done = False
        self._max_layer = -1
        self._run_once_flags = {}

        bus.register_service("task_manager", self)

    @property
    def boot_phase(self):
        if self._boot_done:
            return "running"
        return self._boot_layer

    def advance_to_running(self):
        if not self._boot_done:
            self._boot_done = True
            print("⚙ [TM] Boot → running (forced)")

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
        print(f"Task [{name}] L{int(layer)} affinity {default_affinity}")

    def set_affinity(self, name, affinity):
        if affinity == (1, 1):
            print(f"Error: Task [{name}] cannot run on both cores simultaneously.")
            return False
        self.config[name] = affinity
        print(f"Task [{name}] affinity → {affinity}")
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

    def _task_eligible_for_boot(self, name):
        layer = self.layers.get(name, 0)
        if layer == -1:
            return False
        if self._boot_done:
            return self.is_layer_enabled(layer)
        return layer <= self._boot_layer

    def _update_tasks(self, core_id):
        for name, affinity in self.config.items():
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
                            print(f"❌ [Core {core_id}] Failed to instantiate {name}: {e}")
                            continue
                    else:
                        print(f"⚠️ [Core {core_id}] Task class for {name} not found!")
                        continue

                task = self.tasks[name]
                print(f"[Core {core_id}] Starting task: {name}")
                try:
                    task.on_start()
                    self.active_tasks[core_id][name] = task
                    self._tasks_dirty[core_id] = True
                except Exception as e:
                    print(f"❌ [Core {core_id}] Failed to start {name}: {e}")

            elif not should_run and is_running:
                task = self.active_tasks[core_id][name]
                print(f"[Core {core_id}] Stopping task: {name}")
                try:
                    task.on_stop()
                except Exception as e:
                    print(f"❌ [Core {core_id}] Error stopping {name}: {e}")
                del self.active_tasks[core_id][name]
                self._tasks_dirty[core_id] = True

        if not self._boot_done:
            self._check_boot_layer_done()

    def _maybe_update_tasks(self, core_id):
        now = time.ticks_ms()
        if time.ticks_diff(now, self._last_update_ms[core_id]) >= self._update_interval_ms(core_id):
            self._last_update_ms[core_id] = now
            self._update_tasks(core_id)
            self._rebuild_cached_tasks(core_id)

    def _update_interval_ms(self, core_id):
        if self._tasks_dirty.get(core_id, True):
            return 0
        if not self._boot_done:
            return 20
        if not self.active_tasks[core_id]:
            return 100
        return 250

    def _rebuild_cached_tasks(self, core_id):
        if not self._tasks_dirty.get(core_id, True):
            return
        active = self.active_tasks[core_id]
        self._cached_tasks[core_id] = list(active.items())
        self._tasks_dirty[core_id] = False

    def _check_boot_layer_done(self):
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
                print("")
                print("⚙ [TM] Boot complete → running")
        else:
            self._boot_layer += 1
            print("")
            print(f"⚙ [TM] Boot layer {self._boot_layer}")

    def runner_loop(self, core_id):
        print(f"🚀 [Core {core_id}] Task Runner Started")

        time.sleep_ms(100 if core_id == 0 else 500)

        loop_count = 0
        start_time = time.ticks_ms()
        busy_total_us = 0
        perf_enabled = bus.shared.get("perf_enabled", False)
        engine_run = bus.shared

        if "perf" not in bus.shared:
            bus.shared["perf"] = {}

        while engine_run.get("engine_run", True):
            self._maybe_update_tasks(core_id)

            cached = self._cached_tasks[core_id]
            if not cached:
                time.sleep_ms(100)
                loop_count = 0
                start_time = time.ticks_ms()
                busy_total_us = 0
                continue

            need_rebuild = False
            t0 = time.ticks_us() if perf_enabled else 0

            for name, task in cached:
                try:
                    task.loop()

                    if getattr(task, 'run_once', False):
                        print(f"[Core {core_id}] One-shot task {name} finished. Stopping.")
                        try:
                            task.on_stop()
                        except Exception:
                            pass
                        self.active_tasks[core_id].pop(name, None)
                        self.config[name] = (0, 0)
                        need_rebuild = True

                except Exception as e:
                    print(f"❌ [Core {core_id}] Task {task.name} Loop Error: {e}")
                    time.sleep_ms(1000)

            if need_rebuild:
                self._tasks_dirty[core_id] = True

            time.sleep_ms(1)
            if perf_enabled:
                t1 = time.ticks_us()
                busy_total_us += time.ticks_diff(t1, t0)
                loop_count += 1
                now = time.ticks_ms()
                duration = time.ticks_diff(now, start_time)

                if duration >= 2000 and loop_count > 0:
                    avg_tick_us = busy_total_us // loop_count
                    elapsed_total_us = duration * 1000
                    idle_pct = max(0, 100 - (busy_total_us * 100 // elapsed_total_us))
                    try:
                        bus.shared["perf"][f"core{core_id}_tick_us"] = avg_tick_us
                        bus.shared["perf"][f"core{core_id}_idle_pct"] = idle_pct
                        bus.shared["perf"][f"core{core_id}_loops_per_sec"] = (loop_count * 1000) // duration
                    except Exception:
                        bus.shared["perf"] = {
                            f"core{core_id}_tick_us": avg_tick_us,
                            f"core{core_id}_idle_pct": idle_pct,
                            f"core{core_id}_loops_per_sec": (loop_count * 1000) // duration,
                        }

                    loop_count = 0
                    start_time = now
                    busy_total_us = 0

        print(f"🛑 [Core {core_id}] Runner Stopped")
