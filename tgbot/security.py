from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

GITHUB_HOST = "github.com"

_REPO_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com[/:]([A-Za-z0-9](?:[A-Za-z0-9_.-]{0,38}))/([A-Za-z0-9_.-]{1,100}?)(?:\.git)?/?$",
    re.IGNORECASE,
)
_SHORTHAND_RE = re.compile(r"^([A-Za-z0-9_.-]{1,39})/([A-Za-z0-9_.-]{1,100})$")

_SECRET_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?i)(token|password|passwd|secret|api[_-]?key)\s*[=:]\s*[^\s\"']+"),
]


class ValidationError(ValueError):
    pass


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str
    full_name: str

    @property
    def clone_url(self) -> str:
        return f"https://{GITHUB_HOST}/{self.full_name}.git"


def parse_repo_url(raw: str) -> RepoRef:
    value = (raw or "").strip()
    if not value:
        raise ValidationError("Repository URL is empty")

    ssh_match = re.match(r"^(?:ssh://)?git@github\.com[/:](.+)$", value, re.IGNORECASE)
    if ssh_match:
        value = "https://github.com/" + ssh_match.group(1)

    match = _REPO_RE.match(value)
    if not match:
        shorthand = _SHORTHAND_RE.match(value)
        if shorthand and "." not in shorthand.group(1):
            owner, name = shorthand.group(1), shorthand.group(2)
        else:
            raise ValidationError(
                "Only github.com repositories are supported, e.g. https://github.com/owner/repo"
            )
    else:
        owner, name = match.group(1), match.group(2)

    name = name.removesuffix(".git")
    if not owner or not name:
        raise ValidationError("Repository URL must include an owner and a repository name")
    if name in {".", ".."}:
        raise ValidationError("Invalid repository name")

    return RepoRef(owner=owner, name=name, full_name=f"{owner}/{name}")


def check_owner_allowed(repo: RepoRef, allowed_owners: set[str]) -> None:
    if not allowed_owners:
        return
    if repo.owner.lower() not in allowed_owners:
        raise ValidationError(
            f"Owner '{repo.owner}' is not in the configured allowlist "
            f"({', '.join(sorted(allowed_owners))})"
        )


def validate_branch(branch: str | None) -> str | None:
    if branch is None:
        return None
    branch = branch.strip()
    if not branch:
        return None
    if len(branch) > 100 or branch.startswith("-") or not re.match(r"^[A-Za-z0-9._/\-]+$", branch):
        raise ValidationError("Invalid branch name")
    return branch


def redact_secrets(text: str) -> str:
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub("***", result)
    return result


def sanitize_token(text: str) -> str:
    return redact_secrets(text)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def create_session_token(user_id: int, secret: str, ttl_seconds: int = 3600) -> str:
    payload = json.dumps({"uid": user_id, "exp": int(time.time()) + ttl_seconds}, separators=(",", ":"))
    body = _b64url_encode(payload.encode("utf-8"))
    signature = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64url_encode(signature)}"


def verify_session_token(token: str, secret: str) -> int | None:
    if not token or "." not in token:
        return None
    body, signature = token.split(".", 1)
    expected = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    try:
        provided = _b64url_decode(signature)
    except (ValueError, base64.binascii.Error):
        return None
    if not hmac.compare_digest(expected, provided):
        return None
    try:
        payload = json.loads(_b64url_decode(body))
    except (ValueError, json.JSONDecodeError, base64.binascii.Error):
        return None
    if int(payload.get("exp", 0)) < time.time():
        return None
    return int(payload.get("uid"))


def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86400) -> dict | None:
    if not init_data or not bot_token:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    except ValueError:
        return None
    data = dict(pairs)
    received_hash = data.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    computed = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, received_hash):
        return None

    try:
        auth_date = int(data.get("auth_date", "0"))
    except ValueError:
        return None
    if max_age_seconds and time.time() - auth_date > max_age_seconds:
        return None

    user: dict = {}
    if "user" in data:
        try:
            user = json.loads(data["user"])
        except json.JSONDecodeError:
            return None
    if not user.get("id"):
        return None

    return {"user": user, "auth_date": auth_date, "raw": data}
