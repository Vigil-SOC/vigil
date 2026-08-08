/**
 * Authentication Context - Global auth state management.
 * 
 * Provides authentication state, login/logout functions, and permission checking.
 * Supports DEV_MODE for bypassing authentication during development.
 */

import { createContext, useContext, useState, useEffect, ReactNode } from 'react';
import api from '../services/api';

// Dev mode flag - reads from Vite environment variable
const DEV_MODE = import.meta.env.VITE_DEV_MODE === 'true';

if (DEV_MODE) {
  console.warn('⚠️  DEV_MODE is ENABLED - Authentication is BYPASSED!');
  console.warn('⚠️  This should NEVER be enabled in production!');
}

// Mock dev user with FULL ADMIN permissions
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
    // Findings permissions
    'findings.read': true,
    'findings.write': true,
    'findings.delete': true,
    // Cases permissions
    'cases.read': true,
    'cases.write': true,
    'cases.delete': true,
    'cases.assign': true,
    // Integrations permissions
    'integrations.read': true,
    'integrations.write': true,
    // Users permissions
    'users.read': true,
    'users.write': true,
    'users.delete': true,
    // Settings permissions
    'settings.read': true,
    'settings.write': true,
    'config.write': true,
    // AI permissions
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

  // Load user on mount. Auth cookies are HttpOnly so we can't read them
  // from JS — we just call /auth/me and let the cookie (if present and
  // valid) identify the user. A 401 means not logged in; show the login UI.
  useEffect(() => {
    const loadUser = async () => {
      if (DEV_MODE) {
        console.log('DEV_MODE: Using mock dev user');
        setUser(DEV_USER);
        setIsLoading(false);
        return;
      }

      try {
        const response = await api.get('/auth/me');
        setUser(response.data);
      } catch {
        // Not logged in — AuthContext stays with user=null and the app
        // renders the login page. /auth/me also seeds the csrf_token
        // cookie as a side effect so the subsequent login POST works.
      }
      setIsLoading(false);
    };

    loadUser();
  }, []);

  // Auto refresh token before expiration
  useEffect(() => {
    if (!user) return;

    // Refresh token every 23 hours (tokens expire in 24 hours)
    const interval = setInterval(() => {
      refreshToken();
    }, 23 * 60 * 60 * 1000);

    return () => clearInterval(interval);
  }, [user]);

  const login = async (usernameOrEmail: string, password: string, mfaCode?: string) => {
    // DEV MODE: Skip actual login and use mock user
    if (DEV_MODE) {
      console.log('DEV_MODE: Bypassing login, using mock user');
      setUser(DEV_USER);
      return;
    }

    // PRODUCTION MODE: Normal login flow. The backend sets HttpOnly
    // cookies for access_token and refresh_token; we just read the user
    // from the response body.
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
    // DEV MODE: Just clear mock user
    if (DEV_MODE) {
      console.log('DEV_MODE: Mock logout');
      setUser(null);
      return;
    }

    // PRODUCTION MODE: Normal logout. Backend clears the auth cookies
    // and blacklists the JTI; we just drop the user state.
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
      // Refresh cookie is HttpOnly — the browser sends it automatically.
      const response = await api.post('/auth/refresh');
      setUser(response.data.user);
    } catch (error) {
      console.error('Token refresh failed:', error);
      await logout();
    }
  };

  const hasPermission = (permission: string): boolean => {
    // DEV MODE: Always grant all permissions
    if (DEV_MODE && user) return true;
    
    if (!user || !user.permissions) return false;
    return user.permissions[permission] === true;
  };

  const hasAnyPermission = (...permissions: string[]): boolean => {
    // DEV MODE: Always grant all permissions
    if (DEV_MODE && user) return true;
    
    if (!user || !user.permissions) return false;
    return permissions.some(perm => user.permissions![perm] === true);
  };

  const hasAllPermissions = (...permissions: string[]): boolean => {
    // DEV MODE: Always grant all permissions
    if (DEV_MODE && user) return true;
    
    if (!user || !user.permissions) return false;
    return permissions.every(perm => user.permissions![perm] === true);
  };

  const value: AuthContextType = {
    user,
    isAuthenticated: !!user,
    isLoading,
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

