import time

from tgbot.maintenance import (
    cleanup_work_dir,
    dir_size,
    disk_usage,
    format_bytes,
)


def test_format_bytes():
    assert format_bytes(0) == "0 B"
    assert format_bytes(2048) == "2.0 KB"
    assert format_bytes(5 * 1024 * 1024) == "5.0 MB"


def test_disk_usage_has_sane_values(tmp_path):
    usage = disk_usage(tmp_path)
    assert usage.total > 0
    assert 0 <= usage.percent <= 100
    assert usage.as_dict()["percent"] == round(usage.percent, 1)


def test_dir_size_sums_files(tmp_path):
    (tmp_path / "a").write_bytes(b"x" * 100)
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "b").write_bytes(b"y" * 50)
    assert dir_size(tmp_path) == 150


def test_cleanup_removes_only_old_dirs(tmp_path):
    old = tmp_path / "old-job"
    fresh = tmp_path / "fresh-job"
    old.mkdir()
    fresh.mkdir()
    (old / "build.log").write_text("old", encoding="utf-8")
    (fresh / "build.log").write_text("fresh", encoding="utf-8")

    stale = time.time() - 7200
    import os

    os.utime(old, (stale, stale))

    removed, freed = cleanup_work_dir(tmp_path, max_age_seconds=3600)
    assert removed == 1
    assert freed == 3
    assert not old.exists()
    assert fresh.exists()


def test_cleanup_disabled_when_age_zero(tmp_path):
    (tmp_path / "job").mkdir()
    removed, freed = cleanup_work_dir(tmp_path, max_age_seconds=0)
    assert removed == 0
    assert freed == 0
    assert (tmp_path / "job").exists()
