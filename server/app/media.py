"""yt-dlp wrapper: read video info and download a trimmed clip."""

import html
import importlib.metadata
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
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from shutil import which
from typing import NamedTuple
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from yt_dlp import YoutubeDL
from yt_dlp.utils import YoutubeDLError, download_range_func

from .formats import Quality
from .models import Source, SourceType


class Mode(StrEnum):
    EXACT = "exact"  # re-encode to H.264/AAC: frame-accurate cut, plays everywhere
    FAST = "fast"  # stream copy: near-instant, but the cut snaps to the nearest keyframe


class ClipSpec(NamedTuple):
    url: str | None
    start: float
    end: float
    quality: Quality
    mode: Mode
    source_id: str | None = None


def make_clip_spec(
    *,
    url: str | None,
    source_id: str | None,
    start: float,
    end: float,
    quality: Quality,
    mode: Mode,
) -> ClipSpec:
    if (url is None) == (source_id is None):
        raise ValueError("Exactly one of url or source_id is required.")
    return ClipSpec(url, start, end, quality, mode, source_id)


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
ProgressCallback = Callable[[str, int | None], None]
_INFO_CACHE_TTL_SECONDS = 10.0
_INFO_CACHE_MAX_ENTRIES = 128
_DURATION_CACHE_TTL_SECONDS = 900.0
_DURATION_CACHE_MAX_ENTRIES = 1024
_duration_cache: dict[str, tuple[float, float]] = {}
_info_cache_lock = threading.Lock()
_info_cache: dict[str, "_InfoCacheEntry"] = {}


class _InfoCacheEntry:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: dict | None = None
        self.error: Exception | None = None
        self.expires_at = 0.0
        self.in_flight = True


def _normalized_info_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/", 1)[0]
        return f"https://www.youtube.com/watch?{urlencode({'v': video_id})}"
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        video_id = parse_qs(parsed.query).get("v", [None])[0]
        if video_id:
            return f"https://www.youtube.com/watch?{urlencode({'v': video_id})}"
    return urlunparse((parsed.scheme.lower(), host, parsed.path, "", parsed.query, ""))


def _clear_info_cache() -> None:
    with _info_cache_lock:
        _info_cache.clear()
        _duration_cache.clear()


def _video_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        return parsed.path.strip("/").split("/", 1)[0] or None
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        return parse_qs(parsed.query).get("v", [None])[0]
    return None


def get_cached_youtube_duration(url: str) -> float | None:
    video_id = _video_id(url)
    if not video_id:
        return None
    with _info_cache_lock:
        cached = _duration_cache.get(video_id)
        if cached is None:
            return None
        duration, expires_at = cached
        if expires_at <= time.monotonic():
            del _duration_cache[video_id]
            return None
        return duration


def _cache_youtube_duration(url: str, info: dict) -> None:
    video_id = _video_id(url)
    duration = info.get("duration")
    if not video_id or duration is None:
        return
    with _info_cache_lock:
        now = time.monotonic()
        for cached_id, (_, expires_at) in list(_duration_cache.items()):
            if expires_at <= now:
                del _duration_cache[cached_id]
        while len(_duration_cache) >= _DURATION_CACHE_MAX_ENTRIES:
            del _duration_cache[min(_duration_cache, key=lambda item: _duration_cache[item][1])]
        _duration_cache[video_id] = (
            float(duration),
            now
            + float(
                os.getenv(
                    "YOUTUBE_DURATION_CACHE_TTL_SECONDS",
                    str(int(_DURATION_CACHE_TTL_SECONDS)),
                )
            ),
        )


def _evict_info_cache_entries(now: float) -> None:
    for key, entry in list(_info_cache.items()):
        if not entry.in_flight and entry.expires_at <= now:
            del _info_cache[key]
    while len(_info_cache) >= _INFO_CACHE_MAX_ENTRIES:
        completed = [
            (entry.expires_at, key)
            for key, entry in _info_cache.items()
            if not entry.in_flight
        ]
        if not completed:
            return
        _, key = min(completed)
        del _info_cache[key]


