# Stale lazy chunks after a deploy (issue #743)

Zero-downtime deploys (`docs/zero-downtime-deploys.md`) cover the server side; a browser tab open
across a release is the other half. A tab holds the content-hashed chunk names of the build it
loaded, and a code-split chunk (jszip in avatar training, any `React.lazy` route) is only fetched
when the user triggers that feature — so at 4 releases/day a tab open across one release 404s on
a hash the new image no longer has. That reads to the user as "the feature is broken," not
"reload me." Three layers cover different windows.

## Layer 1 — Retention (`api/spa_assets.py`)

Both blue/green colors mount the named `spa_asset_archive` volume at `SPA_ASSET_ARCHIVE_DIR`.
Each container syncs its own `ui/dist/assets` into the volume at startup, keeps
`SPA_ASSET_ARCHIVE_KEEP` builds (default 5), and serves a live-bundle miss out of the archive — a
content-hashed name resolves to one file forever, so the `immutable` cache header stays honest.
This lives in the app, NOT `deploy.sh`, so archive maintenance can never fail a deploy; an
unwritable volume logs a warning and falls back to serving the live bundle only.

## Layer 2 — One reload (`ui/src/utils/chunkReload.ts`)

The fallback for anything older than the archive window. `importWithChunkRecovery` /
`lazyWithChunkRecovery` wrap a dynamic import — react-query and error boundaries CATCH the
rejection, so the window-level `vite:preloadError`/`unhandledrejection` handlers would never see
it otherwise — and a failure triggers exactly one reload. `index.html` is served `no-store`, so a
reload always lands on the current build.

- **Loop guard**: a sessionStorage marker. A tab that can't PERSIST it never reloads at all, and a
  second failure inside the cooldown shows `NewVersionNotice` instead of reloading again.
- **Offline guard**: a tab is never reloaded when `navigator.onLine === false` — a disconnected
  dynamic import reports the SAME message a stale chunk does, and reloading with no network would
  turn a working app into the browser's offline page.

## Layer 3 — New-version awareness (`ui/src/hooks/useNewVersion.ts`, #754)

The proactive layer: layers 1 and 2 only fire AFTER something has already failed, and a tab
several builds behind can keep working while running old client code against a newer API.

- Polls `/api/app-info` every `VERSION_POLL_INTERVAL_MS` (5 min), skips the request entirely
  while the tab is hidden, and re-checks on `visibilitychange`.
- Raises the SAME `NewVersionNotice` component as layer 2 — but this path is a PROMPT and never
  reloads on its own, so it can't race the one automatic reload from layer 2.
- Baseline is the FIRST version this tab read, held in module scope so a remount can't
  re-baseline onto a build that shipped after boot.
- The BOOT read is the one poll that ignores the hidden-tab skip — a ctrl-clicked background tab
  runs the bundle while hidden, so deferring its first read would baseline it onto a build it
  isn't actually running.
- An unreachable endpoint or blank version raises nothing and never becomes the baseline.
