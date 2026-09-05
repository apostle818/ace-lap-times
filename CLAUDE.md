# CLAUDE.md

## Security & Privacy

Guidance for keeping this repo's security posture intact as it grows.
Grounded in a repo audit on 2026-08-31 — see the findings below before
assuming something still needs fixing; most of the obvious issues were
already closed by an earlier pass (secret-key validation, rate limiting,
groups-as-privacy-boundary, CSV injection, strict CSP, non-root containers,
`pip-audit` in CI). Keep it that way.

### What to preserve when touching `ace-laptimes/backend/app.py`

- **Every new route needs an explicit auth decorator.** There is no
  default-deny middleware — `token_required` / `token_or_key_required` /
  `superadmin_required` are opt-in per route. A route added without one is
  wide open. When adding an endpoint, also ask whether an API key should be
  able to reach it at all (most shouldn't — see `token_or_key_required`'s
  docstring) and whether a group admin's reach needs to be bounded the way
  `_may_act_for` / `_visibility_clause` bound the existing ones.
- **Reads stay scoped through `_visibility_clause` / `_can_view_user`.**
  Any new query that returns lap data must run it through the visibility
  helpers, not just check group membership on write. The whole privacy model
  (`groups are a boundary`) breaks the moment a read endpoint skips this.
- **New free-text fields need an entry in `FIELD_LIMITS`** and must be run
  through `clean_text`. An unbounded text field is a disk-fill vector against
  a single SQLite file with no external quota.
- **New text ever destined for CSV/JSON export** must go through
  `_csv_safe` (or equivalent) — Excel/Sheets formula injection via a leading
  `=`, `+`, `-`, `@`, tab, or CR is the concrete threat model already
  handled for the existing export columns.
- **New text ever rendered into the DOM in `app.js`** must go through
  `escapeHtml`. The CSP (`script-src 'self'`, no `unsafe-inline`) is a
  second layer, not a substitute for escaping — don't rely on it alone.
- **The JWT lives in `localStorage`, not an httpOnly cookie**, so an XSS
  bug would leak it directly. That's an accepted tradeoff given the strict
  CSP and consistent escaping, but it raises the cost of *any* regression in
  either — treat missed `escapeHtml` calls and CSP loosening as high-severity,
  not cosmetic.
- **Don't add a `SECRET_KEY` fallback or default.** `_load_secret_key`
  refuses to start without one on purpose; if you're tempted to add a
  dev-mode default, add it to `_REJECTED_SECRET_KEYS` instead of accepting it.

### Dependencies

- Backend (`ace-laptimes/backend/requirements.txt`) and tray
  (`ace-tray/requirements.txt`) are both covered by `pip-audit --strict` in
  CI (`.github/workflows/ci.yml`) and by weekly Dependabot PRs
  (`.github/dependabot.yml`) — a known CVE in a pin fails the build, so
  don't add a new dependency without leaving it inside that check.
- **`ace-tray/requirements.txt` now pins exact versions (`==`), matching the
  backend.** It used to use `>=` (`PyQt6>=6.6.0`, `requests>=2.31.0`), which
  made the tray app's dependency set non-reproducible across builds — two
  builds of the same commit could ship different transitive versions, and
  PyInstaller bundles whatever was resolved at build time
  (`.github/workflows/tray-release.yml` has no lockfile to pin against).
  Fixed in the 2026-08-31 follow-up pass (see audit summary below). Keep
  pinning exactly here going forward; Dependabot already opens PRs for both
  manifests, so a bump is a review, not a rewrite.
- Chart.js is vendored at a pinned version (`ace-laptimes/frontend/Dockerfile`,
  `chart.js@4.4.7`) rather than pulled from a CDN — keep doing this for any
  future frontend dependency; it's what lets the CSP stay `script-src 'self'`
  with no third-party origins trusted.
- No `package.json`/`package-lock.json` exists because the frontend is
  vanilla JS by design (see `CONTRIBUTING.md`) — don't introduce npm
  dependencies into `ace-laptimes/frontend/` without also introducing a
  lockfile and reconsidering the CSP.

### Tray app (`ace-tray/ace_tray.py`)

- The API key and server URL are persisted via `QSettings`, which on
  Windows lands in the registry **in plaintext**, not an OS credential
  store. This is a deliberate simplicity tradeoff for a homelab tool and is
  partly mitigated server-side (a tray API key is pinned to `scope='tray'`
  and can never carry admin rights, however privileged the account —
  see `_acting_as_superadmin` in `app.py`). Don't make it worse: never add a
  second, higher-privilege credential type stored the same way. If this
  needs hardening later, look at Windows Credential Manager via `keyring`
  before rolling anything custom.

