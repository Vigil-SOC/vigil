import React from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import Loader from './Loader';
import { useColorScheme } from '../contexts/ColorSchemeContext';
import { VigilMark } from '../shared/VigilLogo';
import { Icon } from '../shared/icons';

// Same look as Loader, with a retry in place of the progress track.
function BackendUnreachable({ onRetry }: { onRetry: () => void }) {
  const { scheme } = useColorScheme();
  return (
    <div
      className={`soc-console soc-loader ${scheme === 'light' ? 'vg-light' : 'vg-dark'}`}
      data-theme={scheme}
    >
      <div className="soc-loader-inner soc-unreachable" role="alert">
        <VigilMark className="soc-loader-mark" />
        <h1 className="soc-unreachable-title">Can't reach Vigil</h1>
        <p className="soc-unreachable-body">
          The console couldn't get an answer from the Vigil API. Check that the backend is running, then retry.
        </p>
        <button type="button" className="btn primary" onClick={onRetry}>
          <Icon name="refresh" />
          Retry
        </button>
      </div>
    </div>
  );
}

// Auth only. Per-screen permissions live in SocConsole's SCREEN_PERMS.
export default function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isLoading, backendUnreachable, retryLoadUser } = useAuth();
  const location = useLocation();

  if (isLoading) {
    return <Loader />;
  }

  // Session state is unknown, not signed out: stay on the requested URL.
  if (!isAuthenticated && backendUnreachable) {
    return <BackendUnreachable onRetry={retryLoadUser} />;
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  return <>{children}</>;
}
