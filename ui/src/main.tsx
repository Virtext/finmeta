import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import ErrorBoundary from './ErrorBoundary';
import './style.css';
import './tokens.css';

// Dev-only primitive gallery (SPEC_S14_UI.md phase P1): reachable ONLY via
// #/_ds, never linked from anywhere in the real app. Checked before the
// normal App import tree does any work, so it stays a side door, not a
// route the shell (P2) has to know about.
const root = ReactDOM.createRoot(document.getElementById('root') as HTMLElement);

if (window.location.hash === '#/_ds') {
  import('./ui/Gallery').then(({ Gallery }) => {
    root.render(
      <React.StrictMode>
        <ErrorBoundary>
          <Gallery />
        </ErrorBoundary>
      </React.StrictMode>,
    );
  });
} else {
  root.render(
    <React.StrictMode>
      <ErrorBoundary>
        <App />
      </ErrorBoundary>
    </React.StrictMode>,
  );
}
