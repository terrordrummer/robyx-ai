#!/bin/bash
# Robyx — macOS installer (launchd)
set -e
umask 077

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PLIST_NAME="com.robyx.bot"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"

echo "=== Robyx macOS Installer ==="
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

# Stop the live service before clearing its interpreter. Mutating an in-use
# venv can leave a half-updated process importing a mixed dependency set.
if launchctl list "$PLIST_NAME" >/dev/null 2>&1; then
    echo "Stopping existing service before dependency update..."
    if ! launchctl unload "$PLIST_PATH"; then
        echo "Error: Could not stop the existing Robyx service; the venv was not modified. Run: launchctl unload '$PLIST_PATH'"
        exit 1
    fi
    for _wait_attempt in $(seq 1 30); do
        if ! launchctl list "$PLIST_NAME" >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done
    if launchctl list "$PLIST_NAME" >/dev/null 2>&1; then
        echo "Error: $PLIST_NAME did not stop within 30 seconds; the existing venv was not modified. Stop it manually with: launchctl unload '$PLIST_PATH'"
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

# Make remote read-only: disable push to prevent accidental writes from the install directory
if git -C "$PROJECT_ROOT" remote get-url origin >/dev/null 2>&1; then
    git -C "$PROJECT_ROOT" remote set-url --push origin no_push
    echo "Remote push disabled (repo is read-only for this install)."
fi

# Create and repair private runtime paths. This is intentionally run on every
# install/upgrade, including installations whose migration tracker is current.
install -d -m 700 "$PROJECT_ROOT/data"
"$PROJECT_ROOT/.venv/bin/python" "$PROJECT_ROOT/bot/local_security.py" \
    --project-root "$PROJECT_ROOT"

# Generate plist
echo "Creating launchd service..."
cat > "$PLIST_PATH" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_NAME</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PROJECT_ROOT/.venv/bin/python</string>
        <string>$PROJECT_ROOT/bot/bot.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_ROOT</string>
    <key>Umask</key>
    <integer>63</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$PROJECT_ROOT/data/service-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$PROJECT_ROOT/data/service-stderr.log</string>
</dict>
</plist>
EOF

# Load service
launchctl load "$PLIST_PATH"
echo ""
echo "=== Robyx installed ==="
echo ""
echo "Service: $PLIST_NAME"
echo "Status:  launchctl list | grep robyx"
echo "Stop:    launchctl stop $PLIST_NAME"
echo "Start:   launchctl start $PLIST_NAME"
echo "Logs:    tail -f $PROJECT_ROOT/bot.log"
echo ""
