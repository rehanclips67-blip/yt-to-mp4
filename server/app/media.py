"""yt-dlp wrapper: read video info and download a trimmed clip."""

import html
import importlib.util
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from yt_dlp import YoutubeDL
from yt_dlp.utils import download_range_func

from .formats import Quality
from .models import Source, SourceType


class Mode(StrEnum):
    EXACT = "exact"  # re-encode to H.264/AAC: frame-accurate cut, plays everywhere
    FAST = "fast"  # stream copy: near-instant, but the cut snaps to the nearest keyframe


class ClipSpec(NamedTuple):
    url: str
    start: float
    end: float
    quality: Quality
    mode: Mode


class Clip(NamedTuple):
    path: Path
    title: str


class MediaTimeoutError(TimeoutError):
    """Raised when a download or media conversion exceeds its configured limit."""


class CaptionFetchError(ValueError):
    """Raised when a caption URL fails SSRF or response-size validation."""


class PrimaryUrlError(ValueError):
    """Raised when a submitted YouTube URL resolves to an unsafe host."""


class WhisperResourceError(RuntimeError):
    """Raised when Whisper cannot safely start within configured resources."""


_BASE_OPTS = {"quiet": True, "no_warnings": True, "noplaylist": True}

# 4K encoding gets memory-hungry with many threads (and 32-bit FFmpeg builds run out at ~2 GB),
# so cap the encoder and decoder threads. Raise these on a big machine if you want more speed.
_DECODE_THREADS = "2"
_ENCODE_THREADS = "4"

_EXACT_DECODE_ARGS = ["-threads", _DECODE_THREADS]
_EXACT_ENCODE_ARGS = [
    *("-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"),
    *("-threads", _ENCODE_THREADS),
    *("-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"),
]
_CAPTION_HOSTS = frozenset(
    {
        "www.youtube.com",
        "youtube.com",
        "m.youtube.com",
        "video.google.com",
        "googlevideo.com",
    }
)
_CAPTION_MAX_BYTES = 4 * 1024 * 1024
_source_writer: ContextVar[tuple[Callable[[Source], None], int] | None] = ContextVar(
    "source_writer", default=None
)
log = logging.getLogger("clipper.media")


@contextmanager
def source_context(writer: Callable[[Source], None], ttl: int):
    token = _source_writer.set((writer, ttl))
    try:
        yield
    finally:
        _source_writer.reset(token)


def _create_source(info: dict, path: Path, url: str) -> None:
    context = _source_writer.get()
    if context is None:
        return
    writer, ttl = context
    try:
        formats = info.get("requested_downloads") or info.get("requested_formats") or []
        selected = next(
            (item for item in formats if item.get("vcodec") not in {None, "none"}), info
        )
        created_at = datetime.now(UTC)
        writer(
            Source(
                id=secrets.token_urlsafe(16),
                source_type=SourceType.youtube,
                original_url=url,
                storage_key=str(path.resolve()),
                title=info["title"],
                duration_seconds=float(info.get("duration") or 0),
                width=selected.get("width") or info.get("width"),
                height=selected.get("height") or info.get("height"),
                fps=selected.get("fps") or info.get("fps"),
                codec=selected.get("vcodec") or selected.get("codec") or info.get("vcodec"),
                status="ready",
                created_at=created_at,
                expires_at=created_at + timedelta(seconds=ttl),
            )
        )
    except Exception:
        log.warning("source persistence failed for %s", url, exc_info=True)


def _max_download_bytes() -> int:
    value = int(os.getenv("MEDIA_MAX_DOWNLOAD_BYTES", str(4 * 1024**3)))
    if value <= 0:
        raise ValueError("MEDIA_MAX_DOWNLOAD_BYTES must be positive")
    return value


def _caption_host_allowed(hostname: str | None) -> bool:
    if not hostname:
        return False
    host = hostname.rstrip(".").lower()
    return host in _CAPTION_HOSTS or any(
        host.endswith(f".{suffix}") for suffix in ("youtube.com", "googlevideo.com")
    )


