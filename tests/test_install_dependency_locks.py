from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "relative_path",
    (
        "install/install-mac.sh",
        "install/install-linux.sh",
        "install/install-windows.ps1",
    ),
)
def test_installers_resolve_hash_verified_runtime_lock(relative_path):
    script = (ROOT / relative_path).read_text()

    assert "dependency_locks.py" in script
    assert "--kind runtime" in script
    assert "--require-hashes" in script
    assert "bot/requirements.txt" not in script


@pytest.mark.parametrize(
    "relative_path",
    (
        "install/install-mac.sh",
        "install/install-linux.sh",
    ),
)
def test_posix_installers_accept_only_lock_supported_minors(relative_path):
    script = (ROOT / relative_path).read_text()

    assert "python3.10" in script
    assert "python3.11" in script
    assert "python3.12" in script
    assert "python3.13" in script
    assert "python3.14" in script
    assert "version_ge_3_10" in script
    assert "version_le_3_14" in script


def test_windows_installer_accepts_only_lock_supported_minors():
    script = (ROOT / "install" / "install-windows.ps1").read_text()

    assert "($_.Minor -ge 10)" in script
    assert "($_.Minor -le 14)" in script


def test_macos_stops_and_waits_before_clearing_venv():
    script = (ROOT / "install" / "install-mac.sh").read_text()

    assert script.index('launchctl unload "$PLIST_PATH"') < script.index("-m venv --clear")
    assert "Could not stop the existing Robyx service" in script
    assert "did not stop within 30 seconds" in script
    assert script.index("did not stop within 30 seconds") < script.index("-m venv --clear")


def test_linux_stops_and_waits_before_clearing_venv():
    script = (ROOT / "install" / "install-linux.sh").read_text()

    assert script.index('systemctl --user stop "$SERVICE_NAME"') < script.index(
        "-m venv --clear"
    )
    assert "Could not stop the existing Robyx service" in script
    assert "did not stop within 30 seconds" in script
    assert script.index("did not stop within 30 seconds") < script.index("-m venv --clear")


def test_windows_stops_polls_and_unregisters_before_clearing_venv():
    script = (ROOT / "install" / "install-windows.ps1").read_text()
    stop = script.index("Stop-ScheduledTask")
    poll = script.index("$stopDeadline")
    unregister = script.index("Unregister-ScheduledTask")
    clear = script.index("-m venv --clear")

    assert stop < poll < unregister < clear
    assert "Stop it manually" in script
    assert "did not stop within 30 seconds" in script
