#!/bin/bash
# Robyx — Linux uninstaller
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
SERVICE_NAME="robyx"
SERVICE_FILE="$HOME/.config/systemd/user/$SERVICE_NAME.service"

echo "=== Robyx Linux Uninstaller ==="
echo ""

# Stop and remove systemd service
if systemctl --user is-active "$SERVICE_NAME" >/dev/null 2>&1; then
    echo "Stopping service..."
    systemctl --user stop "$SERVICE_NAME"
fi

if [ -f "$SERVICE_FILE" ]; then
    systemctl --user disable "$SERVICE_NAME" 2>/dev/null || true
    rm -f "$SERVICE_FILE"
    systemctl --user daemon-reload
    echo "Service unit removed."
else
    echo "Service not found (already removed)."
fi

# Kill any lingering bot process
if pgrep -f "python.*bot\.py" >/dev/null 2>&1; then
    echo "Stopping lingering bot process..."
    pkill -f "python.*bot\.py" 2>/dev/null || true
fi

# Clean runtime state (logs, locks, pid files)
if [ -d "$PROJECT_ROOT/data" ]; then
    echo "Cleaning runtime state..."
    find "$PROJECT_ROOT/data" -name "lock" -delete 2>/dev/null || true
    find "$PROJECT_ROOT/data" -name "output.log" -delete 2>/dev/null || true
fi

# Clean log
rm -f "$PROJECT_ROOT/bot.log"
rm -f "$PROJECT_ROOT/bot.log."* 2>/dev/null

echo ""
echo "=== Robyx uninstalled ==="
echo ""
echo "Service stopped and removed from startup."
echo "Project files are untouched. Remove $PROJECT_ROOT manually if desired."
echo ""
