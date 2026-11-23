package org.localplaud.android;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.security.SecureRandom;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.format.DateTimeParseException;
import java.util.Base64;

final class RecordingUploadApproval {
    private static final Base64.Encoder ENCODER = Base64.getUrlEncoder().withoutPadding();
    private final URI origin;
    private final String proof;
    private final String challenge;

    RecordingUploadApproval(URI origin, boolean allowDebugLoopback) {
        RecordingUploader.validateOrigin(origin, allowDebugLoopback);
        this.origin = origin;
        byte[] nonce = new byte[32];
        new SecureRandom().nextBytes(nonce);
        proof = ENCODER.encodeToString(nonce);
        challenge = challengeForProof(proof);
    }

    static String challengeForProof(String proof) {
        try {
            return ENCODER.encodeToString(MessageDigest.getInstance("SHA-256")
                    .digest(proof.getBytes(StandardCharsets.US_ASCII)));
        } catch (NoSuchAlgorithmException error) {
            throw new AssertionError(error);
        }
    }

    String comparisonCode() {
        return challenge.substring(0, 8);
    }

    URI browserUri() {
        return origin.resolve("/account/android-upload?challenge=" + challenge);
    }

    static Instant parseExpiry(String timestamp) {
        return OffsetDateTime.parse(timestamp).toInstant();
    }

    static final class Grant {
        final String token;
        final String scope;
        final Instant expires;

        Grant(String token, String scope, Instant expires) {
            this.token = token;
            this.scope = scope;
            this.expires = expires;
        }

        boolean isExpired() {
            return !expires.isAfter(Instant.now());
        }
    }

    Grant claim() throws IOException {
        URI endpoint = origin.resolve("/api/imports/local/android-grant");
        HttpURLConnection connection = (HttpURLConnection) endpoint.toURL().openConnection();
        try {
            connection.setInstanceFollowRedirects(false);
            connection.setUseCaches(false);
            connection.setConnectTimeout(10000);
            connection.setReadTimeout(10000);
            connection.setRequestMethod("POST");
            connection.setRequestProperty("Authorization", "Upload-Proof " + proof);
            connection.setDoOutput(true);
            connection.setFixedLengthStreamingMode(0);
            connection.getOutputStream().close();
            int status = connection.getResponseCode();
            if (status == 404) {
                return null;
            }
            if (status != 200) {
                throw new RecordingUploader.Rejected(status);
            }
            try (InputStream input = connection.getInputStream()) {
                ByteArrayOutputStream body = new ByteArrayOutputStream();
                byte[] buffer = new byte[1024];
                int count;
                while ((count = input.read(buffer)) != -1) {
                    if (body.size() + count > 4096) {
                        throw new IOException("Invalid upload grant response length");
                    }
                    body.write(buffer, 0, count);
                }
                JSONObject json = new JSONObject(new String(body.toByteArray(), StandardCharsets.UTF_8));
                return new Grant(json.getString("upload_token"), json.getString("capture_scope"),
                        parseExpiry(json.getString("expires_at")));
            } catch (JSONException | DateTimeParseException error) {
                throw new IOException("Invalid upload grant response");
            }
        } finally {
            connection.disconnect();
        }
    }
}
