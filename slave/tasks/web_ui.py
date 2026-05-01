import socket
import json
import time
from lib.task import Task
from lib.sys_bus import bus

class WebUITask(Task):
    def __init__(self, name, ctx):
        super().__init__(name, ctx)
        self.port = 80
        self.sock = None
        self.clients = []
        self.app = ctx.get('app')
        
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
            print(f"🌍 [WebUI] Listening on port {self.port}")
        except Exception as e:
            print(f"❌ [WebUI] Start failed: {e}")
            self.sock = None

    def loop(self):
        if not self.running or not self.sock: return

        # Accept new connections
        try:
            cl, addr = self.sock.accept()
            cl.setblocking(False)
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
            
            if path == '/' or path == '/index.html':
                self._serve_file(cl, '/index.html')
            elif path.startswith('/api/cmd') and method == 'POST':
                # Find body
                body = ""
                for i, line in enumerate(lines):
                    if line == "":
                        body = "\r\n".join(lines[i+1:])
                        break
                
                # Simple check if body is JSON
                if body.strip().startswith('{'):
                    self._handle_api(cl, body)
                else:
                    cl.send(b'HTTP/1.1 400 Bad Request\r\n\r\nBody missing or invalid')
            elif path == '/api/perf':
                # Return performance metrics
                perf = bus.shared.get('perf', {})
                resp = json.dumps(perf)
                cl.send('HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n' + resp)
            else:
                 cl.send(b'HTTP/1.1 404 Not Found\r\n\r\n')
        except Exception as e:
            print(f"Web Request Error: {e}")
            try: cl.send(b'HTTP/1.1 500 Internal Error\r\n\r\n')
            except: pass

    def _serve_file(self, cl, path):
        # Basic HTML for testing
        response = """<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Net-Light Control</title>
<style>
body { font-family: sans-serif; padding: 20px; background: #222; color: #eee; }
button { padding: 10px 20px; font-size: 16px; margin: 5px; cursor: pointer; }
.red { background: #d32f2f; color: white; border: none; }
.green { background: #388e3c; color: white; border: none; }
.blue { background: #1976d2; color: white; border: none; }
pre { background: #333; padding: 10px; }
</style>
</head>
<body>
<h1>Net-Light Control</h1>
<p>Status: <span id="status">Online</span></p>
<p>Core 0: <span id="perf0">...</span> ms/loop (<span id="hz0">...</span> Hz)</p>
<p>Core 1: <span id="perf1">...</span> ms/loop (<span id="hz1">...</span> Hz)</p>

<h3>Test Controls</h3>
<button class="green" onclick="cmd(0x1101, {'query_type':1})">Get Status</button>
<button class="blue" onclick="send_cmd('status_get', {})">Status (Alias)</button>
<button class="red" onclick="refresh_perf()">Refresh Perf</button>

<div id="log"></div>

<script>
function log(msg) {
    document.getElementById('log').innerHTML = '<pre>' + JSON.stringify(msg, null, 2) + '</pre>';
}

function cmd(c, p) {
  fetch('/api/cmd', {
    method: 'POST',
    body: JSON.stringify({cmd: c, payload: p})
  }).then(r=>r.json()).then(log).catch(e=>log(e));
}

function send_cmd(name, p) {
    // Mapping for convenience
    const cmds = {
        'status_get': 0x1101
    };
    if(cmds[name]) cmd(cmds[name], p);
}

function refresh_perf() {
    fetch('/api/perf')
    .then(r=>r.json())
    .then(d => {
        if(d.core0_loop_ms) document.getElementById('perf0').innerText = d.core0_loop_ms.toFixed(3);
        if(d.core0_loops_per_sec) document.getElementById('hz0').innerText = d.core0_loops_per_sec.toFixed(1);
        
        if(d.core1_loop_ms) document.getElementById('perf1').innerText = d.core1_loop_ms.toFixed(3);
        if(d.core1_loops_per_sec) document.getElementById('hz1').innerText = d.core1_loops_per_sec.toFixed(1);
    });
}
// Auto refresh
setInterval(refresh_perf, 2000);
</script>
</body></html>"""
        cl.send('HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n' + response)

    def _handle_api(self, cl, body):
        try:
            # Clean up null bytes if any
            body = body.strip('\x00')
            cmd_data = json.loads(body)
            cmd_id = cmd_data.get('cmd')
            payload = cmd_data.get('payload', {})
            
            if self.app and cmd_id:
                # Convert cmd_id to int if it's hex string
                if isinstance(cmd_id, str) and cmd_id.startswith('0x'):
                    cmd_id = int(cmd_id, 16)
                
                # Mock send function
                def mock_send(data):
                    pass

                ctx = {
                    "app": self.app,
                    "transport": "WebUI",
                    "send": mock_send 
                }
                
                self.app.disp.dispatch(cmd_id, payload, ctx)
                cl.send(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{"status":"ok", "dispatched": true}')
            else:
                cl.send(b'HTTP/1.1 400 Bad Request\r\n\r\nMissing cmd')
        except Exception as e:
            print(f"API Error: {e}")
            cl.send(b'HTTP/1.1 500 Error\r\n\r\n' + str(e).encode())

    def on_stop(self):
        super().on_stop()
        if self.sock:
            try: self.sock.close()
            except: pass
        for cl in self.clients:
            try: cl.close()
            except: pass
        self.clients = []
        print("WebUI Stopped")
