import json
import os
import struct

_TYPE_CODE = {"u8": 0, "u16": 1, "u32": 2, "str_u16len": 3, "bytes_fixed": 4, "bytes_rest": 5}


def _pack_cmd_list(raw_cmds):
    cmds = []
    for c in raw_cmds:
        cmd_id = int(c["cmd"], 16) if "0x" in str(c["cmd"]) else int(c["cmd"])
        cmds.append((cmd_id, c.get("payload", [])))
    cmds.sort(key=lambda x: x[0])
    return cmds


def _build_buffers(cmds):
    dispatch_buf = bytearray()
    field_buf = bytearray()
    field_names = {}
    field_start = 0

    for cmd_id, payload in cmds:
        count = len(payload)
        dispatch_buf.extend(struct.pack("<HHBBH", cmd_id, field_start, count, 0, 0))

        names = []
        for f in payload:
            tc = _TYPE_CODE.get(f["type"], 255)
            extra = int(f.get("len", 0)) if tc == 4 else 0
            field_buf.extend(struct.pack("<BB", tc, extra))
            names.append(f["name"])

        field_names[cmd_id] = names
        field_start += count

    return bytes(dispatch_buf), bytes(field_buf), field_names


class SchemaStore:
    def __init__(self, dir_path="/schema"):
        self.cmd_map = {}
        self.dispatch_buf = b""
        self.field_buf = b""
        self.field_names = {}
        if dir_path:
            self.load_dir(dir_path)

    def load_dir(self, dir_path):
        for name in sorted(os.listdir(dir_path)):
            if name.endswith(".json"):
                self.load_file(f"{dir_path}/{name}")

    def load_file(self, path):
        try:
            with open(path, "r") as f:
                data = json.load(f)
                for c in data.get("cmds", []):
                    cmd_id = int(c["cmd"], 16) if "0x" in str(c["cmd"]) else int(c["cmd"])
                    self.cmd_map[cmd_id] = c
        except Exception as e:
            print(f"❌ [Schema] Failed to load {path}: {e}")

    def finalize(self):
        cmds = _pack_cmd_list(list(self.cmd_map.values()))
        self.dispatch_buf, self.field_buf, self.field_names = _build_buffers(cmds)
        dispatch_kb = len(self.dispatch_buf)
        field_kb = len(self.field_buf)
        print(f"⚡ [Schema] dispatch={dispatch_kb}B field={field_kb}B cmds={len(cmds)}")

    def get(self, cmd_id: int):
        return self.cmd_map.get(cmd_id)
