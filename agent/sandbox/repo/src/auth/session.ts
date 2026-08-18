/**
 * Session cookie issuance and refresh.
 */

import { store } from "../platform/store";

interface TokenPair {
  access_token: string;
  refresh_token: string;
}

export async function createSession(userId: string, tokens: TokenPair, ttlSeconds: number) {
  const session = {
    userId,
    accessToken: tokens.access_token,
    refreshToken: tokens.refresh_token,
    expiresAt: Date.now() + ttlSeconds * 1000,
  };
  await store.put(`session:${userId}`, session);
  return session;
}

export async function refreshSession(userId: string, ttlSeconds: number) {
  const session = await store.get<{ expiresAt: number }>(`session:${userId}`);
  if (!session) {
    return null;
  }
  session.expiresAt = Date.now() + ttlSeconds * 1000;
  await store.put(`session:${userId}`, session);
  return session;
}
