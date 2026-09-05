(() => {
  'use strict';
  try {
    const raw = localStorage.getItem('ordo.theme') || 'silver';
    const ids = ['nebula', 'steel', 'coal', 'moss', 'silver', 'limestone', 'linen'];
    const preference = (raw === 'light' || raw === '浅色') ? 'silver' : (raw === 'dark' || raw === '深色') ? 'nebula' : raw;
    const theme = (preference === 'system' || preference === '跟随系统' || !ids.includes(preference))
      ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'nebula' : 'silver')
      : preference;
    document.documentElement.setAttribute('data-theme', theme);
  } catch (error) {
    document.documentElement.setAttribute('data-theme', 'silver');
  }
})();
