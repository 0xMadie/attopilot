"""
Attopilot Edge Server — runs on MacBook (100.72.184.10)
Bridges ADB (DiLink 3.0) + CANable (OBD-II) → MQTT + WebSocket/REST
"""
import asyncio
import json
import logging
import os
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import datetime

import paho.mqtt.client as mqtt
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("attopilot")

# ── Config ────────────────────────────────────────────────────────────────────
ADB_HOST = os.getenv("ADB_HOST", "192.168.1.155")
ADB_PORT = os.getenv("ADB_PORT", "5555")
MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "attopilot")
MQTT_PASS = os.getenv("MQTT_PASS", "Att0p1l0t@2026")
API_TOKEN = os.getenv("API_TOKEN", "attopilot-secret-2026")

# ── State ─────────────────────────────────────────────────────────────────────
state: dict = {
    "adb_connected": False,
    "mcu_online": False,
    "drive_state": 0,
    "soc": None,
    "hv_voltage": None,
    "speed": None,
    "vin": None,
    "last_update": None,
}
ws_clients: list[WebSocket] = []
mqtt_client: mqtt.Client = None

# ── ADB helpers ───────────────────────────────────────────────────────────────
ADB = "/opt/homebrew/bin/adb"

def adb(*args, timeout=8) -> str:
    try:
        r = subprocess.run(
            [ADB, "-s", f"{ADB_HOST}:{ADB_PORT}", *args],
            capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip()
    except Exception as e:
        return f"ERROR:{e}"

def adb_connect() -> bool:
    try:
        out = subprocess.run([ADB, "connect", f"{ADB_HOST}:{ADB_PORT}"],
                             capture_output=True, text=True, timeout=5).stdout
        ok = "connected" in out.lower()
    except Exception:
        ok = False
    state["adb_connected"] = ok
    return ok

def read_prop(prop: str) -> str:
    return adb("shell", "getprop", prop)

def read_car_state() -> dict:
    props = {}
    for p in ["sys.ivi.mcu_hw_ver", "sys.carplay.accoff", "ro.build.product"]:
        props[p] = read_prop(p)

    mcu = props.get("sys.ivi.mcu_hw_ver", "")
    state["mcu_online"] = bool(mcu and mcu != "MCU_OFFLINE" and not mcu.startswith("ERROR"))

    # Try reading drive state via service call
    raw = adb("shell", "service", "call", "byd_car_service", "7")
    state["drive_state"] = _parse_int32(raw) if raw and not raw.startswith("ERROR") else 0

    state["last_update"] = datetime.utcnow().isoformat()
    return dict(state)

def _parse_int32(raw: str) -> int:
    # service call returns: "Result: Parcel(00000000 0000000X ...)"
    try:
        parts = raw.replace("Result:", "").replace("Parcel(", "").strip()
        tokens = parts.split()
        if len(tokens) >= 2:
            return int(tokens[1], 16)
    except Exception:
        pass
    return 0

# ── Commands ──────────────────────────────────────────────────────────────────
COMMANDS = {
    "screenshot": lambda: _screenshot(),
    "sunroof_open": lambda: _intent("com.byd.sunroof.OPEN"),
    "sunroof_close": lambda: _intent("com.byd.sunroof.CLOSE"),
    "sunroof_tilt": lambda: _intent("com.byd.sunroof.TILT"),
    "window_all_up": lambda: _intent("com.byd.window.ALL_UP"),
    "window_all_down": lambda: _intent("com.byd.window.ALL_DOWN"),
    "horn": lambda: adb("shell", "service", "call", "byd_car_service", "20", "i32", "1"),
    "ac_on": lambda: _intent("com.byd.hvac.AC_ON"),
    "ac_off": lambda: _intent("com.byd.hvac.AC_OFF"),
    "hazard_on": lambda: _intent("com.byd.body.HAZARD_ON"),
    "hazard_off": lambda: _intent("com.byd.body.HAZARD_OFF"),
    "adb_reconnect": lambda: str(adb_connect()),
}

def _intent(action: str) -> str:
    return adb("shell", "am", "broadcast", "-a", action)

def _screenshot() -> str:
    ts = int(time.time())
    remote = f"/sdcard/attopilot_{ts}.png"
    local = f"/tmp/attopilot_{ts}.png"
    adb("shell", "screencap", "-p", remote)
    adb("pull", remote, local)
    adb("shell", "rm", remote)
    return local

# ── MQTT ──────────────────────────────────────────────────────────────────────
def mqtt_setup():
    global mqtt_client
    mqtt_client = mqtt.Client(client_id="attopilot-edge")
    mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
    mqtt_client.on_connect = lambda c, u, f, rc: log.info(f"MQTT connected rc={rc}")
    mqtt_client.on_message = _on_mqtt_message
    try:
        mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
        mqtt_client.subscribe("attopilot/command")
        mqtt_client.loop_start()
    except Exception as e:
        log.warning(f"MQTT connect failed: {e}")

def _on_mqtt_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload)
        action = payload.get("action")
        if action in COMMANDS:
            result = COMMANDS[action]()
            client.publish("attopilot/result", json.dumps({"action": action, "result": str(result)}))
    except Exception as e:
        log.error(f"MQTT command error: {e}")

def mqtt_publish(topic: str, data: dict):
    if mqtt_client:
        try:
            mqtt_client.publish(topic, json.dumps(data))
        except Exception:
            pass

# ── Background poller ─────────────────────────────────────────────────────────
async def poll_loop():
    while True:
        try:
            if not state["adb_connected"]:
                adb_connect()
            if state["adb_connected"]:
                data = read_car_state()
                mqtt_publish("attopilot/telemetry", data)
                # Broadcast to WebSocket clients
                dead = []
                for ws in ws_clients:
                    try:
                        await ws.send_json(data)
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    ws_clients.remove(ws)
        except Exception as e:
            log.error(f"Poll error: {e}")
        await asyncio.sleep(5)

# ── FastAPI app ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    mqtt_setup()
    try:
        adb_connect()
    except Exception as e:
        log.warning(f"ADB startup connect failed (will retry in poll loop): {e}")
    asyncio.create_task(poll_loop())
    yield
    if mqtt_client:
        mqtt_client.loop_stop()

app = FastAPI(title="Attopilot Edge", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def _auth(token: str):
    if token != API_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

@app.get("/status")
def get_status(token: str = ""):
    _auth(token)
    return state

@app.post("/command")
def run_command(body: dict, token: str = ""):
    _auth(token)
    action = body.get("action")
    if not action or action not in COMMANDS:
        raise HTTPException(status_code=400, detail=f"Unknown action. Valid: {list(COMMANDS.keys())}")
    result = COMMANDS[action]()
    return {"action": action, "result": str(result)}

@app.get("/screenshot")
def get_screenshot(token: str = ""):
    _auth(token)
    path = _screenshot()
    from fastapi.responses import FileResponse
    if os.path.exists(path):
        return FileResponse(path, media_type="image/png")
    raise HTTPException(status_code=503, detail="Screenshot failed — ADB may be offline")

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str = ""):
    if token != API_TOKEN:
        await ws.close(code=4001)
        return
    await ws.accept()
    ws_clients.append(ws)
    try:
        # Send current state immediately
        await ws.send_json(state)
        while True:
            msg = await ws.receive_json()
            action = msg.get("action")
            if action in COMMANDS:
                result = COMMANDS[action]()
                await ws.send_json({"action": action, "result": str(result)})
    except WebSocketDisconnect:
        ws_clients.remove(ws)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="info")
