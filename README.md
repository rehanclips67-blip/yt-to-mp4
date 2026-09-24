# Clipper

Paste a YouTube link, pick a quality, drag a range slider, download only that part.

```
server/   Python API (FastAPI + yt-dlp + FFmpeg)   <- this milestone
web/      Frontend (Next.js + TypeScript)
```

## Admin YouTube diagnostic

Set `ADMIN_TOKEN` on the backend before using the restricted diagnostic endpoint. It accepts one validated YouTube URL and returns sanitized extraction diagnostics only:

```sh
curl -X POST \
  -H "Content-Type: application/json" \
  -H "X-Admin-Token: $ADMIN_TOKEN" \
  -d '{"url":"https://youtu.be/u2uLI2x405c"}' \
  https://<backend-host>/api/admin/youtube-diagnostic
```

The endpoint is intended for administrators and must not be exposed with the token in client-side code.

## Run it

Two terminals: the server, then the web app.

### 1. Server

Requires Python 3.11+ and FFmpeg on your PATH.

```bash
cd server
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

Open http://localhost:8000/docs to try the endpoints in the browser.

**Smoke test with a real video** (do this first): in `/docs`, run `POST /api/info` with a YouTube URL,
then `POST /api/clip` with `start`, `end` and a `res` from the returned qualities.

If extraction fails on YouTube, update yt-dlp (`pip install -U yt-dlp`). Recent yt-dlp versions may also need a
JavaScript runtime such as Deno installed; check the yt-dlp docs for the current requirement.

### 2. Web app

Requires Node 20+.

```bash
cd web
npm install
npm run dev
```

Open http://localhost:3000. The web app talks to the server at `http://localhost:8000`; to change that,
copy `.env.example` to `.env.local` and edit `NEXT_PUBLIC_API_URL`.

Checks: `npm run typecheck` (run `npm run dev` once first so Next generates its type file) and `npm run build`.

### Deploy the backend on Render

The backend can run as a Render **Web Service** using the repository root
`Dockerfile`. Configure the service with:

- **Branch:** `main`
- **Runtime:** `Docker`
- **Dockerfile path:** `Dockerfile`
- **Docker build context:** repository root
- **Start command:** leave blank so the Dockerfile `CMD` runs
- **Health check path:** optional; `/docs` is available if a health check is required

The image preserves the production runtime: Python 3.11, Node 22, FFmpeg,
`yt-dlp==2026.8.19`, and `yt-dlp-ejs==0.8.0`. The container startup command
uses Render's `PORT` and binds Uvicorn to `0.0.0.0`; do not hardcode a port in
Render settings.

Set these environment variables in Render as needed:

- `ALLOWED_ORIGINS` — required for the deployed frontend origin (comma-separated
  origins; the local default is not suitable for a deployed frontend).
- `DATABASE_URL` — optional for the canary; the default SQLite database is
  local to the service and ephemeral on restarts/redeploys.
- `YOUTUBE_POT_PROVIDER_URL` — optional. Leave unset unless you operate a
  reachable, authorized BgUtils provider. Never copy a Railway-private hostname
  to Render.
- `ADMIN_TOKEN` — optional; set only if the restricted admin diagnostic endpoint
  is intentionally enabled.

All other configuration variables use the defaults documented in the
configuration table below. Storage, Redis/RQ, S3/MinIO, and Whisper variables
are optional features and should be configured only with their corresponding
provider credentials. Do not commit secrets or create a `.env` file in the
repository.

This Render service is suitable as a Host B canary. It does not migrate
production routing, storage, the database, or the Railway service.

## API

`POST /api/info` `{ "url" }` returns title, duration, thumbnail and `qualities`, capped at the video's real maximum (4K).
`POST /api/transcript` returns caption segments when available. Send `fallback_whisper: true` to explicitly
request Whisper only when captions are absent; captions always remain first choice. The response uses
`captions_unavailable` or `whisper_unavailable` reasons when applicable. Whisper must also be enabled with
`ENABLE_WHISPER_FALLBACK=true`; it is bounded by `WHISPER_MAX_SECONDS` (default 600) and a 100 MB audio cap.
Each quality carries `max_seconds` (its clip limit) and `kbps` (for size estimates).

