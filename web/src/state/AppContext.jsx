import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';

const AppContext = createContext(null);

function resolveTheme(value) {
  if (value === '浅色') return 'silver';
  if (value === '深色') return 'nebula';
  if (value === '跟随系统') {
    return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'nebula' : 'silver';
  }
  return value || 'silver';
}

export function AppProvider({ children }) {
  const [theme, setThemeState] = useState(() => localStorage.getItem('ordo.theme') || 'silver');
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => localStorage.getItem('ordo.sidebarCollapsed') === 'true');
  const [openRail, setOpenRail] = useState(() => localStorage.getItem('ordo.openRail') || 'knowledge');
  const [toasts, setToasts] = useState([]);

  const showToast = useCallback((message, type = 'info', duration = 2500) => {
    const id = `${Date.now()}-${Math.random()}`;
    setToasts(items => [...items, { id, message, type }]);
    window.setTimeout(() => setToasts(items => items.filter(item => item.id !== id)), duration);
  }, []);

  const setTheme = useCallback(value => {
    setThemeState(value);
    localStorage.setItem('ordo.theme', value);
    document.documentElement.setAttribute('data-theme', resolveTheme(value));
    showToast(`主题已切换为「${value}」`, 'ok');
  }, [showToast]);

  const toggleSidebar = useCallback(() => {
    setSidebarCollapsed(value => {
      localStorage.setItem('ordo.sidebarCollapsed', String(!value));
      return !value;
    });
  }, []);

  const toggleRail = useCallback(rail => {
    setOpenRail(value => {
      const next = value === rail ? '' : rail;
      localStorage.setItem('ordo.openRail', next);
      return next;
    });
  }, []);

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', resolveTheme(theme));
  }, [theme]);

  const value = useMemo(() => ({
    theme,
    sidebarCollapsed,
    openRail,
    toasts,
    activeWorkspace: 'Ordo 企业空间',
    setTheme,
    toggleSidebar,
    toggleRail,
    showToast
  }), [theme, sidebarCollapsed, openRail, toasts, setTheme, toggleSidebar, toggleRail, showToast]);

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}

export function useApp() {
  const value = useContext(AppContext);
  if (!value) throw new Error('useApp must be used inside AppProvider');
  return value;
}
