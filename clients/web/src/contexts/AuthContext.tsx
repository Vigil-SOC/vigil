import { createContext, useContext, useState, useEffect, useCallback, ReactNode } from 'react';
import api from '../services/api';

const DEV_MODE = import.meta.env.VITE_DEV_MODE === 'true';

if (DEV_MODE) {
  console.warn('⚠️  DEV_MODE is ENABLED - Authentication is BYPASSED!');
  console.warn('⚠️  This should NEVER be enabled in production!');
}

const DEV_USER: User = {
  user_id: 'dev-user-id',
  username: 'dev-user',
  email: 'dev@localhost',
  full_name: 'Dev User (Full Admin)',
  role_id: 'role-admin',
  is_active: true,
  is_verified: true,
  mfa_enabled: false,
  last_login: new Date().toISOString(),
  login_count: 999,
  permissions: {
    'findings.read': true,
    'findings.write': true,
    'findings.delete': true,
    'cases.read': true,
    'cases.write': true,
    'cases.delete': true,
    'cases.assign': true,
    'integrations.read': true,
    'integrations.write': true,
    'users.read': true,
    'users.write': true,
    'users.delete': true,
    'settings.read': true,
    'settings.write': true,
    'config.write': true,
    'ai_chat.use': true,
    'ai_decisions.approve': true,
  },
};

interface User {
  user_id: string;
  username: string;
  email: string;
  full_name: string;
  role_id: string;
  is_active: boolean;
  is_verified: boolean;
  mfa_enabled: boolean;
  last_login: string | null;
  login_count: number;
  permissions?: Record<string, boolean>;
}

interface AuthContextType {
  user: User | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  /** /auth/me failed for a reason other than 401, so the session state is unknown. */
  backendUnreachable: boolean;
  /** Re-runs the /auth/me load. */
  retryLoadUser: () => Promise<void>;
  login: (usernameOrEmail: string, password: string, mfaCode?: string) => Promise<void>;
  logout: () => Promise<void>;
  refreshToken: () => Promise<void>;
  hasPermission: (permission: string) => boolean;
  hasAnyPermission: (...permissions: string[]) => boolean;
  hasAllPermissions: (...permissions: string[]) => boolean;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

interface AuthProviderProps {
  children: ReactNode;
}

export function AuthProvider({ children }: AuthProviderProps) {
  const [user, setUser] = useState<User | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [backendUnreachable, setBackendUnreachable] = useState(false);

  // Auth cookies are HttpOnly, so JS can't read them: call /auth/me and let the
  // cookie identify the user. If the API answered with a 4xx the user is not
  // logged in (a 401 on /auth/me surfaces as the failed /auth/refresh retry's
  // 401, or 403 before the csrf cookie exists); no answer or a 5xx leaves the
  // session unknown (backendUnreachable).
  const loadUser = useCallback(async () => {
    if (DEV_MODE) {
      console.log('DEV_MODE: Using mock dev user');
      setUser(DEV_USER);
      setIsLoading(false);
      return;
    }

    setBackendUnreachable(false);
    try {
      const response = await api.get('/auth/me');
      setUser(response.data);
    } catch (error: any) {
      // /auth/me also seeds the csrf_token cookie, so the login POST works
      const status: number | undefined = error?.response?.status;
      if (status === undefined || status >= 500) {
        setBackendUnreachable(true);
      }
    }
    setIsLoading(false);
  }, []);

  const retryLoadUser = useCallback(async () => {
    setIsLoading(true);
    await loadUser();
  }, [loadUser]);

  useEffect(() => {
    loadUser();
  }, [loadUser]);

  useEffect(() => {
    if (!user) return;

    // tokens expire in 24 hours
    const interval = setInterval(() => {
      refreshToken();
    }, 23 * 60 * 60 * 1000);

    return () => clearInterval(interval);
  }, [user]);

  const login = async (usernameOrEmail: string, password: string, mfaCode?: string) => {
    if (DEV_MODE) {
      console.log('DEV_MODE: Bypassing login, using mock user');
      setUser(DEV_USER);
      return;
    }

    // the backend sets the HttpOnly cookies; only the user comes back in the body
    try {
      const response = await api.post('/auth/login', {
        username_or_email: usernameOrEmail,
        password,
        mfa_code: mfaCode,
      });

      setUser(response.data.user);
    } catch (error: any) {
      const isMfaRequired =
        error.response?.headers?.['x-mfa-required'] === 'true' ||
        error.response?.data?.detail === 'MFA code required';
      if (isMfaRequired) {
        throw new Error('MFA_REQUIRED');
      }
      throw error;
    }
  };

  const logout = async () => {
    if (DEV_MODE) {
      console.log('DEV_MODE: Mock logout');
      setUser(null);
      return;
    }

    // the backend clears the cookies and blacklists the JTI
    try {
      await api.post('/auth/logout');
    } catch (error) {
      console.error('Logout error:', error);
    } finally {
      setUser(null);
    }
  };

  const refreshToken = async () => {
    try {
      // the refresh cookie is HttpOnly; the browser sends it automatically
      const response = await api.post('/auth/refresh');
      setUser(response.data.user);
    } catch (error) {
      console.error('Token refresh failed:', error);
      await logout();
    }
  };

  const hasPermission = (permission: string): boolean => {
    if (DEV_MODE && user) return true;
    
    if (!user || !user.permissions) return false;
    return user.permissions[permission] === true;
  };

  const hasAnyPermission = (...permissions: string[]): boolean => {
    if (DEV_MODE && user) return true;
    
    if (!user || !user.permissions) return false;
    return permissions.some(perm => user.permissions![perm] === true);
  };

  const hasAllPermissions = (...permissions: string[]): boolean => {
    if (DEV_MODE && user) return true;
    
    if (!user || !user.permissions) return false;
    return permissions.every(perm => user.permissions![perm] === true);
  };

  const value: AuthContextType = {
    user,
    isAuthenticated: !!user,
    isLoading,
    backendUnreachable,
    retryLoadUser,
    login,
    logout,
    refreshToken,
    hasPermission,
    hasAnyPermission,
    hasAllPermissions,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
}

