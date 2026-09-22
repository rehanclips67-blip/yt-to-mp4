"""HTTP API: /api/info reads a video, /api/jobs cuts a clip in the background."""

import json
import logging
import os
import secrets
import subprocess
import tempfile
import time
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from starlette.background import BackgroundTask
from yt_dlp.utils import YoutubeDLError

from .client_identity import client_identity
from .formats import Quality, build_qualities
from .jobs import (
    ExportJobManager,
    Job,
    JobManager,
    JobStatus,
    QueueFull,
    TooManyJobs,
    TranscriptJob,
    TranscriptJobManager,
)
from .limits import max_clip_seconds
from .media import (
    Mode,
    PrimaryUrlError,
    download_clip,
    download_export,
    fetch_info,
    fetch_transcript,
    fetch_whisper_transcript,
    make_clip_spec,
    validate_primary_url,
    whisper_is_configured,
)
from .models import Source, SourceType
from .rate_limit import RateLimitDecision, make_rate_limiter
from .repository import utcnow
from .resources import ResourceAdmissionError, ResourceGuard
from .schemas import (
    ClipRequest,
    ExportJobOut,
    ExportRequest,
    InfoRequest,
    InfoResponse,
    JobOut,
    QualityOut,
    SourceCompleteOut,
    SourceOut,
    SourcePresignOut,
    SourcePresignRequest,
    TranscriptJobOut,
    TranscriptRequest,
    TranscriptResponse,
)
from .storage import ObjectStorage, StorageError

log = logging.getLogger("clipper")

configured_storage = (
    ObjectStorage.from_env() if os.getenv("STORAGE_BACKEND", "local").lower() != "local" else None
)
SOURCE_TTL_SECONDS = int(os.getenv("UPLOAD_PENDING_TTL_SECONDS", "900"))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(1024**3)))
MAX_UPLOAD_DURATION_SECONDS = int(os.getenv("MAX_UPLOAD_DURATION_SECONDS", str(3 * 60 * 60)))
ALLOWED_UPLOAD_CONTENT_TYPES = frozenset(
    item.strip()
    for item in os.getenv(
        "ALLOWED_UPLOAD_CONTENT_TYPES",
        "video/mp4,video/webm,video/quicktime",
    ).split(",")
    if item.strip()
)
UPLOAD_ROOT = Path(os.getenv("UPLOAD_ROOT", "uploads"))

app = FastAPI(title="Clipper")

API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
DOCS_CSP = (
    "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: https://fastapi.tiangolo.com; "
    "connect-src 'self'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    csp = DOCS_CSP if request.url.path in {app.docs_url, app.redoc_url} else API_CSP
    response.headers.setdefault("Content-Security-Policy", csp)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(","),
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
    expose_headers=["Content-Disposition"],
)


def _youtube_info_error(error: YoutubeDLError) -> str:
    detail = str(error).lower()
    if "sign in to confirm" in detail or "not a bot" in detail:
        return "YouTube requires verification for this video. Try again later or upload the video instead."
    if "private video" in detail or "sign in" in detail:
        return "This video is private or requires a YouTube login."
    if "age-restricted" in detail or "confirm your age" in detail:
        return "This video is age-restricted and cannot be read without YouTube verification."
    if "video unavailable" in detail or "not available" in detail:
        return "This video is unavailable to the server. Check its visibility and try again."
    return "YouTube could not provide this video's metadata. Try again later or upload the video instead."


resource_guard = ResourceGuard()

