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

    # 執行參數（由 shared 設定）
    pace_ms = int(bus.shared.get("pace_ms", 0) or 0)
    loop_play = bool(bus.shared.get("loop_play", True))
    stats_enabled = bool(bus.shared.get("stats_enabled", False))
    stats_interval_ms = int(bus.shared.get("stats_interval_ms", 1000) or 1000)
    stats_frames_n = int(bus.shared.get("stats_frames_n", 60) or 60)

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

    def _get_pace_frames():
        n = int(bus.shared.get("pace_frames", 1) or 1)
        return 1 if n < 1 else n

    def _advance_idx(i, step):
        if not paths:
            return i
        i += step
        if i < len(paths):
            return i
        if loop_play:
            return i % len(paths)
        return len(paths) - 1

    def _pack_fill_step(w, step):
        step = 1 if step < 1 else step
        frame_idx = 0
        n = 0
        read_us2 = 0
        if step > 1:
            _, dt_skip = pack.skip_next(step - 1)
            read_us2 += dt_skip
        frame_idx, n, dt_read = pack.read_next_into(w, max_jpeg_bytes)
        read_us2 += dt_read
        if n <= 0:
            frame_idx = 0
        tail_off = max_jpeg_bytes
        write_u32_le(w, tail_off + 0, frame_idx if frame_idx is not None else 0)
        write_u32_le(w, tail_off + 4, n)
        write_u32_le(w, tail_off + 8, read_us2)
        io_hub.commit()
    # 主迴圈：持續餵 JPEG 到 io_hub；同時從 frame_hub 取出已解碼 frame 顯示到 LCD
    while True:
        did_work = False


        r = frame_hub.get_read_view()
        if r is not None:
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
                                continue

                    time.sleep_ms(remain if remain < 5 else 5)
        else:
            cache_active = bool(bus.shared.get("cache_active", False))
            if pack is None and (not cache_active) and io_hub.get_fill_level() < io_prefetch:
                w = io_hub.get_write_view()
                if w is not None:
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
                    did_work = True
            if pack is not None and io_hub.get_fill_level() < io_prefetch:
                w = io_hub.get_write_view()
                if w is not None:
                    _pack_fill_step(w, _get_pace_frames())
                    did_work = True

        if not did_work:
            _yield()
