# VortexPost ⚡

A local-first, git-native HTTP client — a Postman replacement built against
Postman's five failure modes:

| Postman pain | VortexPost invariant |
|---|---|
| Forced cloud account / no offline | **Runs with zero network except your request** — no account, no login screen |
| Secrets leak to their servers | **No sync/telemetry code exists.** Collections = plain files on disk |
| Teams pay $15+/user to share | Sharing = a git folder (or zip). Free forever |
| Proprietary collection format | **`.http` files** (REST-Client standard) — diffs in PRs like code |
| 10 s boot, 400 MB RAM | One Python file + customtkinter + stdlib `urllib` |

## Run
```
START_VORTEXPOST.bat
```

## File format (what git sees)
```http
### Get octocat
GET https://api.github.com/users/octocat
Accept: application/json
```
`###` starts a request · method+URL line · headers · blank line · body.
Double-click a request to load it, edit, **SEND**, then **Save to .http** —
`git diff` shows exactly your change.

`{{variables}}` substitute from the local `env` dict (keep tokens in
git-managed env files, never in a cloud dashboard).

## Proof
- Parsed 4/4 seed requests, serialize→parse round-trip is byte-stable
- Live: `GET httpbin.org/get` 200 (1201 ms), `GET api.github.com` 200 (374 ms)
- Error paths return structured `Response(status=0, error=…)` — never crash
