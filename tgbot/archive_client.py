from __future__ import annotations

import os
import shutil
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

import aiohttp

from .security import SourceRef, archive_suffix

GITHUB_DOWNLOAD_HOSTS = {
    "github.com",
    "www.github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}

GITHUB_HOST_SUFFIX = ".githubusercontent.com"

CHUNK = 1 << 16


class ArchiveError(RuntimeError):
    pass


def _host_allowed(host: str) -> bool:
    host = (host or "").lower()
    return host in GITHUB_DOWNLOAD_HOSTS or host.endswith(GITHUB_HOST_SUFFIX)


def _safe_filename(name: str) -> str:
    name = os.path.basename(name.replace("\\", "/")).strip()
    name = name.lstrip(".")
    if not name:
        raise ArchiveError("Invalid archive file name")
    return name


def _is_unsafe_name(name: str) -> bool:
    if not name or "\x00" in name:
        return True
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        return True
    path = PurePosixPath(normalized)
    if path.is_absolute():
        return True
    return any(part == ".." for part in path.parts)


def _target_path(root: Path, name: str) -> Path:
    normalized = name.replace("\\", "/").lstrip("/")
    target = (root / normalized).resolve()
    base = root.resolve()
    if base != target and base not in target.parents:
        raise ArchiveError(f"Archive entry escapes the destination: {name}")
    return target


async def download_archive(
    source: SourceRef,
    dest_dir: Path,
    token: str | None = None,
    max_bytes: int = 512 * 1024 * 1024,
    timeout: int = 900,
) -> Path:
    if not source.is_archive or not source.url or not source.asset:
        raise ArchiveError("This source is not a release archive")

    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / _safe_filename(source.asset)

    headers = {"User-Agent": "tgbot-runner", "Accept": "application/octet-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    client_timeout = aiohttp.ClientTimeout(total=timeout, sock_connect=20, sock_read=180)
    written = 0
    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        try:
            async with session.get(source.url, headers=headers, allow_redirects=True) as response:
                if response.status == 404:
                    raise ArchiveError(
                        f"Release asset not found ({source.tag}/{source.asset}). "
                        "Check that the tag and file name exist (private repos need GITHUB_TOKEN)."
                    )
                if response.status >= 400:
                    raise ArchiveError(f"Download failed with HTTP {response.status}")
                if not _host_allowed(response.url.host or ""):
                    raise ArchiveError(f"Refusing to download from unexpected host: {response.url.host}")

                declared = response.content_length
                if declared and declared > max_bytes:
                    raise ArchiveError(
                        f"Archive is {declared} bytes, above the {max_bytes} byte limit "
                        "(raise ARCHIVE_MAX_BYTES if this is expected)"
                    )

                with target.open("wb") as handle:
                    async for chunk in response.content.iter_chunked(CHUNK):
                        written += len(chunk)
                        if written > max_bytes:
                            raise ArchiveError(
                                f"Archive exceeds the {max_bytes} byte limit "
                                "(raise ARCHIVE_MAX_BYTES if this is expected)"
                            )
                        handle.write(chunk)
        except aiohttp.ClientError as exc:
            raise ArchiveError(f"Could not download the archive: {exc}") from exc

    if written == 0:
        raise ArchiveError("Downloaded archive is empty")
    return target


def _extract_zip(archive: Path, dest: Path, max_total_bytes: int) -> None:
    try:
        with zipfile.ZipFile(archive) as zf:
            total = 0
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if _is_unsafe_name(info.filename):
                    raise ArchiveError(f"Refusing unsafe archive entry: {info.filename}")
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    continue
                total += info.file_size
                if total > max_total_bytes:
                    raise ArchiveError(
                        f"Archive expands to more than {max_total_bytes} bytes "
                        "(raise ARCHIVE_EXTRACT_MAX_BYTES if this is expected)"
                    )
                target = _target_path(dest, info.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"Not a valid zip archive: {exc}") from exc


def _extract_tar(archive: Path, dest: Path, max_total_bytes: int) -> None:
    try:
        with tarfile.open(archive, "r:*") as tf:
            total = 0
            for member in tf:
                if member.isdir():
                    continue
                if not member.isfile():
                    continue
                if _is_unsafe_name(member.name):
                    raise ArchiveError(f"Refusing unsafe archive entry: {member.name}")
                total += member.size
                if total > max_total_bytes:
                    raise ArchiveError(
                        f"Archive expands to more than {max_total_bytes} bytes "
                        "(raise ARCHIVE_EXTRACT_MAX_BYTES if this is expected)"
                    )
                src = tf.extractfile(member)
                if src is None:
                    continue
                target = _target_path(dest, member.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                with src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)
    except tarfile.TarError as exc:
        raise ArchiveError(f"Not a valid tar archive: {exc}") from exc


def extract_archive(archive: Path, dest: Path, max_total_bytes: int = 2 * 1024 * 1024 * 1024) -> Path:
    if not archive.exists():
        raise ArchiveError(f"Archive does not exist: {archive}")
    suffix = archive_suffix(archive.name)
    if suffix is None:
        raise ArchiveError(f"Unsupported archive type: {archive.name}")

    dest.mkdir(parents=True, exist_ok=True)
    if suffix == ".zip":
        _extract_zip(archive, dest, max_total_bytes)
    else:
        _extract_tar(archive, dest, max_total_bytes)

    entries = [entry for entry in dest.iterdir() if entry.name not in {"__MACOSX"}]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return dest
