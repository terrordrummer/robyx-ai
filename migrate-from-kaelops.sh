#!/bin/bash
# Robyx — Migration script from KaelOps (kael-ops) to Robyx (robyx-ai)
#
# Usage: ./migrate-from-kaelops.sh /path/to/old/kael-ops
#
# This script migrates an existing KaelOps installation to Robyx by:
# 1. Stopping the old service
# 2. Copying data/ and .env from the old installation
# 3. Renaming env vars (KAELOPS_* → ROBYX_*)
# 4. Running the new installer
#
# For a fresh install, just run: ./install/install-mac.sh (or install-linux.sh)
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NEW_ROOT="$SCRIPT_DIR"

echo ""
echo "========================================"
echo "  Robyx — Migration from KaelOps"
echo "========================================"
echo ""

# ── Validate arguments ──────────────────────────────────────────────────

if [ -z "$1" ]; then
    echo "Usage: $0 /path/to/old/kael-ops"
    echo ""
    echo "Example: $0 ~/kael-ops"
    echo ""
    echo "The old installation directory must contain:"
    echo "  - data/     (runtime state)"
    echo "  - .env      (configuration)"
    echo "  - bot/      (confirms it's a KaelOps install)"
    exit 1
fi

OLD_ROOT="$(cd "$1" 2>/dev/null && pwd)" || {
    echo "Error: '$1' is not a valid directory."
    exit 1
}

if [ ! -d "$OLD_ROOT/bot" ] || [ ! -d "$OLD_ROOT/data" ]; then
    echo "Error: '$OLD_ROOT' does not look like a KaelOps installation."
    echo "Expected: bot/ and data/ directories."
    exit 1
fi

if [ "$OLD_ROOT" = "$NEW_ROOT" ]; then
    echo "Error: old and new directories are the same."
    echo "This script migrates from a SEPARATE kael-ops installation."
    echo "If you're upgrading in-place, the bot handles it automatically on boot."
    exit 1
fi

echo "Old installation: $OLD_ROOT"
echo "New installation: $NEW_ROOT"
echo ""

# ── Detect platform ─────────────────────────────────────────────────────

OS="$(uname -s)"

# ── Step 1: Stop old service ────────────────────────────────────────────

echo "[1/5] Stopping old KaelOps service..."

if [ "$OS" = "Darwin" ]; then
    OLD_PLIST="$HOME/Library/LaunchAgents/com.kaelops.bot.plist"
    if [ -f "$OLD_PLIST" ]; then
        launchctl unload "$OLD_PLIST" 2>/dev/null || true
        echo "  Stopped com.kaelops.bot (launchd)"
    else
        echo "  No launchd service found (skipped)"
    fi
elif [ "$OS" = "Linux" ]; then
    if systemctl --user is-active kaelops >/dev/null 2>&1; then
        systemctl --user stop kaelops
        systemctl --user disable kaelops 2>/dev/null || true
        echo "  Stopped kaelops (systemd)"
    else
        echo "  No systemd service found (skipped)"
    fi
else
    echo "  Windows: please stop the KaelOps scheduled task manually."
    echo "  Run: Stop-ScheduledTask -TaskName KaelOps"
fi

# ── Step 2: Copy data/ ─────────────────────────────────────────────────

echo "[2/5] Copying data directory..."

if [ -d "$NEW_ROOT/data" ] && [ "$(ls -A "$NEW_ROOT/data" 2>/dev/null)" ]; then
    echo "  Warning: $NEW_ROOT/data/ already has content."
    echo "  Merging (existing files will NOT be overwritten)..."
    # Copy without overwriting — rsync with --ignore-existing
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --ignore-existing "$OLD_ROOT/data/" "$NEW_ROOT/data/"
    else
        cp -rn "$OLD_ROOT/data/" "$NEW_ROOT/data/" 2>/dev/null || true
    fi
else
    mkdir -p "$NEW_ROOT/data"
    cp -r "$OLD_ROOT/data/"* "$NEW_ROOT/data/" 2>/dev/null || true
fi
echo "  Done."

