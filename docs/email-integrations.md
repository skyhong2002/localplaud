# Authorized SMTP email

SMTP integrations are opt-in destinations managed under Settings. Creating an
integration does not connect or send. Pressing **Test** sends one message containing
only a localplaud test marker; recording data is sent only when an enabled destination
is selected by an AutoFlow rule.

## Transport and credentials

- STARTTLS is the default. Implicit TLS is available for providers using ports such as
  465; both use the system trust store and hostname verification.
- Plain SMTP, private/LAN hosts, and loopback hosts require the separate
  **Explicitly allow private/LAN host and plain SMTP** setting.
- SMTP hosts are resolved and checked before every connection.
- Passwords use `env:VARIABLE_NAME` references. A referenced password requires an
  explicit username. The resolved password is used only for SMTP AUTH and is never
  stored in settings, rules, snapshots, messages, or delivery history.
- From/To addresses and the subject prefix reject line breaks and malformed values to
  prevent mail-header injection. A destination supports up to 20 unique recipients.

Example:

```sh
export LOCALPLAUD_SMTP_PASSWORD='replace-me'
```

Set the password reference to `env:LOCALPLAUD_SMTP_PASSWORD`.

## Data scopes and messages

Every AutoFlow email includes recording metadata. Additional scopes are explicit:

- `transcript`: corrected canonical local transcript with timestamps and speaker
  display names;
- `notes`: current exportable local generated notes and saved notes.

Messages are plain text and capped at 5 MiB. One AutoFlow run and destination receive
a stable `Message-ID` and `X-Localplaud-Delivery-Id`, so a receiver can deduplicate an
indeterminate retry.

## Delivery behavior

Email runs after core AutoFlow actions commit. Each destination gets a durable row
containing its non-secret configuration/scope snapshot, payload SHA-256, stable IDs,
attempt count, status, and error. SMTP rejection, missing credentials, network failure,
or destination disablement never rolls back folders, tags, profiles, notifications,
exports, or webhooks. Retry from Discover reuses the same message identity without
rerunning ASR or any other processing stage.
