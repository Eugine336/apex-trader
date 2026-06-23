import { createContext, useCallback, useContext, useEffect, useState } from "react";

import * as authApi from "../api/auth";
import { clearTokens, getAccessToken } from "../api/client";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  // On mount, if a token exists, resolve the current user.
  useEffect(() => {
    let active = true;
    async function bootstrap() {
      if (!getAccessToken()) {
        setLoading(false);
        return;
      }
      try {
        const me = await authApi.getMe();
        if (active) setUser(me);
      } catch {
        if (active) {
          clearTokens();
          setUser(null);
        }
      } finally {
        if (active) setLoading(false);
      }
    }
    bootstrap();
    return () => {
      active = false;
    };
  }, []);

  const login = useCallback(async (email, password) => {
    await authApi.login(email, password);
    const me = await authApi.getMe();
    setUser(me);
    return me;
  }, []);

  const register = useCallback(async (email, password) => {
    await authApi.register(email, password);
    const me = await authApi.getMe();
    setUser(me);
    return me;
  }, []);

  const logout = useCallback(() => {
    clearTokens();
    setUser(null);
  }, []);

  const value = {
    user,
    loading,
    isAuthenticated: !!user,
    login,
    register,
    logout,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return ctx;
}
