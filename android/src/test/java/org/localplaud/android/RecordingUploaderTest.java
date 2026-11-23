package org.localplaud.android;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import java.io.BufferedInputStream;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.Closeable;
import java.io.IOException;
import java.io.InputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.Locale;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicBoolean;
import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;

public class RecordingUploaderTest {
    private static final String SCOPE = "account:1:workspace:2";
    private static final String TOKEN = "lpu1.1.2.1900000000." + "a".repeat(64);
    private static final String RECEIPT = "local-sync-01010101010101010101010101010101";

    @Rule public TemporaryFolder temporary = new TemporaryFolder();

    @Test
    public void dropsAndMismatchedReceiptsCanRetryWithOneUploadId() throws IOException {
        Path root = temporary.newFolder().toPath();
        RecordingUploadQueue queue = queue(root);
        RecordingUploadQueue.Entry audio = enqueue(queue);
        try (LoopbackServer server = new LoopbackServer(null, receipt("wrong"), receipt(audio.uploadId.toString()))) {
            RecordingUploader uploader = new RecordingUploader(server.origin(), true);
            assertThrows(IOException.class, () -> uploader.upload(queue, audio, SCOPE, TOKEN));
            RecordingUploadQueue reopened = queue(root);
            RecordingUploadQueue.Entry retry = reopened.pending().get(0);
            assertEquals(audio.uploadId, retry.uploadId);
            assertThrows(IOException.class, () -> uploader.upload(reopened, retry, SCOPE, TOKEN));
            assertTrue(Files.exists(audio.audio));
            assertEquals(RECEIPT, uploader.upload(reopened, retry, SCOPE, TOKEN));
            for (String multipart : server.received()) {
                assertTrue(multipart.contains("name=\"capture_scope\"\r\n\r\n" + SCOPE + "\r\n"));
                assertTrue(multipart.contains("name=\"upload_id\"\r\n\r\n" + audio.uploadId + "\r\n"));
                assertTrue(multipart.contains("filename=\"錄音.opus\""));
                assertTrue(multipart.contains("\r\n\r\ntest audio\r\n"));
            }
            assertFalse(Files.exists(audio.audio));
            assertTrue(reopened.pending().isEmpty());
        }
    }

    @Test
    public void refusesOtherWorkspacesAndDoesNotForwardRedirects() throws IOException {
        RecordingUploadQueue queue = queue(temporary.newFolder().toPath());
        RecordingUploadQueue.Entry audio = enqueue(queue);
        try (LoopbackServer server = new LoopbackServer("307 Temporary Redirect\r\nLocation: /other\r\n")) {
            assertThrows(IllegalArgumentException.class, () -> new RecordingUploader(server.origin(), false));
            RecordingUploader uploader = new RecordingUploader(server.origin(), true);
            assertThrows(IllegalArgumentException.class,
                    () -> uploader.upload(queue, audio, "account:2:workspace:3", TOKEN));
            assertEquals(307, assertThrows(RecordingUploader.Rejected.class,
                    () -> uploader.upload(queue, audio, SCOPE, TOKEN)).status);
            assertEquals(1, server.received().length);
            assertTrue(Files.exists(audio.audio));
        }
    }

    @Test
    public void permanentFailuresAreRetainedAndDoNotBlockLaterEntriesOrRetryOnReopen() throws IOException {
        Path root = temporary.newFolder().toPath();
        RecordingUploadQueue queue = queue(root);
        List<RecordingUploadQueue.Entry> entries = enqueueBatch(queue, 5);
        int[] failures = {413, 415, 422, 409};
        try (LoopbackServer server = new LoopbackServer("413 Content Too Large\r\n", "415 Unsupported Media Type\r\n",
                "422 Unprocessable Entity\r\n", "409 Conflict\r\n", receipt(entries.get(4).uploadId.toString()))) {
            RecordingUploader uploader = new RecordingUploader(server.origin(), true);
            RecordingUploader.BatchResult result = uploader.uploadPending(queue, SCOPE, TOKEN);
            assertEquals(1, result.uploaded);
            assertEquals(4, result.failed);
            assertEquals(0, result.retryable);
            assertFalse(result.unauthorized);
            assertEquals(4, result.diagnostics.failed);
            assertEquals(5, server.received().length);

            RecordingUploadQueue reopened = queue(root);
            assertTrue(reopened.pending().isEmpty());
            RecordingUploader.BatchResult retry = uploader.uploadPending(reopened, SCOPE, TOKEN);
            assertEquals(0, retry.uploaded);
            assertEquals(0, retry.failed);
            assertEquals(4, retry.diagnostics.failed);
            for (int index = 0; index < failures.length; index++) {
                RecordingUploadQueue.Entry failed = entries.get(index);
                assertArrayEquals("test audio".getBytes(StandardCharsets.UTF_8), Files.readAllBytes(failed.audio));
                assertThrows(IOException.class, () -> uploader.upload(reopened, failed, SCOPE, TOKEN));
            }
            assertFalse(Files.exists(entries.get(4).audio));
        }
    }

