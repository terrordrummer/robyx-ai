import os
import stat

import pytest

import local_security
from local_security import (
    HardeningReport,
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    PermissionHardeningError,
    establish_private_umask,
    emit_hardening_report,
    ensure_private_directory,
    harden_runtime_permissions,
    require_hardening_success,
)


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX mode assertions")


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_existing_install_is_hardened_recursively_and_idempotently(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("ROBYX_BOT_TOKEN=secret\n")
    env_file.chmod(0o644)
    log_file = tmp_path / "bot.log.1"
    log_file.write_text("runtime log\n")
    log_file.chmod(0o664)
    nested = tmp_path / "data" / "continuous" / "task"
    nested.mkdir(parents=True)
    state_file = nested / "state.json"
    state_file.write_text("{}")
    for directory in (tmp_path / "data", nested.parent, nested):
        directory.chmod(0o755)
    state_file.chmod(0o644)

    first = harden_runtime_permissions(tmp_path)

    assert first.ok
    assert _mode(env_file) == PRIVATE_FILE_MODE
    assert _mode(log_file) == PRIVATE_FILE_MODE
    assert _mode(tmp_path / "data") == PRIVATE_DIRECTORY_MODE
    assert _mode(nested.parent) == PRIVATE_DIRECTORY_MODE
    assert _mode(nested) == PRIVATE_DIRECTORY_MODE
    assert _mode(state_file) == PRIVATE_FILE_MODE

    second = harden_runtime_permissions(tmp_path)
    assert second.ok
    assert second.changed_files == 0
    assert second.changed_directories == 0


def test_hardener_never_follows_runtime_symlinks(tmp_path):
    external = tmp_path / "external"
    external.write_text("do not chmod")
    external.chmod(0o644)
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "linked-secret").symlink_to(external)

    report = harden_runtime_permissions(tmp_path)

    assert _mode(external) == 0o644
    assert not report.ok
    assert any("symlink" in message for message in report.errors)


def test_symlinked_env_is_reported_as_error_without_following(tmp_path):
    external = tmp_path / "external-env"
    external.write_text("ROBYX_BOT_TOKEN=secret\n")
    external.chmod(0o644)
    env_link = tmp_path / ".env"
    env_link.unlink()
    env_link.symlink_to(external)

    report = harden_runtime_permissions(tmp_path)

    assert not report.ok
    assert _mode(external) == 0o644
    assert any("symlink" in message for message in report.errors)


def test_boot_gate_fails_closed_without_embedding_sensitive_paths():
    report = HardeningReport(errors=["/private/location/.env: permission denied"])

    with pytest.raises(PermissionHardeningError) as caught:
        require_hardening_success(report)

    assert "/private/location" not in str(caught.value)


def test_boot_gate_accepts_windows_noop_report():
    require_hardening_success(HardeningReport())


def test_ensure_private_directory_creates_private_and_rejects_symlink(tmp_path):
    runtime_dir = tmp_path / "nested" / "runtime"
    ensure_private_directory(runtime_dir)
    assert _mode(runtime_dir) == PRIVATE_DIRECTORY_MODE

    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError, match="must not be a symlink"):
        ensure_private_directory(linked)


def test_report_emission_redacts_contents_and_labels_severity():
    messages = []
    report = HardeningReport(
        skipped=["refused nested link"],
        errors=["could not chmod .env"],
    )
    emit_hardening_report(report, emit=messages.append)
    assert messages == [
        "[robyx security] warning: refused nested link",
        "[robyx security] error: could not chmod .env",
    ]


def test_installer_cli_status_reflects_hardening_result(tmp_path, monkeypatch):
    assert local_security._main(["--project-root", str(tmp_path)]) == 0

    monkeypatch.setattr(
        local_security,
        "harden_runtime_permissions",
        lambda _root: HardeningReport(errors=["permission denied"]),
    )
    assert local_security._main(["--project-root", str(tmp_path)]) == 1


def test_missing_runtime_path_is_a_hardening_error(tmp_path):
    report = HardeningReport()
    local_security._harden_one(tmp_path / "missing", report, strict=True)
    assert not report.ok
    assert "could not inspect" in report.errors[0]


def test_windows_compatibility_path_is_a_noop(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("secret")
    env_file.chmod(0o644)

    report = harden_runtime_permissions(tmp_path, platform_name="nt")

    assert report.ok
    assert report.changed_files == 0
    assert _mode(env_file) == 0o644
    assert establish_private_umask(platform_name="nt") is None


def test_private_umask_protects_new_atomic_and_plain_runtime_files(tmp_path):
    previous = os.umask(0o022)
    try:
        returned = establish_private_umask()
        assert returned == 0o022
        directory = tmp_path / "runtime"
        directory.mkdir()
        runtime_file = directory / "queue.tmp"
        runtime_file.write_text("[]")
        assert _mode(directory) == PRIVATE_DIRECTORY_MODE
        assert _mode(runtime_file) == PRIVATE_FILE_MODE
    finally:
        os.umask(previous)
