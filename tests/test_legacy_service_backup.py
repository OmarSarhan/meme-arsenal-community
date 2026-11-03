"""Exercise the legacy shell helper using tiny fixtures and no Docker daemon."""
from pathlib import Path
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/service_backup_postgres.sh"


def run_backup(tmp_path, *, available=1073741824 + 10, enabled=True, present=True, fault=""):
    compose = tmp_path / "compose"
    compose.mkdir()
    (compose / "compose.yml").write_text("services: {}\n")
    if present:
        (compose / "reserve").mkdir()
    env_file = compose / "fixture.env"
    env_file.write_text("RESERVE_HOST_PATH=reserve\n")
    destination = tmp_path / "backups"
    stubs = tmp_path / "bin"
    stubs.mkdir()
    log = tmp_path / "calls"
    # Every potentially expensive reader is replaced; never use operator config.
    for name, body in {
        "du": 'printf "10\\t%s\\n" "$2"',
        "df": 'printf "Avail\\n%s\\n" "$AVAILABLE"',
        "docker": '''case "$*" in
          *pg_dump*) printf fixture-dump ;;
          *pg_restore*) cat >/dev/null ;;
          *) exit 98 ;;
        esac''',
        "tar": '''case "$*" in
          *-czf*) printf fixture-archive > "$4" ;;
          *-tzf*) test -s "$2" ;;
          *) exit 98 ;;
        esac''',
    }.items():
        stub = stubs / name
        stub.write_text('#!/bin/bash\nset -eu\n'
                        f'echo "{name} $*" >> "$CALL_LOG"\n'
                        f'if [[ "$FAULT" == "{name}" || "$*" == *"$FAULT"* && -n "$FAULT" ]]; then exit 42; fi\n'
                        + body + '\n')
        stub.chmod(0o700)
    env = {"PATH": f"{stubs}:/usr/bin:/bin", "CALL_LOG": str(log),
           "FAULT": fault, "AVAILABLE": str(available), "BACKUP_DIR": str(destination),
           "COMPOSE_FILE": str(compose / "compose.yml"), "BACKUP_RESERVE": str(int(enabled))}
    result = subprocess.run(["bash", str(SCRIPT), str(env_file)], env=env,
                            capture_output=True, text=True, timeout=10)
    assert "WARNING: legacy/incomplete backup" in result.stderr
    return result, log.read_text() if log.exists() else "", destination


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_capacity_boundary_before_dump(tmp_path, delta):
    result, calls, destination = run_backup(tmp_path, available=1073741824 + 10 + delta)
    assert f"du -sb {tmp_path}/compose/reserve" in calls
    if delta < 0:
        assert result.returncode != 0
        assert "Insufficient backup capacity" in result.stderr
        assert "docker" not in calls and "tar" not in calls
        assert list(destination.iterdir()) == []
    else:
        assert result.returncode == 0, result.stderr
        assert calls.index("df ") < calls.index("docker ") < calls.index("tar ")
        files = list(destination.iterdir())
        assert len(files) == 4
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in files)


@pytest.mark.parametrize("enabled,present", [(False, True), (True, False)])
def test_database_only_skips_capacity_and_archive(tmp_path, enabled, present):
    result, calls, destination = run_backup(tmp_path, available=0, enabled=enabled, present=present)
    assert result.returncode == 0, result.stderr
    assert "du " not in calls and "df " not in calls and "tar " not in calls
    assert "Skipping reserve backup" in result.stderr
    assert len(list(destination.iterdir())) == 2


@pytest.mark.parametrize("fault", ["du", "df", "pg_dump", "pg_restore", "tar"])
def test_reader_errors_abort_and_remove_partial_files(tmp_path, fault):
    result, calls, destination = run_backup(tmp_path, fault=fault)
    assert result.returncode != 0
    assert not list(destination.glob("*.tmp"))
    assert not list(destination.glob("*.tar.gz*"))
    if fault != "tar":
        assert not list(destination.iterdir())
    if fault in {"du", "df"}:
        assert "docker" not in calls
