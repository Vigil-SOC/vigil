import React, { lazy, Suspense } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import Loader from './Loader';
import { useColorScheme } from '../contexts/ColorSchemeContext';
import { VigilMark } from '../shared/VigilLogo';
import { Icon } from '../shared/icons';

const SignedOutScreen = lazy(() => import('../screens/login/SignedOutScreen'))
const MfaSetupScreen = lazy(() => import('../screens/login/MfaSetupScreen'))

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
  const { isAuthenticated, isLoading, backendUnreachable, signedOut, retryLoadUser, user } = useAuth();
  const location = useLocation();

  if (isLoading) {
    return <Loader />;
  }

  // Session state is unknown, not signed out: stay on the requested URL.
  if (!isAuthenticated && backendUnreachable) {
    return <BackendUnreachable onRetry={retryLoadUser} />;
  }

  // The server ended the sign-in: say why, at the address the person was on.
  if (!isAuthenticated && signedOut) {
    return (
      <Suspense fallback={<Loader />}>
        <SignedOutScreen reason={signedOut.reason} from={location} />
      </Suspense>
    )
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  // The MFA requirement covers this person and they have none: setup, then the console.
  if (user?.mfa_setup_required) {
    return (
      <Suspense fallback={<Loader />}>
        <MfaSetupScreen />
      </Suspense>
    );
  }

  return <>{children}</>;
}
