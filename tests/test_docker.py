"""Regression tests for the Docker runtime contract."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_module_dependency_directories_are_persistent():
    dockerfile = (ROOT / "Dockerfile").read_text()
    entrypoint = (ROOT / "docker-entrypoint.sh").read_text()

    assert "PIP_TARGET=/data/python" in dockerfile
    assert "PYTHONPATH=/data/python" in dockerfile
    assert 'mkdir -p "$app_dir" /data/python "${TMPDIR:-/data/tmp}"' in entrypoint


def test_tmp_allows_native_dependency_builds():
    compose = (ROOT / "docker-compose.yml").read_text()
    tmp_mount = next(
        line.strip() for line in compose.splitlines() if line.strip().startswith("- /tmp:")
    )

    assert "exec" in tmp_mount.split(":", 1)[1].split(",")
    assert "noexec" not in tmp_mount
