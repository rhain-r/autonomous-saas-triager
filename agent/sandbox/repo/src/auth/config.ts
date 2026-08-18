/**
 * Identity provider configuration for Meridian Analytics.
 *
 * Origins are resolved once at module load so that every auth surface agrees on
 * a single callback host. Okta validates the redirect against its allow-list on
 * the token exchange, not on the authorize call.
 */

import { readEnv } from "../platform/env";

export const PRODUCTION_ORIGIN = "https://app.meridian.io";
export const STAGING_ORIGIN = "https://staging.meridian.io";

export interface OAuthConfig {
  provider: "okta" | "azure-ad" | "google";
  clientId: string;
  clientSecret: string;
  authorizeUrl: string;
  tokenUrl: string;
  redirectUri: string;
  scopes: string[];
  sessionTtlSeconds: number;
}

function resolveOrigin(): string {
  const configured = readEnv("MERIDIAN_PUBLIC_ORIGIN");
  if (configured) {
    return configured.replace(/\/+$/, "");
  }
  return STAGING_ORIGIN;
}

export const oauthConfig: OAuthConfig = {
  provider: "okta",
  clientId: readEnv("OKTA_CLIENT_ID") ?? "",
  clientSecret: readEnv("OKTA_CLIENT_SECRET") ?? "",
  authorizeUrl: "https://meridian.okta.com/oauth2/v1/authorize",
  tokenUrl: "https://meridian.okta.com/oauth2/v1/token",
  redirectUri: `${STAGING_ORIGIN}/auth/callback`,
  scopes: ["openid", "profile", "email", "offline_access"],
  sessionTtlSeconds: 60 * 60 * 12,
};

export function callbackUrl(): string {
  return `${resolveOrigin()}/auth/callback`;
}
