/* Theme-matched real UI previews, with explicit motion and full-size image controls. */
(() => {
  const pausedKey = "dumb-docs-showcase-paused";
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
  const systemTheme = matchMedia("(prefers-color-scheme: dark)");
  const controllers = new Set();
  const initialized = new WeakSet();
  const manualVideos = new WeakMap();
  let imageDialog;

  function darkTheme() {
    const scheme = document.body?.dataset.mdColorScheme;
    return scheme ? scheme === "slate" : systemTheme.matches;
  }

  function preference() {
    try { return localStorage.getItem(pausedKey) === "true"; }
    catch { return false; }
  }

  function savePreference(paused) {
    try { localStorage.setItem(pausedKey, String(paused)); }
    catch { /* Motion controls still work when storage is unavailable. */ }
  }

  function localAsset(value) {
    try {
      const url = new URL(value, location.href);
      return url.origin === location.origin && ["http:", "https:"].includes(url.protocol) ? url.href : null;
    } catch { return null; }
  }

  function syncPictures() {
    const dark = darkTheme();
    document.querySelectorAll("picture[data-ui-picture] source[data-ui-dark]").forEach(source => {
      source.media = dark ? "all" : "not all";
    });
    requestAnimationFrame(() => {
      document.querySelectorAll("a[data-showcase-zoom]").forEach(link => {
        const image = link.querySelector("img");
        if (image?.currentSrc) link.href = image.currentSrc;
      });
    });
  }

  function syncManualVideos() {
    const theme = darkTheme() ? "dark" : "light";
    document.querySelectorAll("video[data-ui-video]").forEach(video => {
      const source = localAsset(theme === "dark" ? video.dataset.darkVideo : video.dataset.lightVideo);
      const poster = localAsset(theme === "dark" ? video.dataset.darkPoster : video.dataset.lightPoster);
      if (!source || !poster) return;
      video.poster = poster;
      const previous = manualVideos.get(video);
      if (previous?.source === source) return;
      const position = Number.isFinite(video.currentTime) ? video.currentTime : 0;
      const wasPlaying = !video.paused && !video.ended;
      const selection = { source };
      manualVideos.set(video, selection);
      video.pause();
      video.src = source;
      video.load();
      video.addEventListener("loadedmetadata", () => {
        if (manualVideos.get(video) !== selection) return;
        if (position > 0 && Number.isFinite(video.duration)) video.currentTime = Math.min(position, Math.max(0, video.duration - .05));
        if (wasPlaying && document.contains(video) && !document.hidden) void video.play().catch(() => {});
      }, { once: true });
    });
  }

  function enlargeImage(event, link) {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const image = link.querySelector("img");
    const source = localAsset(image?.currentSrc || image?.src);
    if (!source || typeof HTMLDialogElement === "undefined") return;
    event.preventDefault();
    if (!imageDialog) {
      imageDialog = document.createElement("dialog");
      imageDialog.className = "docs-lightbox showcase-lightbox";
      imageDialog.setAttribute("aria-label", "Full-size DUMB interface example");
      const enlarged = document.createElement("img");
      const close = document.createElement("button");
      close.type = "button";
      close.textContent = "Close image";
      close.addEventListener("click", () => imageDialog.close());
      imageDialog.addEventListener("click", event => {
        if (event.target !== imageDialog) return;
        const bounds = imageDialog.getBoundingClientRect();
        if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) imageDialog.close();
      });
      imageDialog.append(enlarged, close);
      document.body.append(imageDialog);
    }
    const enlarged = imageDialog.querySelector("img");
    enlarged.src = source;
    enlarged.alt = image.alt;
    imageDialog.showModal();
  }

  function initializeShowcase(figure) {
    const video = figure.querySelector("[data-showcase-video]");
    const button = figure.querySelector("[data-showcase-toggle]");
    if (!video || !button) return;
    const state = {
      figure, video, button, visible: false, manual: false,
      userPaused: preference(), loadedTheme: null, stopReason: null,
    };
    video.muted = true;
    button.hidden = false;

    function updateControl() {
      const playing = !video.paused && !video.ended;
      button.querySelector("[data-showcase-label]").textContent = playing ? "Pause preview" : "Play preview";
      button.querySelector("[data-showcase-icon]").textContent = playing ? "Ⅱ" : "▶";
      button.setAttribute("aria-label", playing ? "Pause the interface preview" : "Play the interface preview");
      figure.dataset.state = playing ? "playing" : video.hidden ? "poster" : "paused";
    }

    function stop(reason) {
      if (!video.paused) {
        state.stopReason = reason;
        video.pause();
      }
      updateControl();
    }

    function selectedSource() {
      const theme = darkTheme() ? "dark" : "light";
      return {
        theme,
        video: localAsset(theme === "dark" ? video.dataset.darkVideo : video.dataset.lightVideo),
        poster: localAsset(theme === "dark" ? video.dataset.darkPoster : video.dataset.lightPoster),
      };
    }

    function loadTheme() {
      const source = selectedSource();
      if (!source.video || !source.poster) return false;
      video.poster = source.poster;
      if (state.loadedTheme !== source.theme) {
        stop("theme");
        video.hidden = true;
        video.src = source.video;
        video.load();
        state.loadedTheme = source.theme;
      }
      return true;
    }

    async function playIfNeeded() {
      const automatic = !reducedMotion.matches && !navigator.connection?.saveData;
      if (!state.visible || document.hidden || state.userPaused || (!automatic && !state.manual)) return;
      if (!loadTheme()) return;
      try {
        await video.play();
        video.hidden = false;
      } catch {
        video.hidden = true;
      }
      updateControl();
    }

    button.addEventListener("click", () => {
      if (!video.paused) {
        state.userPaused = true;
        state.manual = false;
        savePreference(true);
        stop("manual");
      } else {
        state.userPaused = false;
        state.manual = true;
        state.visible = true;
        savePreference(false);
        void playIfNeeded();
      }
    });
    video.addEventListener("play", updateControl);
    video.addEventListener("pause", () => {
      if (!state.stopReason && !video.ended) {
        state.userPaused = true;
        state.manual = false;
        savePreference(true);
      }
      state.stopReason = null;
      updateControl();
    });
    video.addEventListener("error", () => { video.hidden = true; updateControl(); });
    const observer = new IntersectionObserver(entries => {
      state.visible = entries.some(entry => entry.isIntersecting && entry.intersectionRatio >= .15);
      if (state.visible) void playIfNeeded();
      else stop("offscreen");
    }, { threshold: [0, .15] });
    observer.observe(figure);
    state.sync = () => {
      if (state.loadedTheme && state.loadedTheme !== (darkTheme() ? "dark" : "light")) loadTheme();
      if (document.hidden || (reducedMotion.matches && !state.manual)) {
        stop("preference");
        if (reducedMotion.matches && !state.manual) video.hidden = true;
      } else void playIfNeeded();
      updateControl();
    };
    state.dispose = () => { observer.disconnect(); stop("navigation"); };
    controllers.add(state);
    updateControl();
  }

  function synchronize() {
    syncPictures();
    syncManualVideos();
    controllers.forEach(state => state.sync());
  }

  function initialize() {
    controllers.forEach(state => {
      if (!document.contains(state.figure)) { state.dispose(); controllers.delete(state); }
    });
    document.querySelectorAll("[data-showcase]").forEach(figure => {
      if (initialized.has(figure)) return;
      initialized.add(figure);
      initializeShowcase(figure);
    });
    document.querySelectorAll("a[data-showcase-zoom]").forEach(link => {
      if (initialized.has(link)) return;
      initialized.add(link);
      link.addEventListener("click", event => enlargeImage(event, link));
    });
    syncPictures();
    syncManualVideos();
  }

  new MutationObserver(synchronize).observe(document.body, { attributes: true, attributeFilter: ["data-md-color-scheme"] });
  reducedMotion.addEventListener("change", synchronize);
  systemTheme.addEventListener("change", synchronize);
  document.addEventListener("visibilitychange", synchronize);
  if (typeof document$ !== "undefined") document$.subscribe(initialize);
  else if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initialize);
  else initialize();
})();
