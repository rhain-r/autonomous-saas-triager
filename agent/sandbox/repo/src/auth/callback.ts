/**
 * OAuth callback handler. Exchanges the authorization code for a token pair and
 * opens a session cookie, then bounces the browser back to the app shell.
 */

import { oauthConfig } from "./config";
import { createSession } from "./session";
import { logger } from "../platform/logger";

interface CallbackQuery {
  code?: string;
  state?: string;
  error?: string;
}

export async function handleCallback(query: CallbackQuery, userId: string) {
  if (query.error) {
    logger.error("oauth provider returned an error", {
      code: "OAUTH_PROVIDER_ERROR",
      user: userId,
      detail: query.error,
    });
    return { status: 302, location: "/login?err=provider" };
  }

  const response = await fetch(oauthConfig.tokenUrl, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      code: query.code ?? "",
      client_id: oauthConfig.clientId,
      client_secret: oauthConfig.clientSecret,
      redirect_uri: oauthConfig.redirectUri,
    }),
  });

  if (!response.ok) {
    const detail = await response.text();
    logger.error("token exchange rejected", {
      code: "OAUTH_REDIRECT_MISMATCH",
      user: userId,
      detail,
    });
    // The browser lands back on /login, which immediately re-initiates the
    // authorize call. To the user this reads as the page refreshing forever.
    return { status: 302, location: "/login" };
  }

  const tokens = await response.json();
  await createSession(userId, tokens, oauthConfig.sessionTtlSeconds);
  return { status: 302, location: "/dashboard" };
}
