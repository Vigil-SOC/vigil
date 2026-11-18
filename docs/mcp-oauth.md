# OAuth on the MCP surface

Vigil's own MCP server answers at `/mcp`. A client reaches it in one of two ways, and both stay supported:

- **A personal API token** (`vgl_api_`, or an older `vgl_mcp_`). You mint it in Settings or with `python -m core.auth.mcp_token <username>`, and the client sends it as a bearer header. Use this for headless and CLI callers: scripts, CI, the Claude Code plugin's own server entry, and anything with no browser.
- **OAuth.** The client starts from the URL alone. It discovers Vigil's authorization server, registers itself, and sends a person to Vigil's consent page. When the person approves, the client receives tokens. Use this for hosted connectors, which cannot hold a pasted token, and for interactive clients that would rather sign in.

OAuth is off by default, and it stays off until an operator turns it on.

## Turning it on

OAuth goes on only when all three of these hold. Vigil checks them each time a request arrives, not just when the switch is flipped:

1. **The MCP surface is open.** Settings → Integrations → Vigil MCP server, or `VIGIL_MCP_ENABLED`.
2. **The public base URL is https or loopback.** That URL is `VIGIL_FRONTEND_URL`; unset, it falls back to the console on `localhost`, which counts as loopback. Under a sub-path deploy it carries the same prefix as `VIGIL_CONTEXT_PATH`, and it has no query or fragment. Plain `http` is accepted only for `localhost`, `127.0.0.1` and `::1`.
3. **`mcp.oauth_enabled` is on.** Use the **OAuth for MCP clients** switch on the same panel. `VIGIL_MCP_OAUTH_ENABLED` sets the value an install boots with, and a value saved from Settings wins over it.

If you switch it on while (1) or (2) does not hold, Vigil refuses and gives the reason. A value that was already stored, or set by the environment, has no effect until both hold. The panel then reads "OAuth is switched on but not in effect" and gives the reason.

