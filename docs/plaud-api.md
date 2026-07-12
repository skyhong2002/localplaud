# Official Plaud cloud interfaces

localplaud only issues read-only requests to the authenticated user's Plaud
account. The supported transports are Plaud's official Open API and official MCP.
The reverse-engineered browser-session/apse1 adapter has been removed.

## Product boundary

Plaud supplies recording metadata and raw audio. localplaud owns ASR, alignment,
diarization, transcript correction, notes, mind maps, search, Ask, automation, and
exports. Plaud transcripts and notes are explicit migration/debug inputs only and
cannot satisfy independent-mode acceptance.

## Official Open API

- Base: `https://platform.plaud.ai/developer/api`
- Setup: `localplaud auth login`
- OAuth: S256 PKCE with loopback callback and automatic token refresh
- Token cache: `~/.plaud/tokens.json`, atomically written with mode `0600`
- Read endpoints:
  - `GET /open/third-party/users/current`
  - `GET /open/third-party/files/?page=&page_size=`
  - `GET /open/third-party/files/{id}`
- Detail supplies a 24-hour `presigned_url` for raw audio. Optional `source_list`
  and `note_list` remain migration/debug artifacts with Plaud provenance.

The client rejects non-HTTPS and private/loopback/link-local signed URLs, disables
redirect following, limits raw audio to 2 GiB, and never sends OAuth credentials to
the object-storage host.

## Official Plaud MCP

Install and authorize the official local MCP server:

```bash
npx -y @plaud-ai/mcp@latest install
```

Then set `plaud.provider = "mcp"`. localplaud starts the stdio server without a
shell and uses only these read tools:

- `get_current_user`
- `list_files`
- `get_file`
- `get_note`
- `get_transcript`

MCP listing and `get_file.presigned_url` are valid primary ingest inputs. The same
signed-audio SSRF and size protections apply. `get_note` and `get_transcript` remain
explicit migration/debug paths and are excluded from subscription-independence
acceptance. OAuth state lives in `~/.plaud/tokens-mcp.json` and is never copied into
ordinary database rows, logs, diagnostics, or repository files.

Official documentation: <https://docs.plaud.ai/plaud-mcp-cli/mcp>
