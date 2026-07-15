# Vendored browser runtime

`htmx-1.9.12.min.js` is the unmodified HTMX 1.9.12 distribution from:

`https://github.com/bigskysoftware/htmx/blob/v1.9.12/dist/htmx.min.js`

- SHA-256: `449317ade7881e949510db614991e195c3a099c4c791c24dacec55f9f4a2a452`
- License: Zero-Clause BSD (`HTMX-LICENSE.txt`)

It is vendored so the self-hosted Web App has no CDN/runtime network dependency.
When updating it, replace the distribution and license together, update the pinned
filename and checksum here, then run the Web and Docker test suites.

The `lucide/` directory contains only the navigation and command icons used by the
Web App, copied without modification from `lucide-static` 1.24.0. Lucide is licensed
under ISC; the vendored text is in `LUCIDE-LICENSE.txt`. Keeping the SVGs local
preserves the same offline/private-deployment boundary as HTMX.
