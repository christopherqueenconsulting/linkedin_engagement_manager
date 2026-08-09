"""Per-area `/api` routers, split out of `api/main.py` (#1154).

Each module owns ONE path prefix and declares it on its own `APIRouter`, so `main` includes it with
no `prefix=` argument. That is load-bearing: every scope/admin check in this codebase reads
`route.path`, and a prefix supplied at include time does not appear there — the routes would serve
correctly and then be invisible to `_scope_path`, `_hide_admin_routes_from_schema` and the surface
guards.
"""
