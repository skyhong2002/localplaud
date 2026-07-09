# ADR 0005: Storage — audio on the filesystem, everything else in SQLite

Status: Accepted

## Context

Per recording we hold: the original `.opus` (up to tens of MB), a
converted WAV, cloud metadata, a transcript with per-segment/per-word
timing and speakers, one or more summaries, and embedding vectors for
Q&A. Scale is personal: hundreds to low thousands of recordings,
thousands of embedding chunks. We want trivial backup, no extra services,
and files a user can inspect by hand.

## Decision

- **Audio bytes live on the filesystem**, under id-addressed directories
  keyed by the Plaud file id (e.g. `data/audio/<id>/<fullname>`), with the
  path recorded in the DB (`audio_path`, `wav_path`). Blobs don't belong
  in SQLite; files stay directly playable and rsync-able.
- **Everything else lives in SQLite** (SQLAlchemy models in
  `localplaud/db/models.py`): `plaud_files` mirrors cloud metadata plus
  local pipeline state; `transcripts` stores full text plus the segment
  list (speakers, words, timestamps) as a JSON column; `summaries` holds
  markdown notes keyed by template; `kv` keeps sync cursors.
- **Embeddings in the `chunks` table**: each retrievable chunk stores its
  text, time span, speaker, and its vector as a **float32 blob**
  (`numpy.frombuffer` to decode, `dim` recorded alongside).
- **Retrieval is brute-force cosine similarity** in NumPy: load all
  chunk vectors, one matrix–vector product against the query embedding.
  At personal scale (thousands of chunks × ~384 dims) this is
  single-digit milliseconds — no index needed.

## Consequences

- Backup = copy one `.db` file + one audio directory; restore is the
  reverse. No vector DB, no migration burden for v1.
- Segments-as-JSON keeps writes simple and reads whole-transcript-at-a-
  time (the actual access pattern); the cost is no SQL queries *inside*
  segments — anything queryable per-chunk (time span, speaker) is
  duplicated onto `chunks` columns instead.
- Brute-force search degrades linearly. The documented scale-up path is
  **sqlite-vec** (same file, SQL-visible vectors) or **FAISS** if chunk
  counts reach the hundreds of thousands; the `Chunk` schema already
  stores raw vectors, so either can be adopted without re-embedding.
- Postgres remains reachable via `store.database_url` (ADR 0001); the
  float32-blob column ports as `bytea`.
