from __future__ import annotations

import dataclasses
import sqlite3
import subprocess
import time
from collections.abc import Generator
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.smoke


@dataclasses.dataclass(frozen=True)
class SmokeEnv:
    url: str
    container_id: str
    config_dir: Path
    data_dir: Path


def _docker_available() -> bool:
    try:
        subprocess.run(
            ["docker", "info"],
            capture_output=True,
            check=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _build_image(root: Path) -> str:
    tag = "organizer:smoke"
    subprocess.run(
        ["docker", "build", "-t", tag, "."],
        cwd=root,
        capture_output=True,
        check=True,
    )
    return tag


def _start_container(
    tag: str,
    config_dir: Path,
    data_dir: Path,
) -> tuple[str, int]:
    proc = subprocess.run(
        [
            "docker", "run", "-d", "--rm",
            "-p", "0:8000",
            "-v", f"{config_dir}:/config",
            "-v", f"{data_dir}:/data",
            tag,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    container_id = proc.stdout.strip()

    port_proc = subprocess.run(
        ["docker", "port", container_id, "8000"],
        capture_output=True, text=True, check=True,
    )
    host_port = int(port_proc.stdout.strip().split(":")[-1])
    return container_id, host_port


def _stop_container(container_id: str) -> None:
    subprocess.run(
        ["docker", "stop", container_id],
        capture_output=True,
        timeout=60,
    )


def _wait_for_http(url: str, *, timeout: float = 60.0, interval: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=interval + 0.5)
            if r.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(interval)
    return False


def test_needs_docker() -> None:
    if not _docker_available():
        pytest.skip("Docker is not available")


@pytest.fixture(scope="module")
def smoke_env(tmp_path_factory: pytest.TempPathFactory) -> Generator[SmokeEnv, None, None]:
    root = Path(__file__).resolve().parent.parent
    tag = _build_image(root)

    config_dir = tmp_path_factory.mktemp("config")
    data_dir = tmp_path_factory.mktemp("data")

    (config_dir / "organizer.yaml").write_text("""\
data_roots:
  - /data
quarantine_root: /data/.quarantine
scan_interval: 1
log_level: INFO
retention_days: 14
watches:
  - id: data
    root: /data
    rules: /config/rules.yaml
""")
    (config_dir / "rules.yaml").write_text("""\
rules:
  - name: move
    match:
      field: file_name
      pattern: '.*'
    actions:
      - move:
          destination: /data/sorted
""")
    (data_dir / "sorted").mkdir()

    container_id, host_port = _start_container(tag, config_dir, data_dir)
    url = f"http://127.0.0.1:{host_port}"

    if not _wait_for_http(f"{url}/", timeout=30):
        _stop_container(container_id)
        pytest.fail("Container failed to become ready within 30 seconds")

    yield SmokeEnv(url=url, container_id=container_id, config_dir=config_dir, data_dir=data_dir)

    _stop_container(container_id)


class TestDockerSmoke:
    @pytest.fixture(autouse=True)
    def _check_docker(self) -> None:
        if not _docker_available():
            pytest.skip("Docker is not available")

    def test_web_ui_reachable(self, smoke_env: SmokeEnv) -> None:
        response = httpx.get(f"{smoke_env.url}/", timeout=5)
        assert response.status_code == 200

    def test_dashboard_shows_watch(self, smoke_env: SmokeEnv) -> None:
        response = httpx.get(f"{smoke_env.url}/", timeout=5)
        assert "data" in response.text

    def test_health_endpoint_healthy(self, smoke_env: SmokeEnv) -> None:
        response = httpx.get(f"{smoke_env.url}/health", timeout=5)
        assert response.status_code == 200
        body = response.json()
        assert body.get("all_healthy") is True
        assert len(body.get("watch_folder_healths", [])) == 1
        assert body["watch_folder_healths"][0]["watch_id"] == "data"
        assert body["watch_folder_healths"][0]["accessible"] is True
        assert body["persistence_health"]["tracking_db_writable"] is True

    def test_watch_folder_processes_item_and_records_attempt(
        self, smoke_env: SmokeEnv
    ) -> None:
        data_dir = smoke_env.data_dir
        config_dir = smoke_env.config_dir

        item_file = data_dir / "test.txt"
        item_file.write_text("hello world")

        deadline = time.monotonic() + 30
        sorted_file = data_dir / "sorted" / "test.txt"
        while time.monotonic() < deadline:
            if sorted_file.exists():
                break
            time.sleep(0.5)
        assert sorted_file.exists(), "Item was not moved to sorted directory within 30 seconds"
        assert sorted_file.read_text() == "hello world"

        db_path = config_dir / "organizer.db"
        assert db_path.exists(), "Tracking DB was not created"

        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT status, source_path, rule_name FROM processing_attempts WHERE watch_id = ? ORDER BY started_at DESC",
                ("data",),
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) >= 1, "No attempts found in tracking DB"
        completed = [r for r in rows if r[0] == "completed"]
        assert len(completed) >= 1, f"No completed attempts found. Attempts: {rows}"

    def test_structured_logs_persist(self, smoke_env: SmokeEnv) -> None:
        config_dir = smoke_env.config_dir
        log_path = config_dir / "logs" / "organizer.log"
        assert log_path.exists(), f"Log file not found at {log_path}"
        log_content = log_path.read_text()
        assert "move" in log_content, "Expected 'move' action in structured logs"
