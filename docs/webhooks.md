# Authorized webhooks

Webhook integrations are opt-in outbound delivery destinations managed under
Settings. Creating an integration does not send data. Pressing **Test** sends only a
small `localplaud.webhook.test` event; an AutoFlow sends recording data only after the
integration is selected as one of that rule's actions.

## Authorization and network policy

- Public destinations must use HTTPS.
- URLs cannot contain credentials, query parameters, or fragments. Put a bearer token
  in an environment variable and save only an `env:VARIABLE_NAME` reference.
- Private, loopback, link-local, and other non-routable targets are denied unless
  **Explicitly allow private/LAN destination and HTTP** is selected.
- Redirects are not followed. DNS is checked before each request, payloads are capped
  at 5 MiB, and response capture is capped at 64 KiB.

Example environment setup:

```sh
export LOCALPLAUD_TEAM_WEBHOOK_TOKEN='replace-me'
```

Use `env:LOCALPLAUD_TEAM_WEBHOOK_TOKEN` as the secret reference. The resolved value is
used only for `Authorization: Bearer …`; it is never written to an integration, rule,
run snapshot, payload, or delivery row.

## Payload scopes

Every payload contains local recording metadata. Additional scopes are explicit:

- `transcript`: corrected canonical local transcript segments, speaker display names,
  and transcript/revision provenance;
- `notes`: current exportable local generated notes and saved notes.

The event type is `localplaud.autoflow.completed`, version `1`. The
`X-Localplaud-Delivery-Id` header and payload `idempotency_key` are stable for one
AutoFlow run and destination. Receivers should use that value to deduplicate retries.

## Delivery behavior

Webhook delivery runs only after core AutoFlow actions commit. Each destination gets
an independent durable row containing its destination/scope snapshot, payload SHA-256,
attempt count, HTTP status, bounded response excerpt, and error. A failed webhook does
not roll back folders, tags, templates, profiles, notifications, or exports. Retry from
Discover reuses the same idempotency key and does not rerun ASR or other processing.
