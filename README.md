# Attopilot 🚗⚡

BYD Atto 3 IoT control system — CANable OBD-II + Android ADB + MQTT + PWA dashboard.

## Architecture

```
BYD Atto 3
  ├── OBD-II (CANable Pro) ──────────────────┐
  └── DiLink 3.0 (ADB WiFi 192.168.1.155) ──┤
                                              ▼
                                    MacBook Edge (100.72.184.10)
                                    ├── Mosquitto MQTT :1883
                                    ├── Attopilot Edge API :8765
                                    │     ├── GET  /status
                                    │     ├── POST /command
                                    │     ├── GET  /screenshot
                                    │     └── WS   /ws
                                    └── (Tailscale VPN)
                                              │
                                      ┌───────┴───────┐
                                   PWA App        MQTT clients
                                 (Vercel)        (Electro app)
```

## Phase 1 — MQTT Server ✅
- Mosquitto running on MacBook at `100.72.184.10:1883`
- User: `attopilot` / Pass: `Att0p1l0t@2026`
- Topics: `attopilot/telemetry`, `attopilot/command`, `attopilot/result`
- Edge API: `http://100.72.184.10:8765`

## Phase 2 — Edge API + Surveillance (in progress)
- FastAPI edge server with REST + WebSocket
- ADB-based car state polling every 5s
- Screenshot endpoint for DiLink screen capture
- Command execution: sunroof, windows, horn, HVAC, hazards

## Phase 3 — PWA Dashboard (planned)
- Next.js PWA hosted on Vercel
- Live telemetry dashboard (SOC, voltage, speed, MCU state)
- Game-mode remote control UI
- Webcam/camera feeds from DiLink
- Internet-accessible via Tailscale or Cloudflare tunnel

## Setup

### Edge Server
```bash
cd edge
source ~/Attopilot/venv39/bin/activate
pip install -r requirements.txt
API_TOKEN=your-secret python3 server.py
```

### Connect Electro App
- Host: `100.72.184.10`
- Port: `1883`
- User: `attopilot`
- Pass: `Att0p1l0t@2026`

## Commands

POST `/command?token=attopilot-secret-2026` with `{"action": "..."}`:

| Action | Description |
|--------|-------------|
| `screenshot` | Capture DiLink screen |
| `sunroof_open` | Open panoramic roof |
| `sunroof_close` | Close panoramic roof |
| `sunroof_tilt` | Tilt sunroof |
| `window_all_up` | All windows up |
| `window_all_down` | All windows down |
| `horn` | Honk horn |
| `ac_on` / `ac_off` | Air conditioning |
| `hazard_on` / `hazard_off` | Hazard lights |
| `adb_reconnect` | Reconnect ADB |