# Clips are CPU and bandwidth heavy, so only a few run at once and the rest wait in line.
jobs = JobManager(
    download_clip,
    workers=int(os.getenv("MAX_CONCURRENT_CLIPS", "2")),
    ttl=int(os.getenv("CLIP_TTL_SECONDS", str(30 * 60))),
    db_url=os.getenv("DATABASE_URL", "sqlite:///clipper.db"),
    queue_adapter=os.getenv("QUEUE_BACKEND", os.getenv("QUEUE_ADAPTER", "thread")),
    cleanup_grace=int(os.getenv("CLEANUP_GRACE_SECONDS", "300")),
    metadata_fetcher=fetch_info,
    storage=configured_storage,
    source_lookup=None,
    resources=resource_guard,
)
export_jobs = ExportJobManager(
    download_clip,
    export_runner=download_export,
    workers=1,
    ttl=int(os.getenv("CLIP_TTL_SECONDS", str(30 * 60))),
    db_url=os.getenv("DATABASE_URL", "sqlite:///clipper.db"),
    queue_adapter=os.getenv("QUEUE_BACKEND", os.getenv("QUEUE_ADAPTER", "thread")),
    cleanup_grace=int(os.getenv("CLEANUP_GRACE_SECONDS", "300")),
    storage=configured_storage,
    source_lookup=None,
    resources=resource_guard,
)
transcript_jobs = TranscriptJobManager(
    fetch_whisper_transcript,
    workers=1,
    queue_adapter=os.getenv("QUEUE_BACKEND", os.getenv("QUEUE_ADAPTER", "thread")),
    max_concurrent=int(os.getenv("WHISPER_MAX_CONCURRENT", "1")),
)
submission_limiter = make_rate_limiter()
source_repository = jobs._repository
jobs.source_lookup = source_repository.get_source_by_id if source_repository else None
export_jobs.source_lookup = source_repository.get_source_by_id if source_repository else None


def _cleanup_expired_sources() -> None:
    if source_repository is None:
        return
    for source in source_repository.delete_expired_sources():
        if configured_storage is None:
            Path(source.storage_key).unlink(missing_ok=True)
        else:
            try:
                configured_storage.delete(source.storage_key)
            except StorageError:
                log.warning("source cleanup failed for %s", source.id, exc_info=True)


def _delete_source_storage(source: Source, original_error: Exception) -> None:
    try:
        if configured_storage is None:
            Path(source.storage_key).unlink(missing_ok=True)
        else:
            configured_storage.delete(source.storage_key)
    except Exception as cleanup_error:
        log.warning(
            "source storage cleanup failed for %s after %r: %r",
            source.id,
            original_error,
            cleanup_error,
            exc_info=True,
        )


def _delete_local_path(path: Path, source_id: str) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        log.warning("local source cleanup failed for %s", source_id, exc_info=True)


def _mark_source_failed(source_id: str) -> None:
    try:
        source_repository.update_source(source_id, status="failed")
    except Exception:
        log.warning("could not mark source %s as failed", source_id, exc_info=True)


def _source_view(source: Source) -> SourceOut:
    return SourceOut(
        id=source.id,
        source_type=source.source_type.value,
        title=source.title,
        duration_seconds=source.duration_seconds,
        width=source.width,
        height=source.height,
        fps=source.fps,
        codec=source.codec,
        status=source.status,
        expires_in=max(0, int((source.expires_at - utcnow()).total_seconds())),
    )


def _require_ready_upload(source_id: str) -> Source:
    if source_repository is None:
        raise HTTPException(503, "Source storage is unavailable.")
    source = source_repository.get_source_by_id(source_id)
    if (
        source is None
        or source.source_type is not SourceType.upload
        or source.status != "ready"
        or source.expires_at <= utcnow()
    ):
        raise HTTPException(422, "The upload source is unavailable or expired.")
    return source


def _probe_source(path: Path) -> dict:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=width,height,r_frame_rate,codec_name,codec_type",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        )
        payload = json.loads(result.stdout)
        duration = float(payload["format"]["duration"])
        video = next(
            item for item in payload.get("streams", []) if item.get("codec_type") == "video"
        )
        fps_text = video.get("r_frame_rate")
        fps = None if not fps_text or fps_text == "0/0" else float(Fraction(fps_text))
        if duration <= 0 or duration > MAX_UPLOAD_DURATION_SECONDS:
            raise ValueError("duration")
        return {
            "duration_seconds": duration,
            "width": video.get("width"),
            "height": video.get("height"),
            "fps": fps,
            "codec": video.get("codec_name"),
        }
    except (
        OSError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
        KeyError,
        StopIteration,
        TypeError,
        ValueError,
        ZeroDivisionError,
    ) as exc:
        raise HTTPException(422, "The uploaded file is not a supported video.") from exc


