# Versioning and releases

localplaud uses `MAJOR.MINOR.PATCH` versions. The version in
`src/localplaud/__init__.py` also sets the Python package version, the Android
`versionName`, and the version shown by `localplaud --version` and Web Settings
→ Support & about. Android's `versionCode` is `MAJOR × 1,000,000 + MINOR × 1,000 + PATCH`.
Use stable numeric components from 0 to 999, with a positive version code.
When updating an APK, use the same signing key and a higher version code.

## Cut a release

1. In a PR, bump `__version__` and add the matching `## [X.Y.Z]` section to
   `CHANGELOG.md`. Include required migration or deployment steps. Run
   `uv lock --check` (regenerate the lockfile only if dependencies changed).
2. Merge the PR and wait for CI to pass on `main`. Confirm the intended version
   is in the merged commit, then push an annotated tag for that exact commit:

   ```sh
   git fetch origin main
   git tag -a v0.2.0 origin/main -m 'localplaud 0.2.0'
   git push origin refs/tags/v0.2.0
   ```

3. The Release workflow rejects tags that don't match the source version or
   lack changelog notes. It publishes the Python wheel, source distribution,
   and `SHA256SUMS` on [GitHub Releases](https://github.com/skyhong2002/localplaud/releases).
   Tagging does not deploy to a server or publish to PyPI. Don't move a published
   tag to fix an artifact; cut a new patch release instead.

## Optional signed Android asset

An Android APK embeds the workspace origin and isn't a universal installer.
Leave `RELEASE_ANDROID` unset for a Python-only release, or set the repository
variable to `true` and set `RELEASE_ANDROID_ORIGIN` to an HTTPS origin **intended
to be public in the APK**. In the `android-release` GitHub environment, set
`ANDROID_KEYSTORE_BASE64`, `ANDROID_STORE_PASSWORD`, `ANDROID_KEY_ALIAS`, and
`ANDROID_KEY_PASSWORD` as secrets. Keep a backup of the signing key outside CI.
Never reuse a debug key or publish private workspace URLs or credentials.

With Android enabled, the workflow tests the build, signs and verifies the APK,
and includes it and its checksum in the same release. A failed Android job
blocks publishing rather than silently dropping the APK. Before distributing,
publish the APK certificate fingerprint and run the checks in
[Android origin verification](android.md#verify-the-workspace-origin). For a
private workspace, build and sign the same tag locally instead.
