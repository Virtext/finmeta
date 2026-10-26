import { useEffect, useState } from 'react';
import { getAuthStatus, login, setupPassword } from '../api';
import type { AuthStatus } from '../types';

interface Props {
  onAuthed: () => void;
}

export default function LoginScreen({ onAuthed }: Props) {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getAuthStatus().then(setStatus).catch(() => setStatus({ auth_enabled: true, has_password: false, first_run: true }));
  }, []);

  const isSetup = status?.first_run && !status?.has_password;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (isSetup) {
        if (password !== confirm) { setError('Passwords do not match'); return; }
        await setupPassword(password);
      } else {
        await login(username.trim(), password);
      }
      onAuthed();
    } catch (err: any) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-screen">
      <div className="login-card">
        <div className="login-brand">
          <img src="/finmeta-icon.svg" alt="" className="brand-icon" />
          Finmeta
        </div>
        {isSetup ? (
          <>
            <h2 className="login-title">Set admin password</h2>
            <p className="muted login-hint">Choose a password to secure access to Finmeta.</p>
          </>
        ) : (
          <>
            <h2 className="login-title">Sign in</h2>
            <p className="muted login-hint">Enter your username and password.</p>
          </>
        )}
        <form onSubmit={submit}>
          {!isSetup && (
            <input
              className="login-input"
              type="text"
              placeholder="Username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoFocus
              autoComplete="username"
            />
          )}
          <input
            className="login-input"
            type="password"
            placeholder="Password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoFocus={isSetup}
            autoComplete="current-password"
          />
          {isSetup && (
            <input
              className="login-input"
              type="password"
              placeholder="Confirm password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
            />
          )}
          {error && <div className="error login-error">{error}</div>}
          <button className="btn primary login-btn" disabled={busy || !password || (!isSetup && !username.trim())}>
            {isSetup ? 'Create password' : 'Sign in'}
          </button>
        </form>
      </div>
    </div>
  );
}
