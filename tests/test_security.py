import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from tgbot.security import (
    ValidationError,
    check_owner_allowed,
    create_session_token,
    parse_repo_url,
    redact_secrets,
    validate_branch,
    validate_init_data,
    verify_session_token,
)


def test_parse_repo_url_accepts_common_forms():
    for value in [
        "https://github.com/owner/repo",
        "https://github.com/owner/repo.git",
        "http://www.github.com/owner/repo/",
        "git@github.com:owner/repo.git",
        "owner/repo",
    ]:
        ref = parse_repo_url(value)
        assert ref.full_name == "owner/repo"
        assert ref.clone_url == "https://github.com/owner/repo.git"


def test_parse_repo_url_rejects_other_hosts():
    for value in ["https://gitlab.com/owner/repo", "https://evil.com/github.com/a/b", "", "just-a-name"]:
        with pytest.raises(ValidationError):
            parse_repo_url(value)


def test_check_owner_allowed():
    ref = parse_repo_url("https://github.com/owner/repo")
    check_owner_allowed(ref, set())
    check_owner_allowed(ref, {"owner"})
    with pytest.raises(ValidationError):
        check_owner_allowed(ref, {"someoneelse"})


def test_validate_branch():
    assert validate_branch(None) is None
    assert validate_branch(" main ") == "main"
    assert validate_branch("feature/x-1") == "feature/x-1"
    for bad in ["-x", "a b", "a;b", "x" * 200]:
        with pytest.raises(ValidationError):
            validate_branch(bad)


def test_redact_secrets():
    text = "token=ghp_abcdefghijklmnopqrstuvwxyz012345 and password: hunter2"
    cleaned = redact_secrets(text)
    assert "ghp_abcdefghijklmnopqrstuvwxyz012345" not in cleaned
    assert "hunter2" not in cleaned


def test_session_token_roundtrip():
    token = create_session_token(42, "secret", ttl_seconds=60)
    assert verify_session_token(token, "secret") == 42
    assert verify_session_token(token, "other-secret") is None
    assert verify_session_token("garbage", "secret") is None


def test_session_token_expiry():
    token = create_session_token(7, "secret", ttl_seconds=-1)
    assert verify_session_token(token, "secret") is None


def _make_init_data(bot_token: str, user_id: int) -> str:
    data = {
        "auth_date": str(int(time.time())),
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
    }
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


def test_validate_init_data_accepts_valid_payload():
    bot_token = "123:ABC"
    init_data = _make_init_data(bot_token, 555)
    result = validate_init_data(init_data, bot_token)
    assert result is not None
    assert result["user"]["id"] == 555


def test_validate_init_data_rejects_tampering():
    bot_token = "123:ABC"
    init_data = _make_init_data(bot_token, 555)
    assert validate_init_data(init_data, "999:XYZ") is None
    assert validate_init_data(init_data, bot_token + "x") is None
    assert validate_init_data("", bot_token) is None