def _resolve_public_host(hostname: str, error_type: type[ValueError], label: str) -> None:
    try:
        addresses = {
            item[4][0] for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise error_type(f"{label} host could not be resolved.") from exc
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise error_type(f"{label} host returned an invalid IP address.") from exc
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise error_type(f"{label} host resolves to a non-public IP address.")


def validate_primary_url(url: str) -> None:
    parsed = urlparse(url)
    host = parsed.hostname.rstrip(".").lower() if parsed.hostname else None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or host
        not in {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "music.youtube.com",
            "youtu.be",
        }
    ):
        raise PrimaryUrlError("Only public YouTube links are supported.")
    _resolve_public_host(host, PrimaryUrlError, "YouTube")


def _validate_caption_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or not _caption_host_allowed(parsed.hostname)
    ):
        raise CaptionFetchError("Caption URL is not an allowed HTTPS YouTube endpoint.")
    _resolve_public_host(parsed.hostname.rstrip("."), CaptionFetchError, "Caption")


class _CaptionRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_caption_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_caption_payload(url: str) -> bytes:
    _validate_caption_url(url)
    request = Request(url, headers={"Accept": "text/vtt, application/json"})
    try:
        with build_opener(_CaptionRedirectHandler).open(request, timeout=15) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > _CAPTION_MAX_BYTES:
                raise CaptionFetchError("Caption response exceeds the allowed size.")
            chunks: list[bytes] = []
            total = 0
            while chunk := response.read(min(64 * 1024, _CAPTION_MAX_BYTES - total + 1)):
                total += len(chunk)
                if total > _CAPTION_MAX_BYTES:
                    raise CaptionFetchError("Caption response exceeds the allowed size.")
                chunks.append(chunk)
            return b"".join(chunks)
    except CaptionFetchError:
        raise
    except (OSError, ValueError) as exc:
        raise CaptionFetchError("Caption request failed.") from exc


def media_timeout_seconds(duration_seconds: float | None = None) -> float:
    """Return a bounded media-operation budget scaled to the requested duration."""
    base = float(os.getenv("MEDIA_TIMEOUT_BASE_SECONDS", "300"))
    per_minute = float(os.getenv("MEDIA_TIMEOUT_PER_MINUTE_SECONDS", "60"))
    floor = float(os.getenv("MEDIA_TIMEOUT_MIN_SECONDS", "120"))
    ceiling = float(os.getenv("MEDIA_TIMEOUT_MAX_SECONDS", "7200"))
    if base <= 0 or per_minute < 0 or floor <= 0 or ceiling < floor:
        raise ValueError(
            "MEDIA_TIMEOUT_BASE_SECONDS, MEDIA_TIMEOUT_MIN_SECONDS, and "
            "MEDIA_TIMEOUT_MAX_SECONDS must be positive; per-minute timeout cannot be negative"
        )
    duration = max(0.0, float(duration_seconds or 0.0))
    return min(ceiling, max(floor, base + per_minute * (duration / 60)))


def _timeout_hook(deadline: float, budget: float):
    def check(status: dict) -> None:
        if time.monotonic() > deadline:
            raise MediaTimeoutError(f"Media operation exceeded its {budget:g}-second timeout")

    return check


def fetch_info(url: str) -> dict:
    """Read metadata and the available formats without downloading anything."""
    validate_primary_url(url)
    with YoutubeDL({**_BASE_OPTS, "skip_download": True}) as ydl:
        return ydl.extract_info(url, download=False)


def fetch_transcript(url: str) -> list[dict] | None:
    """Download the best available YouTube caption track and return timestamped segments."""
    raw = fetch_info(url)
    tracks = {**(raw.get("automatic_captions") or {}), **(raw.get("subtitles") or {})}
    if not tracks:
        return None
    language = next(
        (key for key in tracks if key.lower() == "en"),
        next((key for key in tracks if key.lower().startswith("en")), next(iter(tracks))),
    )
    track = next((item for item in tracks[language] if item.get("url")), None)
    if not track:
        return None
    payload = _fetch_caption_payload(track["url"]).decode("utf-8-sig")
    if track.get("ext") == "json3" or payload.lstrip().startswith("{"):
        return _parse_json3(payload)
    return _parse_vtt(payload)


