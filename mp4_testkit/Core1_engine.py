import time

from lib.tail_codec import read_u32_le, write_u32_le


def _yield():
    time.sleep_ms(0)


def task_loop(bus):
    io_hub = bus.get_service("io_hub")
    frame_hub = bus.get_service("frame_hub")
    decoder = bus.get_service("decoder")
    jpeg_cache = bus.get_service("jpeg_cache")
    if bool(bus.shared.get("debug", False)):
        if jpeg_cache is None:
            print("[Engine] jpeg_cache: None")
        else:
            print("[Engine] jpeg_cache:", len(jpeg_cache))

    max_jpeg_bytes = int(bus.shared.get("max_jpeg_bytes", 0) or 0)
    frame_bytes = int(bus.shared.get("frame_bytes", 0) or 0)
    step_blocks = int(bus.shared.get("jpeg_step_blocks", 0) or 0)
    block = bool(bus.shared.get("jpeg_block", True))

    bus.shared["core1_ready"] = True

    cache_idx = 0
    while bus.shared.get("engine_run", True):
        if jpeg_cache is not None and bool(bus.shared.get("cache_active", False)):
            pace_frames = int(bus.shared.get("pace_frames", 1) or 1)
            if pace_frames < 1:
                pace_frames = 1
            out_view = frame_hub.get_write_view()
            while out_view is None:
                out_view = frame_hub.get_write_view()
                _yield()

            frame_idx, in_buf, n = jpeg_cache[cache_idx]
            t0 = time.ticks_us()
            try:
                if block and step_blocks > 0:
                    done = False
                    while not done:
                        done = decoder.decode_into(in_buf[:n], out_view[:frame_bytes], blocks=step_blocks)
                else:
                    decoder.decode_into(in_buf[:n], out_view[:frame_bytes])
            except Exception:
                _yield()
                continue
            t1 = time.ticks_us()

            hdr_off = frame_bytes
            write_u32_le(out_view, hdr_off + 0, frame_idx)
            dec_us = time.ticks_diff(t1, t0)
            write_u32_le(out_view, hdr_off + 4, dec_us)
            write_u32_le(out_view, hdr_off + 8, 0)
            write_u32_le(out_view, hdr_off + 12, n)
            frame_hub.commit()

            cache_idx += pace_frames
            if cache_idx >= len(jpeg_cache):
                cache_idx = 0
                bus.shared["cache_active"] = False
            continue

        in_view = io_hub.get_read_view()
        if in_view is None:
            _yield()
            continue

        tail_off = max_jpeg_bytes
        frame_idx = read_u32_le(in_view, tail_off + 0)
        n = read_u32_le(in_view, tail_off + 4)
        read_us = read_u32_le(in_view, tail_off + 8)

        if n <= 0:
            io_hub.release_read()
            _yield()
            continue

        out_view = frame_hub.get_write_view()
        while out_view is None:
            out_view = frame_hub.get_write_view()
            _yield()

        t0 = time.ticks_us()
        try:
            if block and step_blocks > 0:
                done = False
                while not done:
                    done = decoder.decode_into(in_view[:n], out_view[:frame_bytes], blocks=step_blocks)
            else:
                decoder.decode_into(in_view[:n], out_view[:frame_bytes])
        except Exception:
            io_hub.release_read()
            _yield()
            continue
        t1 = time.ticks_us()

        hdr_off = frame_bytes
        write_u32_le(out_view, hdr_off + 0, frame_idx)
        dec_us = time.ticks_diff(t1, t0)
        write_u32_le(out_view, hdr_off + 4, dec_us)
        write_u32_le(out_view, hdr_off + 8, read_us)
        write_u32_le(out_view, hdr_off + 12, n)
        frame_hub.commit()
        io_hub.release_read()
