"use client";

import { useCallback, useEffect, useRef, useState } from "react";

declare global {
  interface Window {
    onYouTubeIframeAPIReady?: () => void;
  }
}

let apiLoaded: Promise<void> | undefined;

type PlayerApi = {
  destroy?: unknown;
  getCurrentTime?: unknown;
  getPlayerState?: unknown;
  isMuted?: unknown;
  mute?: unknown;
  unMute?: unknown;
  pauseVideo?: unknown;
  playVideo?: unknown;
  seekTo?: unknown;
};

function playerApi(value: unknown): PlayerApi | null {
  return value !== null && typeof value === "object" ? (value as PlayerApi) : null;
}

function callPlayerMethod<K extends keyof PlayerApi>(
  value: unknown,
  method: K,
  ...args: unknown[]
): unknown {
  const player = playerApi(value);
  const callback = player?.[method];
  if (typeof callback !== "function") return undefined;
  return callback.call(value, ...args);
}

function loadYouTubeApi(): Promise<void> {
  apiLoaded ??= new Promise((resolve) => {
    if (window.YT?.Player) return resolve();
    window.onYouTubeIframeAPIReady = resolve;
    const script = document.createElement("script");
    script.src = "https://www.youtube.com/iframe_api";
    document.head.append(script);
  });
  return apiLoaded;
}

/** Embeds a YouTube player into `mountRef` and exposes its playhead. */
export function useYouTubePlayer(videoId: string | null) {
  const mountRef = useRef<HTMLDivElement>(null);
  const playerRef = useRef<YT.Player | undefined>(undefined);
  const [time, setTime] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [muted, setMuted] = useState(false);
  const [started, setStarted] = useState(false);

  useEffect(() => {
    const mount = mountRef.current;
    let cancelled = false;
    if (!videoId) return () => undefined;

    loadYouTubeApi().then(() => {
      if (cancelled || !mount) return;
      // The API swaps its target for an iframe, so give it a disposable child.
      const target = mount.appendChild(document.createElement("div"));
      playerRef.current = new YT.Player(target, {
        videoId,
        width: "100%",
        height: "100%",
        playerVars: { controls: 0, modestbranding: 1, rel: 0, iv_load_policy: 3, playsinline: 1 },
      });
    });

    const poll = setInterval(() => {
      const t = callPlayerMethod(playerRef.current, "getCurrentTime");
      if (typeof t === "number") setTime((prev) => (Math.abs(prev - t) < 0.05 ? prev : t));
      const playerState = callPlayerMethod(playerRef.current, "getPlayerState");
      setPlaying(playerState === 1);
      setStarted(playerState !== -1 && playerState !== 5 && typeof playerState === "number");
      const playerMuted = callPlayerMethod(playerRef.current, "isMuted");
      if (typeof playerMuted === "boolean") setMuted(playerMuted);
    }, 100);

    return () => {
      cancelled = true;
      clearInterval(poll);
      callPlayerMethod(playerRef.current, "destroy");
      playerRef.current = undefined;
      mount?.replaceChildren();
    };
  }, [videoId]);

  const seekTo = useCallback((seconds: number) => {
    callPlayerMethod(playerRef.current, "seekTo", seconds, true);
    setTime(seconds);
  }, []);
  const play = useCallback(() => {
    callPlayerMethod(playerRef.current, "playVideo");
  }, []);
  const pause = useCallback(() => {
    callPlayerMethod(playerRef.current, "pauseVideo");
  }, []);
  const toggleMute = useCallback(() => {
    if (muted) callPlayerMethod(playerRef.current, "unMute");
    else callPlayerMethod(playerRef.current, "mute");
    setMuted((value) => !value);
  }, [muted]);

  return { mountRef, time, playing, muted, started, seekTo, play, pause, toggleMute };
}
