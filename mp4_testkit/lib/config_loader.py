import json


def load_config():
    candidates = [
        "mp4_testkit/config.json",
        "./mp4_testkit/config.json",
        "/config.json",
        "config.json",
        "./config.json",
        "mp4_testkit/dp_config.json",
        "./mp4_testkit/dp_config.json",
        "/dp_config.json",
        "dp_config.json",
        "./config.json",
        "jpeg/dp_config.json",
        "./jpeg/dp_config.json",
        "/jpeg/dp_config.json",
    ]
    last_err = None
    for p in candidates:
        try:
            with open(p, "r") as f:
                cfg = json.load(f)
                if isinstance(cfg, dict):
                    cfg["_config_path"] = p
                return cfg
        except Exception as e:
            last_err = e
    raise last_err
