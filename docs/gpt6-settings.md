# GPT-6 text-stage configuration

The Codex subscription adapter defaults to `gpt-6-sol` with high reasoning effort.
It can be explicitly selected for `correct`, `summarize`, `mind_map`, and `ask`.
Ask keeps the existing single-recording/library retrieval boundary, playable
citations, profile provenance, and durable provider-cost reservations. All four
stages use the same ephemeral no-tools invocation and subscription reserve.
The adapter remains profile-scoped and requires explicit cloud egress.

GPT-6 is a text-generation selection, not an ASR, alignment, diarization or
embedding model. Keep those stages on models that advertise the required
capability. Existing recording artifacts and their historical model attribution
must not be relabelled when settings change.

For an authorized workspace migration:

1. Verify the requested model with the configured Codex login using synthetic
   input. Model-cache visibility and login health alone do not prove inference.
2. Back up configuration and the database. Pause automatic dispatch and let active
   work finish, or stop the daemon and recover its interrupted claims through
   the normal restart path, before changing provider selections.
3. Add the verified model to the capability catalog. Create immutable profile
   versions selecting it for all four text stages, with explicit cloud egress.
   Remove non-target text fallbacks when the user requests only GPT-6 execution.
4. Update the system default and recording, folder, template or automation
   selections that would otherwise override it. Preserve other stage selections,
   cost limits, old profiles and artifact provenance.
5. Validate resolved profiles, a grounded Ask response and quota failure behavior,
   then restore the previous automatic-processing preference.

Offline-named profiles must not silently become cloud profiles: preserve the old
version and give a new cloud-enabled version a name that describes its behavior.
Changing settings does not itself authorize regenerating existing artifacts.

Official model guidance: <https://developers.openai.com/api/docs/guides/latest-model>.
