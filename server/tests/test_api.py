import time
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app import main
from app.db import create_db_and_tables, make_engine
from app.jobs import JobManager, TranscriptJobManager
from app.media import Clip, PrimaryUrlError
from app.models import Source, SourceType
from app.rate_limit import RateLimitDecision
from app.repository import JobRepository

URL = "https://www.youtube.com/watch?v=abc"
CLIP = {"url": URL, "start": 5, "end": 12.5, "res": 1080}


@pytest.fixture
def client(tmp_path, monkeypatch):
    def runner(spec, out_dir):
        path = out_dir / "clip.mp4"
        path.write_bytes(b"mp4-bytes")
        return Clip(path, 'My: "Title"')

    monkeypatch.setattr(main, "jobs", JobManager(runner, root=tmp_path / "jobs"))
    monkeypatch.setattr(main, "validate_primary_url", lambda url: None)
    return TestClient(main.app)


def finish(client, job_id):
    for _ in range(300):
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(0.01)
    raise AssertionError("job never finished")


def test_info_returns_quality_menu_with_limits(client, monkeypatch):
    raw = {
        "id": "abc",
        "title": "Demo",
        "duration": 90,
        "thumbnail": None,
        "formats": [
            {"vcodec": "vp9", "width": 3840, "height": 2160, "fps": 60, "tbr": 12000},
            {"vcodec": "vp9", "width": 1920, "height": 1080, "fps": 30, "tbr": 3000},
            {"vcodec": "none", "acodec": "opus", "abr": 128},
        ],
    }
    monkeypatch.setattr(main, "fetch_info", lambda url: raw)
    qualities = client.post("/api/info", json={"url": URL}).json()["qualities"]
    assert [(q["label"], q["max_seconds"], q["kbps"]) for q in qualities] == [
        ("4K (2160p)", 600, 12128),
        ("1080p", 3600, 3128),
    ]
    assert qualities[0]["fps"] == 60
    assert qualities[0]["codec"] == "vp9"
    assert qualities[0]["container"] is None
    assert qualities[0]["has_audio"] is False
    assert qualities[0]["audio_available"] is True


def test_transcript_reports_unavailable_explicitly(client, monkeypatch):
    monkeypatch.setattr(main, "fetch_transcript", lambda url: None)
    assert client.post("/api/transcript", json={"url": URL}).json() == {
        "available": False,
        "reason": "captions_unavailable",
        "segments": [],
    }


def test_transcript_returns_timestamped_segments(client, monkeypatch):
    monkeypatch.setattr(
        main,
        "fetch_transcript",
        lambda url: [{"start": 1.5, "end": 3, "text": "Hello", "words": None}],
    )
    assert client.post("/api/transcript", json={"url": URL}).json() == {
        "available": True,
        "reason": None,
        "segments": [{"start": 1.5, "end": 3, "text": "Hello", "words": None}],
    }


def test_transcript_whisper_fallback_is_opt_in(client, monkeypatch):
    monkeypatch.setattr(main, "fetch_transcript", lambda url: None)
    monkeypatch.setattr(
        main,
        "fetch_whisper_transcript",
        lambda url: [{"start": 0, "end": 1, "text": "Fallback"}],
    )
    assert client.post("/api/transcript", json={"url": URL}).json()["reason"] == (
        "captions_unavailable"
    )
    response = client.post(
        "/api/transcript",
        json={"url": URL, "fallback_whisper": True},
    )
    assert response.json()["available"] is True


def test_transcript_reports_whisper_unavailable(client, monkeypatch):
    monkeypatch.setattr(main, "fetch_transcript", lambda url: None)
    monkeypatch.setattr(main, "fetch_whisper_transcript", lambda url: None)
    response = client.post(
        "/api/transcript",
        json={"url": URL, "fallback_whisper": True},
    )
    assert response.json()["reason"] == "whisper_unavailable"


def test_info_rejects_live_streams(client, monkeypatch):
    monkeypatch.setattr(main, "fetch_info", lambda url: {"id": "x", "title": "Live"})
    assert client.post("/api/info", json={"url": URL}).status_code == 422


def test_info_rejects_non_youtube_urls(client):
    assert client.post("/api/info", json={"url": "file:///etc/passwd"}).status_code == 422


def test_info_preflight_rejects_before_fetch_info(client, monkeypatch):
    called = False

    def fail_fetch(url):
        nonlocal called
        called = True
        raise AssertionError("fetch_info must not run after URL preflight failure")

    monkeypatch.setattr(
        main,
        "validate_primary_url",
        lambda url: (_ for _ in ()).throw(
            PrimaryUrlError("YouTube host resolves to a non-public IP address.")
        ),
    )
    monkeypatch.setattr(main, "fetch_info", fail_fetch)
    response = client.post("/api/info", json={"url": URL})
    assert response.status_code == 422
    assert "non-public" in response.json()["detail"]
    assert called is False