Clips are cut in the background:

1. `POST /api/jobs` `{ "url", "start", "end", "res", "mode": "fast"|"exact" }` returns a job id. Fast
   stream-copy mode is the default; sending `mode: "exact"` preserves frame-accurate conversion.
2. `GET /api/jobs/{id}` returns `status` (`queued`, `working`, `done`, `failed`), `phase`, `percent`, queue `position`, `elapsed_seconds`,
   `estimate_seconds` (learned from earlier jobs on this machine) and, once done, `size_bytes` and `expires_in`.
3. `GET /api/jobs/{id}/file` streams the MP4. The link works for `CLIP_TTL_SECONDS`, then the file is deleted.

Multi-range exports use the same queue and limits:

1. `POST /api/exports` with `{ "url", "res", "mode", "ranges": [{"start", "end"}, ...] }`
   accepts 1–20 sorted, non-overlapping ranges and returns an export job id. The source is downloaded once
   and reused for all cuts; fast mode keeps cuts as stream copies while exact mode converts each cut.
2. `GET /api/exports/{id}` reports the job, and `GET /api/exports/{id}/file` downloads a ZIP once it is done.
   Entries have deterministic, filesystem-safe names based on the video title and range number.

| mode    | how                              | trade-off                                        |
|---------|----------------------------------|--------------------------------------------------|
| `fast`  | stream copy (default)            | near-instant, cut snaps to the nearest keyframe  |
| `exact` | re-encode to H.264/AAC           | frame-accurate cut, plays everywhere, uses CPU   |

The selected source format is deterministic and never exceeds the requested resolution. Quality metadata
includes the source FPS and codec/container where available. Job progress is staged (`downloading`,
`cutting`, `packaging`, `complete`) and is always reported as an integer `percent` from 0 to 100.
Worker concurrency and encoder/decoder thread counts are bounded; job and export temporary directories
are removed on failure and after the download TTL.

Uploaded sources use a presign/upload/complete flow:

1. `POST /api/sources/presign` with `filename` and `content_type` creates a pending source. Local mode
   returns a protected upload URL; S3/MinIO returns a presigned POST URL and fields.
2. Upload the bytes using the returned URL. Local uploads require the exact source id in the URL and
   enforce the byte limit while streaming.
3. `POST /api/sources/{id}/complete` validates the stored object with `ffprobe`, then
   `GET /api/sources/{id}` returns pending or ready metadata. Pending sources expire automatically.

### Limits

Longest clip per quality (`server/app/limits.py`, one table): 4K 10 min, 2K 30 min, 1080p 1 hour, 720p and below 3 hours.
Also: YouTube links only, at most 2 active jobs per user, at most 20 waiting jobs overall.

Job state is durable in SQLite by default (`DATABASE_URL=sqlite:///clipper.db`), with WAL enabled for concurrent
API/worker access. The default `ThreadPoolExecutor` queue resumes queued jobs after a restart. Use
`DATABASE_URL=sqlite://` in tests for an in-memory database. Jobs can be cancelled with
`POST /api/jobs/{id}/cancel`; transient yt-dlp failures retry with exponential backoff.

The queue adapter is selected with `QUEUE_BACKEND=thread` (default; `QUEUE_ADAPTER` remains supported for compatibility).
RQ is explicit and never silently falls back:
install `pip install -e ".[rq]"`, set `QUEUE_BACKEND=rq` and `REDIS_URL`, then run
`python -m app.worker`. RQ integration fails fast at API startup until the worker integration is configured.
Behind a reverse proxy, make sure the real client IP reaches the app (the per-user limit uses it).

## Config

