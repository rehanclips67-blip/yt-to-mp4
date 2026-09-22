"""In-memory job queue: a small worker pool runs clips while the API tracks their state."""

import inspect
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from yt_dlp.utils import YoutubeDLError

from .db import create_db_and_tables, make_engine
from .formats import Quality
from .media import Clip, ClipSpec, Mode, source_context
from .models import JobRecord
from .queue import make_queue
from .repository import JobRepository
from .resources import ResourceAdmissionError, ResourceGuard
from .storage import ObjectStorage

log = logging.getLogger("clipper.jobs")

MAX_QUEUED = 20  # jobs waiting for a worker, across all users
MAX_ACTIVE_PER_CLIENT = 2  # queued or running jobs one user may have at once
_SPEED_SMOOTHING = 0.3


class JobStatus(StrEnum):
    QUEUED = "queued"
    WORKING = "working"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


_ACTIVE = {JobStatus.QUEUED, JobStatus.WORKING}


class QueueFull(Exception):
    pass


class TooManyJobs(Exception):
    pass


@dataclass
class Job:
    id: str
    spec: ClipSpec
    client: str
    work_dir: Path
    status: JobStatus = JobStatus.QUEUED
    started: float | None = None
    finished: float | None = None
    error: str | None = None
    clip: Clip | None = None
    phase: str = "queued"
    percent: int = 0
    download_refs: int = 0
    object_key: str | None = None
    result_size: int | None = None
    resource_reserved: bool = False

    @property
    def filename(self) -> str | None:
        if self.clip is None:
            return None
        title = safe_title(self.clip.title)
        return f"{title} ({self.spec.start:g}s-{self.spec.end:g}s){self.clip.path.suffix}"


def safe_title(title: str) -> str:
    """Return a stable, filesystem-safe base name for downloads."""
    title = re.sub(r"[\x00-\x1f\\/:*?\"<>|]", "", title)
    title = re.sub(r"\s+", " ", title).strip().rstrip(".")
    return title[:120] or "clip"


def export_filename(title: str, index: int, start: float, end: float) -> str:
    return f"{safe_title(title)} {index:02d} ({start:g}s-{end:g}s).mp4"


@dataclass
class ExportJob:
    id: str
    specs: tuple[ClipSpec, ...]
    client: str
    work_dir: Path
    status: JobStatus = JobStatus.QUEUED
    started: float | None = None
    finished: float | None = None
    error: str | None = None
    path: Path | None = None
    title: str = "export"
    phase: str = "queued"
    percent: int = 0
    download_refs: int = 0
    object_key: str | None = None
    result_size: int | None = None
    resource_reserved: bool = False

    @property
    def filename(self) -> str:
        return f"{safe_title(self.title)}.zip"