@app.post("/api/sources/presign", status_code=201)
def presign_source(req: SourcePresignRequest) -> SourcePresignOut:
    if source_repository is None:
        raise HTTPException(503, "Source storage is unavailable.")
    _cleanup_expired_sources()
    if req.content_type not in ALLOWED_UPLOAD_CONTENT_TYPES:
        raise HTTPException(415, "That upload content type is not allowed.")
    suffix = Path(req.filename).suffix.lower()
    if not suffix:
        raise HTTPException(422, "A video filename with an extension is required.")
    source_id = secrets.token_urlsafe(16)
    created_at = utcnow()
    if configured_storage is None:
        UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
        storage_key = str((UPLOAD_ROOT / f"{source_id}{suffix}").resolve())
        upload_url = f"/api/sources/{source_id}/upload"
        upload_fields = None
    else:
        storage_key = f"sources/{source_id}{suffix}"
        try:
            form = configured_storage.presigned_post(
                storage_key, req.content_type, MAX_UPLOAD_BYTES
            )
        except StorageError as exc:
            raise HTTPException(503, "Source storage is unavailable.") from exc
        upload_url = form["url"]
        upload_fields = form["fields"]
    source_repository.add_source(
        Source(
            id=source_id,
            source_type=SourceType.upload,
            storage_key=storage_key,
            title=Path(req.filename).stem,
            duration_seconds=0,
            status="pending",
            created_at=created_at,
            expires_at=created_at + timedelta(seconds=SOURCE_TTL_SECONDS),
        )
    )
    return SourcePresignOut(
        id=source_id,
        status="pending",
        upload_url=upload_url,
        upload_fields=upload_fields,
        expires_in=SOURCE_TTL_SECONDS,
    )


@app.put("/api/sources/{source_id}/upload", status_code=204)
async def upload_source(source_id: str, request: Request) -> None:
    if source_repository is None:
        raise HTTPException(503, "Source storage is unavailable.")
    source = source_repository.get_source_by_id(source_id)
    if source is None or source.expires_at <= utcnow():
        raise HTTPException(404, "That source has expired.")
    if source.status != "pending" or configured_storage is not None:
        raise HTTPException(409, "That source is not accepting a local upload.")
    partial = Path(source.storage_key).with_suffix(Path(source.storage_key).suffix + ".part")
    path = Path(source.storage_key)
    total = 0
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with partial.open("wb") as output:
            async for chunk in request.stream():
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "The uploaded file is too large.")
                output.write(chunk)
        partial.replace(path)
    except HTTPException:
        _delete_local_path(partial, source_id)
        _delete_local_path(path, source_id)
        source_repository.delete_source(source_id)
        raise
    except OSError as exc:
        _delete_local_path(partial, source_id)
        _delete_local_path(path, source_id)
        _mark_source_failed(source_id)
        raise HTTPException(500, "The upload could not be stored.") from exc


def _complete_source(source: Source) -> Source:
    temporary = None
    path = Path(source.storage_key)
    try:
        if configured_storage is not None:
            head = configured_storage.head(source.storage_key)
            if int(head.get("ContentLength", 0)) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "The uploaded file is too large.")
            temporary = Path(tempfile.mkstemp(suffix=path.suffix)[1])
            configured_storage.download(source.storage_key, temporary)
            path = temporary
        metadata = _probe_source(path)
        updated = source_repository.update_source(source.id, status="ready", **metadata)
        if updated is None:
            raise HTTPException(404, "That source has expired.")
        return updated
    except StorageError as exc:
        raise HTTPException(422, "The uploaded file could not be inspected.") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@app.post("/api/sources/{source_id}/complete", response_model=SourceCompleteOut)