def fetch_whisper_transcript(
    url: str, progress: Callable[[str, int], None] | None = None
) -> list[dict] | None:
    """Optionally transcribe audio when the caller explicitly requests fallback."""
    raw = fetch_info(url)
    duration = float(raw.get("duration") or 0)
    if not duration or duration > float(os.getenv("WHISPER_MAX_SECONDS", "600")):
        return None
    if os.getenv("ENABLE_WHISPER_FALLBACK", "").lower() not in {"1", "true", "yes"}:
        return None
    if progress:
        progress("downloading", 5)
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper is unavailable; install the optional whisper extra"
        ) from exc
    _check_whisper_memory()
    with tempfile.TemporaryDirectory(prefix="clipper-whisper-") as directory:
        source = str(Path(directory) / "audio.%(ext)s")
        budget = media_timeout_seconds(duration)
        with YoutubeDL(
            {
                **_BASE_OPTS,
                "socket_timeout": budget,
                "progress_hooks": [_timeout_hook(time.monotonic() + budget, budget)],
                "format": "bestaudio/best",
                "outtmpl": source,
                "max_filesize": int(os.getenv("WHISPER_MAX_BYTES", str(100 * 1024 * 1024))),
            }
        ) as ydl:
            info = ydl.extract_info(url, download=True)
            audio = Path(ydl.prepare_filename(info))
        if progress:
            progress("loading_model", 10)
        model = WhisperModel(
            os.getenv("WHISPER_MODEL", "base"),
            device=os.getenv("WHISPER_DEVICE", "cpu"),
            compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "int8"),
            cpu_threads=int(os.getenv("WHISPER_CPU_THREADS", "2")),
        )
        segments, _info = model.transcribe(str(audio), word_timestamps=True)
        result = []
        deadline = time.monotonic() + budget
        for segment in segments:
            if time.monotonic() > deadline:
                raise MediaTimeoutError(f"Media operation exceeded its {budget:g}-second timeout")
            text = segment.text.strip()
            if not text:
                continue
            words = [
                {"start": word.start, "end": word.end, "text": word.word.strip()}
                for word in (segment.words or [])
                if word.word.strip()
            ]
            result.append(
                {
                    "start": segment.start,
                    "end": segment.end,
                    "text": text,
                    "words": words or None,
                }
            )
            if progress:
                progress("transcribing", min(95, max(10, int(segment.end / duration * 85) + 10)))
        if progress:
            progress("complete", 100)
        return result


def _check_whisper_memory() -> None:
    limit = int(os.getenv("WHISPER_MIN_AVAILABLE_MEMORY_MB", "512"))
    try:
        import psutil
    except ImportError:
        return
    if psutil.virtual_memory().available < limit * 1024 * 1024:
        raise WhisperResourceError(
            f"Whisper requires at least {limit} MB of available memory before starting."
        )


def whisper_is_configured() -> bool:
    return (
        os.getenv("ENABLE_WHISPER_FALLBACK", "").lower() in {"1", "true", "yes"}
        and importlib.util.find_spec("faster_whisper") is not None
    )


def _parse_vtt(payload: str) -> list[dict]:
    segments = []
    for block in re.split(r"\n\s*\n", payload):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing = next((line for line in lines if " --> " in line), None)
        if not timing:
            continue
        start_text, end_text = timing.split(" --> ", 1)
        text = " ".join(lines[lines.index(timing) + 1 :])
        if text:
            segments.append(
                {
                    "start": _timestamp(start_text),
                    "end": _timestamp(end_text.split()[0]),
                    "text": html.unescape(re.sub(r"<[^>]+>", "", text)),
                }
            )
    return segments


def _parse_json3(payload: str) -> list[dict]:
    data = json.loads(payload)
    segments = []
    for event in data.get("events", []):
        if not event.get("segs") or event.get("tStartMs") is None:
            continue
        text = "".join(segment.get("utf8", "") for segment in event["segs"]).strip()
        if text:
            segments.append(
                {
                    "start": event["tStartMs"] / 1000,
                    "end": (event["tStartMs"] + event.get("dDurationMs", 0)) / 1000,
                    "text": text,
                }
            )
    return segments


