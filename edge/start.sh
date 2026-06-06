#!/bin/bash
# Start Attopilot edge server
cd "$(dirname "$0")"
source ~/Attopilot/venv39/bin/activate
exec python3 server.py
