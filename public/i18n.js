// Audiobus Remote — tiny i18n runtime.
// Picks the best language from the browser (which follows the OS language),
// falling back to English, and applies translations to the page. connect.js
// also calls I18N.t(key) for dynamic strings.
//
// Load order (before connect.js): translations.js, then i18n.js.
(function () {
  const STRINGS = window.I18N_STRINGS || { en: {} };
  const EN = STRINGS.en || {};
  const RTL = new Set(["ar", "he", "fa", "ur"]);

  function pickLang() {
    const prefs =
      navigator.languages && navigator.languages.length
        ? navigator.languages
        : [navigator.language || "en"];
    for (const raw of prefs) {
      if (!raw) continue;
      const lower = String(raw).toLowerCase();
      if (STRINGS[lower]) return lower; // exact, e.g. "zh-tw", "pt-br"
      const base = lower.split("-")[0];
      if (STRINGS[base]) return base; // base, e.g. "es", "fr"
    }
    return "en";
  }

  const LANG = pickLang();
  const DICT = STRINGS[LANG] || EN;

  function t(key) {
    if (DICT && DICT[key] != null) return DICT[key];
    if (EN[key] != null) return EN[key];
    return key;
  }

  function apply(root) {
    root = root || document;
    root.querySelectorAll("[data-i18n]").forEach((el) => {
      el.textContent = t(el.getAttribute("data-i18n"));
    });
    root.querySelectorAll("[data-i18n-attr]").forEach((el) => {
      el.getAttribute("data-i18n-attr")
        .split(";")
        .forEach((pair) => {
          const idx = pair.indexOf(":");
          if (idx < 0) return;
          const attr = pair.slice(0, idx).trim();
          const key = pair.slice(idx + 1).trim();
          if (attr && key) el.setAttribute(attr, t(key));
        });
    });
    const titleKey = document.body && document.body.getAttribute("data-i18n-title");
    if (titleKey) document.title = t(titleKey);
  }

  document.documentElement.lang = LANG;
  if (RTL.has(LANG.split("-")[0])) document.documentElement.dir = "rtl";

  window.I18N = { t: t, apply: apply, lang: LANG };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      apply();
    });
  } else {
    apply();
  }
})();
