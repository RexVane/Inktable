import { useApp } from '../state/AppContext.jsx';

export default function Toasts() {
  const { toasts } = useApp();
  return (
    <div className="toast-container" role="status" aria-live="polite" style={{ position: 'fixed', bottom: 24, right: 24, display: 'flex', flexDirection: 'column', gap: 8, zIndex: 9999 }}>
      {toasts.map(toast => (
        <div key={toast.id} className="toast-item" style={{ padding: '10px 18px', borderRadius: 8, fontSize: 13, boxShadow: '0 8px 24px rgba(0,0,0,.18)', color: toast.type === 'info' ? 'var(--ink-strong)' : '#fff', background: toast.type === 'ok' ? 'var(--accent)' : toast.type === 'error' ? 'var(--danger)' : 'var(--card-bg)', border: toast.type === 'info' ? '1px solid var(--line)' : 'none' }}>
          {toast.message}
        </div>
      ))}
    </div>
  );
}
