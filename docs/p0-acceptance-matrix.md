# P0 acceptance matrix

Status captured 2026-07-10. Code-level tests are necessary but do not substitute
for host, credential, or consented-recording acceptance evidence.

| Path | Current evidence | Result | Required next evidence |
| --- | --- | --- | --- |
| Apple local | On a database copy, a 1-second existing raw recording completed MLX large-v3-turbo ASR, 2 word timestamps, alignment validation, correction, full notes, mind map, Ollama embedding, and immutable attempts. | Partial | Accept pyannote Community-1 terms, configure a Hugging Face token, and repeat with distinct speakers. |
| NVIDIA local | CUDA profile, capability catalog, routing, remote protocol, and tests exist. SSH to `nvplaud.observe.tw:22` timed out. | Not externally verified | Restore a reachable deployment channel, run model/pyannote health, then run the same clean recording and benchmark manifest. |
| CPU/aarch64 | CPU profile and conservative hardware detection exist. The current SSH key was rejected by `plaud.skyhong.tw`. | Not externally verified | Provide the deployment host's accepted key/channel, then measure accuracy, runtime, and memory. |
| OpenAI cloud | Audio, chat-compatible text, and embeddings are independently declared/routed with separate model capabilities, limits, headers, timeouts, base URLs, and secret references. Model-shape tests verify Whisper verbose timestamps, GPT-4o JSON transcription, and diarized speaker segments without inventing precision. | Code verified only | Configure an explicitly authorized test key/cost ceiling and run one clean artifact through each separately. |
| Partial OpenAI-compatible service | Profile/catalog validation rejects a text-only shape when selected for ASR. | Tested | Repeat against the chosen real compatible endpoint's advertised shapes. |
| Remote worker | Protocol-v1 server, durable runner, signed audio fetch, client adapter, idempotency, cancellation, checksums, retryable resubmission, claim heartbeats, crash recovery, and stale-owner rejection are tested. | Code verified only | Deploy once on a self-owned NVIDIA host and once on an isolated rentable GPU. |
| Taiwan Mandarin / Mandarin-English | Consent/ownership-gated numeric-only benchmark harness is implemented. | No dataset result | Supply a private consented manifest and run CER/WER, DER proxy, timestamp MAE, hallucination, RTF, and memory thresholds. |
| Local-only policy | Primary and fallback route resolution rejects egress, disallowed providers, quality-floor, timeout, and cost violations after authoritative connection materialization. | Tested | Repeat on the deployed profile and inspect `/api/files/{id}/stage-runs`. |
| Transcript/edit/export P0 | Raw/aligned/diarized artifacts, conflict-checked segment/bulk/find-replace revisions, dependency links/history, and full-outline PNG export have integration tests. | 259-test suite passes; visual QA blocked | Re-authorize T3/in-app browser or wait for the Codex computer-use quota reset, then repeat desktop and 390 px mobile checks. |

The Apple smoke used only a temporary database copy and did not print transcript or
note content. It ended `partial` solely because diarization correctly reported the
missing Hugging Face authorization. This is evidence that resumability works; it is
not subscription-replacement completion because the required speaker result is
absent.