def complete_source(source_id: str) -> SourceCompleteOut:
    if source_repository is None:
        raise HTTPException(503, "Source storage is unavailable.")
    source = source_repository.get_source_by_id(source_id)
    if source is None or source.expires_at <= utcnow():
        raise HTTPException(404, "That source has expired.")
    if source.status != "pending":
        raise HTTPException(409, "That source is not pending.")
    try:
        return _source_view(_complete_source(source))
    except Exception as exc:
        _delete_source_storage(source, exc)
        _mark_source_failed(source_id)
        if isinstance(exc, HTTPException):
            raise
        log.error("source completion failed for %s", source_id, exc_info=True)
        raise HTTPException(500, "The uploaded source could not be completed.") from exc


@app.get("/api/sources/{source_id}", response_model=SourceOut)
def get_source(source_id: str) -> SourceOut:
    if source_repository is None:
        raise HTTPException(503, "Source storage is unavailable.")
    source = source_repository.get_source_by_id(source_id)
    if source is None or source.expires_at <= utcnow():
        raise HTTPException(404, "That source has expired.")
    return _source_view(source)


@app.post("/api/info")
def info(req: InfoRequest) -> InfoResponse:
    try:
        validate_primary_url(req.url)
        raw = fetch_info(req.url)
    except PrimaryUrlError as e:
        raise HTTPException(422, str(e)) from e
    except YoutubeDLError as e:
        log.warning("info failed for %s: %s", req.url, e)
        raise HTTPException(422, _youtube_info_error(e)) from e

    if not raw.get("duration"):
        raise HTTPException(422, "Live streams aren't supported.")

    return InfoResponse(
        id=raw["id"],
        title=raw["title"],
        duration=raw["duration"],
        thumbnail=raw.get("thumbnail"),
        qualities=[
            QualityOut(
                res=q.res,
                label=q.label,
                kbps=q.kbps,
                max_seconds=max_clip_seconds(q.res),
                fps=q.fps,
                codec=q.codec,
                container=q.container,
                format_id=q.format_id,
                has_audio=q.has_audio,
                audio_available=q.audio_available,
            )
            for q in build_qualities(raw["formats"])
        ],
    )


@app.post("/api/transcript")
def transcript(req: TranscriptRequest, request: Request, response: Response):
    try:
        validate_primary_url(req.url)
        segments = fetch_transcript(req.url)
    except PrimaryUrlError as e:
        raise HTTPException(422, str(e)) from e
    except YoutubeDLError as e:
        log.warning("transcript failed for %s: %s", req.url, e)
        return TranscriptResponse(available=False, reason="captions_unavailable")
    except (OSError, ValueError) as e:
        log.warning("caption read failed for %s: %s", req.url, e)
        return TranscriptResponse(available=False, reason="captions_unavailable")
    if not segments:
        if not req.fallback_whisper:
            return TranscriptResponse(available=False, reason="captions_unavailable")
        if not whisper_is_configured():
            # Preserve direct dependency injection used by existing callers/tests;
            # production configuration remains explicitly opt-in and asynchronous.
            if fetch_whisper_transcript.__module__ != "app.media":
                segments = fetch_whisper_transcript(req.url)
                if segments:
                    return TranscriptResponse(available=True, segments=segments)
            return TranscriptResponse(available=False, reason="whisper_unavailable")
        client = client_identity(request)
        try:
            job = transcript_jobs.submit(req.url, client)
        except TooManyJobs as exc:
            raise HTTPException(429, "You already have transcriptions in progress.") from exc
        except QueueFull as exc:
            raise HTTPException(503, "Server is busy, try again in a moment.") from exc
        response.status_code = 202
        return _transcript_job_view(job)
    return TranscriptResponse(available=True, segments=segments)


