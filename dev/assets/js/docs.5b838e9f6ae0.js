/* One shared channel selector and a reading preference; no remote services. */
(() => {
  const preferenceKey = "dumb-docs-geek-mode";
  const inventories = new Map();

  function readPreference() {
    try { return localStorage.getItem(preferenceKey) === "true"; }
    catch { return false; }
  }

  function showHashTarget() {
    let id;
    try { id = decodeURIComponent(location.hash.slice(1)); }
    catch { return; }
    const target = document.getElementById(id);
    if (!target) return;
    for (let parent = target.parentElement; parent; parent = parent.parentElement) {
      if (parent.tagName === "DETAILS") parent.open = true;
    }
    if (location.hash) target.scrollIntoView({ block: "start" });
  }

  function applyMode(enabled) {
    document.documentElement.dataset.geekMode = String(enabled);
    const details = document.querySelectorAll("details.geek-details");
    document.querySelectorAll(".docs-geek").forEach(button => {
      if (!button.querySelector(".docs-geek-state")) {
        const label = document.createElement("span");
        label.className = "docs-geek-button-label";
        label.textContent = "Geek mode";
        const shortLabel = document.createElement("span");
        shortLabel.className = "docs-geek-button-short";
        shortLabel.textContent = "Geek";
        shortLabel.setAttribute("aria-hidden", "true");
        const state = document.createElement("span");
        state.className = "docs-geek-state";
        state.setAttribute("aria-hidden", "true");
        button.replaceChildren(label, shortLabel, state);
      }
      button.querySelector(".docs-geek-state").textContent = enabled ? "On" : "Off";
      button.setAttribute("aria-pressed", String(enabled));
      button.setAttribute("aria-label", `Geek mode is ${enabled ? "on" : "off"}. ${enabled ? "Hide" : "Show"} technical explanations and alternative methods.`);
      button.title = enabled ? "Collapse optional explanations and alternative methods" : "Show internals and alternative API, CLI, or configuration methods";
    });
    details.forEach(detail => {
      const summary = detail.querySelector("summary");
      if (summary && !summary.querySelector(".docs-geek-badge")) {
        const badge = document.createElement("span");
        badge.className = "docs-geek-badge";
        badge.textContent = "Geek detail";
        summary.prepend(badge, document.createTextNode(" "));
      }
      detail.open = enabled;
    });
    const article = document.querySelector("article.md-content__inner, article");
    if (article) {
      let status = article.querySelector(":scope > .docs-geek-status");
      if (!status) {
        status = document.createElement("aside");
        status.className = "docs-geek-status";
        status.setAttribute("role", "status");
        const heading = document.createElement("strong");
        heading.textContent = "Geek mode is on ";
        status.append(heading, document.createElement("span"));
        const channelContext = article.querySelector(":scope > .docs-channel-context");
        if (channelContext) channelContext.after(status);
        else article.prepend(status);
      }
      status.querySelector("span").textContent = details.length
        ? `${details.length} optional Geek ${details.length === 1 ? "section" : "sections"}, highlighted in green: internals and alternative methods. The main frontend instructions stay visible in either mode.`
        : "This page has no optional Geek sections. The main guide stays available in either mode; Geek reference contains technical explanations and other methods.";
      status.hidden = !enabled;
    }
    showHashTarget();
  }

  async function switchChannel(event, link, tools) {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    const root = new URL(tools.dataset.docsRoot, location.href);
    const destination = new URL(link.dataset.docsVersion === "dev" ? "dev/" : "./", root);
    const currentBase = new URL(tools.dataset.docsChannel === "dev" ? "dev/" : "./", root);
    const currentPath = location.pathname.startsWith(currentBase.pathname)
      ? location.pathname.slice(currentBase.pathname.length) : "";
    try {
      const inventoryUrl = new URL("channel.json", destination).href;
      if (!inventories.has(inventoryUrl)) {
        inventories.set(inventoryUrl, fetch(inventoryUrl).then(response => {
          if (!response.ok) throw new Error("Channel inventory unavailable");
          return response.json();
        }).catch(error => { inventories.delete(inventoryUrl); throw error; }));
      }
      const inventory = await inventories.get(inventoryUrl);
      if (inventory.pages.includes(currentPath)) {
        destination.pathname += currentPath;
        destination.hash = location.hash;
      }
      location.assign(destination.href);
    } catch {
      // A newer dev page may have no equivalent in the frozen release.
      location.assign(destination.href);
    }
  }

  function initialize() {
    // The header survives instant navigation; keep home links on their original
    // origin and channel when the current page changes directory depth.
    document.querySelectorAll("[data-docs-home]").forEach(link => {
      link.href = new URL(link.getAttribute("href"), location.href).href;
    });
    const tools = document.querySelector(".docs-tools");
    if (tools && !tools.dataset.initialized) {
      // The shared header survives instant navigation; freeze its site root
      // before the current document URL can move to another path depth.
      tools.dataset.docsRoot = new URL(tools.dataset.docsRoot, location.href).href;
      tools.dataset.initialized = "true";
      tools.querySelector(".docs-geek").addEventListener("click", () => {
        const enabled = document.documentElement.dataset.geekMode !== "true";
        try { localStorage.setItem(preferenceKey, String(enabled)); } catch { /* Storage is optional. */ }
        applyMode(enabled);
      });
      tools.querySelectorAll("[data-docs-version]").forEach(link => {
        link.addEventListener("click", event => switchChannel(event, link, tools));
      });
      tools.addEventListener("keydown", event => {
        if (event.key === "Escape") {
          const details = tools.querySelector("details");
          details.open = false;
          details.querySelector("summary").focus();
        }
      });
    }
    if (tools) {
      const root = new URL(tools.dataset.docsRoot, location.href);
      const currentBase = new URL(tools.dataset.docsChannel === "dev" ? "dev/" : "./", root);
      const path = location.pathname.startsWith(currentBase.pathname)
        ? location.pathname.slice(currentBase.pathname.length) : "";
      tools.querySelectorAll("[data-docs-version]").forEach(link => {
        const base = new URL(link.dataset.docsVersion === "dev" ? "dev/" : "./", root);
        link.href = new URL(path, base).href;
      });
    }
    applyMode(readPreference());
  }

  if (typeof document$ !== "undefined") document$.subscribe(initialize);
  else if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initialize);
  else initialize();
  window.addEventListener("hashchange", showHashTarget);
})();
