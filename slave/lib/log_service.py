import time

_MAX_ENTRIES = 128
_SLOT_BYTES = 4


class LogService:
    def __init__(self):
        self._entries = []
        self._states = {}
        self._pending = []
        self._last_flush_ms = 0

        self._metric_names = []
        self._metric_index = {}
        self._buf = None
        self._allocated = False

    def immediate(self, msg):
        from lib.sys_bus import bus
        if not bus.shared.get("log_task_ready"):
            try:
                print(str(msg))
            except Exception:
                pass
            return
        self._push("immediate", msg)

    def info(self, msg):
        self._push("info", msg)

    def warn(self, msg):
        self._push("warn", msg)

    def error(self, msg):
        self._push("error", msg)

    def state(self, key, value):
        from lib.sys_bus import bus
        if not bus.shared.get("log_record", True):
            return
        self._states[key] = value

    def _push(self, level, msg):
        ts = time.ticks_ms()
        entry = (ts, level, str(msg))
        self._pending.append(entry)
        if len(self._pending) > _MAX_ENTRIES:
            self._pending.pop(0)

    def _resolve_settings(self):
        from lib.sys_bus import bus
        log_enabled = bus.shared.get("log_print", True)
        interval = bus.shared.get("log_print_interval_ms", 0)
        levels = bus.shared.get("log_print_levels", ["info", "warn", "error", "immediate"])
        show_params = bus.shared.get("log_print_params", True)
        return log_enabled, interval, levels, show_params

    def flush(self):
        if not self._pending:
            return

        log_enabled, interval, levels, show_params = self._resolve_settings()

        now = time.ticks_ms()
        if interval > 0:
            if time.ticks_diff(now, self._last_flush_ms) < interval:
                return
        self._last_flush_ms = now

        batch = list(self._pending)
        self._pending.clear()

        for entry in batch:
            self._entries.append(entry)

        if len(self._entries) > _MAX_ENTRIES:
            self._entries = self._entries[-_MAX_ENTRIES:]

        if not log_enabled:
            return

        for entry in batch:
            try:
                level, msg = entry[1], entry[2]
                if level not in levels:
                    continue
                if show_params:
                    print("[{}] {}".format(level.upper(), msg))
                else:
                    print(str(msg))
            except Exception:
                pass

    def get_recent(self, n=20):
        return list(self._entries[-n:])

    def get_states(self):
        return dict(self._states)

    def get_status(self):
        return {
            "log_count": len(self._entries),
            "recent": self.get_recent(10),
            "states": self.get_states(),
        }

    def register_metric(self, name):
        if self._allocated:
            return -1
        if name in self._metric_index:
            return self._metric_index[name]
        idx = len(self._metric_names)
        self._metric_names.append(name)
        self._metric_index[name] = idx
        return idx

    def allocate(self, slot_bytes=_SLOT_BYTES):
        if self._allocated:
            return
        n = len(self._metric_names)
        if n > 0:
            self._buf = bytearray(n * slot_bytes)
            for i in range(n * slot_bytes):
                self._buf[i] = 0
        self._slot_bytes = slot_bytes
        self._allocated = True

    def set_metric(self, name, value):
        if not self._allocated or self._buf is None:
            return
        idx = self._metric_index.get(name)
        if idx is None:
            return
        offset = idx * self._slot_bytes
        v = int(value) & 0xFFFFFFFF
        self._buf[offset] = v & 0xFF
        self._buf[offset + 1] = (v >> 8) & 0xFF
        self._buf[offset + 2] = (v >> 16) & 0xFF
        self._buf[offset + 3] = (v >> 24) & 0xFF

    def get_metric(self, name, default=0):
        if not self._allocated or self._buf is None:
            return default
        idx = self._metric_index.get(name)
        if idx is None:
            return default
        offset = idx * self._slot_bytes
        return (self._buf[offset] |
                (self._buf[offset + 1] << 8) |
                (self._buf[offset + 2] << 16) |
                (self._buf[offset + 3] << 24))

    def get_metric_names(self):
        return list(self._metric_names)


_log_instance = None


def get_log():
    global _log_instance
    if _log_instance is None:
        _log_instance = LogService()
    return _log_instance
