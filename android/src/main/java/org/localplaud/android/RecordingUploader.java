package org.localplaud.android;

import java.io.IOException;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.UUID;

final class RecordingUploader {
    private static final String AUDIO_PATH = "/api/imports/local/audio";
    private final URI endpoint;

    RecordingUploader() {
        this(URI.create(BuildConfig.WORKSPACE_ORIGIN), BuildConfig.DEBUG);
    }

    RecordingUploader(URI origin, boolean allowDebugLoopback) {
        validateOrigin(origin, allowDebugLoopback);
        endpoint = origin.resolve(AUDIO_PATH);
    }

    static void validateOrigin(URI origin, boolean allowDebugLoopback) {
        boolean loopback = allowDebugLoopback && "http".equals(origin.getScheme())
                && "127.0.0.1".equals(origin.getHost()) && origin.getPort() > 0;
        if ((!"https".equals(origin.getScheme()) && !loopback) || origin.getHost() == null
                || origin.getRawUserInfo() != null || origin.getRawQuery() != null
                || origin.getRawFragment() != null
                || (!origin.getRawPath().isEmpty() && !"/".equals(origin.getRawPath()))) {
            throw new IllegalArgumentException("Expected a workspace origin");
        }
    }

    static final class Rejected extends IOException {
        final int status;

        Rejected(int status) {
            super("Recording upload rejected (HTTP " + status + ")");
            this.status = status;
        }
    }

    static final class BatchResult {
        final int uploaded;
        final int failed;
        final int retryable;
        final boolean unauthorized;
        final RecordingUploadQueue.Diagnostics diagnostics;

        BatchResult(int uploaded, int failed, int retryable, boolean unauthorized,
                RecordingUploadQueue.Diagnostics diagnostics) {
            this.uploaded = uploaded;
            this.failed = failed;
            this.retryable = retryable;
            this.unauthorized = unauthorized;
            this.diagnostics = diagnostics;
        }
    }

    BatchResult uploadPending(RecordingUploadQueue queue, String authorizedScope, String token)
            throws IOException {
        int uploaded = 0;
        int failed = 0;
        int retryable = 0;
        boolean unauthorized = false;
        for (RecordingUploadQueue.Entry entry : queue.pending()) {
            try {
                upload(queue, entry, authorizedScope, token);
                uploaded++;
            } catch (Rejected error) {
                if (error.status == 401) {
                    unauthorized = true;
                    break;
                }
                if (RecordingUploadQueue.isPermanentFailure(error.status)) {
                    failed++;
                } else {
                    retryable++;
                }
            } catch (IOException error) {
                retryable++;
            }
        }
        return new BatchResult(uploaded, failed, retryable, unauthorized, queue.diagnostics());
    }

    String upload(RecordingUploadQueue queue, RecordingUploadQueue.Entry entry,
            String authorizedScope, String token) throws IOException {
        queue.requirePending(entry);
        if (!queue.captureScope.equals(authorizedScope)
                || !authorizedScope.matches("account:[1-9][0-9]*:workspace:[1-9][0-9]*")
                || token == null || !token.matches("lpu1\\.[1-9][0-9]{0,17}\\.[1-9][0-9]{0,17}\\.[0-9]{10}\\.[a-f0-9]{64}")) {
            throw new IllegalArgumentException("Upload grant does not match queued recording workspace");
        }

        String boundary = "localplaud-" + UUID.randomUUID();
        String filename = entry.filename.replace("\"", "\\\"");
        byte[] preamble = (field(boundary, "capture_scope", authorizedScope)
                + field(boundary, "upload_id", entry.uploadId.toString())
                + "--" + boundary + "\r\nContent-Disposition: form-data; name=\"file\"; filename=\""
                + filename + "\"\r\nContent-Type: application/octet-stream\r\n\r\n")
                .getBytes(StandardCharsets.UTF_8);
        byte[] closing = ("\r\n--" + boundary + "--\r\n").getBytes(StandardCharsets.US_ASCII);
        long length = Files.size(entry.audio) + preamble.length + closing.length;

        HttpURLConnection connection = (HttpURLConnection) endpoint.toURL().openConnection();
        try {
            connection.setInstanceFollowRedirects(false);
            connection.setConnectTimeout(15000);
            connection.setReadTimeout(5 * 60 * 1000);
            connection.setRequestMethod("POST");
            connection.setRequestProperty("Authorization", "Bearer " + token);
            connection.setRequestProperty("Content-Type", "multipart/form-data; boundary=" + boundary);
            connection.setDoOutput(true);
            connection.setFixedLengthStreamingMode(length);
            try (OutputStream output = connection.getOutputStream()) {
                output.write(preamble);
                Files.copy(entry.audio, output);
                output.write(closing);
            }
            int status = connection.getResponseCode();
            if (status != 201) {
                if (RecordingUploadQueue.isPermanentFailure(status)) {
                    queue.markFailed(entry, status);
                }
                throw new Rejected(status);
            }
            String receipt = connection.getHeaderField("X-Localplaud-Recording-Id");
            if (!entry.uploadId.toString().equals(connection.getHeaderField("X-Localplaud-Upload-Id"))
                    || receipt == null || !receipt.matches("local-sync-[0-9a-f]{32}")) {
                throw new IOException("Upload response did not verify the queued recording");
            }
            queue.acknowledge(entry, receipt);
            return receipt;
        } finally {
            connection.disconnect();
        }
    }

    private static String field(String boundary, String name, String value) {
        return "--" + boundary + "\r\nContent-Disposition: form-data; name=\"" + name
                + "\"\r\n\r\n" + value + "\r\n";
    }
}
