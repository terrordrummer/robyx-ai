#!/bin/bash
# Robyx — Linux installer (systemd user service)
set -e
umask 077

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
SERVICE_NAME="robyx"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/$SERVICE_NAME.service"

echo "=== Robyx Linux Installer ==="
echo ""

# Pick the newest lock-supported Python (3.10 through 3.14).
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

version_le_3_14() {
    local version="$1"
    local major minor
    IFS='.' read -r major minor _ << EOF
$version
EOF
    [ "$major" -lt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -le 14 ]; }
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

for candidate in python python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
    version=$(get_python_version "$candidate") || continue
    if [ "$candidate" = "python" ]; then
        FOUND_PYTHON="$version"
    elif [ "$candidate" = "python3" ]; then
        FOUND_PYTHON3="$version"
    fi
    if ! version_ge_3_10 "$version" || ! version_le_3_14 "$version"; then
        continue
    fi
    if [ -z "$PYTHON_BIN" ] || version_gt "$version" "$PYTHON_VERSION"; then
        PYTHON_BIN="$candidate"
        PYTHON_VERSION="$version"
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    echo "Error: Neither 'python' nor 'python3' provides a lock-supported Python (3.10-3.14). Found python=$FOUND_PYTHON, python3=$FOUND_PYTHON3."
    exit 1
fi

echo "Python: $PYTHON_BIN ($PYTHON_VERSION)"

# Stop the live user service before clearing its interpreter. A fresh install
# or a host without a systemd user session skips this block safely.
if command -v systemctl >/dev/null 2>&1 && \
        systemctl --user is-active --quiet "$SERVICE_NAME"; then
    echo "Stopping existing service before dependency update..."
    if ! systemctl --user stop "$SERVICE_NAME"; then
        echo "Error: Could not stop the existing Robyx service; the venv was not modified. Run: systemctl --user stop '$SERVICE_NAME'"
        exit 1
    fi
    for _wait_attempt in $(seq 1 30); do
        if ! systemctl --user is-active --quiet "$SERVICE_NAME"; then
            break
        fi
        sleep 1
    done
    if systemctl --user is-active --quiet "$SERVICE_NAME"; then
        echo "Error: $SERVICE_NAME did not stop within 30 seconds; the existing venv was not modified. Stop it manually with: systemctl --user stop '$SERVICE_NAME'"
        exit 1
    fi
fi

# Create venv
echo "Creating virtual environment..."
"$PYTHON_BIN" -m venv --clear "$PROJECT_ROOT/.venv"
source "$PROJECT_ROOT/.venv/bin/activate"

# Install deps
echo "Installing dependencies..."
RUNTIME_LOCK="$("$PROJECT_ROOT/.venv/bin/python" \
    "$PROJECT_ROOT/bot/dependency_locks.py" \
    --project-root "$PROJECT_ROOT" --kind runtime)"
"$PROJECT_ROOT/.venv/bin/python" -m pip install -q --require-hashes \
    -r "$RUNTIME_LOCK"

# Run setup if no .env
if [ ! -f "$PROJECT_ROOT/.env" ]; then
    echo ""
    echo "No .env found — running setup wizard..."
    "$PYTHON_BIN" "$PROJECT_ROOT/setup.py"
fi

# Create and repair private runtime paths. This is intentionally run on every
# install/upgrade, including installations whose migration tracker is current.
install -d -m 700 "$PROJECT_ROOT/data/system-monitor"
"$PROJECT_ROOT/.venv/bin/python" "$PROJECT_ROOT/bot/local_security.py" \
    --project-root "$PROJECT_ROOT"

# Check if systemd user is available
if ! command -v systemctl &>/dev/null; then
    echo "systemd not found. You can start manually:"
    echo "  $PROJECT_ROOT/.venv/bin/python $PROJECT_ROOT/bot/bot.py"
    exit 0
fi

# Create systemd unit
mkdir -p "$SERVICE_DIR"
cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Robyx AI Agent Orchestrator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
UMask=0077
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
