import time
from lib.sys.task import Task
from lib.sys.log_service import get_log, _viper_read_i32
from lib.sys.sys_bus import bus


class LogTask(Task):
    def on_start(self):
        super().on_start()
        bus.shared["log_task_ready"] = True
        self._cpu0 = False
        self._cpu1 = False
        self._core_buf = None
        self._rows = ()
        self._others = ()
        self._last_print_ms = 0

        names = bus.shared.get("log_subscribe", [])
        if not isinstance(names, (list, tuple)) and names != "__list__":
            names = []
        log = get_log()

        if names == "__list__":
            all_names = log.get_metric_names()
            task_bufs = bus.shared.get("_task_bufs", {})
            custom = sorted(n for n in all_names if not (str(n).startswith("core0_") or str(n).startswith("core1_")))
            tnames = sorted(task_bufs)
            print("[LOG] -- copy-paste subscribe list ----------------------------------")
            print("subscribe = [")
            print('    "cpu0",')
            print('    "cpu1",')
            for n in custom:
                print('    "' + n + '",')
            for tn in tnames:
                print('    "' + tn + '",')
            print("]")
            return

        task_bufs = bus.shared.get("_task_bufs", {})
        core_buf = bus.shared.get("_core_buf")
        if isinstance(names, (list, tuple)):
            for n in names:
                if n == "cpu0":
                    self._cpu0 = True
                elif n == "cpu1":
                    self._cpu1 = True
        self._core_buf = core_buf

        sub_tasks = set()
        sub_names = []
        for n in names:
            if n == "cpu0" or n == "cpu1":
                continue
            if n in task_bufs:
                sub_tasks.add(n)
            else:
                sub_names.append(n)

        if sub_tasks:
            self._rows = tuple((tn, b) for tn, b in sorted(task_bufs.items()) if tn in sub_tasks)
        else:
            self._rows = ()

        if sub_names:
            slots = log.subscribe(sub_names)
            self._others = tuple((n, b, o) for n, b, o in slots)
        else:
            self._others = ()

        self._last_print_ms = 0

    def loop(self):
        if not self.running:
            return

        rows = self._rows
        others = self._others

        now = time.ticks_ms()
        interval = int(bus.shared.get("log_print_interval_ms", 1000) or 1000)
        if interval <= 0:
            interval = 1000
        if time.ticks_diff(now, self._last_print_ms) < interval:
            return
        self._last_print_ms = now

        log = get_log()
        if not rows and not others and not self._cpu0 and not self._cpu1:
            # 沒有訂閱任何 metrics 時，仍需定期 flush 一般 info/warn/error 日誌。
            log.flush()
            return

        tm = bus.get_service("task_manager")
        live_names = None
        if tm is not None:
            live_names = set()
            for core in (0, 1):
                live_names.update(tm.active_tasks.get(core, {}).keys())

        out = []

        for (task_name, buf) in rows:
            if live_names is not None and task_name not in live_names:
                continue
            avg = _viper_read_i32(buf, 0)
            mx = _viper_read_i32(buf, 4)
            cnt = _viper_read_i32(buf, 8)
            touch_v = _viper_read_i32(buf, 12)
            succ_v = _viper_read_i32(buf, 16)
            if avg <= 0 and touch_v <= 0 and succ_v <= 0:
                continue
            out.append("Task[" + task_name + "] avg=" + str(avg) + "us max=" +
                       str(mx) + "us n=" + str(cnt) + " t=" + str(touch_v) +
                       " s=" + str(succ_v))

        core_buf = self._core_buf
        if core_buf:
            if self._cpu0:
                loops = _viper_read_i32(core_buf, 0)
                out.append("CPU0 loops=" + str(loops))
            if self._cpu1:
                loops = _viper_read_i32(core_buf, 12)
                out.append("CPU1 loops=" + str(loops))

        for name, buf, off in others:
            v = _viper_read_i32(buf, off)
            if v > 0:
                out.append(str(name) + "=" + str(v))

        if out:
            print("[IMMEDIATE]")
            for line in out:
                print("  - " + line)
        log.flush()
