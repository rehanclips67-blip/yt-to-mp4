"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { exportFileUrl, fetchTranscript, fileUrl, type ClipRange, type Mode, type TranscriptResponse, type VideoInfo } from "@/lib/api";
import { formatLimit, formatSize } from "@/lib/format";
import { clamp, formatClock, formatTime, MIN_CLIP, round1 } from "@/lib/time";
import { type JobView, useClipJob, useExportJob } from "@/lib/useClipJob";
import { useYouTubePlayer } from "@/lib/useYouTubePlayer";
import { type Range, Timeline } from "./Timeline";
import { TimeField } from "./TimeField";
import styles from "./Workspace.module.css";

const TRANSCRIPT_PADDING = 1.5;
const MAX_TRANSCRIPT_MATCHES = 100;

function normalizeRange(
  start: number,
  end: number,
  duration: number,
  maxSeconds: number,
): Range {
  const safeStart = clamp(Math.min(start, end), 0, duration);
  const safeEnd = clamp(Math.max(start, end), 0, duration);
  const boundedEnd = Math.min(safeEnd, safeStart + maxSeconds);
  return {
    start: round1(safeStart),
    end: round1(Math.max(safeStart + MIN_CLIP, Math.min(duration, boundedEnd))),
  };
}

const MODES: { id: Mode; name: string; note: string }[] = [
  { id: "fast", name: "Fast cut", note: "Stream copy. Near-instant; boundaries can shift by a keyframe." },
  { id: "exact", name: "Exact cut", note: "Frame-accurate H.264/AAC conversion. Slower." },
];

interface Props {
  info: VideoInfo;
  url: string;
  sourceId?: string;
}

