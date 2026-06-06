"""
Attopilot Car Brain — MQTT subscriber + CAN frame decoder
Subscribes to attopilot/telemetry and attopilot/canframes
Decodes BYD Atto 3 broadcast frames, maintains car state, sends notifications
"""
import json
import logging
import os
import time
import struct
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("car-brain")

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "attopilot")
MQTT_PASS = os.getenv("MQTT_PASS", "Att0p1l0t@2026")

# Maddy notification webhook (OpenClaw inbound or Telegram bot)
NOTIFY_TOKEN = os.getenv("NOTIFY_TOKEN", "")
NOTIFY_CHAT = os.getenv("NOTIFY_CHAT", "196536622")  # Nisal's Telegram ID

STATE_FILE = Path(os.getenv("STATE_FILE", os.path.expanduser("~/Attopilot/car_state.json")))
LOG_FILE = Path(os.path.expanduser("~/Attopilot/car_brain.log"))

# ── CAN frame decoders (BYD Atto 3) ─────────────────────────────────────────
# Based on Electro app RE + our confirmed captures

def decode_0x434(raw: str) -> dict:
    """Battery pack voltage + current (from BMS broadcast)"""
    b = bytes.fromhex(raw)
    if len(b) < 8:
        return {}
    # Bytes 4-5: HV voltage × 0.01 = volts
    hv_raw = (b[4] << 8) | b[5]
    voltage = hv_raw * 0.01
    # Bytes 6-7: pack current (signed) × 0.1 = amps
    curr_raw = struct.unpack(">h", bytes([b[6], b[7]]))[0]
    current = curr_raw * 0.1
    return {"hv_voltage_v": round(voltage, 2), "pack_current_a": round(current, 2)}

def decode_0x294(raw: str) -> dict:
    """SOC + range estimate (body/BCM broadcast)"""
    b = bytes.fromhex(raw)
    if len(b) < 8:
        return {}
    # Byte 2: SOC % (0–100, scaled ×1)
    soc = b[2]
    # Bytes 4-5: estimated range km × 0.1
    range_raw = (b[4] << 8) | b[5]
    range_km = range_raw * 0.1
    # Byte 0: rolling counter (0–0xFF)
    counter = b[0]
    return {"soc_pct": soc, "range_km": round(range_km, 1), "counter": counter}

def decode_0x12D(raw: str) -> dict:
    """Instrument cluster / ambient conditions"""
    b = bytes.fromhex(raw)
    if len(b) < 8:
        return {}
    # Byte 3: ambient temp °C - 40 offset
    temp = b[3] - 40
    # Byte 1: vehicle speed km/h (if non-zero)
    speed = b[1]
    return {"ambient_temp_c": temp, "speed_kmh": speed}

def decode_0x3D9(raw: str) -> dict:
    """Motor / powertrain data"""
    b = bytes.fromhex(raw)
    if len(b) < 8:
        return {}
    # Byte 2: motor temp °C - 40
    motor_temp = b[2] - 40
    # Bytes 0-1: motor speed RPM (signed)
    motor_rpm = struct.unpack(">h", bytes([b[0], b[1]]))[0]
    # Byte 4: torque Nm (signed) × 0.5
    torque = struct.unpack(">b", bytes([b[4]]))[0] * 0.5
    return {"motor_rpm": motor_rpm, "motor_temp_c": motor_temp, "torque_nm": round(torque, 1)}

def decode_0x34F(raw: str) -> dict:
    """Body systems bitmask (door/window/lock states)"""
    b = bytes.fromhex(raw)
    if len(b) < 8:
        return {}
    # Byte 0 bits: door open states
    # Byte 1 bits: lock states
    doors_open = bool(b[0] & 0x0F)  # any door open
    locked = bool(b[1] & 0x01)
    return {"any_door_open": doors_open, "locked": locked, "raw_b0": hex(b[0]), "raw_b1": hex(b[1])}

DECODERS = {
    "0x434": decode_0x434,
    "0x294": decode_0x294,
    "0x12D": decode_0x12D,
    "0x3D9": decode_0x3D9,
    "0x34F": decode_0x34F,
}

# ── Car state ────────────────────────────────────────────────────────────────
car = {
    "soc_pct": None,
    "range_km": None,
    "hv_voltage_v": None,
    "pack_current_a": None,
    "speed_kmh": None,
    "ambient_temp_c": None,
    "motor_rpm": None,
    "motor_temp_c": None,
    "torque_nm": None,
    "any_door_open": None,
    "locked": None,
    "mcu_online": False,
    "drive_state": 0,
    "adb_connected": False,
    "is_driving": False,
    "is_charging": False,
    "last_can_update": None,
    "last_adb_update": None,
    "updated_at": None,
}
prev = {}

