// Auth context: current user, login/register/logout, and permission checks.
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";

import {
  apiJson,
  clearTokens,
  getAccessToken,
  setAuthFailureHandler,
  setTokens,
} from "./api/client";

export type AuthUser = {
  id: string;
  email: string;
  full_name: string | null;
  is_active: boolean;
  mfa_enabled: boolean;
  roles: string[];
  permissions: string[];
};

type TokenPair = { access_token: string; refresh_token: string };
type LoginResult = {
  mfa_required: boolean;
  challenge_token: string | null;
  access_token: string | null;
  refresh_token: string | null;
};

// login() resolves to an MFA challenge (caller must complete it) or completes.
type LoginOutcome = { mfaRequired: false } | { mfaRequired: true; challengeToken: string };

type AuthContextValue = {
  user: AuthUser | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<LoginOutcome>;
  completeMfaLogin: (challengeToken: string, code: string) => Promise<void>;
  refreshUser: () => Promise<void>;
  register: (email: string, password: string, fullName?: string) => Promise<void>;
  logout: () => Promise<void>;
  hasPermission: (perm: string) => boolean;
  hasRole: (role: string) => boolean;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);

  // On mount, restore the session from a stored token.
  useEffect(() => {
    (async () => {
      if (getAccessToken()) {
        try {
          setUser(await apiJson<AuthUser>("/auth/me"));
        } catch {
          clearTokens();
        }
      }
      setLoading(false);
    })();
  }, []);

  // When a token refresh fails (refresh token expired), drop the session so the
  // UI falls back to the login screen automatically.
  useEffect(() => {
    setAuthFailureHandler(() => setUser(null));
    return () => setAuthFailureHandler(null);
  }, []);

  async function login(email: string, password: string): Promise<LoginOutcome> {
    const res = await apiJson<LoginResult>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    });
    if (res.mfa_required && res.challenge_token) {
      return { mfaRequired: true, challengeToken: res.challenge_token };
    }
    setTokens(res.access_token!, res.refresh_token!);
    setUser(await apiJson<AuthUser>("/auth/me"));
    return { mfaRequired: false };
  }

  async function completeMfaLogin(challengeToken: string, code: string) {
    const tokens = await apiJson<TokenPair>("/auth/mfa/login", {
      method: "POST",
      body: JSON.stringify({ challenge_token: challengeToken, code }),
    });
    setTokens(tokens.access_token, tokens.refresh_token);
    setUser(await apiJson<AuthUser>("/auth/me"));
  }

  async function refreshUser() {
    setUser(await apiJson<AuthUser>("/auth/me"));
  }

  async function register(email: string, password: string, fullName?: string) {
    const tokens = await apiJson<TokenPair>("/auth/register", {
      method: "POST",
      body: JSON.stringify({ email, password, full_name: fullName || null }),
    });
    setTokens(tokens.access_token, tokens.refresh_token);
    setUser(await apiJson<AuthUser>("/auth/me"));
  }

  async function logout() {
    try {
      await apiJson("/auth/logout", { method: "POST" });
    } catch {
      /* ignore — we clear locally regardless */
    }
    clearTokens();
    setUser(null);
  }

  function hasPermission(perm: string) {
    return !!user && user.permissions.includes(perm);
  }

  function hasRole(role: string) {
    return !!user && user.roles.includes(role);
  }

  return (
    <AuthContext.Provider
      value={{
        user,
        loading,
        login,
        completeMfaLogin,
        refreshUser,
        register,
        logout,
        hasPermission,
        hasRole,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within <AuthProvider>");
  return ctx;
}
