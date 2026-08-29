"""Tests for run_now.sh's run-time Python interpreter resolution.

run_now.sh used to hard-code an absolute pyenv path, and baked that same path
into the cron entry it installed — so a Python upgrade, or anyone cloning the
repo, got a cron job that silently never ran. These tests pin the replacement
behaviour: resolve at run time, prefer a venv, fall back to PATH, and fail
loudly rather than quietly when nothing is usable.

Nothing here touches the real crontab. The script is exercised through two
read-only diagnostic flags, `--print-python` and `--print-cron-entry`.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_NOW = REPO_ROOT / "run_now.sh"


def _clean_env(**overrides):
    """A copy of os.environ with the interpreter-selecting vars removed."""
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    env.pop("MOVIE_MONITOR_PYTHON", None)
    env.update(overrides)
    return env


def _run(args, script=RUN_NOW, env=None):
    return subprocess.run(
        ["/bin/bash", str(script), *args],
        capture_output=True,
        text=True,
        env=env if env is not None else _clean_env(),
    )


def _fake_python(path: Path) -> Path:
    """Create an executable stub standing in for a python3 binary."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def script_copy(tmp_path):
    """run_now.sh copied into a temp dir, so SCRIPT_DIR points there.

    Lets the repo-local-venv branch be tested without creating a venv inside
    the actual working tree.
    """
    dest = tmp_path / "run_now.sh"
    shutil.copy2(RUN_NOW, dest)
    return dest


# ── The four resolution branches, in priority order ──────────────────────────


def test_explicit_override_wins(tmp_path):
    """MOVIE_MONITOR_PYTHON beats everything else."""
    stub = _fake_python(tmp_path / "chosen" / "python3")

    result = _run(
        ["--print-python"],
        env=_clean_env(MOVIE_MONITOR_PYTHON=str(stub)),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(stub)


def test_activated_virtualenv_is_preferred_over_path(tmp_path):
    """An activated venv ($VIRTUAL_ENV) wins over python3 on PATH."""
    stub = _fake_python(tmp_path / "venv" / "bin" / "python")

    result = _run(
        ["--print-python"],
        env=_clean_env(VIRTUAL_ENV=str(tmp_path / "venv")),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(stub)


def test_repo_local_venv_used_when_nothing_is_activated(script_copy, tmp_path):
    """./venv/bin/python next to the script is picked up with no env vars set."""
    stub = _fake_python(tmp_path / "venv" / "bin" / "python")

    result = _run(["--print-python"], script=script_copy)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(stub)


def test_dot_venv_is_also_recognised(script_copy, tmp_path):
    """The .venv spelling works too."""
    stub = _fake_python(tmp_path / ".venv" / "bin" / "python")

    result = _run(["--print-python"], script=script_copy)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(stub)


def test_falls_back_to_python3_on_path(script_copy, tmp_path):
    """With no override and no venv, python3 from PATH is used."""
    bin_dir = tmp_path / "bin"
    stub = _fake_python(bin_dir / "python3")

    result = _run(
        ["--print-python"],
        script=script_copy,
        env=_clean_env(PATH=f"{bin_dir}:/usr/bin:/bin"),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(stub)


# ── Failure behaviour: loud, not silent ──────────────────────────────────────


def test_invalid_override_fails_loudly(tmp_path):
    """A MOVIE_MONITOR_PYTHON that is not executable is an error, not a fallback."""
    missing = tmp_path / "does-not-exist" / "python3"

    result = _run(
        ["--print-python"],
        env=_clean_env(MOVIE_MONITOR_PYTHON=str(missing)),
    )

    assert result.returncode != 0
    assert result.stdout.strip() == ""
    assert "MOVIE_MONITOR_PYTHON" in result.stderr
    assert str(missing) in result.stderr


def test_no_interpreter_anywhere_fails_loudly(script_copy, tmp_path):
    """No override, no venv, nothing on PATH → non-zero exit and a real message.

    The message must survive a broken PATH, since that is one of the ways to
    end up here — so it cannot be produced by an external command.
    """
    empty_path = tmp_path / "empty-bin"
    empty_path.mkdir()

    result = _run(
        ["--print-python"],
        script=script_copy,
        env=_clean_env(PATH=str(empty_path)),
    )

    assert result.returncode != 0
    assert result.stdout.strip() == ""
    assert "no usable Python interpreter found" in result.stderr
    # It should tell the reader how to fix it, not just that it failed.
    assert "MOVIE_MONITOR_PYTHON" in result.stderr
    assert "python3 -m venv" in result.stderr


# ── The cron entry must not embed an interpreter path ────────────────────────


def test_cron_entry_resolves_at_run_time(tmp_path):
    """The installed cron line invokes run_now.sh, not a Python binary."""
    result = _run(["--print-cron-entry"])

    assert result.returncode == 0, result.stderr
    entry = result.stdout.strip()

    assert entry.startswith("*/30 * * * *")
    assert "run_now.sh" in entry
    assert "--cron-run" in entry
    # The regression this whole change exists to prevent.
    assert "pyenv" not in entry
    assert "/bin/python" not in entry
    # stderr must reach the log, or resolution failures are invisible.
    assert "2>/dev/null" not in entry
    assert "2>&1" in entry


def test_script_contains_no_hardcoded_interpreter_path():
    """No absolute interpreter path in the executable part of the script.

    Comments are stripped first: the script deliberately documents the old
    hard-coded path as an explanation of what is being avoided.
    """
    code = "\n".join(
        line
        for line in RUN_NOW.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )

    assert ".pyenv" not in code
    assert "/Users/" not in code
    # The only assignment to PYTHON must come from resolution, never a literal.
    assert 'PYTHON="/' not in code
