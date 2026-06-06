"""
Attopilot Car Brain — subscribes to Electro MQTT stream
Decodes electro/telemetry/light-fang/data, maintains car_state.json
No AI involved — pure Python state tracker
"""
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("car-brain")

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "attopilot")
MQTT_PASS = os.getenv("MQTT_PASS", "Att0p1l0t@2026")
DEVICE    = os.getenv("ELECTRO_DEVICE", "light-fang")

STATE_FILE   = Path("/root/attopilot/car_state.json")
HISTORY_FILE = Path("/root/attopilot/car_history.jsonl")

car = {}
prev = {}

def save_state():
    car["_updated"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.write_text(json.dumps(car, indent=2))

def detect_events(data: dict):
    events = []
    soc  = data.get("soc")
    psoc = prev.get("soc")
    gear = data.get("gear")
    pgear= prev.get("gear")

    if gear != pgear and pgear is not None:
        events.append(f"gear {pgear}→{gear}")

    if soc and psoc and abs(soc - psoc) >= 5:
        events.append(f"SOC {psoc:.1f}%→{soc:.1f}%")

    soc_val = soc or 0
    if soc_val <= 15 and (not psoc or psoc > 15):
        events.append(f"LOW BATTERY {soc_val:.0f}%")

    cp = data.get("charging_power", 0)
    pcp = prev.get("charging_power", 0)
    if cp > 0 and pcp == 0:
        events.append(f"charging started {cp:.1f}kW")
    if cp == 0 and pcp > 0:
        events.append(f"charging stopped SOC={soc:.0f}%")

    for ev in events:
        log.info(f"EVENT: {ev}")
        with open(HISTORY_FILE, "a") as f:
            f.write(json.dumps({"ts": car["_updated"], "event": ev, "state": dict(car)}) + "\n")

def on_connect(client, userdata, flags, rc):
    log.info(f"MQTT connected rc={rc}")
    client.subscribe(f"electro/telemetry/{DEVICE}/data")
    client.subscribe("attopilot/telemetry")

def on_message(client, userdata, msg):
    global prev
    try:
        data = json.loads(msg.payload)
        prev = dict(car)
        car.update(data)
        detect_events(data)
        save_state()
        log.info(
            f"SOC={car.get('soc')}% range={car.get('electric_driving_range_km')}km "
            f"gear={car.get('gear')} spd={car.get('speed')}km/h "
            f"V={car.get('battery_total_voltage')}V"
        )
    except Exception as e:
        log.error(f"Message error: {e}")

def main():
    client = mqtt.Client(client_id="attopilot-brain")
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    log.info(f"Car Brain started — watching electro/telemetry/{DEVICE}/data")
    client.loop_forever()

if __name__ == "__main__":
    main()
