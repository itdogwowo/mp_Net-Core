import time
import micropython

_MAX_ENTRIES = 128


@micropython.viper
def _viper_write_i32(buf, offset: int, val: int):
    p = ptr8(buf)
    o = int(offset)
    p[o] = val & 0xFF
    p[o + 1] = (val >> 8) & 0xFF
    p[o + 2] = (val >> 16) & 0xFF
    p[o + 3] = (val >> 24) & 0xFF


@micropython.viper
def _viper_read_i32(buf, offset: int) -> int:
    p = ptr8(buf)
    o = int(offset)
    return p[o] | (p[o + 1] << 8) | (p[o + 2] << 16) | (p[o + 3] << 24)


class LogService:
    def __init__(self):
        self._entries = []
        self._states = {}
        self._pending = []
        self._last_flush_ms = 0
        self._slots = {}

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
        show_params = True
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

    def register_slot(self, name, buf, offset):
        self._slots[name] = (buf, offset)

    def set_metric(self, name, value):
        slot = self._slots.get(name)
        if slot:
            _viper_write_i32(slot[0], slot[1], value)

    def subscribe(self, names):
        result = []
        for name in names:
            slot = self._slots.get(name)
            if slot:
                result.append((name, slot[0], slot[1]))
        return result

    def get_metric_names(self):
        return list(self._slots.keys())


_log_instance = None


def get_log():
    global _log_instance
    if _log_instance is None:
        _log_instance = LogService()
    return _log_instance
