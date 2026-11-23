// Light or dark. The system's appearance decides unless you've said
// otherwise from ⌘K ("Use the light / dark theme"); that choice sets
// `data-theme` on <html>, which styles.css reads, and is remembered per
// browser. Choosing what the system would pick anyway clears the override,
// so the app follows the system again from then on.

const KEY = "theme";
const systemDark = () => window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;

function read() {
  try { return localStorage.getItem(KEY); } catch { return null; }
}

export function applyStoredTheme() {
  const t = read();
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
}

export function isDark() {
  const t = document.documentElement.dataset.theme;
  return t ? t === "dark" : systemDark();
}

// Settings says it in so many words, so what you pick is what stays picked:
// "system", or a theme held even if the system changes under it.
export const themeChoice = () => {
  const t = read();
  return t === "light" || t === "dark" ? t : "system";
};

export function setThemeChoice(choice) {
  try {
    if (choice === "system") localStorage.removeItem(KEY); else localStorage.setItem(KEY, choice);
  } catch { /* private mode: the choice lasts this page only */ }
  if (choice === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = choice;
}

export function setTheme(t) {
  const follow = (t === "dark") === systemDark();
  try {
    if (follow) localStorage.removeItem(KEY); else localStorage.setItem(KEY, t);
  } catch { /* private mode: the choice lasts this page only */ }
  if (follow) delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
}
