// Apply before paint, independently of recording and automation behavior.
(() => {
  const root = document.documentElement;
  const systemDark = matchMedia('(prefers-color-scheme: dark)');
  const choices = ['light', 'dark', 'mistral'];
  let preference = 'mistral';
  try {
    const saved = localStorage.getItem('chonkytasks-appearance');
    if (choices.includes(saved)) preference = saved;
  } catch { /* Appearance also works when storage is unavailable. */ }

  function apply() {
    const theme = preference === 'light' || preference === 'dark'
      ? preference : systemDark.matches ? 'dark' : 'light';
    root.dataset.theme = theme;
    root.dataset.palette = 'mistral';
    root.style.colorScheme = theme;
    const mark = `/static/brand/mark-${root.dataset.palette}.svg`;
    document.querySelector('link[rel="icon"]').href = mark;
    document.querySelectorAll('[data-brand-mark]').forEach(img => { img.src = mark; });
  }
  apply();
  systemDark.addEventListener('change', apply);
  document.addEventListener('DOMContentLoaded', apply);
})();
