"""End-to-end check of the clip pipeline against a local synthetic video (no network needed)."""

import functools
import http.server
import json
import shutil
import subprocess
import threading
import time

import pytest

import app.media as media
from app.formats import Quality
from app.jobs import JobManager, JobStatus
from app.media import (
    CaptionFetchError,
    MediaTimeoutError,
    Mode,
    PrimaryUrlError,
    _CaptionRedirectHandler,
    _fetch_caption_payload,
    _max_download_bytes,
    _run_ffmpeg,
    _timeout_hook,
    download_clip,
    fetch_info,
    make_clip_spec,
    media_timeout_seconds,
    validate_primary_url,
)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Sign in to confirm you're not a bot", "bot_check"),
        ("JavaScript challenge failed", "js_challenge"),
        ("certificate verify failed: SSL", "ssl"),
        ("This is a private video; sign in", "age_or_login"),
        ("Video unavailable in your region", "region_or_unavailable"),
        ("HTTP Error 429: Too Many Requests", "network"),
        ("unexpected extractor failure", "other"),
    ],
)
def test_youtube_error_categories(message, expected):
    assert media._youtube_error_category(Exception(message)) == expected


def test_sanitize_log_message_removes_url_credentials_and_paths():
    message = media._sanitize_log_message(
        "https://example.test/video?token=secret C:\\private\\cookie.txt "
        "Authorization: Bearer secret"
    )
    assert "example.test" not in message
    assert "secret" not in message
    assert "private" not in message


def test_fetch_info_retries_youtube_verification_with_fallback_client(monkeypatch):
    media._clear_info_cache()
    calls = []

    class FakeYoutubeDL:
        def __init__(self, options):
            calls.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            if len(calls) == 1:
                raise media.YoutubeDLError("Sign in to confirm you're not a bot")
            return {"id": "video", "duration": 10}

    monkeypatch.setattr(media, "YoutubeDL", FakeYoutubeDL)
    assert fetch_info("https://www.youtube.com/watch?v=video")["id"] == "video"
    assert calls[1]["extractor_args"] == {"youtube": {"player_client": ["web_safari"]}}
    assert calls[0]["js_runtimes"] == {"node": {}}


def test_fetch_info_does_not_fallback_on_rate_limit(monkeypatch):
    media._clear_info_cache()
    calls = []

    class FakeYoutubeDL:
        def __init__(self, options):
            calls.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            raise media.YoutubeDLError("HTTP Error 429: Too Many Requests")

    monkeypatch.setattr(media, "YoutubeDL", FakeYoutubeDL)
    with pytest.raises(media.YoutubeDLError, match="429"):
        media.fetch_info("https://www.youtube.com/watch?v=rate-limit")
    assert len(calls) == 1


def test_fetch_info_does_not_retry_bot_check_before_fallback(monkeypatch):
    media._clear_info_cache()
    calls = []

    class FakeYoutubeDL:
        def __init__(self, options):
            calls.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            raise media.YoutubeDLError("Sign in to confirm you're not a bot")

    monkeypatch.setattr(media, "YoutubeDL", FakeYoutubeDL)
    with pytest.raises(media.YoutubeDLError):
        media.fetch_info("https://www.youtube.com/watch?v=retried")
    assert len(calls) == 3
    assert calls[1]["extractor_args"] == {"youtube": {"player_client": ["web_safari"]}}
    assert calls[2]["extractor_args"] == {"youtube": {"player_client": ["android_vr"]}}


def test_download_does_not_retry_bot_check(monkeypatch, tmp_path):
    calls = 0

    class FakeYoutubeDL:
        def __init__(self, options):
            nonlocal calls
            calls += 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            raise media.YoutubeDLError("Sign in to confirm you're not a bot")

    monkeypatch.setattr(media, "YoutubeDL", FakeYoutubeDL)
    spec = make_clip_spec(
        url="https://www.youtube.com/watch?v=download-verification",
        source_id=None,
        start=0,
        end=5,
        quality=Quality(720),
        mode=Mode.FAST,
    )
    with pytest.raises(media.YoutubeDLError):
        media.download_clip(spec, tmp_path)
    assert calls == 1


