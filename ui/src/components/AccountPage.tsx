import { useState } from 'react';
import { updateAccount } from '../api';

interface Props {
  onBack: () => void;
}

export default function AccountPage({ onBack }: Props) {
  const [current, setCurrent] = useState('');
  const [newUsername, setNewUsername] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setOk(null);
    if (!current) { setError('Enter your current password.'); return; }
    if (newPassword && newPassword !== confirm) { setError('New passwords do not match.'); return; }
    if (newPassword && newPassword.length < 8) { setError('New password must be at least 8 characters.'); return; }
    setBusy(true);
    try {
      await updateAccount({
        current_password: current,
        new_username: newUsername.trim() || undefined,
        new_password: newPassword || undefined,
      });
      setCurrent('');
      setNewUsername('');
      setNewPassword('');
      setConfirm('');
      setOk('Account updated.');
    } catch (err: any) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="settings-page">
      <header className="settings-header">
        <button className="btn settings-back" onClick={onBack}>← Back</button>
        <h2>Account</h2>
      </header>
      <div className="account-form">
        <form onSubmit={submit} className="account-inner">
          <label className="field-label">Current password</label>
          <input
            className="login-input config-input"
            type="password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            autoComplete="current-password"
            autoFocus
          />
          <label className="field-label">New username (optional)</label>
          <input
            className="login-input config-input"
            type="text"
            placeholder="Leave empty to keep current name"
            value={newUsername}
            onChange={(e) => setNewUsername(e.target.value)}
            autoComplete="username"
          />
          <label className="field-label">New password (optional, min 8 characters)</label>
          <input
            className="login-input config-input"
            type="password"
            placeholder="Leave empty to keep current password"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            autoComplete="new-password"
          />
          {newPassword && (
            <>
              <label className="field-label">Confirm new password</label>
              <input
                className="login-input config-input"
                type="password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                autoComplete="new-password"
              />
            </>
          )}
          {error && <div className="error login-error">{error}</div>}
          {ok && <div className="success login-error">{ok}</div>}
          <button className="btn primary" disabled={busy || !current}>
            {busy ? 'Saving…' : 'Save changes'}
          </button>
        </form>
      </div>
    </div>
  );
}
