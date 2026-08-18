---
article_id: KB-0008
title: Configure SSO with Okta
tags: [sso, okta, oauth, saml, login, redirect, callback]
updated: 2026-06-22
---

# Configure SSO with Okta

Meridian supports OIDC against Okta, Azure AD, and Google Workspace.

## Add the callback URL to your provider

In your Okta application, add this exact redirect URI to **Sign-in redirect
URIs**:

```
https://app.meridian.io/auth/callback
```

Okta validates the redirect URI on the token exchange, not on the authorize
request. A mismatch therefore appears *after* the user has already signed in
successfully, which is why a wrong value looks like the login page reloading
rather than an obvious error.

## Common problems

| Symptom | Cause |
| --- | --- |
| Login page reloads in a loop | The redirect URI sent by Meridian does not match the one registered in Okta |
| `invalid_client` | Client secret rotated in Okta but not in Meridian |
| User signs in but sees no workspaces | The user is not assigned to the Meridian app in Okta |

## Self-hosted and staging environments

Self-hosted deployments must set `MERIDIAN_PUBLIC_ORIGIN` to their own public
origin. If it is unset, the service falls back to a non-production origin.
