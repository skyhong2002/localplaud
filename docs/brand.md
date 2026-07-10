# localplaud brand assets

Original marks for the project (in `src/localplaud/api/static/`), served by the
app and usable in docs. Not affiliated with Plaud.

| File | Use |
| --- | --- |
| `logo.svg` | App-tile mark (48×48), microphone in a rounded gradient tile |
| `favicon.svg` | Browser-tab icon (same mark) |
| `wordmark.svg` | Horizontal lockup: mark + "localplaud" |
| `logo-mono.svg` | Single-color mark (uses `currentColor`) |

## Concept

A **microphone** (voice recording) in a rounded tile — localplaud is about owning
the intelligence workflow around your recordings. The name pairs it: *local*
(muted gray, your machine) + *plaud* (accent gradient, the recorder/upload path it
extends).

The Web App may follow Plaud's proven information architecture and interaction
rhythm, but localplaud uses original marks, components, copy, and visual details. The
goal is familiar workflow, not a deceptive pixel clone.

## Colors (shared with the UI tokens)

- Mark gradient: `#3D9BFF → #007AFF → #8F53ED` (iOS blue → violet)
- Wordmark: `local` `#667085`, `plaud` blue→violet gradient
- These align with the app's palette (blue `#007AFF` primary, violet `#8F53ED`
  AI accent) — see the UI in `src/localplaud/api/templates/base.html`.

## Usage

- Keep the tile's corner radius and clear space (≈ ¼ of the mark) intact.
- On dark or busy backgrounds use `logo-mono.svg` with an appropriate `color`.
- The mark is deliberately generic (a microphone) — it does not copy Plaud's
  own logo or any third-party asset.
