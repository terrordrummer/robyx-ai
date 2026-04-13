#!/bin/bash
# Robyx — Linux installer (systemd user service)
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
SERVICE_NAME="robyx"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/$SERVICE_NAME.service"

echo "=== Robyx Linux Installer ==="
echo ""

# Pick the newest available Python >= 3.10 from python/python3
get_python_version() {
    local cmd="$1"
    if ! command -v "$cmd" >/dev/null 2>&1; then
        return 1
    fi
    "$cmd" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null
}

version_ge_3_10() {
    local version="$1"
    local major minor
    IFS='.' read -r major minor _ << EOF
$version
EOF
    [ "$major" -gt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -ge 10 ]; }
}

version_gt() {
    local left="$1"
    local right="$2"
    local l_major l_minor l_micro r_major r_minor r_micro
    IFS='.' read -r l_major l_minor l_micro << EOF
$left
EOF
    IFS='.' read -r r_major r_minor r_micro << EOF
$right
EOF
    if [ "$l_major" -ne "$r_major" ]; then
        [ "$l_major" -gt "$r_major" ]
        return
    fi
    if [ "$l_minor" -ne "$r_minor" ]; then
        [ "$l_minor" -gt "$r_minor" ]
        return
    fi
    [ "$l_micro" -gt "$r_micro" ]
}

PYTHON_BIN=""
PYTHON_VERSION=""
FOUND_PYTHON="not found"
FOUND_PYTHON3="not found"

for candidate in python python3 python3.13 python3.12 python3.11 python3.10; do
    version=$(get_python_version "$candidate") || continue
    if [ "$candidate" = "python" ]; then
        FOUND_PYTHON="$version"
    elif [ "$candidate" = "python3" ]; then
        FOUND_PYTHON3="$version"
    fi
    if ! version_ge_3_10 "$version"; then
        continue
    fi
    if [ -z "$PYTHON_BIN" ] || version_gt "$version" "$PYTHON_VERSION"; then
        PYTHON_BIN="$candidate"
        PYTHON_VERSION="$version"
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    echo "Error: Neither 'python' nor 'python3' provides Python 3.10+. Found python=$FOUND_PYTHON, python3=$FOUND_PYTHON3."
    exit 1
fi

echo "Python: $PYTHON_BIN ($PYTHON_VERSION)"

# Create venv
echo "Creating virtual environment..."
"$PYTHON_BIN" -m venv --clear "$PROJECT_ROOT/.venv"
source "$PROJECT_ROOT/.venv/bin/activate"

# Install deps
echo "Installing dependencies..."
pip install -q -r "$PROJECT_ROOT/bot/requirements.txt"

# Run setup if no .env
if [ ! -f "$PROJECT_ROOT/.env" ]; then
    echo ""
    echo "No .env found — running setup wizard..."
    "$PYTHON_BIN" "$PROJECT_ROOT/setup.py"
fi

# Create data dirs
mkdir -p "$PROJECT_ROOT/data/system-monitor"

# Check if systemd user is available
if ! command -v systemctl &>/dev/null; then
    echo "systemd not found. You can start manually:"
    echo "  $PROJECT_ROOT/.venv/bin/python $PROJECT_ROOT/bot/bot.py"
    exit 0
fi

# Stop existing service
systemctl --user stop "$SERVICE_NAME" 2>/dev/null || true

# Create systemd unit
mkdir -p "$SERVICE_DIR"
cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Robyx AI Agent Orchestrator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_ROOT
EnvironmentFile=$PROJECT_ROOT/.env
ExecStart=$PROJECT_ROOT/.venv/bin/python $PROJECT_ROOT/bot/bot.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF

# Enable and start
systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE_NAME"

echo ""
echo "=== Robyx installed ==="
echo ""
echo "Service: $SERVICE_NAME"
echo "Status:  systemctl --user status $SERVICE_NAME"
echo "Stop:    systemctl --user stop $SERVICE_NAME"
echo "Start:   systemctl --user start $SERVICE_NAME"
echo "Logs:    journalctl --user -u $SERVICE_NAME -f"
echo ""
