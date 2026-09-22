"use client";

import { useMemo, useRef, useState } from "react";
import type { VideoInfo } from "@/lib/api";
import { useEditorYouTube } from "@/lib/useEditorYouTube";
import styles from "./EditorView.module.css";

type Props = { info: VideoInfo; onBack: () => void };
type Quality = "4K" | "1080p" | "720p";
const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(max, value));
const clock = (seconds: number) => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;

function RangeSlider({ label, value, min, max, onChange }: {
  label: string; value: number; min: number; max: number; onChange: (value: number) => void;
}) {
  const set = (next: number) => onChange(clamp(next, min, max));
  return (
    <span
      className={styles.handleHit}
      style={{ left: `${(value / max) * 100}%` }}
      role="slider"
      tabIndex={0}
      aria-label={label}
      aria-valuemin={min}
      aria-valuemax={max}
      aria-valuenow={value}
      onKeyDown={(event) => {
        const step = event.shiftKey ? 5 : 1;
        if (event.key === "ArrowLeft") { event.preventDefault(); set(value - step); }
        if (event.key === "ArrowRight") { event.preventDefault(); set(value + step); }
        if (event.key === "Home") { event.preventDefault(); set(min); }
        if (event.key === "End") { event.preventDefault(); set(max); }
      }}
    />
  );
}

export function EditorView({ info, onBack }: Props) {
  const [start, setStart] = useState(0);
  const [end, setEnd] = useState(Math.min(info.duration, 30));
  const [quality, setQuality] = useState<Quality>("1080p");
  const mountRef = useRef<HTMLDivElement>(null);
  useEditorYouTube(info.id, start, end, mountRef);

  const updateStart = (value: number) => setStart(clamp(Math.min(value, end - 1), 0, info.duration - 1));
  const updateEnd = (value: number) => setEnd(clamp(Math.max(value, start + 1), 1, info.duration));
  const length = useMemo(() => end - start, [end, start]);

  const inputTime = (value: string, fallback: number) => {
    const parts = value.split(":").map(Number);
    if (parts.some(Number.isNaN)) return fallback;
    return parts.length === 2 ? parts[0] * 60 + parts[1] : Number(value);
  };

  return (
    <section className={`${styles.editor} ${styles.entering}`}>
      <button type="button" className={styles.back} onClick={onBack}>← Back</button>
      <h1 className={styles.title}>{info.title}</h1>
      <div className={styles.player}>
        <div ref={mountRef} className={styles.playerMount} />
      </div>
      <div className={styles.timeline} onPointerDown={(event) => {
        if (event.target !== event.currentTarget) return;
        const rect = event.currentTarget.getBoundingClientRect();
        const value = ((event.clientX - rect.left) / rect.width) * info.duration;
        Math.abs(value - start) < Math.abs(value - end) ? updateStart(value) : updateEnd(value);
      }}>
        <span className={styles.selected} style={{ left: `${(start / info.duration) * 100}%`, width: `${(length / info.duration) * 100}%` }} />
        <RangeSlider label="Clip start" value={start} min={0} max={Math.max(0, end - 1)} onChange={updateStart} />
        <RangeSlider label="Clip end" value={end} min={Math.min(info.duration, start + 1)} max={info.duration} onChange={updateEnd} />
      </div>
      <div className={styles.controls}>
        <label>Start<input value={clock(start)} onChange={(e) => updateStart(inputTime(e.target.value, start))} aria-label="Start time" /></label>
        <label>End<input value={clock(end)} onChange={(e) => updateEnd(inputTime(e.target.value, end))} aria-label="End time" /></label>
        <label>Length<output>{clock(length)}</output></label>
        <label className={styles.quality}>Quality<select value={quality} onChange={(e) => setQuality(e.target.value as Quality)}><option value="4K">Up to 4K</option><option value="1080p">1080p</option><option value="720p">720p</option></select></label>
      </div>
      <button type="button" className={styles.download}>Download clip</button>
    </section>
  );
}
