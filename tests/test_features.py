import pytest

from tgbot.jobs import format_eta, parse_build_progress
from tgbot.models import Job, JobStatus
from tgbot.security import ValidationError, parse_source, validate_port


def test_parse_source_release_zip():
    source = parse_source(
        "https://github.com/owner/app/releases/download/v1.2.3/app.zip"
    )
    assert source.is_archive
    assert source.tag == "v1.2.3"
    assert source.asset == "app.zip"
    assert source.full_name == "owner/app"
    assert source.url.endswith("/releases/download/v1.2.3/app.zip")


def test_parse_source_release_tar_gz():
    source = parse_source("https://github.com/owner/app/releases/download/v2/app.tar.gz")
    assert source.is_archive
    assert source.asset == "app.tar.gz"


def test_parse_source_git_still_works():
    source = parse_source("owner/app")
    assert not source.is_archive
    assert source.kind == "git"


def test_parse_source_rejects_non_archive_asset():
    with pytest.raises(ValidationError):
        parse_source("https://github.com/owner/app/releases/download/v1/app.exe")


def test_validate_port():
    assert validate_port(None) is None
    assert validate_port("") is None
    assert validate_port(8080) == 8080
    assert validate_port("3000") == 3000
    for bad in ["80", "-1", "70000", "abc"]:
        with pytest.raises(ValidationError):
            validate_port(bad)


def test_parse_build_progress_classic_builder():
    assert parse_build_progress("Step 3/8 : RUN npm install") == (3, 8)
    assert parse_build_progress("no step here") is None


def test_parse_build_progress_buildkit():
    assert parse_build_progress("#5 [2/4] RUN npm ci") == (5, 0)


def test_format_eta():
    assert format_eta(None) == "estimating..."
    assert format_eta(30) == "~30s"
    assert format_eta(90) == "~1m 30s"
    assert format_eta(3700) == "~1h 1m"


def test_build_eta_scales_with_steps():
    job = Job(
        id="abc",
        user_id=1,
        chat_id=1,
        repo_url="https://github.com/o/r.git",
        repo_full_name="o/r",
        status=JobStatus.BUILDING,
        build_started_at=100.0,
        build_step=2,
        build_total_steps=10,
    )
    eta = job.build_eta_seconds
    assert eta is not None and eta >= 0


def test_build_eta_none_without_steps():
    job = Job(id="a", user_id=1, chat_id=1, repo_url="u", repo_full_name="o/r")
    assert job.build_eta_seconds is None
