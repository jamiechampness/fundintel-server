#!/bin/bash
# Playwright's node driver needs libatomic; install it first (never blocks startup)
(apt-get update -qq && apt-get install -y -qq libatomic1) || echo "libatomic install skipped"
# Install Playwright browsers
python -m playwright install chromium
python -m playwright install-deps chromium
# Start the server
python fundintel_server.py