    @Test
    public void networkAndReceiptFailuresLeaveRetryableAudioAndContinueBatch() throws IOException {
        RecordingUploadQueue queue = queue(temporary.newFolder().toPath());
        List<RecordingUploadQueue.Entry> entries = enqueueBatch(queue, 4);
        try (LoopbackServer server = new LoopbackServer(null, receipt("wrong"), "500 Internal Server Error\r\n",
                receipt(entries.get(3).uploadId.toString()))) {
            RecordingUploader.BatchResult result = new RecordingUploader(server.origin(), true)
                    .uploadPending(queue, SCOPE, TOKEN);
            assertEquals(1, result.uploaded);
            assertEquals(0, result.failed);
            assertEquals(3, result.retryable);
            assertFalse(result.unauthorized);
            assertEquals(0, result.diagnostics.failed);
            assertEquals(4, server.received().length);
            assertEquals(3, queue.pending().size());
            for (int index = 0; index < 3; index++) {
                assertTrue(Files.exists(entries.get(index).audio));
                assertEquals(entries.get(index).uploadId, queue.pending().get(index).uploadId);
            }
        }
    }

    @Test
    public void unauthorizedStopsBatchAndRetainsRemainingAudio() throws IOException {
        RecordingUploadQueue queue = queue(temporary.newFolder().toPath());
        List<RecordingUploadQueue.Entry> entries = enqueueBatch(queue, 3);
        try (LoopbackServer server = new LoopbackServer(receipt(entries.get(0).uploadId.toString()),
                "401 Unauthorized\r\n")) {
            RecordingUploader.BatchResult result = new RecordingUploader(server.origin(), true)
                    .uploadPending(queue, SCOPE, TOKEN);
            assertEquals(1, result.uploaded);
            assertEquals(0, result.failed);
            assertEquals(0, result.retryable);
            assertTrue(result.unauthorized);
            assertEquals(2, server.received().length);
            assertEquals(2, queue.pending().size());
            assertTrue(Files.exists(entries.get(1).audio));
            assertTrue(Files.exists(entries.get(2).audio));
        }
    }

    @Test
    public void corruptEntryDoesNotPreventValidBatchUpload() throws IOException {
        RecordingUploadQueue queue = queue(temporary.newFolder().toPath());
        List<RecordingUploadQueue.Entry> entries = enqueueBatch(queue, 2);
        Files.write(entries.get(0).audio.getParent().resolve("entry"), new byte[]{1});
        try (LoopbackServer server = new LoopbackServer(receipt(entries.get(1).uploadId.toString()))) {
            RecordingUploader.BatchResult result = new RecordingUploader(server.origin(), true)
                    .uploadPending(queue, SCOPE, TOKEN);
            assertEquals(1, result.uploaded);
            assertEquals(1, result.diagnostics.quarantined);
            assertEquals(1, server.received().length);
            assertTrue(queue.pending().isEmpty());
        }
    }

    @Test
    public void diagnosticsCountExistingAndNewFailureMarkersOnce() throws IOException {
        RecordingUploadQueue queue = queue(temporary.newFolder().toPath());
        List<RecordingUploadQueue.Entry> entries = enqueueBatch(queue, 3);
        queue.markFailed(entries.get(0), 413);
        try (LoopbackServer server = new LoopbackServer("422 Unprocessable Entity\r\n",
                receipt(entries.get(2).uploadId.toString()))) {
            RecordingUploader uploader = new RecordingUploader(server.origin(), true);
            RecordingUploader.BatchResult result = uploader.uploadPending(queue, SCOPE, TOKEN);
            assertEquals(1, result.uploaded);
            assertEquals(1, result.failed);
            assertEquals(2, result.diagnostics.failed);
            assertEquals(0, result.diagnostics.unavailable);
            assertEquals(2, server.received().length);

            RecordingUploader.BatchResult next = uploader.uploadPending(queue, SCOPE, TOKEN);
            assertEquals(0, next.failed);
            assertEquals(2, next.diagnostics.failed);
            assertTrue(Files.exists(entries.get(0).audio));
            assertTrue(Files.exists(entries.get(1).audio));
        }
    }

