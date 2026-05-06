import time

_MAX_ENTRIES = 128


class LogService:
    def __init__(self):
        self._entries = []
        self._states = {}
        self._pending = []

    def immediate(self, msg):
        from lib.sys_bus import bus
        if not bus.shared.get("log_task_ready"):
            try:
                print(str(msg))
            except Exception:
                pass
            return
        self._push(">>>", msg)

    def info(self, msg):
        from lib.sys_bus import bus
        if bus.shared.get("log_print", True):
            self._push("info", msg)

    def warn(self, msg):
        from lib.sys_bus import bus
        if bus.shared.get("log_print", True):
            self._push("warn", msg)

    def error(self, msg):
        from lib.sys_bus import bus
        if bus.shared.get("log_print", True):
            self._push("error", msg)

    def state(self, key, value):
        from lib.sys_bus import bus
        if not bus.shared.get("log_record", True):
            return
        self._states[key] = value

    def _push(self, level, msg):
        ts = time.ticks_ms()
        entry = (ts, level, str(msg))
        if level == ">>>":
            self._pending.insert(0, entry)
        else:
            self._pending.append(entry)
        if len(self._pending) > _MAX_ENTRIES:
            self._pending.pop(0)

    def flush(self):
        from lib.sys_bus import bus
        if not self._pending:
            return
        batch = list(self._pending)
        self._pending.clear()
        log_enabled = bus.shared.get("log_print", True)
        for entry in batch:
            if not log_enabled and entry[1] != ">>>":
                continue
            self._entries.append(entry)
        if len(self._entries) > _MAX_ENTRIES:
            self._entries = self._entries[-_MAX_ENTRIES:]
        for entry in batch:
            if not log_enabled and entry[1] != ">>>":
                continue
            try:
                level, msg = entry[1], entry[2]
                if level == ">>>":
                    print(str(msg))
                else:
                    print("[{}] {}".format(level.upper(), msg))
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


_log_instance = None


def get_log():
    global _log_instance
    if _log_instance is None:
        _log_instance = LogService()
    return _log_instance