| env var                | default                 |
|------------------------|-------------------------|
| `ALLOWED_ORIGINS`      | `http://localhost:3000` |
| `MAX_CONCURRENT_CLIPS` | `2`                     |
| `CLIP_TTL_SECONDS`     | `1800`                  |
| `TEMP_STORAGE_BUDGET_BYTES` | `21474836480` | Reserved local temporary/download/output budget (20 GiB) |
| `TEMP_STORAGE_MIN_FREE_BYTES` | `1073741824` | Required free space on the temporary filesystem after a reservation (1 GiB) |
| `CLIP_TEMP_RESERVATION_BYTES` | `536870912` | Per-clip reservation covering downloads, FFmpeg partials, and output (512 MiB) |
| `EXPORT_TEMP_RESERVATION_BYTES` | `2147483648` | Per-export reservation covering source, cuts, ZIP, and partials (2 GiB) |
| `RESOURCE_MAX_HEAVY_JOBS` | `0` | Maximum admitted exact/export jobs (0 disables this cap) |
| `RESOURCE_MAX_MEMORY_MB` | `0` | Aggregate memory reservation for admitted heavy jobs (0 disables this cap) |
| `CLIP_MEMORY_RESERVATION_MB` | `512` | Memory reservation for an exact clip |
| `EXPORT_MEMORY_RESERVATION_MB` | `1024` | Memory reservation for an export |
| `DATABASE_URL`         | `sqlite:///clipper.db`  |
| `QUEUE_ADAPTER`        | `thread`                |
| `MEDIA_TIMEOUT_BASE_SECONDS` | `300`          | Base yt-dlp/FFmpeg media-operation budget |
| `MEDIA_TIMEOUT_PER_MINUTE_SECONDS` | `60`     | Additional budget per requested media minute |
| `MEDIA_TIMEOUT_MIN_SECONDS` | `120`          | Minimum media-operation budget |
| `MEDIA_TIMEOUT_MAX_SECONDS` | `7200`         | Maximum media-operation budget |
| `MEDIA_MAX_DOWNLOAD_BYTES` | `4294967296` | Hard yt-dlp source-download cap (4 GiB) |
| `MAX_UPLOAD_BYTES` | `1073741824` | Maximum uploaded source size (1 GiB) |
| `MAX_UPLOAD_DURATION_SECONDS` | `10800` | Maximum uploaded source duration (3 hours) |
| `ALLOWED_UPLOAD_CONTENT_TYPES` | `video/mp4,video/webm,video/quicktime` | Comma-separated upload MIME allowlist |
| `UPLOAD_PENDING_TTL_SECONDS` | `900` | Lifetime of a pending upload |
| `CLEANUP_GRACE_SECONDS` | `300`                 | Grace period before expired files are removed |
| `STORAGE_BACKEND` | `local` | `local` (default), `s3`, or `minio` |
| `STORAGE_BUCKET` | `clipper-results` | S3-compatible bucket |
| `S3_ENDPOINT_URL` | unset | MinIO/S3-compatible endpoint |
| `STORAGE_SIGNED_URL_SECONDS` | `300` | Maximum presigned URL lifetime |
| `REDIS_URL`            | unset (required by explicit RQ or Redis rate-limit mode) |
| `RATE_LIMIT_BACKEND` | `local` | `local` for in-process tests/development, or explicit `redis` |
| `RATE_LIMIT_CLIENT_MAX` | `10` | Submissions per client/IP window |
| `RATE_LIMIT_CLIENT_WINDOW_SECONDS` | `3600` | Client submission window |
| `RATE_LIMIT_GLOBAL_MAX` | `100` | Total clip/export submissions per window |
| `RATE_LIMIT_GLOBAL_WINDOW_SECONDS` | `60` | Global submission window |
| `ENABLE_WHISPER_FALLBACK` | unset/false | Explicitly enables optional Whisper fallback |
| `WHISPER_MODEL` | `base` | faster-whisper model size/path |
| `WHISPER_DEVICE` | `cpu` | Inference device |
| `WHISPER_COMPUTE_TYPE` | `int8` | CTranslate2 compute type |
| `WHISPER_CPU_THREADS` | `2` | CPU inference threads |
| `WHISPER_MAX_CONCURRENT` | `1` | Separate concurrent Whisper-job cap |
| `WHISPER_MIN_AVAILABLE_MEMORY_MB` | `512` | Soft available-memory floor before model load |
| `WHISPER_MAX_SECONDS` | `600` | Maximum source duration |
| `CLOUDFLARE_TRUSTED_CIDRS` | built-in Cloudflare IPv4/IPv6 ranges | Optional comma-separated CIDR override for trusted Cloudflare peers |
| `YOUTUBE_POT_PROVIDER_URL` | unset | Private HTTP BgUtils provider base URL; when unset, PO-token integration is disabled |

