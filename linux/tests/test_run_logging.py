"""Exercise startup and rollover without a GTK session or a two-day wait."""

import logging
import logging.handlers
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from doubao_murmur import run_logging


def _launch(state_dir: Path, marker: str) -> subprocess.CompletedProcess:
    # Use the real application entry point, with GTK replaced by an app stub.
    return subprocess.run(
        [sys.executable, "-c", """
import logging
import sys
from types import ModuleType, SimpleNamespace

app = ModuleType("doubao_murmur.app")
def run(argv):
    logging.getLogger("doubao_murmur.app").info("Launch marker: %s", argv[1])
    return 0
app.DoubaoMurmurApp = lambda: SimpleNamespace(run=run)
sys.modules[app.__name__] = app
from doubao_murmur.__main__ import main
main()
""", marker],
        env={
            **os.environ,
            "XDG_STATE_HOME": str(state_dir),
            "PYTHONPATH": str(Path(run_logging.__file__).resolve().parent.parent),
        },
        text=True, capture_output=True, check=True,
    )


def test_each_start_gets_its_own_private_log(tmp_path):
    _launch(tmp_path, "first")
    _launch(tmp_path, "second")
    logs = sorted((tmp_path / "doubao-murmur" / "logs").glob("run-*.log"))
    assert len(logs) == 2
    for path, expected, unexpected in zip(logs, ("first", "second"), ("second", "first")):
        content = path.read_text(encoding="utf-8")
        assert "Application starting" in content
        assert f"Launch marker: {expected}" in content
        assert f"Launch marker: {unexpected}" not in content
        assert "Application exited (status=0)" in content
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_unavailable_log_directory_does_not_prevent_launch(tmp_path):
    blocked = tmp_path / "not-a-directory"
    blocked.touch()
    result = _launch(blocked, "still runs")
    assert "Could not create run log" in result.stderr
    assert "Launch marker: still runs" in result.stderr


def test_active_run_rotates_after_two_days(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    caplog.set_level(logging.INFO)
    logger = logging.getLogger("doubao_murmur")
    existing_handlers = set(logger.handlers)
    run_logging.configure_logging()
    handler, = set(logger.handlers) - existing_handlers
    try:
        current = Path(handler.baseFilename)
        started = int(current.stat().st_mtime)
        real_time = logging.handlers.time
        clock = SimpleNamespace(
            time=lambda: started + 2 * 86400 - 1,
            gmtime=real_time.gmtime,
            strftime=real_time.strftime,
        )
        monkeypatch.setattr(logging.handlers, "time", clock)
        logger.info("before two days")
        assert "before two days" in current.read_text(encoding="utf-8")
        clock.time = lambda: started + 2 * 86400
        logger.info("after two days")

        archive, = current.parent.glob(f"{current.name}.*")
        assert "before two days" in archive.read_text(encoding="utf-8")
        content = current.read_text(encoding="utf-8")
        assert "after two days" in content
        assert "before two days" not in content
        assert stat.S_IMODE(current.stat().st_mode) == 0o600
    finally:
        logger.removeHandler(handler)
        handler.close()
