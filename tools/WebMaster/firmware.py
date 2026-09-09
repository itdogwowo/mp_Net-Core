"""固件全量更新（delta）：比對本地 slave/ vs 設備 manifest，只上傳差異檔。

- 本地來源：<repo>/slave（即設備韌體）。
- 設備 manifest 是 write-through 的權威哈希表（透過 transfer.download 抓取）。
- 上傳走 transfer.upload（FILE_BEGIN/CHUNK/END，ACK 停等 + sha 驗證）。
- 上傳同名檔會觸發兩段式 commit（.bak + pending），預設再 confirm 清 pending。
"""
import os
import json
import hashlib

_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(_DIR, "..", ".."))
SLAVE_DIR = os.path.join(PROJECT_ROOT, "slave")

JUNK_DIR_NAMES = {"__pycache__", "__MACOSX", ".Spotlight-V100", ".Trashes",
                  ".fseventsd", "$RECYCLE.BIN", "System Volume Information"}
JUNK_FILE_NAMES = {".DS_Store", "Thumbs.db", "ehthumbs.db", "desktop.ini"}
JUNK_FILE_SUFFIXES = (".pyc", ".pyo", ".swp", ".swo", ".tmp")


def _is_junk_dir(name):
    return name in JUNK_DIR_NAMES


def _is_junk_name(name):
    if name in JUNK_FILE_NAMES:
        return True
    if name.startswith(("._", "~$")):
        return True
    return name.endswith(JUNK_FILE_SUFFIXES)


def collect_local_files():
    """掃本地 slave 目錄 → [(local_path, remote_path), ...]（跳 config.json / 垃圾檔）。"""
    out = []
    if not os.path.isdir(SLAVE_DIR):
        return out
    for root, dirs, files in os.walk(SLAVE_DIR):
        dirs[:] = [d for d in dirs if not _is_junk_dir(d)]
        for name in files:
            if name == "config.json" or _is_junk_name(name):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, SLAVE_DIR).replace("\\", "/")
            out.append((full, "/" + rel))
    return out


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(65536), b""):
            h.update(c)
    return h.hexdigest()


async def firmware_update(dev, dry_run=False, confirm=True, reboot=False, progress_cb=None):
    """比對 + 只上傳差異檔。回傳 dict:
    {total, changed, uploaded, dry_run, match}
    """
    import transfer
    local = collect_local_files()

    # 設備当前 manifest
    mdata = await transfer.download(dev, "/manifest.json")
    manifest = {}
    if mdata:
        try:
            manifest = json.loads(mdata.decode("utf-8"))
        except Exception:
            manifest = {}
    remote_sha = {}
    for p, info in manifest.items():
        if isinstance(info, dict):
            remote_sha[p] = info.get("h")

    # 差異
    diff = []
    for full, rpath in local:
        lsha = sha256_file(full)
        if remote_sha.get(rpath) != lsha:
            diff.append((full, rpath, lsha))

    uploaded = []
    if not dry_run:
        for i, (full, rpath, lsha) in enumerate(diff, 1):
            with open(full, "rb") as f:
                data = f.read()
            await transfer.upload(dev, data, rpath, progress_cb=None)
            uploaded.append(rpath)
            if progress_cb:
                progress_cb(i, len(diff), rpath)
        # confirm 清 pending（避免 3 次重啟自動回滾）
        if confirm:
            for rpath in uploaded:
                try:
                    await transfer.confirm(dev, rpath)
                except Exception:
                    pass
        # reboot
        if reboot:
            try:
                await dev.send(0x100F, {"delay_ms": 500})
            except Exception:
                pass

    matched = len(local) - len(diff)
    return {
        "total": len(local),
        "changed": len(diff),
        "matched": matched,
        "uploaded": uploaded,
        "dry_run": dry_run,
    }