# ── Step 3: Copy and migrate .env ────────────────────────────────────────

echo "[3/5] Migrating .env file..."

if [ -f "$NEW_ROOT/.env" ]; then
    echo "  .env already exists in new installation — backing up as .env.kaelops"
    cp "$NEW_ROOT/.env" "$NEW_ROOT/.env.kaelops"
fi

if [ -f "$OLD_ROOT/.env" ]; then
    # Copy old .env
    cp "$OLD_ROOT/.env" "$NEW_ROOT/.env"

    # Rename KAELOPS_* → ROBYX_*
    if [ "$OS" = "Darwin" ]; then
        sed -i '' 's/KAELOPS_BOT_TOKEN/ROBYX_BOT_TOKEN/g' "$NEW_ROOT/.env"
        sed -i '' 's/KAELOPS_CHAT_ID/ROBYX_CHAT_ID/g' "$NEW_ROOT/.env"
        sed -i '' 's/KAELOPS_OWNER_ID/ROBYX_OWNER_ID/g' "$NEW_ROOT/.env"
        sed -i '' 's/KAELOPS_PLATFORM/ROBYX_PLATFORM/g' "$NEW_ROOT/.env"
        sed -i '' 's/KAELOPS_WORKSPACE/ROBYX_WORKSPACE/g' "$NEW_ROOT/.env"
    else
        sed -i 's/KAELOPS_BOT_TOKEN/ROBYX_BOT_TOKEN/g' "$NEW_ROOT/.env"
        sed -i 's/KAELOPS_CHAT_ID/ROBYX_CHAT_ID/g' "$NEW_ROOT/.env"
        sed -i 's/KAELOPS_OWNER_ID/ROBYX_OWNER_ID/g' "$NEW_ROOT/.env"
        sed -i 's/KAELOPS_PLATFORM/ROBYX_PLATFORM/g' "$NEW_ROOT/.env"
        sed -i 's/KAELOPS_WORKSPACE/ROBYX_WORKSPACE/g' "$NEW_ROOT/.env"
    fi
    echo "  Copied and renamed env vars (KAELOPS_* → ROBYX_*)."
else
    echo "  No .env found in old installation — setup wizard will run on install."
fi

# ── Step 4: Clean old service files ──────────────────────────────────────

echo "[4/5] Cleaning up old service..."

if [ "$OS" = "Darwin" ]; then
    OLD_PLIST="$HOME/Library/LaunchAgents/com.kaelops.bot.plist"
    if [ -f "$OLD_PLIST" ]; then
        rm -f "$OLD_PLIST"
        echo "  Removed $OLD_PLIST"
    fi
elif [ "$OS" = "Linux" ]; then
    OLD_SERVICE="$HOME/.config/systemd/user/kaelops.service"
    if [ -f "$OLD_SERVICE" ]; then
        rm -f "$OLD_SERVICE"
        systemctl --user daemon-reload 2>/dev/null || true
        echo "  Removed $OLD_SERVICE"
    fi
fi

# ── Step 5: Run new installer ────────────────────────────────────────────

echo "[5/5] Installing Robyx..."
echo ""

if [ "$OS" = "Darwin" ]; then
    bash "$NEW_ROOT/install/install-mac.sh"
elif [ "$OS" = "Linux" ]; then
    bash "$NEW_ROOT/install/install-linux.sh"
else
    echo "  Windows: run install\\install-windows.ps1 manually."
fi

echo ""
echo "========================================"
echo "  Migration complete!"
echo "========================================"
echo ""
echo "What was migrated:"
echo "  - data/ directory (state, agents, tasks, memory)"
echo "  - .env (env vars renamed KAELOPS_* → ROBYX_*)"
echo ""
echo "On first boot, Robyx will automatically:"
echo "  - Rename the orchestrator agent (kael → robyx)"
echo "  - Migrate memory directories (.kaelops/ → .robyx/)"
echo "  - Reset AI sessions (new system prompts)"
echo ""
echo "Your old KaelOps installation at $OLD_ROOT is untouched."
echo "You can remove it when you're satisfied with the migration."
echo ""