def _begin_info_extraction(key: str) -> tuple[_InfoCacheEntry | None, bool]:
    now = time.monotonic()
    with _info_cache_lock:
        entry = _info_cache.get(key)
        if entry is not None:
            if entry.in_flight:
                return entry, False
            if entry.expires_at > now and entry.result is not None:
                return entry, False
            del _info_cache[key]
        _evict_info_cache_entries(now)
        if len(_info_cache) >= _INFO_CACHE_MAX_ENTRIES:
            return None, True
        entry = _InfoCacheEntry()
        _info_cache[key] = entry
        return entry, True


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _runtime_version(command: str, args: tuple[str, ...]) -> str | None:
    if which(command) is None:
        return None
    try:
        result = subprocess.run(
            [command, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (result.stdout or result.stderr).splitlines()
    return output[0].strip()[:80] if output else None


def youtube_environment_diagnostics() -> dict[str, object]:
    runtimes = {
        "node": _runtime_version("node", ("--version",)),
        "deno": _runtime_version("deno", ("--version",)),
        "bun": _runtime_version("bun", ("--version",)),
        "quickjs": _runtime_version("qjs", ("--version",)),
    }
    return {
        "yt_dlp_version": _package_version("yt-dlp"),
        "yt_dlp_ejs_version": _package_version("yt-dlp-ejs"),
        "js_runtimes": {name: version for name, version in runtimes.items() if version},
        "configured_js_runtime": "node",
        "configured_player_clients": _verification_fallback_clients(),
    }


def youtube_pot_provider_diagnostics() -> dict[str, object]:
    provider_url = os.getenv("YOUTUBE_POT_PROVIDER_URL", "").strip()
    if not provider_url:
        return {"configured": False, "available": False, "status": "disabled"}
    parsed = urlparse(provider_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return {"configured": True, "available": False, "status": "invalid_configuration"}
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return {"configured": True, "available": False, "status": "invalid_configuration"}
    started = time.monotonic()
    try:
        with socket.create_connection((parsed.hostname, port), timeout=2):
            pass
    except OSError:
        return {
            "configured": True,
            "available": False,
            "status": "unavailable",
            "latency_ms": round((time.monotonic() - started) * 1000),
        }
    return {
        "configured": True,
        "available": True,
        "status": "available",
        "latency_ms": round((time.monotonic() - started) * 1000),
    }


def _sanitize_log_message(value: object) -> str:
    message = re.sub(r"(?i)\b(?:https?|ftp)://\S+", "[url]", str(value))
    message = re.sub(r"(?i)\b(?:bearer|basic)\s+\S+", "[credential]", message)
    message = re.sub(
        r"(?i)\b(authorization|cookie|token|password|proxy)[^\s:=]*\s*[:=]\s*\S+",
        r"\1=[redacted]",
        message,
    )
    message = re.sub(r"(?<!\w)(?:[A-Za-z]:)?[\\/](?:[^ \t]+)", "[path]", message)
    return re.sub(r"\s+", " ", message).strip()[:240]


def _youtube_error_category(error: Exception) -> str:
    detail = str(error).lower()
    if "sign in to confirm" in detail or "not a bot" in detail:
        return "bot_check"
    if "javascript" in detail or "js challenge" in detail or "challenge" in detail:
        return "js_challenge"
    if "ssl" in detail or "certificate verify failed" in detail:
        return "ssl"
    if (
        "age-restricted" in detail
        or "confirm your age" in detail
        or "private video" in detail
        or "sign in" in detail
    ):
        return "age_or_login"
    if (
        "video unavailable" in detail
        or "not available" in detail
        or "geo" in detail
        or "region" in detail
    ):
        return "region_or_unavailable"
    if (
        "429" in detail
        or "too many requests" in detail
        or "timed out" in detail
        or "connection" in detail
    ):
        return "network"
    return "other"


def _youtube_options(extra: dict | None = None, *, player_client: str | None = None) -> dict:
    options = {**_BASE_OPTS, **(extra or {})}
    options["js_runtimes"] = {"node": {}}
    extractor_args = dict(options.get("extractor_args") or {})
    youtube_args = dict(extractor_args.get("youtube") or {})
    cookies_file = os.getenv("YOUTUBE_COOKIES_FILE", "").strip()
    if cookies_file:
        options["cookiefile"] = cookies_file
    if player_client:
        youtube_args["player_client"] = [player_client]
    provider_url = os.getenv("YOUTUBE_POT_PROVIDER_URL", "").strip()
    if provider_url:
        extractor_args["youtubepot-bgutilhttp"] = {"base_url": [provider_url]}
    if youtube_args:
        extractor_args["youtube"] = youtube_args
    if extractor_args:
        options["extractor_args"] = extractor_args
    return options


def _verification_fallback_clients() -> tuple[str, ...]:
    configured = os.getenv("YOUTUBE_PLAYER_CLIENTS", "web_safari,android_vr")
    return tuple(client.strip() for client in configured.split(",") if client.strip())


def _is_youtube_verification_error(error: Exception) -> bool:
    detail = str(error).lower()
    return "sign in to confirm" in detail or "not a bot" in detail


class _DiagnosticLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def debug(self, message: str) -> None:
        self.messages.append(_sanitize_log_message(message))

    def warning(self, message: str) -> None:
        self.messages.append(_sanitize_log_message(message))

    def error(self, message: str) -> None:
        self.messages.append(_sanitize_log_message(message))


def diagnose_youtube(url: str) -> dict[str, object]:
    validate_primary_url(url)
    logger = _DiagnosticLogger()
    options = _youtube_options({"skip_download": True, "verbose": True, "logger": logger})
    player_client = (options.get("extractor_args") or {}).get("youtube", {}).get(
        "player_client", ["default"]
    )[0]
    result: dict[str, object] = {
        "error_class": None,
        "player_client": player_client,
        "environment": youtube_environment_diagnostics(),
        "messages": [],
    }
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
        result["format_count"] = len(info.get("formats") or [])
    except YoutubeDLError as error:
        result["error_class"] = _youtube_error_category(error)
        logger.error(error)
    result["messages"] = [message for message in logger.messages if message][:40]
    return result


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
        log.warning("source persistence failed for YouTube source")


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
    key = _normalized_info_url(url)
    entry, owner = _begin_info_extraction(key)
    if entry is not None and not owner:
        entry.event.wait()
        if entry.result is not None:
            return entry.result
        if entry.error is not None:
            raise entry.error
        raise RuntimeError("Metadata extraction ended without a result.")
    try:
        result = _fetch_info_uncached(url)
    except Exception as error:
        if entry is not None:
            with _info_cache_lock:
                entry.error = error
                entry.in_flight = False
                _info_cache.pop(key, None)
                entry.event.set()
        raise
    if entry is not None:
        with _info_cache_lock:
            entry.result = result
            entry.expires_at = time.monotonic() + _INFO_CACHE_TTL_SECONDS
            entry.in_flight = False
            entry.event.set()
    _cache_youtube_duration(url, result)
    return result


def _fetch_info_uncached(url: str) -> dict:
    options = _youtube_options({"skip_download": True})
    try:
        with YoutubeDL(options) as ydl:
            return ydl.extract_info(url, download=False)
    except YoutubeDLError as error:
        if not _is_youtube_verification_error(error):
            raise
        log.warning(
            "YouTube extraction failed with category=%s; trying fallback clients",
            _youtube_error_category(error),
        )
        for player_client in _verification_fallback_clients():
            try:
                with YoutubeDL(
                    _youtube_options({"skip_download": True}, player_client=player_client)
                ) as ydl:
                    return ydl.extract_info(url, download=False)
            except YoutubeDLError as fallback_error:
                log.warning(
                    "YouTube fallback client=%s failed with category=%s",
                    player_client,
                    _youtube_error_category(fallback_error),
                )
        raise


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
            _youtube_options({
                "socket_timeout": budget,
                "progress_hooks": [_timeout_hook(time.monotonic() + budget, budget)],
                "format": "bestaudio/best",
                "outtmpl": source,
                "max_filesize": int(os.getenv("WHISPER_MAX_BYTES", str(100 * 1024 * 1024))),
            })
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


def _resolve_source_file(
    spec: ClipSpec,
    out_dir: Path,
    source_lookup: Callable[[str], Source | None] | None,
    storage,
) -> tuple[Path, Path | None, Source]:
    if spec.source_id is None or source_lookup is None:
        raise ValueError("An upload source resolver is required.")
    source = source_lookup(spec.source_id)
    if (
        source is None
        or source.source_type is not SourceType.upload
        or source.status != "ready"
        or source.expires_at <= datetime.now(UTC)
    ):
        raise ValueError("The upload source is unavailable or expired.")
    if storage is None:
        path = Path(source.storage_key)
        if not path.is_file():
            raise ValueError("The upload source file is unavailable.")
        return path, None, source
    suffix = Path(source.storage_key).suffix or ".mp4"
    temporary = out_dir / f"source-input{suffix}"
    storage.download(source.storage_key, temporary)
    return temporary, temporary, source


def _cut_ranges(
    specs: tuple[ClipSpec, ...],
    source: Path,
    out_dir: Path,
    deadline: float,
    budget: float,
    title: str,
) -> list[Clip]:
    out_dir.mkdir(parents=True, exist_ok=True)
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
        outputs.append(Clip(target, title))
    return outputs


def download_clip(
    spec: ClipSpec,
    out_dir: Path,
    source_lookup: Callable[[str], Source | None] | None = None,
    storage=None,
    progress: ProgressCallback | None = None,
) -> Clip:
    """Download only the requested range, in the requested quality, as one MP4."""
    budget = media_timeout_seconds(spec.end - spec.start)
    deadline = time.monotonic() + budget
    if spec.source_id is not None:
        source_path, temporary, source = _resolve_source_file(spec, out_dir, source_lookup, storage)
        try:
            return _cut_ranges((spec,), source_path, out_dir, deadline, budget, source.title)[0]
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    timeout_hook = _timeout_hook(deadline, budget)
    def progress_hook(status: dict) -> None:
        timeout_hook(status)
        if progress is None:
            return
        if status.get("status") == "downloading":
            total = status.get("total_bytes") or status.get("total_bytes_estimate")
            downloaded = status.get("downloaded_bytes")
            if total and downloaded is not None:
                progress("downloading", 10 + round(60 * min(1, downloaded / total)))
            else:
                progress("downloading", None)
        elif status.get("status") == "finished":
            progress("merging", None)

    opts = _youtube_options({
        "socket_timeout": budget,
        "progress_hooks": [progress_hook],
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
    })
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


def download_export(
    specs: tuple[ClipSpec, ...],
    out_dir: Path,
    source_lookup: Callable[[str], Source | None] | None = None,
    storage=None,
) -> list[Clip]:
    """Download one source and cut all ranges from it.

    Multi-range exports deliberately share the source download.  Fast cuts use
    stream copy; exact cuts are encoded with the same bounded settings as clips.
    """
    first = specs[0]
    if first.source_id is not None and any(spec.source_id != first.source_id for spec in specs):
        raise ValueError("All export ranges must use the same source.")
    source_dir = out_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    budget = media_timeout_seconds(sum(spec.end - spec.start for spec in specs))
    deadline = time.monotonic() + budget
    if first.source_id is not None:
        source_path, temporary, source = _resolve_source_file(
            first, source_dir, source_lookup, storage
        )
        try:
            return _cut_ranges(specs, source_path, out_dir, deadline, budget, source.title)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    timeout_hook = _timeout_hook(deadline, budget)
    opts = _youtube_options({
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
    })
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(first.url, download=True)
        source = Path(ydl.prepare_filename(info))
        if source.suffix != ".mp4":
            candidate = source.with_suffix(".mp4")
            if candidate.exists():
                source = candidate
    if source.suffix != ".mp4":
        candidate = source.with_suffix(".mp4")
        if candidate.exists():
            source = candidate
    _create_source(info, source, first.url)
    return _cut_ranges(specs, source, out_dir, deadline, budget, info["title"])
