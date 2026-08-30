# Security

LoadCoach is the application in the suite most likely to be exposed beyond one machine: it holds a
GPU, it holds every prompt and response for a while, and IdeaPress on a laptop is a supported
client. This page is the LAN-exposure path end to end, in the order things are checked.

## The default: loopback, open

Bound to `127.0.0.1:8766`, with no tokens, LoadCoach is open: the operating-system user boundary
is the security boundary (ADR-0014). `loadcoach serve` needs no credential and `curl` needs no
header. Every request still passes Host validation — `localhost`, `127.0.0.1`, `[::1]` and the
bound address, with or without the port — so a page elsewhere cannot reach it through DNS
rebinding.

## Exposing on a LAN

Three things, and startup refuses without all of them:

```toml
[server]
host = "192.168.1.10"                 # the interface, not 0.0.0.0
allowed_hosts = ["coach.local"]       # every Host header you will accept (ADR-0026 §1)
allow_lan_exposure = true             # only if host is 0.0.0.0
```

```bash
loadcoach token create ideapress --scope write        # printed once; only its SHA-256 is stored
loadcoach token create ops --scope admin --expires-days 90
loadcoach serve
```

* A non-loopback `host` without `allowed_hosts` is `INSECURE_BINDING` at startup.
* A non-loopback `host` with no active token is `INSECURE_BINDING` at startup.
* `0.0.0.0` without `allow_lan_exposure = true` is `INSECURE_BINDING` at startup.

TLS is a reverse proxy's job (ADR-0014 §7); LoadCoach speaks HTTP. Put it behind one, name the
proxy's hostname in `allowed_hosts`, and list the proxy's networks in `trusted_proxies` so the
failed-authentication brake keys on real client addresses from `X-Forwarded-For` rather than
braking everyone behind the proxy at once. `serve` warns at startup on a non-loopback bind with
no `trusted_proxies` configured — that is ADR-0014 §7's "no evidence of a proxy" warning.

## Tokens and scopes

`Authorization: Bearer <token>`. Scopes are cumulative — `admin ⊃ write ⊃ read`:

| Scope | Grants |
|---|---|
| `read` | health, status, models, task profiles, jobs, explanations, evidence, queue, reliability |
| `write` | everything `read` grants, plus `/route`, `/generate`, `/jobs`, cancel, feedback |
| `admin` | everything `write` grants, plus settings, evidence import, queue control |

`GET /api/v1/version` needs nothing, so a client can negotiate before it knows whether its
credential is right (ADR-0026 §5). The scope is checked at the route **and** inside every
mutating service, so an internal caller holding a read-scoped principal is refused too.

Tokens are 256 bits of CSPRNG output, shown once, stored as SHA-256 and compared constant-time.
`loadcoach token list` never shows them; `loadcoach token revoke <name>` makes one a 401 at once.
A failed authentication is logged with the address and request ID, never the token, and an address
is braked after `failed_auth_per_minute` failures.

## The UI on a tokened bind

A browser cannot add a header to a page navigation, so the UI carries the same bearer token in a
`loadcoach_token` cookie — `HttpOnly`, `Secure`, `SameSite=Strict` — set once by pasting the token
into the 401 page's form, cleared by `POST /token-cookie/clear`. No account, no password: the cookie
*is* the token, and revoking the token revokes it. The cookies need a secure context, which
`http://localhost` and a TLS-terminated LAN deployment both are.

**On a plain-HTTP non-loopback bind the UI flow cannot work, by design.** The token cookie and the
CSRF cookie are both `Secure` (the CSRF cookie `__Host`-prefixed besides), so a browser at
`http://<lan-address>` stores neither: `POST /token-cookie` is refused with `403 CSRF_FAILED` and
every page stays 401, while the API's `Authorization` header keeps working. The 401 page says so.
The fix is HTTPS at the reverse proxy, never weaker cookie flags.

## Forms, origins, bodies

* HTML form posts carry MirrorWall's double-submit CSRF token; a forged post is `403 CSRF_FAILED`.
* A JSON write whose `Origin` names another host is `403 CSRF_FAILED`; CORS is disabled, so no
  browser page elsewhere can reach the API.
* A body over `server.max_body_bytes` (16 MiB) is `413 PAYLOAD_TOO_LARGE` before it is buffered.

## Limits

* Per-credential rate limit: `rate_limit_burst` (100) at once, then `rate_limit_per_minute` (600)
  sustained; `429 RATE_LIMITED` with `Retry-After` at the boundary, never a dropped request.
* Per-source queue cap: `queue.max_active_per_source` (200) active jobs; past it, `QUEUE_FULL`
  naming the source.

## What LoadCoach never does

* **Execute a tool call.** A provider's tool call is returned to the caller as the fragments the
  provider produced; nothing on this machine runs it (spec §14).
* Fetch from a host outside `evidence.allowed_source_hosts` (loopback only by default), follow a
  redirect across hosts, fetch a `file://` URL or a link-local address, or send an evidence
  credential to any host but its own (ADR-0026 §3).
* Keep prompt and response text past `storage.content_retention_hours` (24) unless
  `retain_content = true` is set in configuration — that key is refused by the settings API.
* Log a prompt, a response or a token; `logging.include_content = true` is the one exception, and
  it is config-only.

## The checklist

Security Standards §14 is held item by item in `tests/security/`; run it with
`pytest tests/security`. `loadcoach doctor` reports the exposure decision it finds.
