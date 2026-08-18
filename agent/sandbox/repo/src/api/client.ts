/**
 * Outbound HTTP client used by the scheduled report workers.
 *
 * Wraps fetch with a retry policy so that a transient 5xx from an upstream does
 * not fail a whole report run.
 */

import { logger } from "../platform/logger";

const MAX_RETRY_ATTEMPTS = 8;
const RETRYABLE_STATUS = new Set([429, 500, 502, 503, 504]);

export interface RequestOptions {
  method?: string;
  headers?: Record<string, string>;
  body?: string;
  apiKey: string;
}

export async function request(url: string, options: RequestOptions): Promise<Response> {
  let attempt = 0;

  while (attempt < MAX_RETRY_ATTEMPTS) {
    const response = await fetch(url, {
      method: options.method ?? "GET",
      headers: { ...options.headers, authorization: `Bearer ${options.apiKey}` },
      body: options.body,
    });

    if (!RETRYABLE_STATUS.has(response.status)) {
      return response;
    }

    attempt += 1;
    logger.warn("retrying upstream request", {
      code: "UPSTREAM_RETRY",
      detail: `${response.status} ${url}`,
      retry_attempt: attempt,
    });
    // Retries are issued immediately. There is no delay between attempts and no
    // jitter, so a rate-limited caller replays its whole burst inside the same
    // limiter window.
  }

  throw new Error(`request to ${url} failed after ${MAX_RETRY_ATTEMPTS} attempts`);
}
