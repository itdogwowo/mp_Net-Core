import time
from lib.task import Task
from lib.log_service import get_log, _viper_read_i32


class LogTask(Task):
    def on_start(self):
        super().on_start()
        from lib.sys_bus import bus
        bus.shared["log_task_ready"] = True

        names = bus.shared.get("log_subscribe", [])
        log = get_log()

        if names == "__list__":
            all_names = log.get_metric_names()
            task_bufs = bus.shared.get("_task_bufs", {})
            custom = sorted(all_names)
            tnames = sorted(task_bufs)
            print("[LOG] -- copy-paste subscribe list ----------------------------------")
            print("subscribe = [")
            for n in custom:
                print('    "{}",'.format(n))
            for tn in tnames:
                print('    "{}",'.format(tn))
            print("]")
            print("[LOG] {} custom + {} task slots total".format(len(custom), len(tnames)))
            self._rows = ()
            self._others = ()
            return

        task_bufs = bus.shared.get("_task_bufs", {})
        sub_tasks = set()
        sub_names = []
        for n in names:
            if n in task_bufs:
                sub_tasks.add(n)
            else:
                sub_names.append(n)

        if sub_tasks:
            self._rows = tuple((tn, b) for tn, b in sorted(task_bufs.items()) if tn in sub_tasks)
        elif sub_names:
            self._rows = ()
        else:
            self._rows = tuple((tn, b) for tn, b in sorted(task_bufs.items()))

        if not sub_names:
            self._others = ()
        else:
            slots = log.subscribe(sub_names)
            self._others = tuple((n, b, o) for n, b, o in slots)

        self._last_print_ms = 0

    def loop(self):
        if not self.running:
            return
        log = get_log()
        log.flush()

        rows = self._rows
        others = self._others
        if not rows and not others:
            return

        now = time.ticks_ms()
        if time.ticks_diff(now, self._last_print_ms) < 1000:
            return
        self._last_print_ms = now

        for (task_name, buf) in rows:
            avg = _viper_read_i32(buf, 0)
            max_us = _viper_read_i32(buf, 4)
            count = _viper_read_i32(buf, 8)
            touch_v = _viper_read_i32(buf, 12)
            succ_v = _viper_read_i32(buf, 16)
            if avg <= 0 and touch_v <= 0 and succ_v <= 0:
                continue
            log.immediate("Task[{}] avg={}us max={}us n={} t={} s={}".format(
                task_name, avg, max_us, count, touch_v, succ_v))

        for name, buf, off in others:
            v = _viper_read_i32(buf, off)
            if v > 0:
                log.immediate("{}={}".format(name, v))
