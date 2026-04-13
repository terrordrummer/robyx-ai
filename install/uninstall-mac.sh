#!/bin/bash
# Robyx — macOS uninstaller
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PLIST_NAME="com.robyx.bot"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"

echo "=== Robyx macOS Uninstaller ==="
echo ""

# Stop and remove launchd service
if launchctl list 2>/dev/null | grep -q "$PLIST_NAME"; then
    echo "Stopping service..."
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
fi

if [ -f "$PLIST_PATH" ]; then
    rm -f "$PLIST_PATH"
    echo "Service plist removed."
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
    rm -f "$PROJECT_ROOT/data/service-stdout.log"
    rm -f "$PROJECT_ROOT/data/service-stderr.log"
fi

# Clean log
rm -f "$PROJECT_ROOT/bot.log"
rm -f "$PROJECT_ROOT/bot.log."* 2>/dev/null

echo ""
echo "=== Robyx uninstalled ==="
echo ""
echo "Service stopped and removed from login items."
echo "Project files are untouched. Remove $PROJECT_ROOT manually if desired."
echo ""
