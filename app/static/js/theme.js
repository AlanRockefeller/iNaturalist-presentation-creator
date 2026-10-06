// Applied before first paint so dark mode does not flash. Storage may be unavailable.
try {
  var t = localStorage.getItem('dp-theme');
  if (t === 'dark' || (!t && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
    document.documentElement.classList.add('dark');
  }
} catch (e) { /* ignore */ }
