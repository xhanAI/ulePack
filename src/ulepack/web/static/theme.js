// Runs in <head> before first paint: applies the saved theme (auto = follow the OS).
(function () {
  try {
    const t = localStorage.getItem("ulepack-theme");
    if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
  } catch (e) { /* storage blocked: stay on auto */ }
})();
