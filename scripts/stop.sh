#!/usr/bin/env bash
pkill -f "dashboard/server.py" 2>/dev/null && echo "Dashboard stopped" || echo "Dashboard was not running"
pkill -f "demo.py" 2>/dev/null || true
echo "Done."
 