export function Workspace({ info, url, sourceId }: Props) {
  const isUpload = Boolean(sourceId);
  const { mountRef, time, playing, muted, started, seekTo, play, pause, toggleMute } = useYouTubePlayer(isUpload ? null : info.id);
  const [quality, setQuality] = useState(info.qualities[0]);
  const [range, setRange] = useState<Range>({
    start: 0,
    end: Math.min(info.duration, 30, quality.max_seconds),
  });
  const [mode, setMode] = useState<Mode>("fast");
  const [transcript, setTranscript] = useState<TranscriptResponse | null>(null);
  const [transcriptQuery, setTranscriptQuery] = useState("");
  const [transcriptTab, setTranscriptTab] = useState<"transcript" | "details">("transcript");
  const [posterVisible, setPosterVisible] = useState(true);
  const [posterSrc, setPosterSrc] = useState(`https://img.youtube.com/vi/${info.id}/maxresdefault.jpg`);
  const [whisperLoading, setWhisperLoading] = useState(false);
  const [selectedRanges, setSelectedRanges] = useState<ClipRange[]>([]);
  const { view, start: startJob, reset } = useClipJob();
  const { view: exportView, start: startExport, reset: resetExport } = useExportJob();
  const previewEnd = useRef<number | null>(null);

  useEffect(() => {
    document.title = `Clipper: ${info.title}`;
  }, [info.title]);

  useEffect(() => {
    if (started) setPosterVisible(false);
  }, [started]);

  useEffect(() => {
    let cancelled = false;
    if (!url) {
      setTranscript({ available: false, reason: "unavailable", segments: [] });
      return () => {
        cancelled = true;
      };
    }
    fetchTranscript(url)
      .then((result) => {
        if (!cancelled) setTranscript(result);
      })
      .catch(() => {
        if (!cancelled) setTranscript({ available: false, reason: "unavailable", segments: [] });
      });
    return () => {
      cancelled = true;
    };
  }, [url]);

  // Stop the preview when the playhead reaches the end of the selection.
  useEffect(() => {
    if (previewEnd.current !== null && time >= previewEnd.current) {
      previewEnd.current = null;
      pause();
    }
  }, [time, pause]);

  const setStart = (seconds: number) =>
    setRange((r) => ({ ...r, start: clamp(round1(seconds), 0, r.end - MIN_CLIP) }));
  const setEnd = (seconds: number) =>
    setRange((r) => ({ ...r, end: clamp(round1(seconds), r.start + MIN_CLIP, info.duration) }));

  const preview = () => {
    previewEnd.current = range.end;
    seekTo(range.start);
    play();
  };

  const length = range.end - range.start;
  const tooLong = length > quality.max_seconds;
  const estimatedBytes = ((quality.kbps * 1000) / 8) * length;
  const busy = ["starting", "queued", "working"].includes(view.stage);

  const download = () =>
    startJob({ ...(sourceId ? { source_id: sourceId } : { url }), ...range, res: quality.res, mode });
  const matches = useMemo(() => {
    const query = transcriptQuery.trim().toLowerCase();
    if (!transcript?.available || !query) return [];
    return transcript.segments
      .filter((segment) => segment.text.toLowerCase().includes(query))
      .slice(0, MAX_TRANSCRIPT_MATCHES);
  }, [transcript, transcriptQuery]);

  const selectTranscriptMatch = (segment: TranscriptResponse["segments"][number]) => {
    const id = `${segment.start}-${segment.end}-${segment.text}`;
    const nextRange = normalizeRange(
      segment.start - TRANSCRIPT_PADDING,
      segment.end + TRANSCRIPT_PADDING,
      info.duration,
      quality.max_seconds,
    );
    setSelectedRanges((current) => {
      if (current.some((item) => item.id === id)) return current.filter((item) => item.id !== id);
      return [...current, { ...nextRange, id, label: formatClock(segment.start) }].sort(
        (a, b) => a.start - b.start || a.end - b.end,
      );
    });
    setRange(nextRange);
    seekTo(nextRange.start);
  };

  const transcribeWithWhisper = async () => {
    setWhisperLoading(true);
    try {
      setTranscript(await fetchTranscript(url, true));
    } catch (error) {
      setTranscript({
        available: false,
        reason: error instanceof Error ? error.message : "Whisper transcription failed.",
        segments: [],
      });
    } finally {
      setWhisperLoading(false);
    }
  };

  const changeQuality = (nextQuality: VideoInfo["qualities"][number]) => {
    setQuality(nextQuality);
    setSelectedRanges((current) =>
      current.map((item) => ({
        ...item,
        ...normalizeRange(item.start, item.end, info.duration, nextQuality.max_seconds),
      })),
    );
  };

  const exportSelected = () => {
    if (selectedRanges.length === 0) return;
    startExport({
      ...(sourceId ? { source_id: sourceId } : { url }),
      res: quality.res,
      mode,
      ranges: selectedRanges.map(({ start, end }) => ({ start, end })),
    });
  };

  const sourceLabel = (() => {
    if (isUpload) return "Uploaded video";
    try {
      const parsed = new URL(url);
      return parsed.hostname === "youtu.be"
        ? `youtu.be/${parsed.pathname.replace(/^\/+/, "")}`
        : `youtube.com/watch?v=${parsed.searchParams.get("v") ?? info.id}`;
    } catch {
      return `youtube.com/watch?v=${info.id}`;
    }
  })();

  return (
    <section className={styles.workspace}>
      <div className={styles.leftColumn}>
        {typeof document !== "undefined" && document.getElementById("editor-header-actions") && createPortal(
          <div className={styles.editorControls}>
            <button type="button" className={styles.newVideo} onClick={() => window.location.assign("/")}>← Back</button>
            <select aria-label="Export quality" value={quality.label} onChange={(event) => {
              const next = info.qualities.find((item) => item.label === event.target.value);
              if (next) changeQuality(next);
            }}>
              {info.qualities.map((item) => <option key={item.label} value={item.label}>{item.label}</option>)}
            </select>
            <button type="button" className={styles.headerExport} disabled={busy || tooLong} onClick={download}>Export Clip</button>
          </div>,
          document.getElementById("editor-header-actions")!,
        )}
        <div className={styles.meta}>
          <h2 className={styles.title} title={info.title}>{info.title}</h2>
          <p className={styles.sourceMeta}>
            <span className={styles.numeric}>{formatClock(info.duration)}</span>
            <span aria-hidden="true">·</span>
            {isUpload ? <span className={styles.sourceUrl}>{sourceLabel}</span> : <a className={styles.sourceUrl} href={url} target="_blank" rel="noopener noreferrer">{sourceLabel}</a>}
          </p>
        </div>
        <div className={styles.playerBlock}>
          <div className={styles.stage}>
            <div className={styles.player}>
              <div ref={mountRef} className={styles.playerMount} />
              {isUpload && <div className={styles.uploadPreview}>Uploaded video ready to clip</div>}
              {!isUpload && posterVisible && (
                <button
                  type="button"
                  className={styles.poster}
                  onClick={() => {
                    setPosterVisible(false);
                    play();
                  }}
                  aria-label="Play video"
                >
                  <img src={posterSrc} alt="" onError={() => setPosterSrc(`https://img.youtube.com/vi/${info.id}/hqdefault.jpg`)} />
                  <span className={styles.posterPlay} aria-hidden="true"><span /></span>
                </button>
              )}
              {!isUpload && <div className={styles.playerControls}>
              <button type="button" className={styles.iconButton} onClick={playing ? pause : play} aria-label={playing ? "Pause video" : "Play video"}>
                <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                  {playing ? <><path d="M8 5v14M16 5v14" /></> : <path d="m9 5 10 7-10 7V5Z" />}
                </svg>
              </button>
              <span className={styles.playerTime}>{formatClock(time)} / {formatClock(info.duration)}</span>
              <input
                className={styles.seek}
                type="range"
                min={0}
                max={info.duration}
                step={0.1}
                value={Math.min(time, info.duration)}
                onChange={(event) => seekTo(Number(event.target.value))}
                aria-label="Video position"
              />
              <button type="button" className={styles.iconButton} onClick={toggleMute} aria-label={muted ? "Unmute video" : "Mute video"}>
                <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                  <path d="M4 10v4h4l5 4V6l-5 4H4Z" />
                  {muted ? <path d="m18 9 4 6m0-6-4 6" /> : <path d="M17 9.5a4 4 0 0 1 0 5" />}
                </svg>
              </button>
              <button type="button" className={styles.iconButton} onClick={() => mountRef.current?.requestFullscreen?.()} aria-label="Enter fullscreen">
                <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M8 4H4v4M16 4h4v4M8 20H4v-4M20 16v4h-4" /></svg>
              </button>
              </div>}
            </div>
          </div>
        </div>
        <Timeline
          duration={info.duration}
          videoId={info.id}
          thumbnail={info.thumbnail}
          range={range}
          playhead={time}
          onRangeChange={setRange}
          onSeek={seekTo}
        />
        <div className={styles.times}>
          <TimeField
            label="Start"
            value={range.start}
            onCommit={setStart}
            onSetToPlayhead={() => setStart(time)}
          />
          <TimeField
            label="End"
            value={range.end}
            onCommit={setEnd}
            onSetToPlayhead={() => setEnd(time)}
          />
          <div className={styles.length}>
            <span className={styles.label}>Length</span>
            <output>{formatTime(length)}</output>
          </div>
          <button type="button" className={`btn ${styles.previewButton}`} onClick={preview}>
            <span aria-hidden="true">▶</span> Preview selection
          </button>
        </div>
        {tooLong && (
          <p role="alert" className={styles.limit}>
            {quality.label} clips can be up to {formatLimit(quality.max_seconds)}.{" "}
            <button type="button" className="text-action" onClick={() => setEnd(range.start + quality.max_seconds)}>
              Fit the selection
            </button>{" "}
            or pick a lower quality.
          </p>
        )}
        <section className={styles.transcript} aria-label="Transcript">
          <div className={styles.transcriptHeader}>
            <div className={styles.tabs} role="tablist" aria-label="Editor information">
              <button role="tab" aria-selected={transcriptTab === "transcript"} type="button" className={transcriptTab === "transcript" ? styles.activeTab : ""} onClick={() => setTranscriptTab("transcript")}>Transcript</button>
              <button role="tab" aria-selected={transcriptTab === "details"} type="button" className={transcriptTab === "details" ? styles.activeTab : ""} onClick={() => setTranscriptTab("details")}>Details</button>
            </div>
            {transcript?.available && transcriptTab === "transcript" && (
              <label className={styles.searchField}>
                <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="10.5" cy="10.5" r="5.5" /><path d="m15 15 5 5" /></svg>
                <input
                  type="search"
                  value={transcriptQuery}
                  onChange={(event) => setTranscriptQuery(event.target.value)}
                  placeholder="Search transcript…"
                  aria-label="Search transcript"
                />
              </label>
            )}
          </div>
          {transcriptTab === "details" ? (
            <dl className={styles.details}>
              <div><dt>Title</dt><dd>{info.title}</dd></div>
              <div><dt>Duration</dt><dd>{formatClock(info.duration)}</dd></div>
              <div><dt>Source</dt><dd>{url}</dd></div>
            </dl>
          ) : (
          <>
          {!transcript && <p className={styles.note}>Loading transcript...</p>}
          {transcript && !transcript.available && (
            <div className={styles.transcriptUnavailable}>
              <p className={styles.note}>No captions available for this video.</p>
              <button type="button" className="btn btn-small" onClick={transcribeWithWhisper} disabled={whisperLoading}>
                {whisperLoading ? "Transcribing..." : "Transcribe with Whisper"}
              </button>
              <p className={styles.note}>Generate a transcript from the video&apos;s audio.</p>
            </div>
          )}
          {transcript?.available && transcriptQuery && matches.length === 0 && (
            <p className={styles.note}>No transcript matches.</p>
          )}
          {selectedRanges.length > 0 && (
            <div className={styles.clipTray} aria-live="polite">
              <span className={styles.clipTrayCount}>{selectedRanges.length} clip{selectedRanges.length === 1 ? "" : "s"} selected</span>
              <div className={styles.clipTrayActions}>
              <button type="button" className="text-action" onClick={() => setSelectedRanges([])}>Clear</button>
              {exportView.stage === "done" ? (
                <>
                  <a className="btn btn-small btn-primary" href={exportFileUrl(exportView.id)}>
                    Download ZIP
                  </a>
                  <button type="button" className="text-action" onClick={resetExport}>New export</button>
                </>
              ) : (
                <button
                  type="button"
                  className={styles.clipTrayExport}
                  disabled={exportView.stage === "starting" || exportView.stage === "queued" || exportView.stage === "working"}
                  onClick={exportSelected}
                >
                  {exportView.stage === "starting" || exportView.stage === "queued" || exportView.stage === "working"
                    ? "Preparing ZIP..."
                    : `Export ${selectedRanges.length} clip${selectedRanges.length === 1 ? "" : "s"}`}
                </button>
              )}
              </div>
              {exportView.stage === "error" && <span className={styles.error}>{exportView.message}</span>}
              {exportView.stage === "working" && (
                <span className={styles.note}>
                  {exportView.phase} - {exportView.percent}%
                </span>
              )}
            </div>
          )}
          {selectedRanges.length > 0 && (
            <ol className={styles.selectedRanges} aria-label="Selected clip ranges">
              {selectedRanges.map((item) => (
                <li key={item.id}>
                  <span><span className={styles.rangeText}>{item.label} – {formatClock(item.end)}</span><span className={styles.rangeDuration}>{formatTime(item.end - item.start)}</span></span>
                  <button
                    type="button"
                    className="text-action"
                    onClick={() => setSelectedRanges((current) => current.filter((range) => range.id !== item.id))}
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ol>
          )}
          {matches.length > 0 && (
            <ul className={styles.transcriptResults}>
              {matches.map((segment) => {
                const id = `${segment.start}-${segment.end}-${segment.text}`;
                const selected = selectedRanges.some((item) => item.id === id);
                return (
                <li className={selected ? styles.selectedTranscriptRow : ""} key={`${segment.start}-${segment.text}`}>
                  <button type="button" className={styles.timestamp} onClick={() => seekTo(segment.start)}>
                    {formatClock(segment.start)}
                  </button>
                  <span>{segment.text}</span>
                  <button
                    type="button"
                    className={styles.selectMatch}
                    aria-pressed={selected}
                    onClick={() => selectTranscriptMatch(segment)}
                  >
                    {selected ? <><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m5 12 4 4L19 6" /></svg>Selected</> : "Use"}
                  </button>
                </li>
                );
              })}
            </ul>
          )}
          </>
          )}
        </section>
      </div>

      <aside className={styles.panel}>
        <div className={styles.panelHeader}>
          <strong>Export clip</strong>
        </div>
        <div className={styles.panelBody}>
        <fieldset className={styles.group}>
          <legend>Quality</legend>
          {info.qualities.map((q) => (
            <label key={q.label} className={styles.option}>
              <input
                type="radio"
                name="quality"
                checked={q === quality}
                onChange={() => changeQuality(q)}
              />
              <span className={styles.name}>{q.label}</span>
              <span className={styles.cap}>up to {formatLimit(q.max_seconds)}</span>
              {q === quality && (
                <span className={styles.note}>Source{q.fps ? ` · ${q.fps} fps` : ""}</span>
              )}
            </label>
          ))}
        </fieldset>

        <fieldset className={styles.group}>
          <legend>Cut</legend>
          {MODES.map((m) => (
            <label key={m.id} className={styles.option}>
              <input
                type="radio"
                name="mode"
                checked={m.id === mode}
                onChange={() => setMode(m.id)}
              />
              <span className={styles.name}>{m.name}</span>
              <span className={styles.note}>{m.id === "fast" ? "Recommended · " : ""}{m.note.replace("Stream copy. ", "")}</span>
            </label>
          ))}
        </fieldset>
        </div>

        <div className={styles.action} aria-live="polite">
          <div className={styles.summary}>
            <span><small>Length</small>{formatTime(length)}</span>
            <span><small>Est. size</small>~{formatSize(estimatedBytes)}</span>
          </div>
          {view.stage === "done" ? (
            <>
              <a className={styles.exportDownload} href={fileUrl(view.id)}>
                <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 3v12m0 0 4-4m-4 4-4-4M4 20h16" /></svg>
                Save clip - {formatSize(view.size)}
              </a>
              <p className={styles.note}>The link works for about {Math.round(view.expiresIn / 60)} minutes.</p>
              <button type="button" className={styles.exportReset} onClick={reset}>
                Make another clip
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                className={styles.exportDownload}
                disabled={busy || tooLong}
                onClick={download}
              >
                <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 3v12m0 0 4-4m-4 4-4-4M4 20h16" /></svg>
                {busy ? "Preparing clip..." : "Download clip"}
              </button>
              <Progress view={view} />
              {view.stage === "error" && (
                <p role="alert" className={styles.error}>
                  {view.message}
                </p>
              )}
            </>
          )}
        </div>
      </aside>
    </section>
  );
}

function Progress({ view }: { view: JobView }) {
  if (view.stage === "queued") {
    return <p className={styles.note}>Waiting in line, you're number <span className={styles.numeric}>{view.position}</span>.</p>;
  }
  if (view.stage !== "working") return null;

  const { elapsed, estimate } = view;
  const percent = estimate ? Math.min(95, (elapsed / estimate) * 100) : null;
  return (
    <div>
      <div
        className={`${styles.progress} ${percent === null ? styles.indeterminate : ""}`}
        role="progressbar"
        aria-valuenow={percent === null ? undefined : Math.round(percent)}
      >
        <div className={styles.fill} style={percent === null ? undefined : { width: `${percent}%` }} />
      </div>
      <p className={styles.note}>
        {view.phase === "downloading" ? "Downloading source" : view.phase === "cutting" ? "Cutting clip" : "Preparing clip"}{" "}
        <span className={styles.numeric}>{view.percent}%</span>
        {" - "}
        <span className={styles.numeric}>{formatClock(elapsed)}</span>
        {estimate ? <> - about <span className={styles.numeric}>{formatClock(estimate)}</span></> : null}
      </p>
      <p className={styles.note}>Long or 4K clips can take a few minutes. Keep this tab open.</p>
    </div>
  );
}