### Optional BgUtils PO-token provider

The server includes the pinned `bgutil-ytdlp-pot-provider==2.0.0` yt-dlp plugin. When
`YOUTUBE_POT_PROVIDER_URL` is set, the shared yt-dlp configuration adds the documented
`youtubepot-bgutilhttp:base_url` extractor argument. If it is unset or unavailable, Clipper
continues using its normal yt-dlp configuration and existing `web_safari`/`android_vr`
verification fallback.

Run the provider as a separate private Railway service using the official
`brainicism/bgutil-ytdlp-pot-provider:2.0.0` image. Do not create a public port mapping.
Configure the provider to listen on its Railway private interface/port as required by the
service platform, then set `YOUTUBE_POT_PROVIDER_URL` on the Clipper service to the provider's
Railway private service URL, including `http://` and port `4416` when applicable. The exact
private hostname is assigned by Railway and must not be guessed or committed here.

At startup Clipper performs one short TCP availability check and logs only disabled,
available/unavailable status and latency. It never logs PO tokens, cookies, signed URLs, or
the configured provider URL. PO tokens may help with some YouTube verification or 403 cases,
but they do not guarantee that bot checks will be removed.

## Tests and lint

```bash
pytest          # includes an end-to-end cut test on a generated video
ruff check . && ruff format --check .
```

For a fresh database, run `alembic upgrade head` from `server/`. The application also creates the
SQLModel tables automatically, which keeps in-memory test databases convenient.

Clip and export submissions share a fixed-window rate limiter in addition to the
per-client active-job limit: 10 submissions per client/IP per hour and 100 total
submissions per minute by default. Limit responses are HTTP 429 with a
`Retry-After` header. The client identity is the direct ASGI request client
address; forwarded headers are not trusted. Local mode is clock-local and
single-process. Set `RATE_LIMIT_BACKEND=redis` with `REDIS_URL` to share atomic
limits across API processes; Redis connectivity is required and startup fails
explicitly if it is unavailable. Health and status/download endpoints are not
rate-limited.

## Benchmarks

The opt-in benchmark tool reuses the production `download_clip` and `download_export` helpers; it does
not duplicate media logic or run downloads when entries are placeholders:

```bash
python scripts/benchmark_clips.py scripts/benchmark_config.sample.json --report benchmark-report.md
```

The config is JSON. YAML is accepted only when PyYAML is already installed, otherwise the command fails
with install guidance. Each non-placeholder URL is measured in both fast stream-copy and exact
frame-accurate modes. The report records fetched title, duration, URL, caption availability/type, wall
time, output probe data, memory measurement method, failures, aggregates, and the one-call
multi-range reuse check. Download and processing sub-times are explicitly marked unavailable because
the existing single-clip helper combines those phases. Only benchmark videos you have permission to
process; the sample uses placeholders where an official licensed URL was not already established.

