"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  type ClipRequest,
  createExport,
  createJob,
  getExport,
  getJob,
  type ExportRequest,
  type Job,
} from "./api";

export type JobView =
  | { stage: "idle" }
  | { stage: "starting" }
  | { stage: "queued"; position: number }
  | { stage: "working"; elapsed: number; estimate: number | null; phase: Job["phase"]; percent: number; progressKnown: boolean }
  | { stage: "done"; id: string; size: number; expiresIn: number }
  | { stage: "error"; message: string };

const POLL_MS = 1000;

function progressOf(job: Job): JobView {
  return job.status === "queued"
    ? { stage: "queued", position: job.position ?? 1 }
    : {
        stage: "working",
        elapsed: job.elapsed_seconds ?? 0,
        estimate: job.estimate_seconds,
        phase: job.phase,
        percent: job.percent,
        progressKnown: job.progress_known,
      };
}

function useQueuedJob<Request>(
  create: (request: Request) => Promise<Job>,
  get: (id: string) => Promise<Job>,
  fallback: string,
) {
  const [view, setView] = useState<JobView>({ stage: "idle" });
  const run = useRef(0);
  const reset = useCallback(() => {
    run.current++;
    setView({ stage: "idle" });
  }, []);
  useEffect(() => () => void run.current++, []);
  const start = useCallback(async (request: Request) => {
    const mine = ++run.current;
    setView({ stage: "starting" });
    try {
      let job = await create(request);
      while (job.status === "queued" || job.status === "working") {
        if (mine !== run.current) return;
        setView(progressOf(job));
        await new Promise((resolve) => setTimeout(resolve, POLL_MS));
        job = await get(job.id);
      }
      if (mine !== run.current) return;
      setView(
        job.status === "done"
          ? { stage: "done", id: job.id, size: job.size_bytes ?? 0, expiresIn: job.expires_in ?? 0 }
          : { stage: "error", message: job.error ?? fallback },
      );
    } catch (error) {
      if (mine === run.current) {
        setView({ stage: "error", message: error instanceof Error ? error.message : fallback });
      }
    }
  }, [create, fallback, get]);
  return { view, start, reset };
}

export function useClipJob() {
  return useQueuedJob(createJob, getJob, "Something went wrong.");
}

export function useExportJob() {
  return useQueuedJob(createExport, getExport, "Export failed.");
}
