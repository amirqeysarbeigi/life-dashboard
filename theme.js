// Follow the OS appearance before first paint, ignoring previous manual overrides.
(() => {
  const systemTheme = window.matchMedia('(prefers-color-scheme: dark)');
  const applyTheme = () => {
    document.documentElement.dataset.theme = systemTheme.matches ? 'dark' : 'light';
  };
  applyTheme();
  systemTheme.addEventListener('change', () => {
    applyTheme();
    // Charts resolve their palette at startup; retain the open area on reload.
    const current = document.querySelector('.tab-btn.active');
    if (current) {
      history.replaceState(null, '', '#' + current.dataset.tab);
      location.reload();
    }
  });
})();
