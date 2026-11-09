import { Component, type ReactNode } from 'react';
import { getUiShell, setUiShell } from './shell/uiShell';

interface Props { children: ReactNode; }
interface State { error: Error | null; }

// Keep a render error from unmounting the entire app tree.
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  render() {
    if (this.state.error) {
      // SPEC_S14_UI.md §09: a bug in the new shell can never lock anyone
      // out. Purely additive for legacy -- this button only renders when
      // the modern shell is the one that crashed.
      const modern = getUiShell() === 'modern';
      return (
        <div className="fatal">
          <strong>UI error</strong>
          <pre style={{ whiteSpace: 'pre-wrap' }}>{String(this.state.error)}</pre>
          <button className="btn" onClick={() => this.setState({ error: null })}>Dismiss</button>
          {modern && (
            <button className="btn" onClick={() => setUiShell('legacy')}>
              Switch to classic interface
            </button>
          )}
        </div>
      );
    }
    return this.props.children;
  }
}
