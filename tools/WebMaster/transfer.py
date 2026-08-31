"""WebMaster 檔案傳輸 (上傳/下載/promote/confirm/delta 查詢)。

全部走 ACK/回應 停等 (async), 對應 slave 端 file_actions.py 的命令:
  上傳: FILE_BEGIN(0x2001) → FILE_QUERY(0x2005) → FILE_CHUNK(0x2002)/FILE_ACK(0x2004) → FILE_END(0x2003)
  下載: FILE_QUERY(0x2005) → FILE_READ(0x2007)/FILE_CHUNK(0x2002)
  promote: FILE_PROMOTE(0x2011) → FILE_QUERY_RSP(0x2006)
  confirm: FILE_CONFIRM(0x2008) → FILE_QUERY_RSP(0x2006)
"""
import asyncio
import hashlib
import logging

from protocol import protocol

log = logging.getLogger("webmaster.transfer")


async def query(dev, remote_path, timeout=5.0):
    """0x2005 FILE_QUERY → 0x2006; 回傳 (exists, sha_bytes, size) 或 None。"""
    r = await dev.request(0x2005, {"path": remote_path}, expect=0x2006, timeout=timeout)
    if r is None:
        return None
    _, args = r
    return (bool(args.get("exists", 0)),
            args.get("sha256", b""),
            int(args.get("size", 0) or 0))


async def upload(dev, data, remote_path, chunk_size=4096, file_id=1,
                 begin_timeout=5.0, ack_timeout=5.0, end_timeout=30.0,
                 retry_count=3, progress_cb=None):
    """上傳 bytes 到 remote_path (支援 /ram 緩衝區)。回傳本地 sha256 digest bytes。

    progress_cb(done_bytes, total_bytes) 供前端進度回報。
    """
    data = bytes(data)
    total = len(data)
    local_sha = hashlib.sha256(data).digest()

    # 1. FILE_BEGIN
    await dev.send(0x2001, {
        "file_id": file_id,
        "total_size": total,
        "chunk_size": chunk_size,
        "sha256": local_sha,
        "path": remote_path,
    })
    # 2. FILE_QUERY 作為 begin 握手 (與 NetBusMaster 一致)
    r = await dev.request(0x2005, {"path": remote_path}, expect=0x2006, timeout=begin_timeout)
    if r is None:
        raise TimeoutError("FILE_BEGIN handshake timeout")

    # 3. 分塊上傳 (stop-and-wait, 每 chunk 等 0x2004 ACK)
    for off in range(0, total, chunk_size):
        chunk = data[off:off + chunk_size]
        ok = False
        for attempt in range(retry_count):
            r = await dev.request(
                0x2002,
                {"file_id": file_id, "offset": off, "data": chunk},
                expect=0x2004,
                timeout=ack_timeout,
            )
            if r is not None:
                _, ack = r
                if int(ack.get("offset", -1)) == off:
                    ok = True
                    break
                log.warning("忽略錯位 ACK off=%s expect=%s", ack.get("offset"), off)
        if not ok:
            raise TimeoutError(f"upload ACK timeout at offset {off}")
        if progress_cb:
            progress_cb(min(off + len(chunk), total), total)

    # 4. FILE_END → 0x2006 最終校驗
    r = await dev.request(0x2003, {"file_id": file_id}, expect=0x2006, timeout=end_timeout)
    if r is None:
        raise TimeoutError("FILE_END validation timeout")
    _, args = r
    remote_sha = args.get("sha256", b"")
    if remote_sha != local_sha:
        raise ValueError(f"SHA mismatch: {remote_sha.hex()[:8]} != {local_sha.hex()[:8]}")

    if progress_cb:
        progress_cb(total, total)
    return local_sha


async def download(dev, remote_path, expected_size=None, chunk_size=2048,
                   read_timeout=5.0, retry_count=3, progress_cb=None):
    """下載 remote_path 為 bytes。expected_size 已知時可跳過查詢。"""
    if expected_size is None:
        q = await query(dev, remote_path)
        if q is None:
            raise TimeoutError("download query timeout")
        exists, _, expected_size = q
        if not exists:
            return None

    expected_size = int(expected_size or 0)
    if expected_size <= 0:
        return b""

    buf = bytearray()
    offset = 0
    while offset < expected_size:
        req_len = min(chunk_size, expected_size - offset)
        got = None
        for attempt in range(retry_count):
            r = await dev.request(
                0x2007,
                {"path": remote_path, "offset": offset, "length": req_len},
                expect=0x2002,
                timeout=read_timeout,
            )
            if r is None:
                continue
            _, args = r
            data = args.get("data", b"")
            resp_off = int(args.get("offset", -1))
            if resp_off == offset:
                got = bytes(data)
                break
            log.warning("忽略錯位下載回應 off=%s expect=%s", resp_off, offset)
        if got is None:
            raise TimeoutError(f"download timeout at offset {offset}")
        if not got:
            break
        buf.extend(got)
        offset += len(got)
        if progress_cb:
            progress_cb(offset, expected_size)

    return bytes(buf)


async def promote(dev, remote_path, timeout=5.0):
    """0x2011 FILE_PROMOTE: /sd 暫存 → root 目標。回傳 bool。"""
    src = remote_path if remote_path.startswith("/sd") else "/sd" + remote_path
    r = await dev.request(0x2011, {"src": src, "dst": remote_path}, expect=0x2006, timeout=timeout)
    return r is not None


async def confirm(dev, remote_path, timeout=5.0):
    """0x2008 FILE_CONFIRM: 確認覆蓋 → 刪 .bak + 清 pending。回傳 bool。"""
    r = await dev.request(0x2008, {"path": remote_path}, expect=0x2006, timeout=timeout)
    return r is not None


async def undo(dev, remote_path, timeout=5.0):
    """0x200A FILE_UNDO: 復原 .bak。回傳 bool。"""
    r = await dev.request(0x200A, {"path": remote_path}, expect=0x2006, timeout=timeout)
    return r is not None


async def download_delta(dev):
    """下載 /sd/.delta.json → pending dict。失敗回 {}。"""
    try:
        data = await download(dev, "/sd/.delta.json")
        if not data:
            return {}
        import json
        return json.loads(data.decode("utf-8")).get("pending", {})
    except Exception:
        return {}


async def delete(dev, remote_path, timeout=5.0):
    """0x2009 FILE_DELETE: 刪除檔案/目錄。回傳 bool (依 0x2006 exists=0 判定)。

    若路徑已在 pending (待確認)，刪除會連 pending/.bak 一起清掉。
    """
    r = await dev.request(0x2009, {"path": remote_path}, expect=0x2006, timeout=timeout)
    if r is None:
        return False
    _, args = r
    return int(args.get("exists", 1)) == 0


async def list_files(dev):
    """讀取設備 manifest → {path: {"s": size, "h": sha, "pending": bool}} 或 None。

    - manifest 是 write-through 的權威哈希表。
    - pending 從 /sd/.delta.json 取得（哪些檔已寫入 root 但尚未 confirm）。
    注意: manifest 本身不含自己 (scan 跳過 manifest.json)，所以這不會列出 /manifest.json。
    """
    data = await download(dev, "/manifest.json")
    if not data:
        return None
    import json
    try:
        manifest = json.loads(data.decode("utf-8"))
    except Exception:
        return None
    delta = await download_delta(dev)
    result = {}
    for path, info in manifest.items():
        if isinstance(info, dict):
            result[path] = {
                "s": info.get("s", 0),
                "h": info.get("h", ""),
                "pending": path in delta,
            }
    return result
