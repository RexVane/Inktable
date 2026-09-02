(() => {
  'use strict';
  try {
    const raw = localStorage.getItem('ordo.theme') || 'silver';
    const ids = ['nebula', 'steel', 'coal', 'moss', 'silver', 'limestone', 'linen'];
    const preference = raw === 'light' ? 'silver' : raw === 'dark' ? 'nebula' : raw;
    const theme = preference === 'system' || !ids.includes(preference)
      ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'nebula' : 'silver')
      : preference;
    document.documentElement.setAttribute('data-theme', theme);
  } catch (error) {
    document.documentElement.setAttribute('data-theme', 'silver');
  }
})();
