"""laddy-phone: minimal phone-control PWA for the dev-loop engine.

Runs ON the VPS worker (untrusted node) and exposes only what SSH already
exposes there: answer remote-ask gate questions, read status/queue/logs,
enqueue and resume tasks. It has NO merge or GitHub capability by
construction - the node it runs on holds no such credential. Reach it over
the tailnet only; every /api/* call requires the LADDY_PHONE_TOKEN.

Modules:
  server.py   - stdlib ThreadingHTTPServer app + config + entrypoint
  handlers.py - pure request logic (validation, files, argv building)
  static/     - the single-screen PWA (no build step, self-contained)
"""