### Before deploying, not just before committing

- `SECRET_KEY` must be freshly generated per instance (`openssl rand -hex
  32`) and never reused across the values already rejected in
  `_REJECTED_SECRET_KEYS` — extend that set if a new placeholder starts
  showing up in docs or issues.
- Port 8099 (or whatever `nginx` is mapped to) stays off the public
  internet — see `docs/TLS.md`. A VPN or a TLS-terminating reverse proxy is
  required before exposing it beyond the LAN; the stack speaks plain HTTP
  and has no HSTS story until it sits behind real TLS.
- `TRUSTED_PROXY_HOPS` must match the actual proxy chain in front of the
  backend. Set too high, it lets a client forge its own source IP via
  `X-Forwarded-For` and defeats per-IP rate limiting; set too low with a
  real proxy in front, every request appears to originate from the proxy
  and shares one rate-limit bucket.

### Audit findings summary (2026-08-31, branch `claude/cool-heisenberg-bmxpju`)

No secrets in git history (targeted `git log -p` / `-S` search across all
commits for AWS keys, private-key headers, hardcoded passwords/tokens/
connection strings, and committed `.env` files — none found; only rejected
placeholder `SECRET_KEY` strings that the app explicitly refuses to run
with). No world-writable mode bits committed. No CORS wildcards, no debug
mode reachable in the shipped production path, no exposed admin endpoint
missing a decorator, no root-running container beyond nginx's inherent
root master process. Findings, all Low/Medium — nothing Critical or High:

- **Medium** — `ace-tray/requirements.txt` uses unpinned (`>=`) dependency
  versions while every other manifest in the repo pins exactly; see
  "Dependencies" above. **Resolved** in the follow-up pass below.
- **Low** — Tray app persists its API key in the Windows registry via
  `QSettings` in plaintext (`ace_tray.py`); scope is already limited
  server-side.
- **Low** — Session JWT is kept in `localStorage` rather than an httpOnly
  cookie, so a future XSS regression would be directly exploitable; the
  current CSP and consistent `escapeHtml` usage keep this theoretical today.

### Follow-up pass (2026-08-31, branch `claude/affectionate-planck-rj1zld`)

Scheduled regression + dependency sweep, run after the "Let a lap be moved
to another driver" and "Let sign-out-everywhere optionally revoke API keys"
changes had already landed on `main`. Re-checked every closed item above
against the current code rather than assuming it still holds:

- Every route in `app.py` still carries an explicit auth decorator,
  including the two added since the original audit
  (`PUT /api/laptimes/<id>`'s new reassignment path and the new
  `GET /api/meta/assignable-users`, both correctly scoped — the former
  reuses `_may_act_for()` for both the lap's old and new owner, closing a
  user-enumeration path in the same change; the latter is
  `@token_or_key_required` on purpose, since the tray needs it to build its
  own driver picker). `revoke_sessions` (the new opt-in API-key revocation)
  is still `@token_required` only, matching its docstring's claim that an
  API key cannot reach it.
- `_visibility_clause` / `_can_view_user`, `FIELD_LIMITS` / `clean_text`,
  and `_csv_safe` are all unchanged and still exercised the same way; no new
  free-text field or export column was added that bypasses them.
- Every new DOM write in `app.js` (the reassign-driver `<select>`, the move
  confirmation and error messages) goes through `escapeHtml`. CSP in
  `nginx.conf` is unchanged (`script-src 'self'`, no `unsafe-inline`,
  `frame-ancestors 'none'`). Both Dockerfiles still drop to a non-root user
  (backend via `gosu` in `docker-entrypoint.sh`, frontend via `USER node`).
  No CORS layer was added to the backend (same-origin only, via nginx).
  `_load_secret_key` / `_REJECTED_SECRET_KEYS` unchanged.
- `git log -p` / `-S` re-run across the full history (including commits
  since the original audit) for the same secret patterns — nothing found
  beyond the already-known rejected `SECRET_KEY` placeholders.
- `pip-audit --strict` against both `requirements.txt` files (backend and
  tray, same invocation CI uses): **no known vulnerabilities** in either as
  of this pass.
- No `AGENTS.md` or similar file anywhere in the repo, and nothing in
  code comments, README, or CONTRIBUTING.md that reads as an attempt to
  redirect an agent's behavior or claim special authority — none found.