Turning OAuth off stops new connections and refreshes; it does not end existing grants. An access token already issued keeps working on `/mcp` until its hour is up, and the grants work again if OAuth is turned back on. To cut a client off, revoke its grant (see [A grant is a token](#a-grant-is-a-token)).

Every URL comes from `VIGIL_FRONTEND_URL`, never from a request's `Host` header. The panel shows each one:

| | Value |
|---|---|
| Issuer | the origin of `VIGIL_FRONTEND_URL`, with no path |
| Resource | `VIGIL_FRONTEND_URL` + `/mcp` |
| Server metadata | `{issuer}/.well-known/oauth-authorization-server` |
| Resource metadata | `{issuer}/.well-known/oauth-protected-resource{context path}/mcp` |

While OAuth is off, every OAuth path returns 404. A refused `/mcp` request then gets a bare `WWW-Authenticate: Bearer` header. While OAuth is on, that header also carries `resource_metadata="…"`, which is where a client begins discovery.

## The reverse-proxy rule

The authorization server sits at the **host root**, even when the console is served under `VIGIL_CONTEXT_PATH`. Route these paths on the public host to the Vigil backend, in addition to `/mcp` (which is `{context path}/mcp`):

- `/.well-known/oauth-authorization-server`
- `/.well-known/oauth-protected-resource` and everything under it
- `/authorize`
- `/token`
- `/register`
- `/revoke`

A proxy that sends the whole host to Vigil needs nothing more. A proxy that sends only `/vigil/*` to Vigil, or that sends paths to the console and the API separately, needs each of these routes added. Otherwise discovery receives a 404 or the console's HTML. The consent page (`{base}/oauth/authorize`) and its API (`{base}/api/oauth/authorizations/…`) are ordinary console addresses under the context path.

As a Caddy example with no context path, for `./start.sh`: there the console runs on `:6988` behind Vite, which forwards only `/api`, so put this in front and point `VIGIL_FRONTEND_URL` at it (`http://localhost:8480`):

```caddy
:8480 {
  @vigil path /.well-known/oauth-authorization-server* /.well-known/oauth-protected-resource* /authorize /token /register /revoke /mcp /mcp/* /api/*
  handle @vigil {
    reverse_proxy 127.0.0.1:6987   # the backend
  }
  handle {
    reverse_proxy 127.0.0.1:6988   # the console, including the consent page
  }
}
```

Under a context path, `/mcp` and `/api` move under it (`/vigil/mcp`, `/vigil/api/*`) and the six OAuth paths stay at the root.

## Redirect URIs: the allowlist

A client registers the redirect URIs it will use, and Vigil accepts only two kinds:

- **Loopback**: `http` or `https` on `localhost`, `127.0.0.1` or `::1`, on any port. Desktop and CLI clients use these, and they need no setup.
- **The URIs an operator allows**, matched exactly. The list is `mcp.oauth_redirect_allowlist`, under **Redirect URIs allowed** on the Vigil MCP server panel. It starts empty, and no vendor's callback is built in. Each entry must be an absolute `https` URI with no fragment, and the list holds up to 50.

A URI with a fragment is refused even on loopback. Vigil refuses any other registration with `invalid_client_metadata`. The refusal goes on the audit trail as `oauth.client_registration_refused`, with the `redirect_uri` as Vigil parsed it (host lowercased), which is the string to allow. To let that client in:

1. Find the refusal. The panel lists up to 10 recently refused URIs (newest first, from the last 30 days, leaving out any already allowed) under the allowlist field, and Settings › Audit shows each entry ("Refused an OAuth client's redirect URI … allow it in mcp.oauth_redirect_allowlist").
2. Check that the URI belongs to the client you mean to allow.
3. Click **Allow** next to it, or paste it into the field.
4. Have the client connect again. Its next registration succeeds.

If you later remove a URI from the list, a client already registered with it can no longer use it.

The published callbacks of common hosted clients are listed below as equals, so add only the ones you use. The vendor's documentation is the authority, and the refusal on the trail shows the string a client needs.

| Client | Redirect URI |
|---|---|
| Claude (claude.ai, desktop, mobile) | `https://claude.ai/api/mcp/auth_callback`, and `https://claude.com/api/mcp/auth_callback`, which Anthropic says it may move to |
| ChatGPT connectors | `https://chatgpt.com/connector_platform_oauth_redirect` (used when the server advertises RFC 9207 `iss`, as Vigil does) |

Claude Code, MCP Inspector, the MCP SDKs and other local clients use loopback, so they need no entry.

## Connecting a client

1. The client calls `/mcp` and receives a 401 whose header names the protected-resource document. That document names the issuer, and the issuer's metadata names the endpoints.
2. The client registers at `/register`. Every client is public: Vigil issues no client secret and requires PKCE (S256). Vigil holds at most 1,000 clients, and each client may register at most 10 redirect URIs.
3. The client sends the person's browser to `/authorize`, which forwards it to the console's consent page. The person signs in if they need to, then sees the client's name and where it returns to, and approves or denies.
   - Approving is minting a credential for yourself, so the token-minting rules apply. A service account cannot approve. A person outside their confirmation window is asked to confirm it's them first.
   - Denying returns `error=access_denied` to the client.
4. The client exchanges the code at `/token`. It receives a `vgl_oat_` access token and a `vgl_ort_` refresh token, and calls `/mcp` as the person who approved, with that person's role.

From Claude Code, add the server without a header and sign in. The [plugin README](../clients/claude-code/README.md#sign-in-with-oauth-instead-of-a-token) explains how this fits with the plugin's skills.

```bash
claude mcp add --transport http vigil https://vigil.example.com/mcp
claude mcp login vigil
```

## Lifetimes and limits

| | |
|---|---|
| Pending request (approve or deny it within) | 10 minutes |
| Authorization code | 60 seconds, usable once, and only while the approving sign-in stays open |
| Access token (`vgl_oat_`) | 1 hour, and never past the grant |
| Grant | the holder's token lifetime under the token policy, at most 90 days |
| Each refresh | rotates both tokens |
| Registered client with no grant and no pending request | deleted after 30 days unused |

The limits below apply per client address:

| Endpoint | Limit |
|---|---|
| `/register` | 20 per hour |
| `/token` | 60 per minute |
| `/revoke` | 30 per minute |
| `/authorize` | 60 per minute |

When a client goes over a limit, Vigil answers 429 with `Retry-After: 60`.

## A grant is a token

A grant is a row in the same table as personal API tokens. It appears in Settings › Team & access under **API tokens** (and **All API tokens** for a `users.write` holder), labelled with the client's name and marked **Connected app**. It can be revoked but not rotated.

A grant ends when any of the following happens:

- **Revoke** in the tokens list ("Hold to revoke"). Its holder or an administrator can do this.
- The app calls `/revoke` with either token. That ends the whole grant, and the trail records `credential.revoked` with reason "by the app".
- The holder's account is deactivated.
- It reaches its expiry.
- The unused-token rule finds it idle past `token_unused_days`. This works the same as for any other token.
- The refresh token just replaced by a rotation is presented again. Someone else may then hold a copy, so Vigil ends the grant, records `oauth.refresh_reuse`, and sends the holder a high-priority notice.

After a grant ends, its access token gets a 401 at `/mcp` and its refresh token gets `invalid_grant`. To connect again, the person approves a new request.

## On the audit trail

Registration, consent, the grant and refresh reuse each write an entry. [The audit trail page](audit-trail.md#oauth-on-the-mcp-surface) lists them. A tool call through a grant acts as the person who approved it, and the trail names that person; case handoff and closure entries also carry the grant's `credential_id`.
