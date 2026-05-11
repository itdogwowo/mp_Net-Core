import socket
import json
import time
import os
from lib.task import Task
from lib.sys_bus import bus
from lib.log_service import get_log

class WebUITask(Task):
    def __init__(self, name, ctx):
        super().__init__(name, ctx)
        self.port = 80
        self.sock = None
        self.clients = []
        self.app = ctx.get('app')
        self.web_root = "web"
        self._keep_alive_until = 0
        self._keep_alive_owned = False
        self._last_keep_alive_touch = 0
        self._pending_wifi_connect = None
        
    def on_start(self):
        super().on_start()
        try:
            # Check if port 80 is already used?
            # We assume single instance
            addr = socket.getaddrinfo('0.0.0.0', self.port)[0][-1]
            self.sock = socket.socket()
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind(addr)
            self.sock.listen(1)
            self.sock.setblocking(False)
            get_log().info(f"🌍 [WebUI] Listening on port {self.port}")
        except Exception as e:
            get_log().error(f"❌ [WebUI] Start failed: {e}")
            self.sock = None

    def loop(self):
        if not self.running or not self.sock: return

        # Accept new connections
        try:
            cl, addr = self.sock.accept()
            try:
                cl.settimeout(2)
            except:
                try:
                    cl.setblocking(True)
                except:
                    pass
            self.clients.append(cl)
            # print(f"Web Client connected: {addr}")
        except OSError:
            pass

        # Handle clients
        # Use a copy to allow removal during iteration
        for cl in self.clients[:]:
            try:
                # Read request
                request = cl.recv(2048) # Increased buffer
                if request:
                    self._handle_request(cl, request)
                    if cl in self.clients: self.clients.remove(cl)
                    cl.close()
                else:
                    # Connection closed by client
                    if cl in self.clients: self.clients.remove(cl)
                    cl.close()
            except OSError as e:
                # EAGAIN (no data)
                if e.args[0] == 11: # EAGAIN
                    continue
                else:
                    if cl in self.clients: self.clients.remove(cl)
                    cl.close()

        if self._pending_wifi_connect:
            self._pending_wifi_connect = None
            try:
                nm = bus.get_service("network_manager")
                if nm:
                    try:
                        nm.disable_wifi()
                    except:
                        pass
                    nm.enable_wifi()
            except Exception as e:
                get_log().error(f"Web WiFi Connect Error: {e}")

        if self._keep_alive_until and time.time() > self._keep_alive_until:
            if self._keep_alive_owned:
                if bus.shared.get("manual_keep_alive"):
                    bus.shared["manual_keep_alive"] = False
                    bus.shared["app_connected"] = False
                self._keep_alive_owned = False
            self._keep_alive_until = 0

    def _handle_request(self, cl, request):
        try:
            req_str = request.decode('utf-8')
            # Parse first line: GET / HTTP/1.1
            lines = req_str.split('\r\n')
            if not lines: return
            first_line = lines[0]
            parts = first_line.split(' ')
            if len(parts) < 2: return
            method, path = parts[0], parts[1]
            path = path.split('?', 1)[0]
            
            if path == '/' or path == '/index.html':
                self._touch_keep_alive()
                self._serve_file(cl, '/index.html')
            elif path == '/api/wifi/status' and method == 'GET':
                self._touch_keep_alive()
                self._handle_wifi_status(cl)
            elif path == '/api/wifi/scan' and method == 'GET':
                self._touch_keep_alive()
                self._handle_wifi_scan(cl)
            elif path == '/api/wifi/connect' and method == 'POST':
                self._touch_keep_alive()
                body = self._extract_body(lines)
                if body.strip().startswith('{'):
                    self._handle_wifi_connect(cl, body)
                else:
                    self._send_text(cl, 400, "text/plain", "Body missing or invalid")
            elif path.startswith('/api/cmd') and method == 'POST':
                # Find body
                body = self._extract_body(lines)
                
                # Simple check if body is JSON
                if body.strip().startswith('{'):
                    self._handle_api(cl, body)
                else:
                    self._send_text(cl, 400, "text/plain", "Body missing or invalid")
            elif path == '/api/perf':
                # Return performance metrics
                perf = bus.shared.get('perf', {})
                self._send_json(cl, 200, perf)
            elif method == 'GET' and self._is_static_path(path):
                self._touch_keep_alive()
                self._serve_file(cl, path)
            else:
                 self._send_text(cl, 404, "text/plain", "Not Found")
        except Exception as e:
            try:
                if isinstance(e, OSError) and e.args and e.args[0] == 11:
                    return
            except:
                pass
            get_log().error(f"Web Request Error: {e}")
            try: self._send_text(cl, 500, "text/plain", "Internal Error")
            except: pass

    def _sleep_ms(self, ms):
        try:
            time.sleep_ms(ms)
        except:
            try:
                time.sleep(ms / 1000)
            except:
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
                except:
                    pass
                get_log().error(f"Web Serve Error: {e}")
        self._send_text(cl, 404, "text/plain", "Web file not found")

    def _handle_api(self, cl, body):
        try:
            # Clean up null bytes if any
            body = body.strip('\x00')
            cmd_data = json.loads(body)
            cmd_id = cmd_data.get('cmd')
            payload_obj = cmd_data.get('payload', {})
            
            if self.app and cmd_id:
                if isinstance(cmd_id, str):
                    s = cmd_id.strip().lower()
                    if s.startswith('0x'):
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
                    except:
                        pass

                ctx = {
                    "app": self.app,
                    "transport": "WebUI",
                    "send": collect_send
                }

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
            get_log().error(f"API Error: {e}")
            self._send_json(cl, 500, {"status": "error", "error": str(e)})

    def _touch_keep_alive(self):
        now = 0
        try:
            now = time.time()
        except:
            now = 0
        if now and self._last_keep_alive_touch and (now - self._last_keep_alive_touch) < 1:
            keep_alive_secs = 300
            try:
                keep_alive_secs = int(bus.shared.get("Network", {}).get("wifi", {}).get("timeout", 300))
            except:
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
            except:
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
        except:
            keep_alive_secs = 300
        if keep_alive_secs < 5:
            keep_alive_secs = 5
        self._keep_alive_until = time.time() + keep_alive_secs

    def _extract_body(self, lines):
        body = ""
        for i, line in enumerate(lines):
            if line == "":
                body = "\r\n".join(lines[i+1:])
                break
        return body

    def _send_headers(self, cl, code, content_type=None, content_length=None):
        reason = "OK" if code == 200 else "Bad Request" if code == 400 else "Not Found" if code == 404 else "Error"
        headers = f"HTTP/1.1 {code} {reason}\r\n"
        headers += "Connection: close\r\n"
        if content_type:
            headers += f"Content-Type: {content_type}\r\n"
        if content_length is not None:
            headers += f"Content-Length: {content_length}\r\n"
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
                    except:
                        connected = False
                    try:
                        cfg = sta.ifconfig()
                        ip = cfg[0]
                        gw = cfg[2]
                    except:
                        pass
            except:
                pass

            try:
                if ap.active() and not active:
                    mode = "ap"
                    active = True
                    try:
                        cfg = ap.ifconfig()
                        ip = cfg[0]
                        gw = cfg[2]
                    except:
                        pass
                    try:
                        ssid = ap.config("essid")
                    except:
                        pass
            except:
                pass

            if not ssid:
                try:
                    ssid = bus.shared.get("Network", {}).get("wifi", {}).get("ssid", "")
                except:
                    ssid = ""

            self._send_json(cl, 200, {
                "mode": mode,
                "active": active,
                "connected": connected,
                "ip": ip,
                "gw": gw,
                "ssid": ssid,
                "slave_id": bus.slave_id
            })
        except Exception as e:
            self._send_json(cl, 500, {"status": "error", "error": str(e)})

    def _handle_wifi_scan(self, cl):
        try:
            import network
            try:
                import binascii
            except:
                binascii = None

            sta = network.WLAN(network.STA_IF)
            try:
                if not sta.active():
                    sta.active(True)
                    time.sleep(0.5)
            except:
                pass

            nets = []
            try:
                res = sta.scan()
                res.sort(key=lambda x: x[3], reverse=True)
                for info in res:
                    ssid_b = info[0]
                    bssid_b = info[1]
                    ch = info[2]
                    rssi = info[3]
                    auth = info[4]
                    hidden = info[5]
                    try:
                        ssid = ssid_b.decode("utf-8") if ssid_b else ""
                    except:
                        ssid = ""
                    bssid = ""
                    if binascii and bssid_b:
                        try:
                            bssid = binascii.hexlify(bssid_b).decode()
                        except:
                            bssid = ""
                    nets.append({
                        "ssid": ssid,
                        "bssid": bssid,
                        "channel": ch,
                        "rssi": rssi,
                        "auth": auth,
                        "hidden": hidden
                    })
            except Exception as e:
                self._send_json(cl, 500, {"status": "error", "error": str(e), "networks": []})
                return

            self._send_json(cl, 200, {"status": "ok", "networks": nets})
        except Exception as e:
            self._send_json(cl, 500, {"status": "error", "error": str(e)})

    def _handle_wifi_connect(self, cl, body):
        try:
            body = body.strip('\x00')
            data = json.loads(body)
            ssid = (data.get("ssid") or "").strip()
            password = data.get("password") or ""
            save = bool(data.get("save", True))

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
                    get_log().error(f"Web WiFi Save Error: {e}")

            self._pending_wifi_connect = {"ssid": ssid}
            self._send_json(cl, 200, {"status": "ok", "queued": True})
        except Exception as e:
            self._send_json(cl, 500, {"status": "error", "error": str(e)})

    def on_stop(self):
        super().on_stop()
        if self.sock:
            try: self.sock.close()
            except: pass
        for cl in self.clients:
            try: cl.close()
            except: pass
        self.clients = []
        get_log().info("WebUI Stopped")
