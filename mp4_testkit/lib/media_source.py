import os

def list_jpegs(folder_path):
    files = [
        f
        for f in os.listdir(folder_path)
        if f.lower().endswith(".jpeg") or f.lower().endswith(".jpg")
    ]
    files.sort()
    return [folder_path + "/" + f for f in files]


def compute_max_file_size(paths, default_bytes=64 * 1024):
    max_bytes = 0
    for p in paths:
        sz = os.stat(p)[6]
        if sz > max_bytes:
            max_bytes = sz
    return max_bytes if max_bytes > 0 else default_bytes


def compute_max_frame_size(paths, default_bytes=240 * 240, bytes_per_pixel=2):
    max_w = 0
    max_h = 0
    for p in paths:
        try:
            with open(p, 'rb') as f:
                if f.read(2) == b'\xff\xd8':
                    while True:
                        marker_data = f.read(2)
                        if len(marker_data) < 2:
                            break
                        if marker_data[0] != 0xff:
                            continue
                        marker = marker_data[1]
                        if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
                            f.read(3)  # Skip length and precision
                            h = int.from_bytes(f.read(2), 'big')
                            w = int.from_bytes(f.read(2), 'big')
                            if w > max_w: max_w = w
                            if h > max_h: max_h = h
                            break
                        else:
                            length = int.from_bytes(f.read(2), 'big')
                            f.read(length - 2)
        except Exception:
            pass
            
    if max_w > 0 and max_h > 0:
        return max_w * max_h * int(bytes_per_pixel)
    return int(default_bytes) * int(bytes_per_pixel)