- **Medium, fixed** — `ace-tray/requirements.txt` pinned from `>=6.11.0`
  / `>=2.34.2` to `==6.11.0` / `==2.34.2` (both already the latest
  available version at the time of the last Dependabot bump, and both
  verified installable). Dependabot's existing `pip` job for
  `/ace-tray` will keep opening PRs for future bumps the same way it does
  for the backend's exact pins.
- **Low, re-verified, unchanged** — tray API key still in the Windows
  registry via `QSettings` in plaintext; still pinned to `scope='tray'`
  server-side (`_acting_as_superadmin` in `app.py` still refuses to let an
  API key act with elevated rights, however privileged the owning account).
  Accepted tradeoff, not touched.
- **Low, re-verified, unchanged** — JWT still in `localStorage`, not an
  httpOnly cookie. Accepted tradeoff, not touched.
- No new CVEs, no new Critical/High findings, no regressions in any
  previously-closed item.

### Re-audit (2026-09-05, branch `claude/security-audit-rerun-buf4bb`)

Scheduled re-run. The tree was byte-identical to the previous pass (only the
merge commit for #18 had landed), so nothing here is a regression — the two
Medium findings are things the earlier two passes looked past. Full report:
https://claude.ai/code/artifact/77606ad7-7172-4fb3-a2e1-75421decdf43

Re-verified and still holding: auth decorators on all 45 routes; read scoping
via `_visibility_clause` / `_can_view_user`; `FIELD_LIMITS` / `clean_text`;
`_csv_safe`; `escapeHtml` on every server-supplied DOM write; CSP and headers
in `nginx.conf`; non-root containers; `_load_secret_key` /
`_REJECTED_SECRET_KEYS`; parameterised SQL throughout; no `verify=False`
anywhere; no agent-directive files; no secrets across all 63 commits;
`pip-audit --strict` clean on both manifests.

Confirmed by test rather than by reading: a tray API key issued to a group
admin is still refused by `add_group_member`, so the "deny keys by default"
design held under an attack it was not written for.

New findings, none Critical or High:

- **Medium, open** — `group_admin` is not actually a group-scoped role.
  `add_group_member` (`app.py:1270`) accepts any `user_id` with no consent
  step and no check that the target is already visible to the caller, and
  `/api/meta/users` hands group admins the full directory. Because
  `_group_admin_member_ids` derives `_may_act_for` and the visibility helpers
  from that self-controlled set, one POST converts any account on the instance
  into one whose laps the caller can read, reassign and delete. Reproduced
  end-to-end. A plain member gets 403 and an API key is refused, so the bound
  is real — it is just self-serve. **Needs a product decision**: either route
  direct adds through the existing invite flow, or state plainly here that
  `group_admin` is an instance-wide data role. Do not leave the docs claiming
  a boundary that is not enforced.
- **Medium, open** — `.gitignore` has no `.env` rule, while `README.md:63` and
  `docs/UPGRADING.md:61` instruct creating one containing `SECRET_KEY` inside
  the clone. Verified: the file is untracked but not ignored, so `git add .`
  stages a live signing key on a public repo. The previous audit checked that
  no `.env` had been committed, not whether the next one would be. One-line
  fix; also worth mirroring into both `.dockerignore` files.
- **Low, open** — `remove_group_member` (`app.py:1323`) has no self-removal
  case, so a member cannot leave a group they were added to.
- **Low, open** — `index.html:8-9` loads Google Fonts from
  `fonts.googleapis.com` / `fonts.gstatic.com`, and the CSP admits both. The
  "Dependencies" note above claiming no third-party origins are trusted is
  therefore wrong as written — either vendor the fonts or correct the claim.
- **Low, open** — `tray-release.yml` sets `contents: write` at job level, so
  pull-request runs carry a write-scoped token they never need. Fork PRs are
  read-only regardless, so exploitability is low today.
- **Low, open** — actions are pinned to mutable major tags rather than commit
  SHAs. Deliberate per `dependabot.yml`, but undocumented here; weigh
  `dorny/paths-filter` first as the only non-vendor action.

Adjacent, not security: `app.js:349`/`:370` interpolate the avatar `initial`
without `escapeHtml` (one character, self-only, cannot form a tag); and CI
compiles on Python 3.12 while the image ships 3.14, with 14 calls to the
deprecated `datetime.utcnow()` in `app.py`.