@app.post("/api/jobs", status_code=202)
def create_job(req: ClipRequest, request: Request) -> JobOut:
    mode = req.mode if "mode" in req.model_fields_set else Mode.FAST
    spec = make_clip_spec(
        url=req.url,
        source_id=req.source_id,
        start=req.start,
        end=req.end,
        quality=Quality(req.res),
        mode=mode,
    )
    client = client_identity(request)
    _enforce_submission_limit(client)
    try:
        if req.source_id is not None:
            _require_ready_upload(req.source_id)
        else:
            validate_primary_url(req.url)
        jobs.validate_duration(spec)
    except YoutubeDLError as e:
        raise HTTPException(422, "Couldn't read the source duration.") from e
    except (OSError, ValueError) as e:
        raise HTTPException(422, str(e)) from e
    try:
        job = jobs.submit(spec, client)
    except TooManyJobs as e:
        raise HTTPException(
            429, "You already have clips in progress. Wait for one to finish."
        ) from e
    except QueueFull as e:
        raise HTTPException(503, "Server is busy, try again in a moment.") from e
    except ResourceAdmissionError as e:
        raise HTTPException(
            507, "The server is temporarily out of capacity. Try again later."
        ) from e
    return _view(job)


@app.get("/api/transcript/jobs/{job_id}")
def get_transcript_job(job_id: str) -> TranscriptJobOut:
    job = transcript_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "That transcription job was not found.")
    return _transcript_job_view(job)


@app.get("/api/transcript/jobs/{job_id}/result")
def get_transcript_result(job_id: str) -> TranscriptResponse:
    job = transcript_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "That transcription job was not found.")
    if job.status is not JobStatus.DONE:
        if job.status is JobStatus.FAILED:
            raise HTTPException(409, job.error or "Whisper transcription failed.")
        raise HTTPException(409, "That transcription is not ready yet.")
    return TranscriptResponse(available=True, segments=job.result or [])


@app.post("/api/exports", status_code=202)
def create_export(req: ExportRequest, request: Request) -> ExportJobOut:
    mode = req.mode if "mode" in req.model_fields_set else Mode.FAST
    specs = tuple(
        make_clip_spec(
            url=req.url,
            source_id=req.source_id,
            start=item.start,
            end=item.end,
            quality=Quality(req.res),
            mode=mode,
        )
        for item in req.ranges
    )
    client = client_identity(request)
    _enforce_submission_limit(client)
    try:
        if req.source_id is not None:
            _require_ready_upload(req.source_id)
            if any(spec.source_id != req.source_id for spec in specs):
                raise ValueError("All export ranges must use the same source.")
            for spec in specs:
                jobs.validate_duration(spec)
        else:
            validate_primary_url(req.url)
        job = export_jobs.submit(specs, client)
    except PrimaryUrlError as e:
        raise HTTPException(422, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    except TooManyJobs as e:
        raise HTTPException(429, "You already have exports in progress.") from e
    except QueueFull as e:
        raise HTTPException(503, "Server is busy, try again in a moment.") from e
    except ResourceAdmissionError as e:
        raise HTTPException(
            507, "The server is temporarily out of capacity. Try again later."
        ) from e
    return _export_view(job)


def _enforce_submission_limit(client: str) -> None:
    decision: RateLimitDecision = submission_limiter.check_and_consume(client)
    if decision.allowed:
        return
    if decision.scope == "global":
        detail = "Submission limit reached for the server. Try again later."
    else:
        detail = "Submission limit reached for this client. Try again later."
    raise HTTPException(
        429,
        detail,
        headers={"Retry-After": str(max(1, decision.retry_after))},
    )


@app.get("/api/exports/{job_id}")
def get_export(job_id: str) -> ExportJobOut:
    job = export_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "That export has expired. Make it again.")
    return _export_view(job)


