import time
from collections import namedtuple

import pytest

from app.formats import Quality
from app.jobs import JobManager, JobStatus
from app.media import Clip, Mode, make_clip_spec
from app.resources import ResourceAdmissionError, ResourceGuard

SPEC = make_clip_spec(
    url="https://youtu.be/x",
    source_id=None,
    start=0,
    end=10,
    quality=Quality(1080),
    mode=Mode.FAST,
)
Usage = namedtuple("Usage", "total used free")


def test_reservation_passes_with_enough_disk(tmp_path):
    guard = ResourceGuard(temp_budget_bytes=100, min_free_bytes=10)
    reservation = guard.reserve("job", tmp_path, estimated_bytes=20, heavy=False, memory_mb=0)
    assert reservation.bytes_reserved == 20
    assert guard.reserved_bytes == 20


def test_reservation_rejects_insufficient_disk(tmp_path, monkeypatch):
    monkeypatch.setattr("app.resources.shutil.disk_usage", lambda path: Usage(100, 95, 5))
    guard = ResourceGuard(temp_budget_bytes=100, min_free_bytes=10)
    with pytest.raises(ResourceAdmissionError, match="insufficient disk"):
        guard.reserve("job", tmp_path, estimated_bytes=1, heavy=False, memory_mb=0)


def test_reservation_rejects_budget_exhaustion(tmp_path):
    guard = ResourceGuard(temp_budget_bytes=10, min_free_bytes=0)
    guard.reserve("first", tmp_path, estimated_bytes=10, heavy=False, memory_mb=0)
    with pytest.raises(ResourceAdmissionError, match="temporary storage"):
        guard.reserve("second", tmp_path, estimated_bytes=1, heavy=False, memory_mb=0)


def test_job_rejection_does_not_create_an_orphan_directory(tmp_path):
    guard = ResourceGuard(temp_budget_bytes=1, min_free_bytes=0)
    manager = JobManager(
        lambda spec, out: Clip(out / "clip.mp4", "title"),
        root=tmp_path,
        resources=guard,
    )
    with pytest.raises(ResourceAdmissionError):
        manager.submit(SPEC, "client")
    assert not list(tmp_path.iterdir())


def test_job_reservation_is_released_after_failure(tmp_path):
    guard = ResourceGuard(temp_budget_bytes=1024**3, min_free_bytes=0)
    manager = JobManager(
        lambda spec, out: (_ for _ in ()).throw(RuntimeError("failure")),
        root=tmp_path,
        resources=guard,
    )
    job = manager.submit(SPEC, "client")
    for _ in range(100):
        if job.status is JobStatus.FAILED:
            break
        time.sleep(0.01)
    assert job.status is JobStatus.FAILED
    assert guard.reserved_bytes == 0