    @Test
    public void failureMarkerSyncErrorsRemainVisibleAndDoNotBlockOtherUploads() throws IOException {
        Path root = temporary.newFolder().toPath();
        AtomicBoolean fail = new AtomicBoolean(true);
        RecordingUploadQueue queue = new RecordingUploadQueue(root, SCOPE, path -> {
            if (fail.get() && Files.exists(path.resolve("failed"))) {
                throw new IOException("Injected failure marker sync error");
            }
            RecordingUploadQueueTest.syncDirectory(path);
        });
        List<RecordingUploadQueue.Entry> entries = enqueueBatch(queue, 2);
        try (LoopbackServer server = new LoopbackServer("422 Unprocessable Entity\r\n",
                receipt(entries.get(1).uploadId.toString()))) {
            RecordingUploader.BatchResult result = new RecordingUploader(server.origin(), true)
                    .uploadPending(queue, SCOPE, TOKEN);
            assertEquals(1, result.uploaded);
            assertEquals(0, result.failed);
            assertEquals(1, result.retryable);
            assertEquals(1, result.diagnostics.unavailable);
            assertTrue(queue.pending().isEmpty());
            assertTrue(Files.exists(entries.get(0).audio));
            assertEquals(2, server.received().length);

            fail.set(false);
            assertEquals(1, queue.diagnostics().failed);
            assertEquals(0, queue.diagnostics().unavailable);
            assertEquals(1, result.diagnostics.unavailable);
            assertTrue(Files.exists(entries.get(0).audio));
        }
    }

    @Test
    public void receiptMarkerSyncErrorsRemainVisibleUntilDurableRecovery() throws IOException {
        AtomicBoolean fail = new AtomicBoolean(true);
        RecordingUploadQueue queue = new RecordingUploadQueue(temporary.newFolder().toPath(), SCOPE, path -> {
            if (fail.get() && Files.exists(path.resolve("receipt"))) {
                throw new IOException("Injected receipt marker sync error");
            }
            RecordingUploadQueueTest.syncDirectory(path);
        });
        RecordingUploadQueue.Entry entry = enqueue(queue);
        try (LoopbackServer server = new LoopbackServer(receipt(entry.uploadId.toString()))) {
            RecordingUploader.BatchResult result = new RecordingUploader(server.origin(), true)
                    .uploadPending(queue, SCOPE, TOKEN);
            assertEquals(0, result.uploaded);
            assertEquals(1, result.retryable);
            assertEquals(1, result.diagnostics.unavailable);
            assertTrue(Files.exists(entry.audio));
            assertEquals(1, server.received().length);

            fail.set(false);
            assertEquals(0, queue.diagnostics().unavailable);
            assertTrue(queue.pending().isEmpty());
            assertFalse(Files.exists(entry.audio));
            assertEquals(1, result.diagnostics.unavailable);
        }
    }

    @Test
    public void recoveredMarkerStorageErrorStillAppearsInBatchFailureCount() throws IOException {
        AtomicBoolean fail = new AtomicBoolean(true);
        RecordingUploadQueue queue = new RecordingUploadQueue(temporary.newFolder().toPath(), SCOPE, path -> {
            if (Files.exists(path.resolve("failed")) && fail.getAndSet(false)) {
                throw new IOException("Injected temporary marker sync error");
            }
            RecordingUploadQueueTest.syncDirectory(path);
        });
        RecordingUploadQueue.Entry entry = enqueue(queue);
        try (LoopbackServer server = new LoopbackServer("422 Unprocessable Entity\r\n")) {
            RecordingUploader uploader = new RecordingUploader(server.origin(), true);
            RecordingUploader.BatchResult result = uploader.uploadPending(queue, SCOPE, TOKEN);
            assertEquals(0, result.uploaded);
            assertEquals(0, result.failed);
            assertEquals(1, result.retryable);
            assertEquals(1, result.diagnostics.failed);
            assertEquals(0, result.diagnostics.unavailable);
            assertTrue(Files.exists(entry.audio));
            assertTrue(queue.pending().isEmpty());
            assertEquals(1, server.received().length);

            RecordingUploader.BatchResult next = uploader.uploadPending(queue, SCOPE, TOKEN);
            assertEquals(0, next.retryable);
            assertEquals(1, next.diagnostics.failed);
        }
    }