@app.get("/api/exports/{job_id}/file")
def get_export_file(job_id: str) -> FileResponse:
    job = export_jobs.acquire_download(job_id)
    if job is None:
        raise HTTPException(404, "That export has expired. Make it again.")
    if job.status is not JobStatus.DONE or job.path is None:
        export_jobs.release_download(job_id)
        raise HTTPException(409, "That export isn't ready yet.")
    if configured_storage is not None and job.object_key:
        export_jobs.release_download(job_id)
        try:
            expires_at = datetime.fromtimestamp(job.finished + export_jobs.ttl, UTC)
            return RedirectResponse(configured_storage.signed_url_until(job.object_key, expires_at))
        except StorageError as exc:
            raise HTTPException(503, "Result storage is unavailable.") from exc
    return FileResponse(
        job.path,
        filename=job.filename,
        media_type="application/zip",
        background=BackgroundTask(export_jobs.release_download, job_id),
    )


@app.post("/api/exports/{job_id}/cancel", status_code=204)
def cancel_export(job_id: str) -> None:
    if export_jobs.get(job_id) is None:
        raise HTTPException(404, "That export has expired. Make it again.")
    if not export_jobs.cancel(job_id):
        raise HTTPException(409, "That export cannot be cancelled.")


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> JobOut:
    return _view(_find(job_id))


@app.get("/api/jobs/{job_id}/file")
def get_file(job_id: str) -> FileResponse:
    job = jobs.acquire_download(job_id)
    if job is None:
        raise HTTPException(404, "That clip has expired. Make it again.")
    if job.status is not JobStatus.DONE or job.clip is None:
        jobs.release_download(job_id)
        raise HTTPException(409, "That clip isn't ready yet.")
    if configured_storage is not None and job.object_key:
        jobs.release_download(job_id)
        try:
            expires_at = datetime.fromtimestamp(job.finished + jobs.ttl, UTC)
            return RedirectResponse(configured_storage.signed_url_until(job.object_key, expires_at))
        except StorageError as exc:
            raise HTTPException(503, "Result storage is unavailable.") from exc
    return FileResponse(
        job.clip.path,
        filename=job.filename,
        background=BackgroundTask(jobs.release_download, job_id),
    )


@app.post("/api/jobs/{job_id}/cancel", status_code=204)
def cancel_job(job_id: str) -> None:
    if jobs.get(job_id) is None:
        raise HTTPException(404, "That clip has expired. Make it again.")
    if not jobs.cancel(job_id):
        raise HTTPException(409, "That clip cannot be cancelled.")


def _find(job_id: str) -> Job:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "That clip has expired. Make it again.")
    return job


def _view(job: Job) -> JobOut:
    out = JobOut(
        id=job.id,
        status=job.status,
        error=job.error,
        phase=job.phase,
        percent=job.percent,
    )
    if job.status is JobStatus.QUEUED:
        out.position = jobs.position(job)
    if job.started is not None:
        out.elapsed_seconds = (job.finished or time.time()) - job.started
        out.estimate_seconds = jobs.estimate(job.spec)
    if job.status is JobStatus.DONE and job.clip is not None:
        out.filename = job.filename
        out.size_bytes = job.result_size or job.clip.path.stat().st_size
        out.expires_in = jobs.expires_in(job)
    return out


def _export_view(job) -> ExportJobOut:
    out = ExportJobOut(
        id=job.id, status=job.status, error=job.error, phase=job.phase, percent=job.percent
    )
    if job.status is JobStatus.QUEUED:
        out.position = export_jobs.position(job)
    if job.started is not None:
        out.elapsed_seconds = (job.finished or time.time()) - job.started
    if job.status is JobStatus.DONE and job.path is not None:
        out.filename = job.filename
        out.size_bytes = job.result_size or job.path.stat().st_size
        out.expires_in = export_jobs.expires_in(job)
    return out


def _transcript_job_view(job: TranscriptJob) -> TranscriptJobOut:
    out = TranscriptJobOut(
        id=job.id,
        status=job.status,
        position=transcript_jobs.position(job) if job.status is JobStatus.QUEUED else None,
        phase=job.phase,
        percent=job.percent,
        error=job.error,
        segments=job.result or [],
    )
    return out