def test_clip_submission_rate_limit_returns_retry_after(client, monkeypatch):
    class Limited:
        def check_and_consume(self, client):
            return RateLimitDecision(False, 17, "client")

    monkeypatch.setattr(
        main,
        "submission_limiter",
        Limited(),
    )
    response = client.post("/api/jobs", json=CLIP)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "17"
    assert "client" in response.json()["detail"]


def test_export_submission_global_rate_limit_returns_retry_after(client, monkeypatch):
    class Limited:
        def check_and_consume(self, client):
            return RateLimitDecision(False, 23, "global")

    monkeypatch.setattr(
        main,
        "submission_limiter",
        Limited(),
    )
    response = client.post(
        "/api/exports",
        json={"url": URL, "res": 720, "ranges": [{"start": 1, "end": 2}]},
    )
    assert response.status_code == 429
    assert response.headers["retry-after"] == "23"
    assert "server" in response.json()["detail"]


def test_security_headers_are_present(client):
    response = client.get("/api/jobs/nope")
    assert response.status_code == 404
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
    )
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["strict-transport-security"].startswith("max-age=")


def test_docs_csp_allows_only_required_swagger_resources(client):
    response = client.get("/docs")
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "img-src 'self' data: https://fastapi.tiangolo.com; "
        "connect-src 'self'"
    )


def test_job_lifecycle_ends_in_a_downloadable_file(client):
    created = client.post("/api/jobs", json=CLIP)
    assert created.status_code == 202
    done = finish(client, created.json()["id"])

    assert done["status"] == "done"
    assert done["size_bytes"] == 9
    assert done["expires_in"] > 0
    res = client.get(f"/api/jobs/{done['id']}/file")
    assert res.content == b"mp4-bytes"
    assert "My Title (5s-12.5s).mp4" in unquote(res.headers["content-disposition"])


def test_file_is_not_served_before_the_job_is_done(client, monkeypatch):
    monkeypatch.setattr(
        main, "jobs", JobManager(lambda spec, out: time.sleep(0.3), root=main.jobs.root)
    )
    job_id = client.post("/api/jobs", json=CLIP).json()["id"]
    assert client.get(f"/api/jobs/{job_id}/file").status_code == 409


def test_unknown_job_is_a_404(client):
    assert client.get("/api/jobs/nope").status_code == 404
    assert client.get("/api/jobs/nope/file").status_code == 404


def test_clip_longer_than_the_quality_allows_is_rejected(client):
    res = client.post("/api/jobs", json={**CLIP, "res": 2160, "start": 0, "end": 601})
    assert res.status_code == 422
    assert "up to 10 minutes" in res.json()["detail"][0]["msg"]


def test_job_end_beyond_source_duration_is_rejected(client, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "fetch_info", lambda url: {"duration": 10})
    monkeypatch.setattr(
        main,
        "jobs",
        JobManager(
            lambda spec, out_dir: time.sleep(1),
            root=tmp_path / "jobs",
            metadata_fetcher=main.fetch_info,
        ),
    )
    response = client.post("/api/jobs", json={**CLIP, "end": 11})
    assert response.status_code == 422
    assert "exceeds the source duration" in response.json()["detail"]


def test_upload_job_end_beyond_source_duration_is_rejected(client, monkeypatch, tmp_path):
    db_url = f"sqlite:///{tmp_path / 'sources.db'}"
    repository = JobRepository(make_engine(db_url))
    create_db_and_tables(repository.engine)
    now = datetime.now(UTC)
    repository.add_source(
        Source(
            id="upload-source",
            source_type=SourceType.upload,
            storage_key=str(tmp_path / "upload.mp4"),
            title="Uploaded source",
            duration_seconds=10,
            status="ready",
            created_at=now,
            expires_at=now + timedelta(minutes=5),
        )
    )
    monkeypatch.setattr(main, "source_repository", repository)
    monkeypatch.setattr(
        main,
        "jobs",
        JobManager(
            lambda spec, out_dir: time.sleep(1),
            root=tmp_path / "jobs",
            db_url=db_url,
        ),
    )

    response = client.post(
        "/api/jobs",
        json={"source_id": "upload-source", "start": 0, "end": 11, "res": 720},
    )

    assert response.status_code == 422
    assert "exceeds the source duration" in response.json()["detail"]


def test_whisper_fallback_is_an_async_job(client, monkeypatch):
    monkeypatch.setattr(main, "fetch_transcript", lambda url: None)
    monkeypatch.setattr(main, "whisper_is_configured", lambda: True)
    monkeypatch.setattr(
        main,
        "transcript_jobs",
        TranscriptJobManager(lambda url: [{"start": 0, "end": 1, "text": "Async"}]),
    )
    response = client.post(
        "/api/transcript",
        json={"url": URL, "fallback_whisper": True},
    )
    assert response.status_code == 202
    job_id = response.json()["id"]
    for _ in range(100):
        result = client.get(f"/api/transcript/jobs/{job_id}").json()
        if result["status"] == "done":
            assert result["segments"][0]["text"] == "Async"
            break
        time.sleep(0.01)
    else:
        raise AssertionError("transcription job did not finish")
