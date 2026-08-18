---
article_id: KB-0044
title: API rate limits
tags: [api, rate-limit, 429, retry, throttling, backoff]
updated: 2026-04-09
---

# API rate limits

The Meridian REST API allows **600 requests per minute per API key** on all
paid plans. The window is fixed, not sliding, and resets on the minute.

When you exceed the limit the API returns HTTP 429 with a `Retry-After` header
giving the number of seconds until the window resets.

## Handling 429 correctly

Clients must honour `Retry-After`. Retrying immediately does not help: the
window has not moved, so every immediate retry is also rejected and counts
against the next window as well.

Recommended policy:

- Sleep for `Retry-After` seconds, or use exponential backoff starting at 1s.
- Add jitter so that parallel workers do not resynchronise.
- Cap total attempts at 5.

## Requesting a higher limit

Sustained workloads above 600 rpm should use the Bulk Export API or contact
their account team about a dedicated limit.
