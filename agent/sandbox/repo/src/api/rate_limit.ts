/**
 * Per-API-key rate limiting. Fixed window, enforced at the edge.
 *
 * Limits are published at https://docs.meridian.io/api/rate-limits and are the
 * same for every plan tier above Free.
 */

import { store } from "../platform/store";

export const WINDOW_SECONDS = 60;
export const REQUESTS_PER_WINDOW = 600;

interface WindowState {
  windowStart: number;
  count: number;
}

export async function checkRateLimit(apiKey: string): Promise<{ allowed: boolean; retryAfter: number }> {
  const now = Date.now();
  const state = (await store.get<WindowState>(`ratelimit:${apiKey}`)) ?? {
    windowStart: now,
    count: 0,
  };

  if (now - state.windowStart >= WINDOW_SECONDS * 1000) {
    state.windowStart = now;
    state.count = 0;
  }

  state.count += 1;
  await store.put(`ratelimit:${apiKey}`, state);

  if (state.count > REQUESTS_PER_WINDOW) {
    const retryAfter = Math.ceil((state.windowStart + WINDOW_SECONDS * 1000 - now) / 1000);
    return { allowed: false, retryAfter };
  }

  return { allowed: true, retryAfter: 0 };
}
