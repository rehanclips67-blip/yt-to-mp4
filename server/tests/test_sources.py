import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import main
from app.db import create_db_and_tables, make_engine
from app.repository import JobRepository


def test_local_source_upload_requires_id_streams_bytes_and_completes(tmp_path, monkeypatch):
    repository = JobRepository(make_engine(f"sqlite:///{tmp_path / 'sources.db'}"))
    create_db_and_tables(repository.engine)
    monkeypatch.setattr(main, "source_repository", repository)
    monkeypatch.setattr(main, "configured_storage", None)
    monkeypatch.setattr(main, "UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 10)
    monkeypatch.setattr(
        main,
        "_probe_source",
        lambda path: {
            "duration_seconds": 12.5,
            "width": 1920,
            "height": 1080,
            "fps": 30.0,
            "codec": "h264",
        },
    )
    client = TestClient(main.app)

    created = client.post(
        "/api/sources/presign",
        json={"filename": "demo.mp4", "content_type": "video/mp4"},
    )
    assert created.status_code == 201
    source_id = created.json()["id"]
    assert created.json()["upload_url"].endswith(f"/api/sources/{source_id}/upload")
    assert client.get(f"/api/sources/{source_id}").json()["status"] == "pending"

    upload = client.put(f"/api/sources/{source_id}/upload", content=b"12345")
    assert upload.status_code == 204
    assert client.get(f"/api/sources/{source_id}").json()["status"] == "pending"

    complete = client.post(f"/api/sources/{source_id}/complete")
    assert complete.status_code == 200
    assert complete.json()["status"] == "ready"
    assert complete.json()["duration_seconds"] == 12.5
    assert client.get(f"/api/sources/{source_id}").json()["title"] == "demo"


def test_local_source_upload_enforces_actual_stream_size_and_cleans_pending(tmp_path, monkeypatch):
    repository = JobRepository(make_engine(f"sqlite:///{tmp_path / 'sources.db'}"))
    create_db_and_tables(repository.engine)
    monkeypatch.setattr(main, "source_repository", repository)
    monkeypatch.setattr(main, "configured_storage", None)
    monkeypatch.setattr(main, "UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 3)
    client = TestClient(main.app)

    source_id = client.post(
        "/api/sources/presign",
        json={"filename": "demo.webm", "content_type": "video/webm"},
    ).json()["id"]
    response = client.put(f"/api/sources/{source_id}/upload", content=b"1234")

    assert response.status_code == 413
    assert client.get(f"/api/sources/{source_id}").status_code == 404
    assert not list((tmp_path / "uploads").glob("*"))


def test_source_upload_rejects_disallowed_content_type(tmp_path, monkeypatch):
    repository = JobRepository(make_engine(f"sqlite:///{tmp_path / 'sources.db'}"))
    create_db_and_tables(repository.engine)
    monkeypatch.setattr(main, "source_repository", repository)
    client = TestClient(main.app)
    response = client.post(
        "/api/sources/presign",
        json={"filename": "demo.txt", "content_type": "text/plain"},
    )
    assert response.status_code == 415


def test_source_complete_rejects_expired_pending_source(tmp_path, monkeypatch):
    repository = JobRepository(make_engine(f"sqlite:///{tmp_path / 'sources.db'}"))
    create_db_and_tables(repository.engine)
    monkeypatch.setattr(main, "source_repository", repository)
    expired = datetime.now(UTC) - timedelta(seconds=1)
    repository.add_source(
        main.Source(
            id="expired",
            source_type=main.SourceType.upload,
            storage_key=str(tmp_path / "expired.mp4"),
            title="expired",
            duration_seconds=0,
            status="pending",
            created_at=expired,
            expires_at=expired,
        )
    )

    assert TestClient(main.app).post("/api/sources/expired/complete").status_code == 404


def test_probe_source_uses_bounded_argument_list(monkeypatch, tmp_path):
    seen = {}

    class Result:
        stdout = (
            '{"format":{"duration":"2.5"},'
            '"streams":[{"codec_type":"video","width":640,"height":360,'
            '"r_frame_rate":"30/1","codec_name":"h264"}]}'
        )

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return Result()

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    assert main._probe_source(tmp_path / "clip.mp4")["duration_seconds"] == 2.5
    assert seen["args"][0] == "ffprobe"
    assert seen["kwargs"]["shell"] is False
    assert seen["kwargs"]["timeout"] == 30


@pytest.mark.parametrize(
    "failure",
    [OSError("ffprobe failed"), subprocess.TimeoutExpired("ffprobe", 30)],
)
def test_source_complete_cleans_file_and_marks_failed_on_unexpected_probe_error(
    tmp_path, monkeypatch, failure
):
    repository = JobRepository(make_engine(f"sqlite:///{tmp_path / 'sources.db'}"))
    create_db_and_tables(repository.engine)
    monkeypatch.setattr(main, "source_repository", repository)
    monkeypatch.setattr(main, "configured_storage", None)
    monkeypatch.setattr(main, "UPLOAD_ROOT", tmp_path / "uploads")

    def fail_ffprobe(*args, **kwargs):
        raise failure

    monkeypatch.setattr(main.subprocess, "run", fail_ffprobe)
    client = TestClient(main.app)

    created = client.post(
        "/api/sources/presign",
        json={"filename": "broken.mp4", "content_type": "video/mp4"},
    )
    source_id = created.json()["id"]
    assert client.put(f"/api/sources/{source_id}/upload", content=b"video").status_code == 204
    storage_path = next((tmp_path / "uploads").glob(f"{source_id}.*"))

    response = client.post(f"/api/sources/{source_id}/complete")

    assert response.status_code == 422
    assert not storage_path.exists()
    source = repository.get_source_by_id(source_id)
    assert source is not None
    assert source.status == "failed"
