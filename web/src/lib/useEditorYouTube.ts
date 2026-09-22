"use client";

import { useEffect, useRef } from "react";

declare global {
  interface Window {
    onYouTubeIframeAPIReady?: () => void;
  }
}

let apiPromise: Promise<void> | undefined;

function loadApi() {
  apiPromise ??= new Promise((resolve) => {
    if (window.YT?.Player) return resolve();
    window.onYouTubeIframeAPIReady = resolve;
    const script = document.createElement("script");
    script.src = "https://www.youtube.com/iframe_api";
    document.head.append(script);
  });
  return apiPromise;
}

export function useEditorYouTube(
  videoId: string,
  start: number,
  end: number,
  mountRef: React.RefObject<HTMLDivElement | null>,
) {
  const playerRef = useRef<YT.Player | undefined>(undefined);

  useEffect(() => {
    let cancelled = false;
    loadApi().then(() => {
      if (cancelled || !mountRef.current) return;
      const target = mountRef.current.appendChild(document.createElement("div"));
      playerRef.current = new YT.Player(target, {
        videoId,
        width: "100%",
        height: "100%",
        host: "https://www.youtube-nocookie.com",
        playerVars: { controls: 1, modestbranding: 1, rel: 0, playsinline: 1 },
      });
    });
    return () => {
      cancelled = true;
      playerRef.current?.destroy();
      playerRef.current = undefined;
      mountRef.current?.replaceChildren();
    };
  }, [videoId, mountRef]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      const player = playerRef.current;
      if (!player || typeof player.getCurrentTime !== "function") return;
      if (player.getCurrentTime() >= end) {
        player.seekTo(start, true);
        player.pauseVideo();
      }
    }, 150);
    return () => window.clearInterval(timer);
  }, [start, end]);
}
