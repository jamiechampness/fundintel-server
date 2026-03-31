#!/bin/bash
# Install Playwright browsers
python -m playwright install chromium
python -m playwright install-deps chromium
# Start the server
python fundintel_server.py