    private static List<RecordingUploadQueue.Entry> enqueueBatch(RecordingUploadQueue queue, int count)
            throws IOException {
        byte[] audio = "test audio".getBytes(StandardCharsets.UTF_8);
        for (int index = 0; index < count; index++) {
            queue.enqueue("batch:" + index, "batch.opus", audio.length, () -> new ByteArrayInputStream(audio));
        }
        return queue.pending();
    }

    private static String receipt(String uploadId) {
        return "201 Created\r\nX-Localplaud-Recording-Id: " + RECEIPT
                + "\r\nX-Localplaud-Upload-Id: " + uploadId + "\r\n";
    }

    private static final class LoopbackServer implements Closeable {
        private final ServerSocket listener;
        private final ExecutorService executor = Executors.newSingleThreadExecutor();
        private final Future<String[]> requests;

        LoopbackServer(String... responses) throws IOException {
            listener = new ServerSocket(0, 5, InetAddress.getByName("127.0.0.1"));
            listener.setSoTimeout(5000);
            requests = executor.submit(() -> {
                String[] result = new String[responses.length];
                for (int index = 0; index < responses.length; index++) {
                    try (Socket socket = listener.accept()) {
                        socket.setSoTimeout(5000);
                        InputStream input = new BufferedInputStream(socket.getInputStream());
                        assertEquals("POST /api/imports/local/audio HTTP/1.1", line(input));
                        Map<String, String> headers = new HashMap<>();
                        for (String field = line(input); !field.isEmpty(); field = line(input)) {
                            int colon = field.indexOf(':');
                            headers.put(field.substring(0, colon).toLowerCase(Locale.ROOT), field.substring(colon + 1).trim());
                        }
                        assertEquals("Bearer " + TOKEN, headers.get("authorization"));
                        int length = Integer.parseInt(headers.get("content-length"));
                        byte[] body = input.readNBytes(length);
                        assertEquals(length, body.length);
                        result[index] = new String(body, StandardCharsets.UTF_8);
                        if (responses[index] != null) {
                            String response = "HTTP/1.1 " + responses[index] + "Content-Length: 0\r\nConnection: close\r\n\r\n";
                            socket.getOutputStream().write(response.getBytes(StandardCharsets.US_ASCII));
                        }
                    }
                }
                return result;
            });
        }

        URI origin() {
            return URI.create("http://127.0.0.1:" + listener.getLocalPort());
        }

        String[] received() {
            try {
                return requests.get(6, TimeUnit.SECONDS);
            } catch (InterruptedException error) {
                Thread.currentThread().interrupt();
                throw new AssertionError(error);
            } catch (ExecutionException | TimeoutException error) {
                throw new AssertionError(error);
            }
        }

        @Override public void close() throws IOException {
            executor.shutdownNow();
            listener.close();
        }

        private static String line(InputStream input) throws IOException {
            ByteArrayOutputStream bytes = new ByteArrayOutputStream();
            for (int next = input.read(); next != -1; next = input.read()) {
                if (next == '\n') {
                    return bytes.toString(StandardCharsets.US_ASCII).trim();
                }
                bytes.write(next);
            }
            throw new IOException("Incomplete HTTP header");
        }
    }

    private static RecordingUploadQueue.Entry enqueue(RecordingUploadQueue queue) throws IOException {
        byte[] audio = "test audio".getBytes(StandardCharsets.UTF_8);
        return queue.enqueue("note:1", "錄音.opus", audio.length, () -> new ByteArrayInputStream(audio));
    }

    private static RecordingUploadQueue queue(Path root) {
        return new RecordingUploadQueue(root, SCOPE, RecordingUploadQueueTest::syncDirectory);
    }
}
