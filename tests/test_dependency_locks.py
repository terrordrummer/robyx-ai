from pathlib import Path

import pytest

from dependency_locks import (
    DependencyLockError,
    SUPPORTED_PYTHON_MINORS,
    dependency_fingerprint,
    dependency_lock_path,
)
import dependency_locks


@pytest.mark.parametrize("minor", SUPPORTED_PYTHON_MINORS)
@pytest.mark.parametrize("kind", ("runtime", "dev"))
def test_every_supported_minor_resolves_a_committed_lock(minor, kind):
    root = Path(__file__).resolve().parents[1]

    result = dependency_lock_path(root, kind=kind, version_info=minor)

    assert result == root / "requirements" / "locks" / (
        "%s-py%d%d.txt" % (kind, minor[0], minor[1])
    )
    assert result.is_file()


def test_unsupported_minor_fails_without_requirements_fallback(tmp_path):
    with pytest.raises(DependencyLockError, match="has no Robyx dependency lock"):
        dependency_lock_path(tmp_path, version_info=(3, 15))


def test_missing_supported_lock_fails_closed(tmp_path):
    with pytest.raises(DependencyLockError, match="required dependency lock is missing"):
        dependency_lock_path(tmp_path, version_info=(3, 12))


def test_unknown_lock_kind_is_rejected(tmp_path):
    with pytest.raises(DependencyLockError, match="unknown dependency lock kind"):
        dependency_lock_path(tmp_path, kind="production", version_info=(3, 12))


def test_fingerprint_changes_for_input_or_lock(tmp_path):
    requirements = tmp_path / "requirements.txt"
    lock = tmp_path / "runtime-lock.txt"
    requirements.write_text("direct==1\n")
    lock.write_text("direct==1 --hash=sha256:one\n")
    initial = dependency_fingerprint(requirements, lock)

    requirements.write_text("direct>=1\n")
    after_input = dependency_fingerprint(requirements, lock)
    lock.write_text("direct==2 --hash=sha256:two\n")
    after_lock = dependency_fingerprint(requirements, lock)

    assert len(initial) == 64
    assert len({initial, after_input, after_lock}) == 3


def test_cli_prints_selected_lock(capsys):
    root = Path(__file__).resolve().parents[1]

    assert dependency_locks._main([
        "--project-root",
        str(root),
        "--kind",
        "runtime",
    ]) == 0

    assert capsys.readouterr().out.strip() == str(dependency_lock_path(root))


def test_cli_exits_when_lock_is_missing(tmp_path, capsys):
    with pytest.raises(SystemExit) as caught:
        dependency_locks._main(["--project-root", str(tmp_path)])

    assert caught.value.code == 2
    assert "dependency lock error" in capsys.readouterr().err
