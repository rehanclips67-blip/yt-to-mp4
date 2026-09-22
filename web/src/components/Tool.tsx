"use client";

import { useRef, useState } from "react";
import { fetchInfo, type VideoInfo } from "@/lib/api";
import { Workspace } from "./Workspace";
import styles from "./Tool.module.css";

type State =
  | { status: "idle" | "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; url: string; info: VideoInfo };

export function Tool() {
  const [state, setState] = useState<State>({ status: "idle" });
  const [validationHint, setValidationHint] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const loading = state.status === "loading";

  const validateUrl = (value: string) => {
    if (!value) return "Please enter a valid YouTube link.";

    try {
      const parsed = new URL(value);
      const host = parsed.hostname.replace(/^www\./, "");
      if (!/^(youtube\.com|youtu\.be|m\.youtube\.com)$/.test(host)) {
        return "Please enter a valid YouTube link.";
      }
      return "";
    } catch {
      return "Please enter a valid YouTube link.";
    }
  };

  const load = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const url = String(new FormData(event.currentTarget).get("video-url")).trim();
    const validationMessage = validateUrl(url);

    if (validationMessage) {
      setValidationHint(validationMessage);
      inputRef.current?.focus();
      return;
    }

    setValidationHint("");
    setState({ status: "loading" });
    try {
      setState({ status: "ready", url, info: await fetchInfo(url) });
    } catch (error) {
      const message = error instanceof Error ? error.message : "Something went wrong.";
      setState({ status: "error", message });
    }
  };

  return (
    <div className={`${styles.page} ${state.status === "ready" ? styles.compact : ""}`}>
      <header className={`${styles.bar} ${state.status === "ready" ? styles.editorBar : ""}`}>
        <span className={styles.mark}>Clipper</span>
        {state.status === "ready" && <div id="editor-header-actions" className={styles.editorHeaderActions} />}
      </header>

      <main className={styles.main}>
        {state.status === "ready" ? (
          <Workspace key={state.info.id} info={state.info} url={state.url} />
        ) : (
          <section className={styles.landingHero} aria-label="Load a video">
            <div className={styles.heroContent}>
              <div className={styles.hero}>
                <p className={styles.eyebrow}>YOUTUBE CLIPPER</p>
                <h1>
                  <span>Take only the</span>
                  <span>part you need.</span>
                </h1>
                <p className={styles.description}>Paste a YouTube link, choose your moment,<br className={styles.desktopBreak} /> and download the clip.</p>
              </div>

              <form className={styles.form} onSubmit={load} noValidate>
                <label className={styles.srOnly} htmlFor="youtube-url">YouTube URL</label>
                <span className={styles.inputIcon} aria-hidden="true">
                  <svg viewBox="0 0 24 24" fill="none">
                    <path d="M9.5 14.5 14.5 9.5M7.25 17.75l-1 1a3.18 3.18 0 0 1-4.5-4.5l3.5-3.5a3.18 3.18 0 0 1 4.5 0" />
                    <path d="m16.75 6.25 1-1a3.18 3.18 0 0 1 4.5 4.5l-3.5 3.5a3.18 3.18 0 0 1-4.5 0" />
                  </svg>
                </span>
                <input
                  ref={inputRef}
                  id="youtube-url"
                  name="video-url"
                  type="url"
                  inputMode="url"
                  autoComplete="off"
                  autoCorrect="off"
                  autoCapitalize="off"
                  spellCheck={false}
                  required
                  autoFocus
                  placeholder="Paste YouTube link…"
                  aria-label="YouTube link"
                  onChange={() => setValidationHint("")}
                  onFocus={() => setValidationHint("")}
                />
                <button type="submit" className={styles.submit} aria-label={loading ? "Loading video" : "Clip it"}>
                  <span className={styles.submitLabel}>{loading ? "Loading…" : "Clip it"}</span>
                  <span className={styles.submitArrow} aria-hidden="true">{loading ? "…" : "→"}</span>
                </button>
              </form>
              {validationHint && <p className={styles.inlineHint} aria-live="polite">{validationHint}</p>}
              <p className={styles.microcopy}>
                <span><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="3" y="4" width="18" height="13" rx="2" /><path d="M8 19h8M12 17v2" /></svg>Up to 4K</span>
                <span><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="10" cy="8" r="3.5" /><path d="M3 20a7 7 0 0 1 14 0M17 6l4 4M21 6l-4 4" /></svg>No signup</span>
                <span><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2" /><path d="m3 18 5-5 3 3 4-5 6 7" /><path d="m3 3 18 18" /></svg>No watermark</span>
              </p>

              {loading && (
                <div className={styles.loading} role="status">
                  <strong>LOADING VIDEO</strong>
                  <span>This won’t take long.</span>
                  <div className={styles.loadingLine} aria-hidden />
                </div>
              )}
              {state.status === "error" && <p role="alert" className={styles.error}>{state.message}</p>}
            </div>
            <ClippingPreview />
          </section>
        )}
      </main>

      <footer className={styles.footer}>
        <div className={styles.footerInner}>
          <span className={styles.footerBrand}>© 2026 Clipper</span>
          <nav className={styles.footerLinks} aria-label="Footer links">
            <a href="#">Privacy</a>
            <a href="#">Terms</a>
          </nav>
        </div>
      </footer>
    </div>
  );
}

function ClippingPreview() {
  return (
    <div className={styles.visual} aria-hidden="true">
      <div className={styles.visualStage}>
        <div className={styles.stack}>
          <div className={styles.thumbnails}>
            <span /><span />
          </div>
          <div className={styles.mainFrame}>
            <span className={styles.framePlaceholder}>VIDEO PREVIEW</span>
            <span className={styles.playButton}><span /></span>
            <div className={styles.frameControls}>
              <div className={styles.frameControlMeta}>
                <span className={styles.framePlayIcon} aria-hidden="true" />
                <span>0:42 / 2:18</span>
              </div>
              <div className={styles.frameTrack} aria-hidden="true">
                <span className={styles.frameRangeLabel}>0:18</span>
                <span className={styles.frameTrackSelected} />
                <span className={`${styles.frameHandle} ${styles.frameHandleStart}`} />
                <span className={`${styles.frameHandle} ${styles.frameHandleEnd}`} />
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
