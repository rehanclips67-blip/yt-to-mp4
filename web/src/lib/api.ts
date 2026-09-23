const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type Mode = "exact" | "fast";

export interface Quality {
  res: number;
  label: string;
  kbps: number;
  max_seconds: number;
  fps: number | null;
  codec: string | null;
  container: string | null;
  has_audio: boolean;
  audio_available: boolean;
  format_id: string | null;
}

export interface TranscriptSegment {
  start: number;
  end: number;
  text: string;
  words: string[] | null;
}

export interface TranscriptResponse {
  available: boolean;
  reason: string | null;
  segments: TranscriptSegment[];
}

export interface TranscriptJob {
  id: string;
  status: "queued" | "working" | "done" | "failed" | "cancelled";
  position: number | null;
  phase: string;
  percent: number;
  error: string | null;
  segments: TranscriptSegment[];
}

export interface ClipRange {
  id: string;
  start: number;
  end: number;
  label: string;
}

export interface ExportRequest {
  url?: string;
  source_id?: string;
  res: number;
  mode: Mode;
  ranges: Array<{ start: number; end: number }>;
}

export interface VideoInfo {
  id: string;
  title: string;
  duration: number;
  thumbnail: string | null;
  qualities: Quality[];
}

export interface ClipRequest {
  url?: string;
  source_id?: string;
  start: number;
  end: number;
  res: number;
  mode: Mode;
}

export interface Job {
  id: string;
  status: "queued" | "working" | "done" | "failed";
  position: number | null;
  elapsed_seconds: number | null;
  estimate_seconds: number | null;
  phase: "queued" | "preparing" | "downloading" | "merging" | "cutting" | "packaging" | "complete";
  percent: number;
  progress_known: boolean;
  error: string | null;
  filename: string | null;
  size_bytes: number | null;
  expires_in: number | null;
}

export interface SourcePresign {
  id: string;
  status: string;
  upload_url: string | null;
  upload_fields: Record<string, string> | null;
  expires_in: number;
}

export interface SourceInfo {
  id: string;
  source_type: "upload";
  title: string;
  duration_seconds: number;
  width: number | null;
  height: number | null;
  fps: number | null;
  codec: string | null;
  status: string;
  expires_in: number;
}

export const fetchInfo = (url: string): Promise<VideoInfo> => send("POST", "/api/info", { url });
export const presignSource = (filename: string, contentType: string): Promise<SourcePresign> =>
  send("POST", "/api/sources/presign", { filename, content_type: contentType });
export async function uploadSource(
  presign: SourcePresign,
  file: File,
): Promise<void> {
  const body = presign.upload_fields
    ? (() => {
        const form = new FormData();
        Object.entries(presign.upload_fields).forEach(([key, value]) => form.append(key, value));
        form.append("file", file);
        return form;
      })()
    : file;
  if (!presign.upload_url) throw new Error("The upload destination was not provided.");
  const uploadUrl = /^https?:\/\//.test(presign.upload_url)
    ? presign.upload_url
    : `${API_URL}${presign.upload_url}`;
  const response = await fetch(uploadUrl, {
    method: presign.upload_fields ? "POST" : "PUT",
    headers: presign.upload_fields ? undefined : { "Content-Type": file.type },
    body,
  });
  if (!response.ok) throw new Error(await errorMessage(response));
}
export const completeSource = (id: string): Promise<SourceInfo> =>
  send("POST", `/api/sources/${id}/complete`);
export async function fetchTranscript(url: string, fallback = false): Promise<TranscriptResponse> {
  if (!fallback) return send("POST", "/api/transcript", { url });

  const submitted = await send<TranscriptJob | TranscriptResponse>("POST", "/api/transcript", {
    url,
    fallback_whisper: true,
  });
  if (!("id" in submitted)) return submitted;
  let job = submitted;
  while (job.status === "queued" || job.status === "working") {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    job = await send<TranscriptJob>("GET", `/api/transcript/jobs/${job.id}`);
  }
  if (job.status !== "done") {
    throw new Error(job.error ?? "Whisper transcription failed.");
  }
  return { available: true, reason: null, segments: job.segments };
}
export const createExport = (request: ExportRequest): Promise<Job> =>
  send("POST", "/api/exports", request);
export const getExport = (id: string): Promise<Job> => send("GET", `/api/exports/${id}`);
export const createJob = (request: ClipRequest): Promise<Job> => send("POST", "/api/jobs", request);
export const getJob = (id: string): Promise<Job> => send("GET", `/api/jobs/${id}`);

/** A plain link: the browser streams the file straight to disk, however big it is. */
export const fileUrl = (id: string) => `${API_URL}/api/jobs/${id}/file`;
export const exportFileUrl = (id: string) => `${API_URL}/api/exports/${id}/file`;

async function send<T>(method: string, path: string, body?: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new Error("Can't reach the server. Is it running?");
  }
  if (!response.ok) throw new Error(await errorMessage(response));
  return response.json();
}

async function errorMessage(response: Response): Promise<string> {
  const { detail } = await response.json().catch(() => ({ detail: null }));
  if (typeof detail === "string") return detail;
  const first = Array.isArray(detail) ? detail[0]?.msg : null;
  return first ? String(first).replace(/^Value error, /, "") : "Something went wrong. Try again.";
}