Whisper fallback is submitted as an asynchronous transcription job when captions are unavailable and
`ENABLE_WHISPER_FALLBACK=true` with the optional faster-whisper package installed:
`pip install -e ".[whisper]"`. The default CTranslate2 model is `base`; the model is downloaded
and cached on first use. CPU deployments should keep `WHISPER_DEVICE=cpu`,
`WHISPER_COMPUTE_TYPE=int8`, and a low `WHISPER_CPU_THREADS` value. Poll
`GET /api/transcript/jobs/{id}` for status, progress, errors, and segments, or
`GET /api/transcript/jobs/{id}/result` once complete. Caption transcripts remain synchronous. Clip
Whisper progress uses bounded phases (`downloading`, `loading_model`, `transcribing`, `complete`);
segment timestamps advance progress when the backend exposes them, without claiming per-token precision.
submission rechecks the actual source duration before queueing and returns `422` when
the requested end exceeds it. Media downloads and FFmpeg processing fail cleanly after
`MEDIA_TIMEOUT_BASE_SECONDS`, `MEDIA_TIMEOUT_PER_MINUTE_SECONDS`, and the bounded
`MEDIA_TIMEOUT_MIN_SECONDS`/`MEDIA_TIMEOUT_MAX_SECONDS` settings control a duration-scaled media
budget. The default is 300 seconds plus 60 seconds per requested media minute, bounded to 120-7200
seconds; a 60-minute Exact operation therefore receives a 3900-second budget. Download responses hold a per-job reference while being
served; expired files are not removed until active responses release that reference, with
`CLEANUP_GRACE_SECONDS` (default 300 seconds) providing additional cleanup safety.

Completed results use the local filesystem behavior by default, so local development and tests need no
object-store credentials. For MinIO or S3, install `pip install -e ".[storage]"`, set
`STORAGE_BACKEND=minio` (or `s3`), `STORAGE_BUCKET=clipper-results`, `S3_ENDPOINT_URL` for MinIO,
and the usual AWS/MinIO credentials. The service uploads only the final clip or ZIP under
`clips/{job_id}/{filename}` or `exports/{job_id}/{filename}` and `/file` returns a time-limited
presigned redirect instead of proxying media through FastAPI. `docker compose up -d minio redis` starts
local service dependencies; create the `clipper-results` bucket before use. Configured storage errors
fail the job explicitly and never silently fall back to local files.
Presigned URLs are derived from the result job's remaining TTL, capped at one hour, and are rejected
once the result has expired. Cloudflare R2 compatibility remains unverified until credentials are
available; do not treat the generic S3 adapter as an R2 production certification.

## Security notes

Primary API URLs must use the exact YouTube hostname allowlist and pass a DNS/IP preflight that rejects
private, loopback, link-local, reserved, multicast, unspecified, malformed, or unresolvable IPv4/IPv6
addresses before yt-dlp is called. Caption downloads additionally accept only HTTPS URLs on the
YouTube/Google caption CDN allowlist, validate every redirect, enforce a 15-second request timeout,
and cap responses at 4 MiB. These controls reduce SSRF risk while preserving normal YouTube captions.
yt-dlp may follow redirects or request extracted resource URLs internally; its version-specific network
behavior is a residual trust boundary and is not claimed to be fully SSRF-closed by the primary URL
preflight. FFmpeg is invoked with an argument list and `shell=False`; user URLs are handled by yt-dlp
and titles are sanitized before becoming filenames. Database access uses SQLModel parameters rather
than interpolated user SQL, and result paths are generated under controlled job directories.

API responses include a restrictive API CSP, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
and HSTS. HSTS is useful only when the API is served exclusively over HTTPS; terminate TLS at the
reverse proxy in production. Environment files are ignored by `.gitignore`; provide secrets through
the environment or a local untracked `.env`.

Whisper remains disabled by default and the server starts without faster-whisper installed when it is
disabled. The optional model cache/download and CPU memory footprint must be tested on the deployment
machine before launch: record actual transcription wall time and peak memory for both 10-minute and
60-minute audio on a 4 GB RAM machine without a GPU. This repository does not claim those real-machine
measurements.

Client/IP limits use the direct ASGI peer by default. When that peer belongs to the maintained
Cloudflare CIDR allowlist, `CF-Connecting-IP` is accepted only if it parses as a globally routable
IPv4/IPv6 address; malformed, private, reserved, and link-local values fall back to the Cloudflare
peer. `X-Forwarded-For` is never trusted. If Cloudflare publishes new ranges, update the versioned
list in `server/app/client_identity.py`, or set `CLOUDFLARE_TRUSTED_CIDRS` to an explicit comma-separated
allowlist. Do not widen this setting to arbitrary proxy networks without equivalent controls.
