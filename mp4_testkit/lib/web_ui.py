import socket
import json
import time
import os
from lib.sys_bus import bus
from lib.log_service import get_log


class WebUIService:
    def __init__(self, *, port=80, web_root="web"):
        self.port = int(port or 80)
        self.web_root = str(web_root or "web")
        self.sock = None
        self.clients = []
        self.app = None
        self.enabled = False
        self._keep_alive_until = 0
        self._keep_alive_owned = False
        self._last_keep_alive_touch = 0
        self._pending_wifi_connect = None

    def set_app(self, app):
        self.app = app

    def enable(self):
        self.enabled = True
        if self.sock is None:
            self._start()

    def disable(self):
        self.enabled = False
        self._stop()

    def poll(self):
        if not self.enabled:
            return
        if self.sock is None:
            self._start()
            if self.sock is None:
                return

        try:
            cl, _addr = self.sock.accept()
            try:
                cl.settimeout(2)
            except Exception:
                try:
                    cl.setblocking(True)
                except Exception:
                    pass
            self.clients.append(cl)
        except OSError:
            pass

        for cl in self.clients[:]:
            try:
                request = cl.recv(2048)
                if request:
                    self._handle_request(cl, request)
                    if cl in self.clients:
                        self.clients.remove(cl)
                    cl.close()
                else:
                    if cl in self.clients:
                        self.clients.remove(cl)
                    cl.close()
            except OSError as e:
                if e.args and e.args[0] == 11:
                    continue
                if cl in self.clients:
                    self.clients.remove(cl)
                try:
                    cl.close()
                except Exception:
                    pass

        if self._pending_wifi_connect:
            self._pending_wifi_connect = None
            try:
                nm = bus.get_service("network_manager")
                if nm:
                    try:
                        nm.disable_wifi()
                    except Exception:
                        pass
                    nm.enable_wifi()
            except Exception as e:
                get_log().error("Web WiFi Connect Error: {}".format(e))

        if self._keep_alive_until and time.time() > self._keep_alive_until:
            if self._keep_alive_owned:
                if bus.shared.get("manual_keep_alive"):
                    bus.shared["manual_keep_alive"] = False
                    bus.shared["app_connected"] = False
                self._keep_alive_owned = False
            self._keep_alive_until = 0

    def _start(self):
        try:
            addr = socket.getaddrinfo("0.0.0.0", self.port)[0][-1]
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(addr)
            s.listen(1)
            s.setblocking(False)
            self.sock = s
            get_log().info("\U0001f30d [WebUI] Listening on port {}".format(self.port))
        except Exception as e:
            get_log().error("\u274c [WebUI] Start failed: {}".format(e))
            self.sock = None

    def _stop(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None
        for cl in self.clients:
            try:
                cl.close()
            except Exception:
                pass
        self.clients = []
        get_log().info("[WebUI] Stopped")

    def _handle_request(self, cl, request):
        try:
            req_str = request.decode("utf-8")
            lines = req_str.split("\r\n")
            if not lines:
                return
            first_line = lines[0]
            parts = first_line.split(" ")
            if len(parts) < 2:
                return
            method, path = parts[0], parts[1]
            path = path.split("?", 1)[0]

            if path == "/" or path == "/index.html":
                self._touch_keep_alive()
                self._serve_file(cl, "/index.html")
            elif path == "/api/wifi/status" and method == "GET":
                self._touch_keep_alive()
                self._handle_wifi_status(cl)
            elif path == "/api/wifi/scan" and method == "GET":
                self._touch_keep_alive()
                self._handle_wifi_scan(cl)
            elif path == "/api/wifi/connect" and method == "POST":
                self._touch_keep_alive()
                body = self._extract_body(lines)
                if body.strip().startswith("{"):
                    self._handle_wifi_connect(cl, body)
                else:
                    self._send_text(cl, 400, "text/plain", "Body missing or invalid")
            elif path.startswith("/api/cmd") and method == "POST":
                body = self._extract_body(lines)
                if body.strip().startswith("{"):
                    self._handle_api(cl, body)
                else:
                    self._send_text(cl, 400, "text/plain", "Body missing or invalid")
            elif path == "/api/perf":
                perf = bus.shared.get("perf", {})
                self._send_json(cl, 200, perf)
            elif method == "GET" and self._is_static_path(path):
                self._touch_keep_alive()
                self._serve_file(cl, path)
            else:
                self._send_text(cl, 404, "text/plain", "Not Found")
        except Exception as e:
            try:
                if isinstance(e, OSError) and e.args and e.args[0] == 11:
                    return
            except Exception:
                pass
            get_log().error("Web Request Error: {}".format(e))
            try:
                self._send_text(cl, 500, "text/plain", "Internal Error")
            except Exception:
                pass

    def _sleep_ms(self, ms):
        try:
            time.sleep_ms(ms)
        except Exception:
            try:
                time.sleep(ms / 1000)
            except Exception:
                pass

    def _send_all(self, cl, data, retry_ms=10, retry_n=80):
        mv = memoryview(data)
        off = 0
        nmax = len(mv)
        while off < nmax:
            try:
                n = cl.send(mv[off:])
                if n is None:
                    n = 0
                if n > 0:
                    off += n
                    continue
            except OSError as e:
                if e.args and e.args[0] == 11:
                    retry_n -= 1
                    if retry_n <= 0:
                        return False
                    self._sleep_ms(retry_ms)
                    continue
                raise
            retry_n -= 1
            if retry_n <= 0:
                return False
            self._sleep_ms(retry_ms)
        return True

    def _serve_file(self, cl, path):
        file_path = self._web_path(path)
        if file_path:
            try:
                st = os.stat(file_path)
                size = st[6] if len(st) > 6 else None
                ct = self._content_type(path)
                self._send_headers(cl, 200, ct, size)
                with open(file_path, "rb") as f:
                    while True:
                        chunk = f.read(1024)
                        if not chunk:
                            break
                        if not self._send_all(cl, chunk):
                            break
                return
            except Exception as e:
                try:
                    if isinstance(e, OSError) and e.args and e.args[0] == 11:
                        return
                except Exception:
                    pass
                get_log().error("Web Serve Error: {}".format(e))
        self._send_text(cl, 404, "text/plain", "Web file not found")

    def _handle_api(self, cl, body):
        try:
            body = body.strip("\x00")
            cmd_data = json.loads(body)
            cmd_id = cmd_data.get("cmd")
            payload_obj = cmd_data.get("payload", {})

            if self.app and cmd_id:
                if isinstance(cmd_id, str):
                    s = cmd_id.strip().lower()
                    if s.startswith("0x"):
                        cmd_id = int(s, 16)
                    else:
                        cmd_id = int(s)

                if not isinstance(payload_obj, dict):
                    payload_obj = {}

                from lib.schema_codec import SchemaCodec
                from lib.proto import StreamParser

                cmd_def = self.app.store.get(cmd_id)
                if not cmd_def:
                    self._send_json(cl, 400, {"status": "error", "error": "Unknown cmd"})
                    return

                try:
                    payload_bytes = SchemaCodec.encode(cmd_def, payload_obj)
                except Exception as e:
                    self._send_json(cl, 400, {"status": "error", "error": "Payload encode failed", "detail": str(e)})
                    return

                frames = []

                def collect_send(data):
                    try:
                        frames.append(data)
                    except Exception:
                        pass

                ctx = {"app": self.app, "transport": "WebUI", "send": collect_send}
                self.app.disp.dispatch(cmd_id, payload_bytes, ctx)

                rsp = []
                if frames:
                    parser = StreamParser(max_len=4096 * 4)
                    for fr in frames:
                        try:
                            parser.feed(fr)
                            for ver, addr, cmd, payload in parser.pop():
                                cmd_def2 = self.app.store.get(cmd)
                                if cmd_def2:
                                    try:
                                        args = SchemaCodec.decode(cmd_def2, payload, self.app.store)
                                    except Exception as e:
                                        args = {"_decode_error": str(e)}
                                    rsp.append({"cmd": cmd, "name": cmd_def2.get("name", ""), "args": args})
                                else:
                                    rsp.append({"cmd": cmd, "name": "", "len": len(payload)})
                        except Exception as e:
                            rsp.append({"_frame_error": str(e)})

                self._send_json(cl, 200, {"status": "ok", "cmd": cmd_id, "name": cmd_def.get("name", ""), "responses": rsp})
            else:
                self._send_text(cl, 400, "text/plain", "Missing cmd")
        except Exception as e:
            get_log().error("API Error: {}".format(e))
            self._send_json(cl, 500, {"status": "error", "error": str(e)})

    def _touch_keep_alive(self):
        now = 0
        try:
            now = time.time()
        except Exception:
            now = 0
        if now and self._last_keep_alive_touch and (now - self._last_keep_alive_touch) < 1:
            keep_alive_secs = 300
            try:
                keep_alive_secs = int(bus.shared.get("Network", {}).get("wifi", {}).get("timeout", 300))
            except Exception:
                keep_alive_secs = 300
            if keep_alive_secs < 5:
                keep_alive_secs = 5
            self._keep_alive_until = now + keep_alive_secs
            return
        self._last_keep_alive_touch = now or self._last_keep_alive_touch
        was_keep = bool(bus.shared.get("manual_keep_alive", False))
        nm = bus.get_service("network_manager")
        if nm:
            try:
                nm.set_app_connected(True)
            except Exception:
                bus.shared["manual_keep_alive"] = True
                bus.shared["app_connected"] = True
        else:
            bus.shared["manual_keep_alive"] = True
            bus.shared["app_connected"] = True
        if not was_keep:
            self._keep_alive_owned = True
        keep_alive_secs = 300
        try:
            keep_alive_secs = int(bus.shared.get("Network", {}).get("wifi", {}).get("timeout", 300))
        except Exception:
            keep_alive_secs = 300
        if keep_alive_secs < 5:
            keep_alive_secs = 5
        self._keep_alive_until = time.time() + keep_alive_secs

    def _extract_body(self, lines):
        body = ""
        for i, line in enumerate(lines):
            if line == "":
                body = "\r\n".join(lines[i + 1 :])
                break
        return body

    def _send_headers(self, cl, code, content_type=None, content_length=None):
        reason = "OK" if code == 200 else "Bad Request" if code == 400 else "Not Found" if code == 404 else "Error"
        headers = "HTTP/1.1 {} {}\r\n".format(code, reason)
        headers += "Connection: close\r\n"
        if content_type:
            headers += "Content-Type: {}\r\n".format(content_type)
        if content_length is not None:
            headers += "Content-Length: {}\r\n".format(content_length)
        headers += "\r\n"
        self._send_all(cl, headers.encode())

    def _send_text(self, cl, code, content_type, text):
        body = text.encode() if isinstance(text, str) else text
        self._send_headers(cl, code, content_type, len(body))
        self._send_all(cl, body)

    def _send_json(self, cl, code, obj):
        body = json.dumps(obj).encode()
        self._send_headers(cl, code, "application/json", len(body))
        self._send_all(cl, body)

    def _content_type(self, path):
        if path.endswith(".html"):
            return "text/html; charset=utf-8"
        if path.endswith(".css"):
            return "text/css; charset=utf-8"
        if path.endswith(".js"):
            return "application/javascript; charset=utf-8"
        if path.endswith(".mjs"):
            return "application/javascript; charset=utf-8"
        return "application/octet-stream"

    def _web_path(self, path):
        if not path.startswith("/"):
            return None
        if ".." in path:
            return None
        return self.web_root + path

    def _is_static_path(self, path):
        if not path.startswith("/"):
            return False
        if ".." in path:
            return False
        if path.startswith("/api/"):
            return False
        if path == "/":
            return False
        if path.endswith(".html") or path.endswith(".css") or path.endswith(".js") or path.endswith(".mjs"):
            return True
        if path.endswith(".png") or path.endswith(".jpg") or path.endswith(".jpeg") or path.endswith(".svg"):
            return True
        if path.endswith(".json") or path.endswith(".txt") or path.endswith(".ico"):
            return True
        return False

    def _handle_wifi_status(self, cl):
        try:
            import network
            sta = network.WLAN(network.STA_IF)
            ap = network.WLAN(network.AP_IF)
            mode = "off"
            active = False
            connected = False
            ip = ""
            gw = ""
            ssid = ""

            try:
                if sta.active():
                    mode = "sta"
                    active = True
                    try:
                        connected = bool(sta.isconnected())
                    except Exception:
                        connected = False
                    try:
                        cfg = sta.ifconfig()
                        ip = cfg[0]
                        gw = cfg[2]
                    except Exception:
                        pass
            except Exception:
                pass

            try:
                if ap.active() and not active:
                    mode = "ap"
                    active = True
                    try:
                        cfg = ap.ifconfig()
                        ip = cfg[0]
                        gw = cfg[2]
                    except Exception:
                        pass
                    try:
                        ssid = ap.config("essid")
                    except Exception:
                        pass
            except Exception:
                pass

            slave_id = getattr(bus, "slave_id", "")
            self._send_json(cl, 200, {"mode": mode, "active": active, "connected": connected, "ip": ip, "gw": gw, "ssid": ssid, "slave_id": slave_id})
        except Exception as e:
            self._send_json(cl, 500, {"status": "error", "error": str(e)})

    def _handle_wifi_scan(self, cl):
        try:
            import network
            sta = network.WLAN(network.STA_IF)
            if not sta.active():
                sta.active(True)
                self._sleep_ms(200)
            res = sta.scan()
            if res:
                res.sort(key=lambda x: x[3], reverse=True)
            out = []
            for info in (res or []):
                try:
                    ssid = info[0].decode("utf-8")
                    if not ssid:
                        ssid = "<Hidden>"
                except Exception:
                    ssid = "<Unknown>"
                out.append({"ssid": ssid, "rssi": int(info[3]), "auth": int(info[4]), "channel": int(info[2])})
            self._send_json(cl, 200, {"status": "ok", "results": out})
        except Exception as e:
            self._send_json(cl, 500, {"status": "error", "error": str(e)})

    def _handle_wifi_connect(self, cl, body):
        try:
            body = body.strip("\x00")
            payload = json.loads(body)
            ssid = payload.get("ssid", "")
            password = payload.get("password", "")
            save = bool(payload.get("save", False))

            if not ssid:
                self._send_json(cl, 400, {"status": "error", "error": "ssid required"})
                return

            if "Network" not in bus.shared:
                bus.shared["Network"] = {}
            if "wifi" not in bus.shared["Network"]:
                bus.shared["Network"]["wifi"] = {}

            wifi_cfg = bus.shared["Network"]["wifi"]
            wifi_cfg["enable"] = 1
            wifi_cfg["ssid"] = ssid
            wifi_cfg["ssid_pw"] = password

            if save:
                try:
                    from lib.ConfigManager import cfg_manager
                    cfg_manager.save_from_bus(update_key="Network.wifi.ssid")
                    cfg_manager.save_from_bus(update_key="Network.wifi.ssid_pw")
                except Exception as e:
                    get_log().error("Web WiFi Save Error: {}".format(e))

            self._pending_wifi_connect = {"ssid": ssid}
            self._send_json(cl, 200, {"status": "ok", "queued": True})
        except Exception as e:
            self._send_json(cl, 500, {"status": "error", "error": str(e)})