@dataclass
class ExportJobManager:
    runner: Callable[[ClipSpec, Path], Clip]
    export_runner: Callable[[tuple[ClipSpec, ...], Path], list[Clip]] | None = None
    workers: int = 1
    ttl: int = 30 * 60
    root: Path = field(default_factory=lambda: Path(tempfile.gettempdir()) / "clipper-exports")
    db_url: str | None = None
    queue_adapter: str = "thread"
    cleanup_grace: int = 0
    storage: ObjectStorage | None = None
    resources: ResourceGuard | None = None
    estimated_bytes: int = field(
        default_factory=lambda: int(os.getenv("EXPORT_TEMP_RESERVATION_BYTES", str(2 * 1024**3)))
    )
    memory_mb: int = field(
        default_factory=lambda: int(os.getenv("EXPORT_MEMORY_RESERVATION_MB", "1024"))
    )

    def __post_init__(self) -> None:
        self.resources = self.resources or ResourceGuard()
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)
        self._queue = make_queue(self.queue_adapter, self.workers, "export")
        self._jobs: dict[str, ExportJob] = {}
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()
        self._repository = None
        if self.db_url:
            self._engine = make_engine(self.db_url)
            create_db_and_tables(self._engine)
            self._repository = JobRepository(self._engine)

    def submit(self, specs: tuple[ClipSpec, ...], client: str) -> ExportJob:
        with self._lock:
            self._expire()
            active = [j for j in self._jobs.values() if j.status in _ACTIVE]
            if sum(j.client == client for j in active) >= MAX_ACTIVE_PER_CLIENT:
                raise TooManyJobs
            if sum(j.status is JobStatus.QUEUED for j in active) >= MAX_QUEUED:
                raise QueueFull
            job = ExportJob(
                secrets.token_urlsafe(16), specs, client, self.root / secrets.token_hex(8)
            )
            try:
                self.resources.reserve(
                    job.id,
                    self.root,
                    estimated_bytes=self.estimated_bytes,
                    heavy=True,
                    memory_mb=self.memory_mb,
                )
            except ResourceAdmissionError as exc:
                log.warning("export admission rejected for job %s: %s", job.id, exc.reason)
                raise
            job.resource_reserved = True
            job.work_dir.mkdir()
            self._jobs[job.id] = job
            if self._repository:
                self._repository.add(
                    JobRecord(
                        id=job.id,
                        kind="export",
                        spec_json=json.dumps([spec._asdict() for spec in specs], default=str),
                        spec_metadata=json.dumps({"count": len(specs)}),
                        client=client,
                        work_dir=str(job.work_dir),
                        status=job.status.value,
                    )
                )
        self._queue.submit(self._run, job)
        return job

    def get(self, job_id: str) -> ExportJob | None:
        with self._lock:
            self._expire()
            return self._jobs.get(job_id)

    def position(self, job: ExportJob) -> int | None:
        with self._lock:
            queued = [j for j in self._jobs.values() if j.status is JobStatus.QUEUED]
        return queued.index(job) + 1 if job in queued else None

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status not in _ACTIVE:
                return False
            if job.status is JobStatus.QUEUED:
                job.status = JobStatus.CANCELLED
                job.finished = time.time()
                self._release_resources(job)
                shutil.rmtree(job.work_dir, ignore_errors=True)
                self._persist(job)
            else:
                self._cancelled.add(job_id)
            return True

    def acquire_download(self, job_id: str, allow_expired: bool = False) -> ExportJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if (
                not allow_expired
                and job.finished is not None
                and time.time() - job.finished > self.ttl + self.cleanup_grace
            ):
                return None
            if job.status is JobStatus.DONE and job.path is not None and job.path.exists():
                job.download_refs += 1
            return job

    def release_download(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.download_refs = max(0, job.download_refs - 1)

    def _persist(self, job: ExportJob) -> None:
        if self._repository:
            self._repository.update(
                job.id,
                status=job.status.value,
                progress_phase=job.phase,
                progress_percent=job.percent,
                phase=job.phase,
                percent=job.percent,
                error=job.error,
                path=str(job.path) if job.path else None,
                result_url=f"/api/exports/{job.id}/file" if job.status is JobStatus.DONE else None,
                object_key=job.object_key,
                result_size=job.result_size,
                title=job.title,
                started_at=datetime.fromtimestamp(job.started, UTC) if job.started else None,
                finished_at=datetime.fromtimestamp(job.finished, UTC) if job.finished else None,
                expires_at=(
                    datetime.fromtimestamp(job.finished + self.ttl, UTC) if job.finished else None
                ),
            )

    def _run(self, job: ExportJob) -> None:
        job.started = time.time()
        job.status = JobStatus.WORKING
        job.phase, job.percent = "downloading", 5
        self._persist(job)
        try:
            if job.status is JobStatus.CANCELLED or job.id in self._cancelled:
                outcome = JobStatus.CANCELLED
            elif self.export_runner is not None:
                if self._repository:
                    with source_context(self._repository.add_source, self.ttl):
                        clips = self.export_runner(job.specs, job.work_dir)
                else:
                    clips = self.export_runner(job.specs, job.work_dir)
            else:
                clips = []
                for index, spec in enumerate(job.specs):
                    part_dir = job.work_dir / f"part-{index}"
                    part_dir.mkdir()
                    if self._repository:
                        with source_context(self._repository.add_source, self.ttl):
                            clips.append(self.runner(spec, part_dir))
                    else:
                        clips.append(self.runner(spec, part_dir))
                    if job.id in self._cancelled:
                        outcome = JobStatus.CANCELLED
                        break
                    job.phase, job.percent = "cutting", 10 + int(70 * (index + 1) / len(job.specs))
            if "outcome" not in locals():
                job.title = safe_title(clips[0].title)
                job.phase, job.percent = "packaging", 90
                zip_path = job.work_dir / "export.zip"
                with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    for index, (spec, clip) in enumerate(zip(job.specs, clips, strict=True), 1):
                        archive.write(
                            clip.path, export_filename(clip.title, index, spec.start, spec.end)
                        )
                job.path = zip_path
                if self.storage is not None:
                    stored = self.storage.upload(zip_path, f"exports/{job.id}/{job.filename}")
                    job.object_key, job.result_size = stored.key, stored.size
                job.phase, job.percent = "complete", 100
                outcome = JobStatus.DONE
        except TimeoutError as exc:
            log.warning("export timed out: %s", exc)
            job.error = "The media operation timed out. Try a shorter range or lower quality."
            outcome = JobStatus.CANCELLED if job.id in self._cancelled else JobStatus.FAILED
        except Exception:
            log.exception("export failed")
            job.error = "Something went wrong on our side."
            outcome = JobStatus.CANCELLED if job.id in self._cancelled else JobStatus.FAILED
        finally:
            for child in job.work_dir.iterdir():
                if child.name != "export.zip":
                    shutil.rmtree(child, ignore_errors=True)
        job.finished = time.time()
        job.status = outcome
        if outcome is not JobStatus.DONE or self.storage is not None:
            self._release_resources(job)
        self._persist(job)

    def _release_resources(self, job: ExportJob) -> None:
        if job.resource_reserved:
            self.resources.release(job.id)
            job.resource_reserved = False

    def expires_in(self, job: ExportJob) -> int | None:
        if job.finished is None:
            return None
        return max(0, int(job.finished + self.ttl - time.time()))

    def _expire(self) -> None:
        now = time.time()
        for job_id, job in list(self._jobs.items()):
            if (
                job.finished is not None
                and now - job.finished > self.ttl + self.cleanup_grace
                and job.download_refs == 0
            ):
                shutil.rmtree(job.work_dir, ignore_errors=True)
                self._release_resources(job)
                del self._jobs[job_id]
                if self.storage is not None and job.object_key:
                    self.storage.delete(job.object_key)


@dataclass
class TranscriptJob:
    id: str
    url: str
    client: str
    status: JobStatus = JobStatus.QUEUED
    started: float | None = None
    finished: float | None = None
    error: str | None = None
    result: list[dict] | None = None
    phase: str = "queued"
    percent: int = 0


@dataclass
class TranscriptJobManager:
    runner: Callable[[str], list[dict] | None]
    workers: int = 1
    queue_adapter: str = "thread"
    max_concurrent: int = 1

    def __post_init__(self) -> None:
        if self.max_concurrent <= 0:
            raise ValueError("max_concurrent must be positive")
        self._queue = make_queue(self.queue_adapter, self.workers, "transcript")
        self._jobs: dict[str, TranscriptJob] = {}
        self._lock = threading.Lock()
        self._running = threading.BoundedSemaphore(self.max_concurrent)

    def submit(self, url: str, client: str) -> TranscriptJob:
        with self._lock:
            active = [job for job in self._jobs.values() if job.status in _ACTIVE]
            if sum(job.client == client for job in active) >= MAX_ACTIVE_PER_CLIENT:
                raise TooManyJobs
            if sum(job.status is JobStatus.QUEUED for job in active) >= MAX_QUEUED:
                raise QueueFull
            job = TranscriptJob(secrets.token_urlsafe(16), url, client)
            self._jobs[job.id] = job
        self._queue.submit(self._run, job)
        return job

    def get(self, job_id: str) -> TranscriptJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def position(self, job: TranscriptJob) -> int | None:
        with self._lock:
            queued = [item for item in self._jobs.values() if item.status is JobStatus.QUEUED]
        return queued.index(job) + 1 if job in queued else None

    def _run(self, job: TranscriptJob) -> None:
        if not self._running.acquire(blocking=False):
            job.error = "Whisper concurrency limit reached; try again later."
            job.status = JobStatus.FAILED
            job.finished = time.time()
            return
        job.started = time.time()
        job.status = JobStatus.WORKING
        job.phase, job.percent = "transcribing", 10
        try:

            def update(phase: str, percent: int) -> None:
                job.phase, job.percent = phase, max(0, min(99, percent))

            if "progress" in inspect.signature(self.runner).parameters:
                result = self.runner(job.url, progress=update)
            else:
                result = self.runner(job.url)
            if not result:
                raise RuntimeError("Whisper did not return any transcript segments.")
            job.result = result
            job.phase, job.percent = "complete", 100
            job.status = JobStatus.DONE
        except TimeoutError:
            job.error = "The transcription timed out."
            job.status = JobStatus.FAILED
        except Exception as exc:
            log.warning("transcription failed: %s", exc)
            job.error = str(exc) or "Whisper transcription failed."
            job.status = JobStatus.FAILED
        finally:
            job.finished = time.time()
            self._running.release()


@dataclass
class JobManager:
    runner: Callable[[ClipSpec, Path], Clip]
    workers: int = 2
    ttl: int = 30 * 60  # seconds a finished clip stays downloadable
    root: Path = field(default_factory=lambda: Path(tempfile.gettempdir()) / "clipper-jobs")
    db_url: str | None = None
    queue_adapter: str = "thread"
    max_attempts: int = 3
    retry_backoff: float = 0.5
    cleanup_grace: int = 0
    metadata_fetcher: Callable[[str], dict] | None = None
    storage: ObjectStorage | None = None
    resources: ResourceGuard | None = None
    estimated_bytes: int = field(
        default_factory=lambda: int(os.getenv("CLIP_TEMP_RESERVATION_BYTES", str(512 * 1024**2)))
    )
    memory_mb: int = field(
        default_factory=lambda: int(os.getenv("CLIP_MEMORY_RESERVATION_MB", "512"))
    )

    def __post_init__(self) -> None:
        self.resources = self.resources or ResourceGuard()
        if self.db_url is None:
            shutil.rmtree(self.root, ignore_errors=True)  # preserve legacy ephemeral behavior
        self.root.mkdir(parents=True, exist_ok=True)
        self._queue = make_queue(self.queue_adapter, self.workers, "clip")
        self._jobs: dict[str, Job] = {}
        self._cancelled: set[str] = set()
        self._speed: dict[tuple[int, Mode], float] = {}  # seconds of video cut per second of work
        self._lock = threading.Lock()
        self._repository = None
        if self.db_url:
            self._engine = make_engine(self.db_url)
            create_db_and_tables(self._engine)
            self._repository = JobRepository(self._engine)
            self._restore()

    def _spec_json(self, spec: ClipSpec) -> str:
        return json.dumps(
            {
                "url": spec.url,
                "start": spec.start,
                "end": spec.end,
                "res": spec.quality.res,
                "mode": spec.mode.value,
            }
        )

    def _restore(self) -> None:
        for record in self._repository.list("clip"):
            if record.status in {JobStatus.QUEUED, JobStatus.WORKING}:
                record.status = JobStatus.QUEUED
                self._repository.update(record.id, status=JobStatus.QUEUED, started_at=None)
                spec_data = json.loads(record.spec_json)
                spec = ClipSpec(
                    spec_data["url"],
                    spec_data["start"],
                    spec_data["end"],
                    Quality(spec_data["res"]),
                    Mode(spec_data["mode"]),
                )
                job = Job(record.id, spec, record.client, Path(record.work_dir))
                job.phase, job.percent = record.phase, record.percent
                job.work_dir.mkdir(parents=True, exist_ok=True)
                self._jobs[job.id] = job
                self._queue.submit(self._run, job)

    def submit(self, spec: ClipSpec, client: str) -> Job:
        with self._lock:
            self._expire()
            active = [j for j in self._jobs.values() if j.status in _ACTIVE]
            if sum(j.client == client for j in active) >= MAX_ACTIVE_PER_CLIENT:
                raise TooManyJobs
            if sum(j.status is JobStatus.QUEUED for j in active) >= MAX_QUEUED:
                raise QueueFull
            job_id = secrets.token_urlsafe(16)  # doubles as the secret link to the file
            job = Job(job_id, spec, client, self.root / job_id)
            try:
                self.resources.reserve(
                    job.id,
                    self.root,
                    estimated_bytes=self.estimated_bytes,
                    heavy=spec.mode is Mode.EXACT,
                    memory_mb=self.memory_mb if spec.mode is Mode.EXACT else 0,
                )
            except ResourceAdmissionError as exc:
                log.warning("clip admission rejected for job %s: %s", job.id, exc.reason)
                raise
            job.resource_reserved = True
            job.work_dir.mkdir()
            self._jobs[job_id] = job
            if self._repository:
                self._repository.add(
                    JobRecord(
                        id=job.id,
                        kind="clip",
                        spec_json=self._spec_json(spec),
                        client=client,
                        work_dir=str(job.work_dir),
                        status=job.status.value,
                        spec_metadata=self._spec_json(spec),
                        max_attempts=self.max_attempts,
                    )
                )
        self._queue.submit(self._run, job)
        return job

    def validate_duration(self, spec: ClipSpec) -> None:
        if self.metadata_fetcher is None:
            return
        metadata = self.metadata_fetcher(spec.url)
        duration = metadata.get("duration")
        if duration is None:
            raise ValueError("The source video duration is unavailable.")
        if spec.end > float(duration):
            raise ValueError(
                f"The clip end ({spec.end:g}s) exceeds the source duration ({float(duration):g}s)."
            )

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            self._expire()
            return self._jobs.get(job_id)

    def position(self, job: Job) -> int | None:
        """1 means next in line; None once the job has started."""
        with self._lock:
            queued = [j for j in self._jobs.values() if j.status is JobStatus.QUEUED]
        return queued.index(job) + 1 if job in queued else None

    def estimate(self, spec: ClipSpec) -> float | None:
        """Expected working time, learned from earlier jobs of the same quality and mode."""
        speed = self._speed.get((spec.quality.res, spec.mode))
        return (spec.end - spec.start) / speed if speed else None

    def expires_in(self, job: Job) -> int | None:
        if job.finished is None:
            return None
        return max(0, int(job.finished + self.ttl - time.time()))

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status not in _ACTIVE:
                return False
            self._cancelled.add(job_id)
            if job.status is JobStatus.QUEUED:
                job.status = JobStatus.CANCELLED
                job.finished = time.time()
                self._release_resources(job)
                shutil.rmtree(job.work_dir, ignore_errors=True)
                self._persist(job)
            return True

    def acquire_download(self, job_id: str, allow_expired: bool = False) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if (
                not allow_expired
                and job.finished is not None
                and time.time() - job.finished > self.ttl + self.cleanup_grace
            ):
                return None
            if job.status is JobStatus.DONE and job.clip is not None and job.clip.path.exists():
                job.download_refs += 1
            return job

    def release_download(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.download_refs = max(0, job.download_refs - 1)

    def _persist(self, job: Job) -> None:
        if self._repository:
            self._repository.update(
                job.id,
                status=job.status.value,
                phase=job.phase,
                percent=job.percent,
                progress_phase=job.phase,
                progress_percent=job.percent,
                error=job.error,
                result_url=f"/api/jobs/{job.id}/file" if job.status is JobStatus.DONE else None,
                object_key=job.object_key,
                result_size=job.result_size,
                updated_at=datetime.now(UTC),
                expires_at=(
                    datetime.fromtimestamp(job.finished + self.ttl, UTC) if job.finished else None
                ),
                cancellation_requested=job.id in self._cancelled,
                started_at=datetime.fromtimestamp(job.started, UTC) if job.started else None,
                finished_at=datetime.fromtimestamp(job.finished, UTC) if job.finished else None,
                attempts=getattr(job, "attempts", 0),
            )

    def _run(self, job: Job) -> None:
        job.started = time.time()
        job.status = JobStatus.WORKING
        job.phase, job.percent = "downloading", 10
        self._persist(job)
        attempts = 0
        while True:
            if job.id in self._cancelled:
                outcome = JobStatus.CANCELLED
                break
            try:
                if self._repository:
                    with source_context(self._repository.add_source, self.ttl):
                        clip = self.runner(job.spec, job.work_dir)
                else:
                    clip = self.runner(job.spec, job.work_dir)
                job.clip = clip
                if job.id in self._cancelled:
                    shutil.rmtree(job.work_dir, ignore_errors=True)
                    outcome = JobStatus.CANCELLED
                    break
                job.phase, job.percent = "complete", 100
                if self.storage is not None:
                    stored = self.storage.upload(job.clip.path, f"clips/{job.id}/{job.filename}")
                    job.object_key, job.result_size = stored.key, stored.size
                outcome = JobStatus.DONE
                break
            except TimeoutError as e:
                log.warning("clip timed out for %s: %s", job.spec.url, e)
                job.error = "The media operation timed out. Try a shorter range or lower quality."
                shutil.rmtree(job.work_dir, ignore_errors=True)
                outcome = JobStatus.CANCELLED if job.id in self._cancelled else JobStatus.FAILED
                break
            except Exception as e:
                attempts += 1
                transient = isinstance(e, YoutubeDLError)
                if transient and attempts < self.max_attempts and job.id not in self._cancelled:
                    time.sleep(self.retry_backoff * (2 ** (attempts - 1)))
                    continue
                if transient:
                    log.warning("clip failed for %s: %s", job.spec.url, e)
                    job.error = "Couldn't download that clip. Try another quality."
                else:
                    log.exception("unexpected failure for %s", job.spec.url)
                    job.error = "Something went wrong on our side."
                shutil.rmtree(job.work_dir, ignore_errors=True)
                outcome = JobStatus.CANCELLED if job.id in self._cancelled else JobStatus.FAILED
                break
        job.finished = time.time()
        if outcome is JobStatus.DONE:
            self._learn(job)
        job.status = outcome
        if outcome is not JobStatus.DONE or self.storage is not None:
            self._release_resources(job)
        self._persist(job)

    def _release_resources(self, job: Job) -> None:
        if job.resource_reserved:
            self.resources.release(job.id)
            job.resource_reserved = False

    def _learn(self, job: Job) -> None:
        spec = job.spec
        speed = (spec.end - spec.start) / max(job.finished - job.started, 0.1)
        key = (spec.quality.res, spec.mode)
        with self._lock:
            previous = self._speed.get(key, speed)
            self._speed[key] = previous + _SPEED_SMOOTHING * (speed - previous)

    def _expire(self) -> None:
        """Drop finished jobs past their TTL, along with their files. Caller holds the lock."""
        now = time.time()
        for job_id, job in list(self._jobs.items()):
            if (
                job.finished is not None
                and now - job.finished > self.ttl + self.cleanup_grace
                and job.download_refs == 0
            ):
                shutil.rmtree(job.work_dir, ignore_errors=True)
                self._release_resources(job)
                del self._jobs[job_id]
                if self.storage is not None and job.object_key:
                    self.storage.delete(job.object_key)
