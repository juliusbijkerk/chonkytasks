// Apply before paint, independently of recording and automation behavior.
(() => {
  const root = document.documentElement;
  try {
    localStorage.setItem('chonkytasks-appearance', 'light');
  } catch { /* Appearance also works when storage is unavailable. */ }

  function apply() {
    root.dataset.theme = 'light';
    root.dataset.palette = 'mistral';
    root.style.colorScheme = 'light';
    const mark = `/static/brand/mark-${root.dataset.palette}.svg`;
    document.querySelector('link[rel="icon"]').href = mark;
    document.querySelectorAll('[data-brand-mark]').forEach(img => { img.src = mark; });
  }
  apply();
  document.addEventListener('DOMContentLoaded', apply);
})();
