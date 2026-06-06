"""
Attopilot MacBook Relay
Runs on MacBook (near car, same WiFi as DiLink 192.168.1.155)
- Polls ADB every 5s, publishes telemetry to Mac Mini MQTT
- Accepts command requests from Mac Mini via simple HTTP
- Publishes raw CAN frames when CANable is connected
"""
import json
import logging
import os
import subprocess
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

import paho.mqtt.client as mqtt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("relay")

# Mac Mini MQTT (always-on Cave)
MQTT_HOST = os.getenv("MQTT_HOST", "100.110.132.91")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "attopilot")
MQTT_PASS = os.getenv("MQTT_PASS", "Att0p1l0t@2026")

ADB_HOST = os.getenv("ADB_HOST", "192.168.1.155")
ADB_PORT = os.getenv("ADB_PORT", "5555")
ADB = "/opt/homebrew/bin/adb"
RELAY_PORT = int(os.getenv("RELAY_PORT", "8766"))
API_TOKEN = os.getenv("API_TOKEN", "attopilot-secret-2026")

mqtt_client = None

# ── ADB ───────────────────────────────────────────────────────────────────────
def adb(*args, timeout=5) -> str:
    try:
        r = subprocess.run([ADB, "-s", f"{ADB_HOST}:{ADB_PORT}", *args],
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception as e:
        return f"ERROR:{e}"

def adb_connect() -> bool:
    try:
        out = subprocess.run([ADB, "connect", f"{ADB_HOST}:{ADB_PORT}"],
                             capture_output=True, text=True, timeout=5).stdout
        return "connected" in out.lower()
    except Exception:
        return False

def read_prop(p: str) -> str:
    return adb("shell", "getprop", p)

def poll_adb() -> dict:
    connected = adb_connect()
    if not connected:
        return {"adb_connected": False}

    mcu = read_prop("sys.ivi.mcu_hw_ver")
    accoff = read_prop("sys.carplay.accoff")
    drive_raw = adb("shell", "service", "call", "byd_car_service", "7")

    drive_state = 0
    try:
        parts = drive_raw.replace("Result:", "").replace("Parcel(", "").strip().split()
        if len(parts) >= 2:
            drive_state = int(parts[1], 16)
    except Exception:
        pass

    return {
        "adb_connected": True,
        "mcu_online": bool(mcu and mcu != "MCU_OFFLINE" and not mcu.startswith("ERROR")),
        "drive_state": drive_state,
        "acc_off": accoff == "true",
        "timestamp": datetime.utcnow().isoformat(),
    }

# ── Commands ──────────────────────────────────────────────────────────────────
COMMANDS = {
    "screenshot": lambda: _screenshot(),
    "sunroof_open": lambda: adb("shell", "am", "broadcast", "-a", "com.byd.sunroof.OPEN"),
    "sunroof_close": lambda: adb("shell", "am", "broadcast", "-a", "com.byd.sunroof.CLOSE"),
    "sunroof_tilt": lambda: adb("shell", "am", "broadcast", "-a", "com.byd.sunroof.TILT"),
    "window_all_up": lambda: adb("shell", "am", "broadcast", "-a", "com.byd.window.ALL_UP"),
    "window_all_down": lambda: adb("shell", "am", "broadcast", "-a", "com.byd.window.ALL_DOWN"),
    "horn": lambda: adb("shell", "service", "call", "byd_car_service", "20", "i32", "1"),
    "ac_on": lambda: adb("shell", "am", "broadcast", "-a", "com.byd.hvac.AC_ON"),
    "ac_off": lambda: adb("shell", "am", "broadcast", "-a", "com.byd.hvac.AC_OFF"),
    "hazard_on": lambda: adb("shell", "am", "broadcast", "-a", "com.byd.body.HAZARD_ON"),
    "hazard_off": lambda: adb("shell", "am", "broadcast", "-a", "com.byd.body.HAZARD_OFF"),
    "adb_reconnect": lambda: str(adb_connect()),
}

def _screenshot() -> str:
    ts = int(time.time())
    remote = f"/sdcard/attopilot_{ts}.png"
    local = f"/tmp/attopilot_{ts}.png"
    adb("shell", "screencap", "-p", remote)
    adb("pull", remote, local)
    adb("shell", "rm", remote)
    return local

# ── HTTP relay server ─────────────────────────────────────────────────────────
class RelayHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress default access log

    def do_GET(self):
        if self.path.startswith("/status"):
            data = poll_adb()
            self._json(200, data)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if "/command" in self.path:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            token = self.headers.get("X-Token", "") or body.get("token", "")
            if token != API_TOKEN:
                self._json(401, {"error": "unauthorized"})
                return
            action = body.get("action")
            if action not in COMMANDS:
                self._json(400, {"error": f"unknown action: {action}"})
                return
            result = COMMANDS[action]()
            self._json(200, {"action": action, "result": str(result)})
        else:
            self._json(404, {"error": "not found"})

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

# ── MQTT setup ────────────────────────────────────────────────────────────────
def mqtt_setup():
    global mqtt_client
    mqtt_client = mqtt.Client(client_id="attopilot-relay")
    mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
    mqtt_client.on_connect = lambda c, u, f, rc: log.info(f"MQTT connected to Cave rc={rc}")
    # Accept command requests via MQTT too
    mqtt_client.on_message = _on_command
    try:
        mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
        mqtt_client.subscribe("attopilot/command")
        mqtt_client.loop_start()
        log.info(f"MQTT connected to Cave ({MQTT_HOST}:{MQTT_PORT})")
    except Exception as e:
        log.warning(f"MQTT connect failed: {e}")

def _on_command(client, userdata, msg):
    try:
        payload = json.loads(msg.payload)
        action = payload.get("action")
        if action in COMMANDS:
            result = COMMANDS[action]()
            client.publish("attopilot/result", json.dumps({"action": action, "result": str(result)}))
    except Exception as e:
        log.error(f"Command error: {e}")

def publish(topic: str, data: dict):
    if mqtt_client:
        try:
            mqtt_client.publish(topic, json.dumps(data))
        except Exception:
            pass

# ── Poll loop ─────────────────────────────────────────────────────────────────
def poll_loop():
    while True:
        try:
            state = poll_adb()
            publish("attopilot/telemetry", state)
            log.info(f"Published: adb={state.get('adb_connected')} mcu={state.get('mcu_online')} drive={state.get('drive_state')}")
        except Exception as e:
            log.error(f"Poll error: {e}")
        time.sleep(10)  # poll every 10s — don't hammer

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mqtt_setup()
    t = threading.Thread(target=poll_loop, daemon=True)
    t.start()
    server = HTTPServer(("0.0.0.0", RELAY_PORT), RelayHandler)
    log.info(f"Relay HTTP on :{RELAY_PORT} | MQTT → Cave {MQTT_HOST}:{MQTT_PORT}")
    server.serve_forever()
