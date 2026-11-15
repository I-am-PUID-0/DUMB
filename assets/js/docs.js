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
    document.querySelectorAll(".docs-geek").forEach(button => {
      button.setAttribute("aria-pressed", String(enabled));
      button.title = enabled ? "Collapse technical details in guides" : "Expand technical details in guides";
    });
    document.querySelectorAll("details.geek-details").forEach(detail => { detail.open = enabled; });
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