def test_fetch_info_shares_in_flight_extraction(monkeypatch):
    media._clear_info_cache()
    calls = 0
    lock = threading.Lock()

    class FakeYoutubeDL:
        def __init__(self, options):
            nonlocal calls
            with lock:
                calls += 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            time.sleep(0.05)
            return {"id": "shared", "duration": 10}

    monkeypatch.setattr(media, "YoutubeDL", FakeYoutubeDL)
    results = []
    threads = [
        threading.Thread(
            target=lambda: results.append(
                media.fetch_info("https://youtu.be/shared?si=tracking")
            )
        )
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert calls == 1
    assert [result["id"] for result in results] == ["shared", "shared"]


def test_fetch_info_reuses_successful_metadata_within_ttl(monkeypatch):
    media._clear_info_cache()
    calls = 0

    class FakeYoutubeDL:
        def __init__(self, options):
            nonlocal calls
            calls += 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            return {"id": "cached", "duration": 10}

    monkeypatch.setattr(media, "YoutubeDL", FakeYoutubeDL)
    assert media.fetch_info("https://www.youtube.com/watch?v=cached")["id"] == "cached"
    assert media.fetch_info("https://youtu.be/cached")["id"] == "cached"
    assert calls == 1


def test_fetch_info_does_not_retain_failed_metadata(monkeypatch):
    media._clear_info_cache()
    calls = 0

    class FakeYoutubeDL:
        def __init__(self, options):
            nonlocal calls
            calls += 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            raise media.YoutubeDLError("temporary extraction failure")

    monkeypatch.setattr(media, "YoutubeDL", FakeYoutubeDL)
    for _ in range(2):
        with pytest.raises(media.YoutubeDLError):
            media.fetch_info("https://www.youtube.com/watch?v=failed")
    assert calls == 2
    assert not media._info_cache


def test_fetch_info_cache_is_bounded_and_separates_urls(monkeypatch):
    media._clear_info_cache()
    monkeypatch.setattr(media, "_INFO_CACHE_MAX_ENTRIES", 2)
    calls = 0

    class FakeYoutubeDL:
        def __init__(self, options):
            nonlocal calls
            calls += 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            return {"id": url.rsplit("=", 1)[-1], "duration": 10}

    monkeypatch.setattr(media, "YoutubeDL", FakeYoutubeDL)
    media.fetch_info("https://www.youtube.com/watch?v=one")
    media.fetch_info("https://www.youtube.com/watch?v=two")
    media.fetch_info("https://www.youtube.com/watch?v=three")
    assert len(media._info_cache) <= 2
    assert media.fetch_info("https://www.youtube.com/watch?v=two")["id"] == "two"
    assert media.fetch_info("https://www.youtube.com/watch?v=different")["id"] == "different"
    assert calls == 4


def test_download_clip_reports_byte_progress(monkeypatch, tmp_path):
    options = {}

    class FakeYoutubeDL:
        def __init__(self, received):
            options.update(received)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            options["progress_hooks"][0](
                {"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100}
            )
            options["progress_hooks"][0]({"status": "finished"})
            return {
                "title": "video",
                "requested_downloads": [{"filepath": str(tmp_path / "clip.mp4")}],
            }

    monkeypatch.setattr(media, "YoutubeDL", FakeYoutubeDL)
    progress = []
    spec = make_clip_spec(
        url="https://www.youtube.com/watch?v=video",
        source_id=None,
        start=0,
        end=5,
        quality=Quality(720),
        mode=Mode.FAST,
    )
    result = media.download_clip(
        spec,
        tmp_path,
        progress=lambda phase, percent: progress.append((phase, percent)),
    )

    assert result.title == "video"
    assert progress == [("downloading", 40), ("merging", None)]


def test_youtube_environment_diagnostics_excludes_runtime_paths(monkeypatch):
    monkeypatch.setattr(media, "_package_version", lambda name: "test-version")
    monkeypatch.setattr(
        media,
        "_runtime_version",
        lambda command, args: "v24.17.0" if command == "node" else None,
    )

    diagnostics = media.youtube_environment_diagnostics()

    assert diagnostics == {
        "yt_dlp_version": "test-version",
        "yt_dlp_ejs_version": "test-version",
        "js_runtimes": {"node": "v24.17.0"},
        "configured_js_runtime": "node",
        "configured_player_clients": ("web_safari", "android_vr"),
    }


@pytest.fixture(scope="module")
def video_url(tmp_path_factory):
    root = tmp_path_factory.mktemp("site")
    subprocess.run(
        [
            *("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30"),
            *("-f", "lavfi", "-i", "sine=frequency=440", "-t", "8"),
            *("-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "60", "-c:a", "aac"),
            *("-movflags", "+faststart", str(root / "source.mp4")),
        ],
        check=True,
    )
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *args: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.handle_error = lambda *args: None  # ffmpeg drops the connection once it has its range
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/source.mp4"
    server.shutdown()


def probe(path):
    out = subprocess.run(
        [
            *("ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_name"),
            *("-of", "json", str(path)),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    data = json.loads(out)
    return float(data["format"]["duration"]), {s["codec_name"] for s in data["streams"]}


def test_exact_mode_cuts_on_the_requested_frames(video_url, tmp_path):
    clip = download_clip(
        make_clip_spec(
            url=video_url,
            source_id=None,
            start=1.5,
            end=4.0,
            quality=Quality(720),
            mode=Mode.EXACT,
        ),
        tmp_path,
    )
    duration, codecs = probe(clip.path)
    assert clip.path.suffix == ".mp4"
    assert duration == pytest.approx(2.5, abs=0.1)
    assert codecs == {"h264", "aac"}


def test_fast_mode_returns_a_playable_clip(video_url, tmp_path):
    clip = download_clip(
        make_clip_spec(
            url=video_url,
            source_id=None,
            start=1.5,
            end=4.0,
            quality=Quality(720),
            mode=Mode.FAST,
        ),
        tmp_path,
    )
    duration, _ = probe(clip.path)
    assert 2.5 <= duration <= 5.5  # snaps back to the previous keyframe


def test_job_manager_runs_the_real_pipeline(video_url, tmp_path):
    manager = JobManager(download_clip, workers=1, root=tmp_path / "jobs")
    job = manager.submit(
        make_clip_spec(
            url=video_url,
            source_id=None,
            start=2.0,
            end=5.0,
            quality=Quality(720),
            mode=Mode.EXACT,
        ),
        "test",
    )
    while job.status in (JobStatus.QUEUED, JobStatus.WORKING):
        time.sleep(0.05)
    assert job.status is JobStatus.DONE
    assert probe(job.clip.path)[0] == pytest.approx(3.0, abs=0.1)
    assert manager.estimate(job.spec) > 0


def test_media_timeout_scales_with_duration_and_is_bounded(monkeypatch):
    for name in (
        "MEDIA_TIMEOUT_BASE_SECONDS",
        "MEDIA_TIMEOUT_PER_MINUTE_SECONDS",
        "MEDIA_TIMEOUT_MIN_SECONDS",
        "MEDIA_TIMEOUT_MAX_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    assert media_timeout_seconds(60 * 60) == 3900

    monkeypatch.setenv("MEDIA_TIMEOUT_BASE_SECONDS", "30")
    monkeypatch.setenv("MEDIA_TIMEOUT_PER_MINUTE_SECONDS", "60")
    monkeypatch.setenv("MEDIA_TIMEOUT_MIN_SECONDS", "45")
    monkeypatch.setenv("MEDIA_TIMEOUT_MAX_SECONDS", "3600")
    assert media_timeout_seconds(5) == 45
    assert media_timeout_seconds(60 * 60) == 3600
    assert media_timeout_seconds(60 * 60 * 100) == 3600


def test_source_download_limit_defaults_to_four_gib_and_remains_configurable(monkeypatch):
    monkeypatch.delenv("MEDIA_MAX_DOWNLOAD_BYTES", raising=False)
    assert _max_download_bytes() == 4 * 1024**3

    monkeypatch.setenv("MEDIA_MAX_DOWNLOAD_BYTES", "123456789")
    assert _max_download_bytes() == 123456789


def test_timeout_hook_fails_fast_for_a_hung_short_operation():
    with pytest.raises(MediaTimeoutError, match="45-second timeout"):
        _timeout_hook(time.monotonic() - 1, 45)({})


def test_caption_url_requires_allowed_https_public_host(monkeypatch):
    class Response:
        headers = {}
        remaining = True

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, size=-1):
            if self.remaining:
                self.remaining = False
                return b"WEBVTT"
            return b""

    class Opener:
        def open(self, request, timeout):
            return Response()

    monkeypatch.setattr(
        "app.media.socket.getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 0))],
    )
    monkeypatch.setattr("app.media.build_opener", lambda *handlers: Opener())
    _fetch_caption_payload("https://www.youtube.com/api/timedtext")
    with pytest.raises(CaptionFetchError, match="allowed HTTPS"):
        _fetch_caption_payload("http://www.youtube.com/api/timedtext")
    with pytest.raises(CaptionFetchError, match="allowed HTTPS"):
        _fetch_caption_payload("https://attacker.example/captions")


def test_caption_url_rejects_private_dns_resolution(monkeypatch):
    monkeypatch.setattr(
        "app.media.socket.getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("127.0.0.1", 0))],
    )
    with pytest.raises(CaptionFetchError, match="non-public"):
        _fetch_caption_payload("https://www.youtube.com/api/timedtext")


def test_primary_url_validation_allows_public_and_rejects_private_or_dns_failure(monkeypatch):
    monkeypatch.setattr(
        "app.media.socket.getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 0))],
    )
    validate_primary_url("https://www.youtube.com/watch?v=abc")
    monkeypatch.setattr(
        "app.media.socket.getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("10.0.0.1", 0))],
    )
    with pytest.raises(PrimaryUrlError, match="non-public"):
        validate_primary_url("https://www.youtube.com/watch?v=abc")
    monkeypatch.setattr(
        "app.media.socket.getaddrinfo",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("dns")),
    )
    with pytest.raises(PrimaryUrlError, match="could not be resolved"):
        validate_primary_url("https://www.youtube.com/watch?v=abc")


def test_caption_redirect_validates_target():
    handler = _CaptionRedirectHandler()
    with pytest.raises(CaptionFetchError, match="allowed HTTPS"):
        handler.redirect_request(
            type("Request", (), {"full_url": "https://www.youtube.com/captions"})(),
            None,
            302,
            "Found",
            {},
            "https://127.0.0.1/private",
        )


def test_ffmpeg_uses_argument_list_without_shell(monkeypatch):
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs

    monkeypatch.setattr("app.media.subprocess.run", fake_run)
    _run_ffmpeg(["ffmpeg", "-i", "https://example.invalid/$(touch pwned)", "safe.mp4"], 1)
    assert seen["kwargs"]["shell"] is False
    assert seen["args"][0] == "ffmpeg"
