// Audiobus Remote — theme selector (auto / light / dark), persisted in localStorage.
// The effective theme (light or dark) is applied as <html data-theme>; "auto"
// follows the OS/browser and updates live. A tiny inline <head> script sets the
// attribute before first paint to avoid a flash; this file wires the <select> and
// keeps auto mode in sync with the OS.
(function () {
  var KEY = "audiobus-theme";
  var mq = window.matchMedia("(prefers-color-scheme: light)");

  function pref() {
    try {
      var v = localStorage.getItem(KEY);
      return v === "light" || v === "dark" ? v : "auto";
    } catch (e) {
      return "auto";
    }
  }
  function effective(p) {
    return p === "auto" ? (mq.matches ? "light" : "dark") : p;
  }
  function apply(p) {
    document.documentElement.setAttribute("data-theme", effective(p));
  }

  // Keep auto mode in sync with OS changes.
  function onMq() {
    if (pref() === "auto") apply("auto");
  }
  if (mq.addEventListener) mq.addEventListener("change", onMq);
  else if (mq.addListener) mq.addListener(onMq);

  function wire() {
    var sel = document.getElementById("themeSelect");
    if (!sel) return;
    sel.value = pref();
    sel.addEventListener("change", function () {
      var p = sel.value;
      try {
        localStorage.setItem(KEY, p);
      } catch (e) {
        /* ignore */
      }
      apply(p);
    });
  }

  apply(pref()); // ensure it's set even if the inline head script was missing
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();
