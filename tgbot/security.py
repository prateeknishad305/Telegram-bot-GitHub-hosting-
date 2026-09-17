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

ARCHIVE_SUFFIXES = (".zip", ".tar.gz", ".tgz", ".tar", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")

_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,38})"
_REPO = r"[A-Za-z0-9_.-]{1,100}?"

_RELEASE_ASSET_RE = re.compile(
    rf"^(?:https?://)?(?:www\.)?github\.com/(?P<owner>{_OWNER})/(?P<name>{_REPO})"
    r"/releases/download/(?P<tag>[^/\s?#]+)/(?P<asset>[^/\s?#]+)$",
    re.IGNORECASE,
)

_ARCHIVE_REF_RE = re.compile(
    rf"^(?:https?://)?(?:www\.)?github\.com/(?P<owner>{_OWNER})/(?P<name>{_REPO})"
    r"/archive/refs/(?P<kind>tags|heads)/(?P<ref>[^/\s?#]+?)(?P<suffix>"
    + "|".join(re.escape(suffix) for suffix in ARCHIVE_SUFFIXES)
    + r")$",
    re.IGNORECASE,
)

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


@dataclass(frozen=True)
class SourceRef:
    """A runnable source: either a git repository or a release archive."""

    kind: str
    owner: str
    name: str
    full_name: str
    tag: str | None = None
    asset: str | None = None
    url: str | None = None

    @property
    def clone_url(self) -> str:
        return f"https://{GITHUB_HOST}/{self.full_name}.git"

    @property
    def is_archive(self) -> bool:
        return self.kind == "archive"

    @property
    def release_url(self) -> str | None:
        if self.tag:
            return f"https://{GITHUB_HOST}/{self.full_name}/releases/tag/{self.tag}"
        return None


def archive_suffix(name: str) -> str | None:
    lowered = name.lower()
    for suffix in ARCHIVE_SUFFIXES:
        if lowered.endswith(suffix):
            return suffix
    return None


def _validate_release_part(value: str, label: str) -> str:
    value = value.strip()
    if not value or value in {".", ".."}:
        raise ValidationError(f"Invalid {label}")
    if "/" in value or "\\" in value or "\x00" in value:
        raise ValidationError(f"Invalid {label}")
    return value


def _parse_archive_url(raw: str) -> SourceRef | None:
    match = _RELEASE_ASSET_RE.match(raw)
    if match:
        asset = _validate_release_part(match.group("asset"), "release asset")
        if archive_suffix(asset) is None:
            raise ValidationError(
                "Release assets must be .zip, .tar.gz, .tgz or .tar archives; "
                "other assets (binaries, installers, checksums) cannot be run"
            )
        return SourceRef(
            kind="archive",
            owner=match.group("owner"),
            name=match.group("name").removesuffix(".git"),
            full_name=f"{match.group('owner')}/{match.group('name').removesuffix('.git')}",
            tag=_validate_release_part(match.group("tag"), "release tag"),
            asset=asset,
            url=f"https://{GITHUB_HOST}/{match.group('owner')}/{match.group('name').removesuffix('.git')}"
            f"/releases/download/{match.group('tag')}/{asset}",
        )

    ref_match = _ARCHIVE_REF_RE.match(raw)
    if ref_match:
        name = ref_match.group("name").removesuffix(".git")
        ref = _validate_release_part(ref_match.group("ref"), "archive ref")
        return SourceRef(
            kind="archive",
            owner=ref_match.group("owner"),
            name=name,
            full_name=f"{ref_match.group('owner')}/{name}",
            tag=ref,
            asset=f"{name}-{ref}{ref_match.group('suffix')}",
            url=f"https://{GITHUB_HOST}/{ref_match.group('owner')}/{name}"
            f"/archive/refs/{ref_match.group('kind')}/{ref}{ref_match.group('suffix')}",
        )

    if archive_suffix(raw.split("?", 1)[0]) is not None and "github.com" not in raw.lower():
        raise ValidationError(
            "Only github.com archive URLs are supported, e.g. "
            "https://github.com/owner/repo/releases/download/v1.0/app.zip"
        )
    return None


def parse_source(raw: str) -> SourceRef:
    value = (raw or "").strip()
    if not value:
        raise ValidationError("Repository URL is empty")

    archive = _parse_archive_url(value)
    if archive is not None:
        return archive

    repo = parse_repo_url(value)
    return SourceRef(kind="git", owner=repo.owner, name=repo.name, full_name=repo.full_name)


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


def check_owner_allowed(repo: RepoRef | SourceRef, allowed_owners: set[str]) -> None:
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


def validate_port(port: int | str | None) -> int | None:
    if port is None or port == "":
        return None
    try:
        value = int(port)
    except (TypeError, ValueError):
        raise ValidationError("Port must be a number between 1024 and 65535") from None
    if not 1024 <= value <= 65535:
        raise ValidationError("Port must be between 1024 and 65535")
    return value


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
