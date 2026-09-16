// A theme chosen with the toggle, applied before the page paints. Only a per-browser preference.
try {
  const theme = localStorage.getItem("maludb.theme");
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
} catch {
  /* storage blocked: follow the system setting */
}
