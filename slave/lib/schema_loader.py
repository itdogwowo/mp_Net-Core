import json
import os

class SchemaStore:
    def __init__(self, dir_path="/schema"):
        self.cmd_map = {}
        if dir_path:
            self.load_dir(dir_path)

    def load_dir(self, dir_path):
        """掃描並載入所有 JSON Schema"""
        for name in os.listdir(dir_path):
            if name.endswith(".json"):
                self.load_file(f"{dir_path}/{name}")

    def load_file(self, path):
        try:
            with open(path, "r") as f:
                data = json.load(f)
                for c in data.get("cmds", []):
                    # 支援 "0x1101" 或 4353 格式
                    cmd_id = int(c["cmd"], 16) if "0x" in str(c["cmd"]) else int(c["cmd"])
                    self.cmd_map[cmd_id] = c
        except Exception as e:
            print(f"❌ [Schema] Failed to load {path}: {e}")

    def get(self, cmd_id: int):
        return self.cmd_map.get(cmd_id)