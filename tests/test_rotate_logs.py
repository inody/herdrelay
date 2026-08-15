import gzip
import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "rotate_logs.py"
SPEC = importlib.util.spec_from_file_location("rotate_logs", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
rotate = MODULE.rotate


def test_rotate_compresses_and_truncates_large_log(tmp_path):
    log = tmp_path / "launchd.err.log"
    log.write_text("first log\n")

    assert rotate(log, max_bytes=5, backups=2) is True
    assert log.read_bytes() == b""
    with gzip.open(tmp_path / "launchd.err.log.1.gz", "rt") as backup:
        assert backup.read() == "first log\n"


def test_rotate_retains_configured_number_of_backups(tmp_path):
    log = tmp_path / "launchd.err.log"
    for value in ("one", "two", "three"):
        log.write_text(value)
        rotate(log, max_bytes=1, backups=2)

    assert not (tmp_path / "launchd.err.log.3.gz").exists()
    with gzip.open(tmp_path / "launchd.err.log.1.gz", "rt") as newest:
        assert newest.read() == "three"
    with gzip.open(tmp_path / "launchd.err.log.2.gz", "rt") as older:
        assert older.read() == "two"


def test_rotate_leaves_small_log_untouched(tmp_path):
    log = tmp_path / "launchd.err.log"
    log.write_text("small")

    assert rotate(log, max_bytes=100, backups=2) is False
    assert log.read_text() == "small"