# ── Notification ──────────────────────────────────────────────────────────────
def notify(msg: str):
    log.info(f"NOTIFY: {msg}")
    LOG_FILE.parent.mkdir(exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    if NOTIFY_TOKEN:
        try:
            url = f"https://api.telegram.org/bot{NOTIFY_TOKEN}/sendMessage"
            data = urllib.parse.urlencode({"chat_id": NOTIFY_CHAT, "text": f"🚗 Attopilot: {msg}"}).encode()
            urllib.request.urlopen(url, data, timeout=5)
        except Exception as e:
            log.warning(f"Telegram notify failed: {e}")

# ── State change detection ────────────────────────────────────────────────────
def detect_events():
    global prev
    events = []

    # MCU came online
    if car["mcu_online"] and not prev.get("mcu_online"):
        events.append("🟢 MCU online — car is in READY mode")

    # MCU went offline
    if not car["mcu_online"] and prev.get("mcu_online"):
        events.append("🔴 MCU offline — car turned off")

    # Started driving
    was_driving = prev.get("is_driving", False)
    speed = car.get("speed_kmh") or 0
    car["is_driving"] = speed > 5 or (car.get("motor_rpm") or 0) > 100
    if car["is_driving"] and not was_driving:
        events.append(f"🚗 Trip started — {speed} km/h")

    # Stopped driving
    if not car["is_driving"] and was_driving:
        events.append("🅿️ Parked")

    # SOC changed ≥5%
    soc = car.get("soc_pct")
    prev_soc = prev.get("soc_pct")
    if soc is not None and prev_soc is not None and abs(soc - prev_soc) >= 5:
        events.append(f"🔋 SOC: {prev_soc}% → {soc}%")

    # Low SOC warning
    if soc is not None and soc <= 15 and (prev_soc is None or prev_soc > 15):
        events.append(f"⚠️ Low battery! SOC {soc}% — ~{car.get('range_km', '?')} km range")

    # Charging detected (negative current = charging)
    curr = car.get("pack_current_a")
    was_charging = prev.get("is_charging", False)
    car["is_charging"] = curr is not None and curr < -1
    if car["is_charging"] and not was_charging:
        events.append(f"⚡ Charging started ({abs(curr):.1f}A)")
    if not car["is_charging"] and was_charging:
        events.append(f"🔋 Charging stopped — SOC {soc}%")

    # Door opened
    if car.get("any_door_open") and not prev.get("any_door_open"):
        events.append("🚪 Door opened")

    for ev in events:
        notify(ev)

    prev = dict(car)
    return events

# ── Save state ────────────────────────────────────────────────────────────────
def save_state():
    car["updated_at"] = datetime.utcnow().isoformat()
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(car, indent=2))

# ── MQTT callbacks ────────────────────────────────────────────────────────────
def on_connect(client, userdata, flags, rc):
    log.info(f"MQTT connected rc={rc}")
    client.subscribe("attopilot/telemetry")
    client.subscribe("attopilot/canframes")

def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload)
        topic = msg.topic

        if topic == "attopilot/telemetry":
            # ADB-derived state from edge server
            for k in ("mcu_online", "drive_state", "adb_connected", "soc", "hv_voltage", "speed", "vin"):
                if k in payload:
                    mapped = {"soc": "soc_pct", "hv_voltage": "hv_voltage_v", "speed": "speed_kmh"}.get(k, k)
                    car[mapped] = payload[k]
            car["last_adb_update"] = datetime.utcnow().isoformat()

        elif topic == "attopilot/canframes":
            # Raw CAN frames — decode known IDs
            frames = payload.get("frames", payload)
            for frame_id, raw in frames.items():
                decoder = DECODERS.get(frame_id)
                if decoder:
                    try:
                        decoded = decoder(raw)
                        car.update(decoded)
                        log.debug(f"{frame_id} → {decoded}")
                    except Exception as e:
                        log.warning(f"Decode error {frame_id}: {e}")
            car["last_can_update"] = datetime.utcnow().isoformat()

        detect_events()
        save_state()
        log.info(f"State: SOC={car.get('soc_pct')}% V={car.get('hv_voltage_v')}V spd={car.get('speed_kmh')}km/h mcu={car.get('mcu_online')}")

    except Exception as e:
        log.error(f"Message error: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    client = mqtt.Client(client_id="attopilot-brain")
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    log.info("Car Brain started — subscribing to attopilot/telemetry + attopilot/canframes")
    notify("🧠 Car Brain started — watching for car events")
    client.loop_forever()

if __name__ == "__main__":
    main()