def _timestamp(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    return sum(float(part) * 60 ** (len(parts) - index - 1) for index, part in enumerate(parts))


def _run_ffmpeg(args: list[str], timeout: float) -> None:
    subprocess.run(args, check=True, capture_output=True, timeout=timeout, shell=False)


def download_clip(spec: ClipSpec, out_dir: Path) -> Clip:
    """Download only the requested range, in the requested quality, as one MP4."""
    budget = media_timeout_seconds(spec.end - spec.start)
    deadline = time.monotonic() + budget
    timeout_hook = _timeout_hook(deadline, budget)
    opts = {
        **_BASE_OPTS,
        "socket_timeout": budget,
        "progress_hooks": [timeout_hook],
        "postprocessor_hooks": [timeout_hook],
        # Keep the selected source at or below the requested resolution.  A
        # format_sort alone is only a preference and may choose a larger stream.
        "format": (
            f"bv*[height<={spec.quality.res}]+ba/"
            f"b[height<={spec.quality.res}]/bv*[height<={spec.quality.res}]/best"
        ),
        "format_sort": [f"res:{spec.quality.res}", "fps"],  # keep the video's own frame rate
        "merge_output_format": "mp4",
        "download_ranges": download_range_func(None, [(spec.start, spec.end)]),
        "outtmpl": str(out_dir / "clip.%(ext)s"),
        "max_filesize": _max_download_bytes(),
    }
    if spec.mode is Mode.EXACT:
        opts["force_keyframes_at_cuts"] = True
        opts["external_downloader_args"] = {
            "ffmpeg_i": _EXACT_DECODE_ARGS,
            "ffmpeg_o": _EXACT_ENCODE_ARGS,
        }

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(spec.url, download=True)
    path = Path(info["requested_downloads"][0]["filepath"])
    _create_source(info, path, spec.url)
    return Clip(path, info["title"])


def download_export(specs: tuple[ClipSpec, ...], out_dir: Path) -> list[Clip]:
    """Download one source and cut all ranges from it.

    Multi-range exports deliberately share the source download.  Fast cuts use
    stream copy; exact cuts are encoded with the same bounded settings as clips.
    """
    first = specs[0]
    source_dir = out_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    budget = media_timeout_seconds(sum(spec.end - spec.start for spec in specs))
    deadline = time.monotonic() + budget
    timeout_hook = _timeout_hook(deadline, budget)
    opts = {
        **_BASE_OPTS,
        "socket_timeout": budget,
        "progress_hooks": [timeout_hook],
        "postprocessor_hooks": [timeout_hook],
        "format": (
            f"bv*[height<={first.quality.res}]+ba/"
            f"b[height<={first.quality.res}]/bv*[height<={first.quality.res}]/best"
        ),
        "format_sort": [f"res:{first.quality.res}", "fps", "codec"],
        "merge_output_format": "mp4",
        "outtmpl": str(source_dir / "source.%(ext)s"),
        "max_filesize": _max_download_bytes(),
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(first.url, download=True)
        source = Path(ydl.prepare_filename(info))
        if source.suffix != ".mp4":
            candidate = source.with_suffix(".mp4")
            if candidate.exists():
                source = candidate
    _create_source(info, source, first.url)
    outputs: list[Clip] = []
    for index, spec in enumerate(specs):
        target = out_dir / f"clip-{index}.mp4"
        args = [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-ss",
            str(spec.start),
            "-i",
            str(source),
            "-t",
            str(spec.end - spec.start),
        ]
        if spec.mode is Mode.FAST:
            args += ["-c", "copy"]
        else:
            args += [*_EXACT_ENCODE_ARGS]
        args += [str(target)]
        remaining = max(0.1, deadline - time.monotonic())
        try:
            _run_ffmpeg(args, remaining)
        except subprocess.TimeoutExpired as exc:
            raise MediaTimeoutError(
                f"Media operation exceeded its {budget:g}-second timeout"
            ) from exc
        outputs.append(Clip(target, info["title"]))
    return outputs
