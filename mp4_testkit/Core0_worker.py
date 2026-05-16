import time

from lib.tail_codec import read_u32_le, write_u32_le


def _yield():
    time.sleep_ms(0)


def _read_file_into(path, dst, max_len, chunk):
    if chunk <= 0:
        with open(path, "rb") as f:
            n = f.readinto(dst[:max_len])
        return 0 if n is None else n

    mv = dst[:max_len]
    off = 0
    with open(path, "rb") as f:
        while off < max_len:
            n = f.readinto(mv[off:off + chunk])
            if not n:
                break
            off += n
            time.sleep_ms(0)
    return off


# Core0 主要工作迴圈：
# - 讀取 JPEG 檔案序列（paths）並寫入 io_hub
# - 從 frame_hub 取出已解碼 frame 顯示到 LCD
# - 可選擇性統計 FPS/耗時
# - 依 pace_ms 控制每帧節奏
def task_loop(bus):
    # 依賴的服務（由 bus 注入）
    lcd = bus.get_service("lcd")
    paths = bus.get_service("paths")
    comm = bus.get_service("comm")
    mp4 = bus.shared.get("mp4_player")
    if not isinstance(mp4, dict):
        mp4 = {}
        bus.shared["mp4_player"] = mp4

    # 執行參數（由 shared 設定）
    pace_ms = int(bus.shared.get("pace_ms", 0) or 0)
    loop_play = bool(bus.shared.get("loop_play", True))
    stats_enabled = bool(bus.shared.get("stats_enabled", False))
    stats_interval_ms = int(bus.shared.get("stats_interval_ms", 1000) or 1000)
    stats_frames_n = int(bus.shared.get("stats_frames_n", 60) or 60)
    range_enabled = bool(bus.shared.get("mp4_range_enabled", False))
    range_start = int(bus.shared.get("mp4_range_start", 0) or 0)
    range_end = int(bus.shared.get("mp4_range_end", 0) or 0)

    # 統計用計數器：以 1 秒窗口與 N 帧窗口各自計算一次
    sec_t0 = time.ticks_ms()
    sec_frames = 0
    n_t0 = time.ticks_ms()
    n_frames = 0
    sec_disp_us = 0
    sec_dec_us = 0
    sec_read_us = 0
    sec_read_bytes = 0
    n_disp_us = 0
    n_dec_us = 0
    n_read_us = 0
    n_read_bytes = 0

    def _stats_on_frame(disp_us, dec_us):
        nonlocal sec_t0, sec_frames, n_t0, n_frames, sec_disp_us, sec_dec_us, sec_read_us, sec_read_bytes, n_disp_us, n_dec_us, n_read_us, n_read_bytes
        if not stats_enabled:
            return
        now = time.ticks_ms()
        sec_frames += 1
        n_frames += 1
        sec_disp_us += disp_us
        sec_dec_us += dec_us
        n_disp_us += disp_us
        n_dec_us += dec_us

        # 1 秒窗口：輸出近 1 秒內的 frame 數量與實際經過毫秒
        dt_sec = time.ticks_diff(now, sec_t0)
        if dt_sec >= stats_interval_ms:
            avg_disp = sec_disp_us // sec_frames if sec_frames else 0
            avg_dec = sec_dec_us // sec_frames if sec_frames else 0
            avg_read = sec_read_us // sec_frames if sec_frames else 0
            if sec_read_us > 0:
                read_kbs = (sec_read_bytes * 1000000) // (sec_read_us * 1024)
            else:
                read_kbs = 0
            print(
                "1s_frames:",
                sec_frames,
                "ms:",
                dt_sec,
                "avg_disp_us:",
                avg_disp,
                "avg_dec_us:",
                avg_dec,
                "avg_read_us:",
                avg_read,
                "read_KB/s:",
                read_kbs,
            )
            sec_t0 = now
            sec_frames = 0
            sec_disp_us = 0
            sec_dec_us = 0
            sec_read_us = 0
            sec_read_bytes = 0

        # N 帧窗口：輸出 N 帧累積耗時（可觀察平均每帧成本）
        if n_frames >= stats_frames_n:
            dt_n = time.ticks_diff(now, n_t0)
            avg_disp = n_disp_us // n_frames if n_frames else 0
            avg_dec = n_dec_us // n_frames if n_frames else 0
            avg_read = n_read_us // n_frames if n_frames else 0
            if n_read_us > 0:
                read_kbs = (n_read_bytes * 1000000) // (n_read_us * 1024)
            else:
                read_kbs = 0
            print(
                "frames:",
                n_frames,
                "ms:",
                dt_n,
                "avg_disp_us:",
                avg_disp,
                "avg_dec_us:",
                avg_dec,
                "avg_read_us:",
                avg_read,
                "read_KB/s:",
                read_kbs,
            )
            n_t0 = now
            n_frames = 0
            n_disp_us = 0
            n_dec_us = 0
            n_read_us = 0
            n_read_bytes = 0

    io_hub = bus.get_service("io_hub")
    frame_hub = bus.get_service("frame_hub")
    max_jpeg_bytes = int(bus.shared.get("max_jpeg_bytes", 0) or 0)
    frame_bytes = int(bus.shared.get("frame_bytes", 0) or 0)
    io_prefetch = int(bus.shared.get("io_prefetch", 0) or 0)
    io_read_chunk = int(bus.shared.get("io_read_chunk", 0) or 0)
    jpeg_cache = bus.get_service("jpeg_cache")
    pack = bus.get_service("pack")
    if bool(bus.shared.get("debug", False)):
        if jpeg_cache is None:
            print("[Core0] jpeg_cache: None")
        else:
            print("[Core0] jpeg_cache:", len(jpeg_cache))
        if pack is not None:
            print("[Core0] pack:", getattr(pack, "path", "?"))

    idx = int(bus.shared.get("src_idx", 0) or 0)
    if paths and idx >= len(paths):
        idx = 0
        bus.shared["src_idx"] = 0

    def _set_range(total, enable, start, end):
        nonlocal range_enabled, range_start, range_end, idx
        total = int(total or 0)
        if total <= 0:
            range_enabled = False
            range_start = 0
            range_end = 0
            bus.shared["mp4_range_enabled"] = False
            bus.shared["mp4_range_start"] = 0
            bus.shared["mp4_range_end"] = 0
            idx = 0
            bus.shared["src_idx"] = 0
            return

        if enable:
            rs = int(start or 0)
            re = int(end or 0)
            if rs < 0:
                rs = 0
            if rs >= total:
                rs = total - 1
            if re == 0xFFFFFFFF:
                re = total - 1
            if re < rs:
                re = rs
            if re >= total:
                re = total - 1
            range_enabled = True
            range_start = rs
            range_end = re
        else:
            range_enabled = False
            range_start = 0
            range_end = total - 1

        bus.shared["mp4_range_enabled"] = bool(range_enabled)
        bus.shared["mp4_range_start"] = int(range_start)
        bus.shared["mp4_range_end"] = int(range_end)

        if idx < range_start or idx > range_end:
            idx = int(range_start)
            bus.shared["src_idx"] = idx

    def _get_pace_frames():
        n = int(bus.shared.get("pace_frames", 1) or 1)
        return 1 if n < 1 else n

    def _advance_idx(i, step):
        if not paths:
            return i
        i += step
        if range_enabled:
            if i <= range_end:
                return i
            if loop_play:
                span = range_end - range_start + 1
                if span <= 0:
                    return range_start
                return range_start + ((i - range_start) % span)
            return range_end
        if i < len(paths):
            return i
        if loop_play:
            return i % len(paths)
        return len(paths) - 1

    def _flush_hubs():
        try:
            io_hub.flush()
        except Exception:
            pass
        try:
            frame_hub.flush()
        except Exception:
            pass

    def _apply_source_req(req):
        nonlocal paths, pack, idx, max_jpeg_bytes
        if not isinstance(req, dict):
            return
        source = str(req.get("source", "") or "").strip()
        mode = int(req.get("mode", 0) or 0)
        req_start = int(req.get("start", req.get("range_start", 0)) or 0)
        raw_range = req.get("range", None)
        if raw_range is None:
            req_end = int(req.get("range_end", 0xFFFFFFFF) or 0)
        else:
            span = int(raw_range or 0)
            if span == 0xFFFFFFFF or span <= 0:
                req_end = 0xFFFFFFFF
            else:
                req_end = req_start + span - 1
        if not source:
            mp4["err"] = "empty source"
            return

        mp4["err"] = ""
        mp4["source"] = source
        mp4["mode"] = mode
        mp4["frame"] = 0
        idx = 0
        bus.shared["src_idx"] = 0

        import os
        if mode == 1:
            try:
                from lib.pack_source import PackSource
                cand = source
                if not cand.startswith("/"):
                    assets_root = (bus.shared.get("config") or {}).get("assets_root") or "/jpeg"
                    assets_root = str(assets_root).rstrip("/")
                    cand = assets_root + "/" + cand
                os.stat(cand)
                new_pack = PackSource(cand, loop=False)
                if int(new_pack.max_size) > int(max_jpeg_bytes):
                    new_pack.close()
                    mp4["err"] = "pack too big"
                    return
                if pack is not None:
                    try:
                        pack.close()
                    except Exception:
                        pass
                pack = new_pack
                paths = []
                bus.set_service("pack", pack)
                bus.set_service("paths", paths)
                mp4["total"] = int(pack.count)
                _set_range(mp4["total"], True, req_start, req_end)
                try:
                    pack.reset()
                    if int(range_start) > 0:
                        pack.skip_next(int(range_start))
                    if hasattr(pack, "tell"):
                        pos, _ = pack.tell()
                        bus.shared["mp4_pack_range_pos"] = int(pos)
                except Exception:
                    pass
                idx = int(range_start)
                bus.shared["src_idx"] = idx
            except Exception as e:
                mp4["err"] = str(e)
                return
        else:
            try:
                folder = source
                if folder.endswith(".jpk"):
                    mp4["err"] = "folder expects name, got .jpk"
                    return
                assets_root = (bus.shared.get("config") or {}).get("assets_root") or "/jpeg"
                assets_root = str(assets_root).rstrip("/")
                folder_path = assets_root + "/" + folder
                from lib.media_source import list_jpegs, compute_max_file_size
                pths = list_jpegs(folder_path)
                if not pths:
                    mp4["err"] = "no jpegs"
                    return
                mx = int(compute_max_file_size(pths))
                if mx > int(max_jpeg_bytes):
                    mp4["err"] = "jpeg too big"
                    return
                if pack is not None:
                    try:
                        pack.close()
                    except Exception:
                        pass
                pack = None
                paths = pths
                bus.set_service("pack", None)
                bus.set_service("paths", paths)
                mp4["total"] = int(len(paths))
                _set_range(mp4["total"], True, req_start, req_end)
                idx = int(range_start)
                bus.shared["src_idx"] = idx
            except Exception as e:
                mp4["err"] = str(e)
                return

        _flush_hubs()

    def _pack_fill_step(w, step):
        step = 1 if step < 1 else step
        read_us2 = 0

        def _goto_range_start():
            nonlocal read_us2
            try:
                pos = bus.shared.get("mp4_pack_range_pos", None)
                if pos is not None and hasattr(pack, "seek_to"):
                    pack.seek_to(int(pos), int(range_start))
                    return True
                pack.reset()
                if int(range_start) > 0:
                    _, dt_skip = pack.skip_next(int(range_start))
                    read_us2 += dt_skip
                if hasattr(pack, "tell"):
                    pos2, _ = pack.tell()
                    bus.shared["mp4_pack_range_pos"] = int(pos2)
                return True
            except Exception:
                return False

        if range_enabled:
            next_i = int(getattr(pack, "_idx", 0) or 0)
            if next_i < range_start:
                _goto_range_start()
            next_i = int(getattr(pack, "_idx", 0) or 0)
            if next_i > range_end:
                if loop_play:
                    _goto_range_start()
                else:
                    bus.shared["mp4_playing"] = False
                    return

        if step > 1:
            ok, dt_skip = pack.skip_next(step - 1)
            read_us2 += dt_skip
            if not ok:
                if loop_play and (not range_enabled or int(range_start) == 0):
                    try:
                        pack.reset()
                        ok2, dt_skip2 = pack.skip_next(step - 1)
                        read_us2 += dt_skip2
                        ok = ok2
                    except Exception:
                        ok = False
            if range_enabled and ok:
                next_i = int(getattr(pack, "_idx", 0) or 0)
                if next_i > range_end:
                    if loop_play:
                        _goto_range_start()
                    else:
                        bus.shared["mp4_playing"] = False
                        return

        frame_idx, n, dt_read = pack.read_next_into(w, max_jpeg_bytes)
        read_us2 += dt_read
        if frame_idx is None:
            if loop_play:
                if range_enabled:
                    if not _goto_range_start():
                        bus.shared["mp4_playing"] = False
                        return
                else:
                    try:
                        pack.reset()
                    except Exception:
                        bus.shared["mp4_playing"] = False
                        return
                frame_idx, n, dt_read = pack.read_next_into(w, max_jpeg_bytes)
                read_us2 += dt_read
            else:
                bus.shared["mp4_playing"] = False
                return

        if range_enabled and frame_idx > range_end:
            if loop_play:
                if not _goto_range_start():
                    bus.shared["mp4_playing"] = False
                    return
                frame_idx, n, dt_read = pack.read_next_into(w, max_jpeg_bytes)
                read_us2 += dt_read
            else:
                bus.shared["mp4_playing"] = False
                return

        if n <= 0:
            frame_idx = 0

        bus.shared["src_idx"] = int(frame_idx or 0)
        tail_off = max_jpeg_bytes
        write_u32_le(w, tail_off + 0, frame_idx if frame_idx is not None else 0)
        write_u32_le(w, tail_off + 4, n)
        write_u32_le(w, tail_off + 8, read_us2)
        io_hub.commit()
    # 主迴圈：持續餵 JPEG 到 io_hub；同時從 frame_hub 取出已解碼 frame 顯示到 LCD
    while True:
        did_work = False
        if comm is not None:
            comm.poll()
        if bus.shared.pop("mp4_flush", 0):
            _flush_hubs()

        req = bus.shared.pop("mp4_source_req", None)
        if req is not None:
            _apply_source_req(req)

        seek = bus.shared.pop("mp4_seek", None)
        if seek is not None:
            try:
                seek = int(seek)
            except Exception:
                seek = 0
            if seek > 0:
                if range_enabled:
                    if seek < range_start:
                        seek = range_start
                    if seek > range_end:
                        seek = range_end
                idx = 0 if not paths else (seek % len(paths))
                bus.shared["src_idx"] = idx
                if pack is not None:
                    try:
                        pack.reset()
                        if seek > 0:
                            pack.skip_next(seek)
                    except Exception:
                        pass
                _flush_hubs()

        paused = bool(bus.shared.get("mp4_paused", False))
        playing = bool(bus.shared.get("mp4_playing", True))
        mp4["playing"] = bool(playing)
        mp4["paused"] = bool(paused)
        mp4["frame"] = int(bus.shared.get("src_idx", 0) or 0)
        if pack is not None:
            mp4["mode"] = 1
            mp4["total"] = int(getattr(pack, "count", 0) or 0)
            mp4["source"] = str(getattr(pack, "path", "") or "")
        else:
            mp4["mode"] = 2
            mp4["total"] = int(len(paths) if paths else 0)

        if paused or not playing:
            time.sleep_ms(1)
            continue

        r = frame_hub.get_read_view()
        if r is not None:
            frame_t0_ms = time.ticks_ms()
            frame_t0_ms = time.ticks_ms()
            dec_us = read_u32_le(r, frame_bytes + 4)
            read_us = read_u32_le(r, frame_bytes + 8)
            read_n = read_u32_le(r, frame_bytes + 12)
            t0 = time.ticks_us()
            try:
                lcd.write_data(r[:frame_bytes])
            finally:
                frame_hub.release_read()
            t1 = time.ticks_us()
            disp_us = time.ticks_diff(t1, t0)
            sec_read_us += read_us
            n_read_us += read_us
            sec_read_bytes += read_n
            n_read_bytes += read_n
            _stats_on_frame(disp_us, dec_us)
            did_work = True
            if pace_ms > 0:
                while True:
                    now_ms = time.ticks_ms()
                    dt_ms = time.ticks_diff(now_ms, frame_t0_ms)
                    if dt_ms >= pace_ms:
                        break
                    remain = pace_ms - dt_ms
                    if remain <= 2:
                        if comm is not None:
                            comm.poll()
                        time.sleep_ms(1)
                        continue

                    cache_active = bool(bus.shared.get("cache_active", False))
                    if io_hub.get_fill_level() < io_prefetch:
                        w = io_hub.get_write_view()
                        if w is not None:
                            if pack is not None:
                                _pack_fill_step(w, _get_pace_frames())
                                continue
                            if (not cache_active) and paths:
                                cur_idx = idx
                                p = paths[idx]
                                t2 = time.ticks_us()
                                n = _read_file_into(p, w, max_jpeg_bytes, io_read_chunk)
                                t3 = time.ticks_us()
                                read_us2 = time.ticks_diff(t3, t2)
                                tail_off = max_jpeg_bytes
                                write_u32_le(w, tail_off + 0, idx)
                                write_u32_le(w, tail_off + 4, n)
                                write_u32_le(w, tail_off + 8, read_us2)
                                io_hub.commit()
                                idx = _advance_idx(idx, _get_pace_frames())
                                bus.shared["src_idx"] = idx
                                if range_enabled and (not loop_play) and cur_idx >= range_end:
                                    bus.shared["mp4_playing"] = False
                                if (not loop_play) and (not range_enabled) and paths and cur_idx >= (len(paths) - 1):
                                    bus.shared["mp4_playing"] = False
                                continue

                    time.sleep_ms(remain if remain < 5 else 5)
        else:
            cache_active = bool(bus.shared.get("cache_active", False))
            if pack is None and (not cache_active) and io_hub.get_fill_level() < io_prefetch:
                w = io_hub.get_write_view()
                if w is not None:
                    cur_idx = idx
                    p = paths[idx]
                    t0 = time.ticks_us()
                    n = _read_file_into(p, w, max_jpeg_bytes, io_read_chunk)
                    t1 = time.ticks_us()
                    read_us = time.ticks_diff(t1, t0)

                    tail_off = max_jpeg_bytes
                    write_u32_le(w, tail_off + 0, idx)
                    write_u32_le(w, tail_off + 4, n)
                    write_u32_le(w, tail_off + 8, read_us)
                    io_hub.commit()

                    idx = _advance_idx(idx, _get_pace_frames())
                    bus.shared["src_idx"] = idx
                    if range_enabled and (not loop_play) and cur_idx >= range_end:
                        bus.shared["mp4_playing"] = False
                    if (not loop_play) and (not range_enabled) and paths and cur_idx >= (len(paths) - 1):
                        bus.shared["mp4_playing"] = False
                    did_work = True
            if pack is not None and io_hub.get_fill_level() < io_prefetch:
                w = io_hub.get_write_view()
                if w is not None:
                    _pack_fill_step(w, _get_pace_frames())
                    did_work = True

        if not did_work:
            _yield()
