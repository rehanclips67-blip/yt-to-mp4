"use client";

import { useEffect, useRef, useState } from "react";
import { clamp, formatClock, formatTime, MIN_CLIP, round1 } from "@/lib/time";
import styles from "./Timeline.module.css";

export interface Range {
  start: number;
  end: number;
}

type Edge = "start" | "end";

interface Props {
  duration: number;
  videoId: string;
  thumbnail: string | null;
  range: Range;
  playhead: number;
  onRangeChange: (range: Range) => void;
  onSeek: (seconds: number) => void;
}

const TICK_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200];

/** Smallest step that keeps the ruler to about eight labels. */
const tickStep = (duration: number) => TICK_STEPS.find((s) => duration / s <= 8) ?? 7200;

export function Timeline({ duration, videoId, thumbnail, range, playhead, onRangeChange, onSeek }: Props) {
  const trackRef = useRef<HTMLDivElement>(null);
  const rulerRef = useRef<HTMLDivElement>(null);
  const grabOffset = useRef(0); // where on the handle the pointer took hold, in seconds
  const [trackWidth, setTrackWidth] = useState(0);
  const [hiddenTicks, setHiddenTicks] = useState<Set<number>>(new Set());
  const percent = (seconds: number) => `${(seconds / duration) * 100}%`;

  useEffect(() => {
    const track = trackRef.current;
    if (!track) return;
    const update = () => setTrackWidth(track.getBoundingClientRect().width);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(track);
    return () => observer.disconnect();
  }, []);

  const timeAt = (clientX: number) => {
    const { left, width } = trackRef.current!.getBoundingClientRect();
    return round1(clamp((clientX - left) / width, 0, 1) * duration);
  };

  const withEdge = (edge: Edge, seconds: number): Range =>
    edge === "start"
      ? { start: clamp(seconds, 0, range.end - MIN_CLIP), end: range.end }
      : { start: range.start, end: clamp(seconds, range.start + MIN_CLIP, duration) };

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target;
      if (target instanceof HTMLElement && target.matches("input, textarea, select, [contenteditable='true']")) return;

      if (event.key.toLowerCase() === "i") {
        event.preventDefault();
        onRangeChange(withEdge("start", playhead));
      } else if (event.key.toLowerCase() === "o") {
        event.preventDefault();
        onRangeChange(withEdge("end", playhead));
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onRangeChange, playhead, range.end, range.start, duration]);

  const nudge = (edge: Edge, event: React.KeyboardEvent) => {
    const direction = { ArrowLeft: -1, ArrowRight: 1 }[event.key];
    if (!direction) return;
    event.preventDefault();
    const next = withEdge(edge, round1(range[edge] + direction * (event.shiftKey ? 1 : 0.1)));
    onRangeChange(next);
    onSeek(next[edge]);
  };

  const step = tickStep(duration);
  const rawTicks = Array.from({ length: Math.floor(duration / step) + 1 }, (_, i) => i * step);
  const ticks = rawTicks.filter((tick, index) => {
    if (index === 0 || trackWidth === 0) return true;
    const position = (tick / duration) * trackWidth;
    const firstWidth = 4 * 7;
    return position >= firstWidth + 24;
  });
  const tickKey = ticks.join(",");
  useEffect(() => {
    const ruler = rulerRef.current;
    const track = trackRef.current;
    if (!ruler || !track || trackWidth === 0) return;
    const measure = () => {
      const labels = [...ruler.querySelectorAll<HTMLElement>("[data-tick-index]")];
      const hidden = new Set<number>();
      let previous: DOMRect | null = null;
      const trackRect = track.getBoundingClientRect();
      labels.forEach((label) => {
        const index = Number(label.dataset.tickIndex);
        const rect = label.getBoundingClientRect();
        if ((previous && rect.left - previous.right < 12) || rect.right > trackRect.right) {
          hidden.add(index);
        } else {
          previous = rect;
        }
      });
      setHiddenTicks(hidden);
    };
    const frame = requestAnimationFrame(measure);
    return () => cancelAnimationFrame(frame);
  }, [tickKey, trackWidth]);
  const narrow = trackWidth > 0 && ((range.end - range.start) / duration) * trackWidth < 40;

  return (
    <div className={styles.timeline}>
      <div ref={trackRef} className={styles.track} onPointerDown={(e) => onSeek(timeAt(e.clientX))}>
        <div className={styles.frames} aria-hidden="true">
          {[thumbnail, `https://img.youtube.com/vi/${videoId}/hq1.jpg`, `https://img.youtube.com/vi/${videoId}/hq2.jpg`, `https://img.youtube.com/vi/${videoId}/hq3.jpg`].map((source, index) => (
            <span className={styles.frame} key={`${source ?? "fallback"}-${index}`}>
              {source && <img src={source} alt="" onError={(event) => { event.currentTarget.style.display = "none"; }} />}
            </span>
          ))}
        </div>
        <div className={`${styles.dim} ${styles.dimLeft}`} style={{ width: percent(range.start) }} />
        <div className={`${styles.dim} ${styles.dimRight}`} style={{ left: percent(range.end), width: percent(duration - range.end) }} />
        <div
          className={styles.range}
          style={{ left: percent(range.start), width: percent(range.end - range.start) }}
        />
        <span
          className={styles.tooltip}
          style={{
            left: `clamp(48px, ${percent((range.start + range.end) / 2)}, calc(100% - 48px))`,
          }}
        >
          {formatClock(range.start)} – {formatClock(range.end)}
        </span>
        <div className={styles.playhead} style={{ left: percent(clamp(playhead, 0, duration)) }} />
        {(["start", "end"] as const).map((edge) => (
          <div
            key={edge}
            role="slider"
            tabIndex={0}
            aria-label={edge === "start" ? "Clip start" : "Clip end"}
            aria-valuemin={edge === "start" ? 0 : range.start + MIN_CLIP}
            aria-valuemax={edge === "start" ? range.end - MIN_CLIP : duration}
            aria-valuenow={range[edge]}
            aria-valuetext={formatTime(range[edge])}
            className={`${styles.handle} ${styles[edge]} ${narrow ? styles.narrow : ""}`}
            style={{ left: percent(range[edge]) }}
            onPointerDown={(e) => {
              e.stopPropagation();
              e.currentTarget.setPointerCapture(e.pointerId);
              grabOffset.current = timeAt(e.clientX) - range[edge];
            }}
            onPointerMove={(e) => {
              if (e.currentTarget.hasPointerCapture(e.pointerId)) {
                onRangeChange(withEdge(edge, round1(timeAt(e.clientX) - grabOffset.current)));
              }
            }}
            onPointerUp={() => onSeek(range[edge])}
            onKeyDown={(e) => nudge(edge, e)}
          >
            <span className={styles.grip} />
          </div>
        ))}
      </div>
      <div ref={rulerRef} className={styles.ruler} aria-hidden>
        {ticks.map((t, index) => (
          hiddenTicks.has(index) ? null : (
          <span
            key={t}
            data-tick-index={index}
            className={`${styles.tick} ${index === 0 ? styles.firstTick : ""}`}
            style={{ left: percent(t) }}
          >
            {formatClock(t)}
          </span>
          )
        ))}
      </div>
    </div>
  );
}
