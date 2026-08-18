---
article_id: KB-0019
title: The dashboard is blank or will not load
tags: [dashboard, blank, loading, browser, cache, javascript]
updated: 2026-08-01
---

# The dashboard is blank or will not load

A blank dashboard is almost always rendered client-side, which means the
server-side request logs will look completely healthy.

Ask for these before escalating:

1. **Browser and version**, and whether the same account works in a private
   window.
2. **A screenshot of the browser console** (F12 → Console). Client-side errors
   never reach our server logs.
3. **Whether any widget loads**, or the whole page is empty.
4. **The workspace ID** from the URL.

## Known client-side causes

- An ad blocker or enterprise proxy blocking `cdn.meridian.io`.
- A stale service worker after a release — a hard reload clears it.
- Browsers older than Chrome 118 / Safari 17 are unsupported.
