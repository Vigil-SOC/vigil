# Security keys

Security keys and passkeys (WebAuthn) are a way to sign in with two-step sign-in, beside the authenticator app and recovery codes. They are tied to the address the console is reached at, so they need two settings. Without them the console shows no key controls and sign-in by password and authenticator app works as before.

## What they need

- **`VIGIL_CONSOLE_HOSTNAME`**: the hostname people type to reach the console, with no scheme, port or path (for example `vigil.example.com`). It is the WebAuthn relying-party id. Vigil never reads it from a request. An IP address is not accepted.
- **HTTPS**: `VIGIL_COOKIE_SECURE` stays `true` (its default), which is the setting that says the console is served over HTTPS.
- **Loopback is the exception.** `localhost` (and any `*.localhost` name) works over plain HTTP with `VIGIL_COOKIE_SECURE=false`, for a local install.

The browser also checks the address bar: a key made at one hostname does not answer at another, and the browser refuses the prompt on a page that is not HTTPS (loopback aside). Reach the console at the configured hostname only; a different name, or an IP address, gets no key prompt.

Set both in `.env` for Compose, or in the chart's `config` values for Helm (`VIGIL_CONSOLE_HOSTNAME`, `VIGIL_COOKIE_SECURE`), then restart the API.

## Air-gapped sites and an internal certificate authority

HTTPS needs a certificate the browser trusts. A site with no route to a public certificate authority runs its own:

1. Issue a certificate for the console's hostname from your internal certificate authority (CA). The hostname must be the one set in `VIGIL_CONSOLE_HOSTNAME`, as a subject alternative name.
2. Serve the console with that certificate (the ingress or reverse proxy in front of it).
3. Put the CA's root certificate in the trust store of every browser or operating system that opens the console. A browser that does not trust the certificate shows a warning, and does not offer keys on that page.

The console's own services keep their own trust settings (`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, see the Helm values); those do not affect browsers.

## Changing the hostname strands enrolled keys

A key is bound to the hostname it was registered at. If `VIGIL_CONSOLE_HOSTNAME` changes, the keys already enrolled no longer answer at the new hostname. They stay listed, and they cannot be used until the hostname is set back.

Someone whose only method is a key signs in with their password and one recovery code until the hostname is set back (see *Require phishing-resistant MFA* below). Choose the hostname before people enrol keys, and keep it stable. After a change, people add their keys again at the new hostname.

## What "keys unavailable" means

When keys are off, the console shows a "Sign-in methods" line in place of the key controls, with the reason from the server. It means one of:

- no hostname is set (`VIGIL_CONSOLE_HOSTNAME` is empty);
- the value is not a bare hostname (it has a scheme, a port, a path, or is an IP address);
- the hostname is not `localhost` and `VIGIL_COOKIE_SECURE` is `false`, so the console is not served over HTTPS.

It is a setting of the whole install, not of one person. A person who has keys enrolled while they are unavailable signs in with their authenticator app or a recovery code, if they have the app. A person whose only method is a key signs in with their password and one recovery code (see below).

## Require phishing-resistant MFA

Settings › Sessions has **Require phishing-resistant MFA** (nobody, privileged roles, everyone; never above Require MFA). For the people it covers the authenticator app no longer counts:

- A covered person with no key is held at setup until they add one. Their sign-in with the app alone does not open the privileged window. A covered person who has a key must sign in with it (a recovery code still works, and removes their methods as it always does).
- Only a person with a key of their own can raise it, and only while keys are available; a service account or API token cannot, and the refusal is on the audit trail. The dialog shows how many covered people have no key before you save.
- While keys are unavailable, covered people sign in with the app. Each such sign-in is marked **keys unavailable** in the sign-in list and carries `step_down` on its `auth.sign_in` trail entry, and holders of `sessions.manage` see a Condition naming the hostname. The requirement returns by itself when keys do.
- A person whose only method is a key, while keys are unavailable, signs in with their password and one recovery code. The code is spent, nothing is removed and no other sign-in ends; the person is notified and the managers are told. Within that sign-in's privileged window they can add the authenticator app and make fresh recovery codes without a code.

## Related

- Sign-in methods are listed in the account menu under **Two-step sign-in**.
- `env.example` has the two settings with their defaults.
