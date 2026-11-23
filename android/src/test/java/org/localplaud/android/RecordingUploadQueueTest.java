package org.localplaud.android;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import java.io.ByteArrayInputStream;
import java.io.DataOutputStream;
import java.io.IOException;
import java.nio.channels.FileChannel;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.stream.Stream;
import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;

public class RecordingUploadQueueTest {
    private static final byte[] AUDIO = {1, 2, 3};
    private static final String RECEIPT = "local-sync-01010101010101010101010101010101";

    @Rule public TemporaryFolder temporary = new TemporaryFolder();

    @Test
    public void recordingSurvivesReopenAndRepeatedDiscoveryWithoutChangingUploadId() throws IOException {
        Path root = temporary.newFolder().toPath();
        RecordingUploadQueue queue = queue(root, "workspace-1");
        RecordingUploadQueue.Entry saved = enqueue(queue);

        RecordingUploadQueue reopened = queue(root, "workspace-1");
        RecordingUploadQueue.Entry restored = reopened.pending().get(0);
        assertEquals(saved.uploadId, restored.uploadId);
        assertEquals("test.opus", restored.filename);
        assertArrayEquals(AUDIO, Files.readAllBytes(restored.audio));
        assertEquals(saved.uploadId, reopened.enqueue("note:1", "test.opus", AUDIO.length, () -> {
            throw new AssertionError("A rediscovered recording must not be downloaded again");
        }).uploadId);
        assertEquals(1, reopened.pending().size());
    }

    @Test
    public void interruptedTransferOrAbandonedStagingIsNeverPublished() throws IOException {
        RecordingUploadQueue queue = queue(temporary.newFolder().toPath(), "workspace-1");
        for (long expected : new long[]{2, 4}) {
            assertThrows(IOException.class, () -> queue.enqueue("note:1", "test.opus", expected,
                    () -> new ByteArrayInputStream(AUDIO)));
            assertTrue(queue.pending().isEmpty());
        }

        RecordingUploadQueue.Entry saved = enqueue(queue);
        Path partial = Files.createTempDirectory(saved.audio.getParent().getParent(), "partial-");
        Files.write(partial.resolve("audio"), new byte[]{1});
        Files.write(saved.audio.getParent().resolve("receipt.partial"), new byte[]{1});
        assertEquals(saved.uploadId, queue.pending().get(0).uploadId);
        assertFalse(Files.exists(partial));
        assertEquals(3, queue.diagnostics().quarantined);
        try (Stream<Path> paths = Files.walk(saved.audio.getParent().getParent())) {
            assertEquals(4, paths.filter(path -> path.getFileName().toString().equals("audio")).count());
        }
        assertArrayEquals(AUDIO, Files.readAllBytes(saved.audio));
        queue.acknowledge(saved, RECEIPT);
        assertTrue(queue.pending().isEmpty());
    }

    @Test
    public void onlyMatchingWorkspaceCanAcknowledgeAndSentMarkerSurvivesReopen() throws IOException {
        Path root = temporary.newFolder().toPath();
        RecordingUploadQueue owner = queue(root, "workspace-1");
        RecordingUploadQueue other = queue(root, "workspace-2");
        RecordingUploadQueue.Entry entry = enqueue(owner);
        assertTrue(other.pending().isEmpty());
        assertThrows(IllegalArgumentException.class, () -> other.acknowledge(entry, RECEIPT));
        assertThrows(IllegalArgumentException.class, () -> owner.acknowledge(entry, "unknown"));
        assertTrue(Files.exists(entry.audio));

        owner.acknowledge(entry, RECEIPT);
        assertFalse(Files.exists(entry.audio));
        Files.write(entry.audio, AUDIO);
        RecordingUploadQueue reopened = queue(root, "workspace-1");
        assertTrue(reopened.pending().isEmpty());
        assertFalse(Files.exists(entry.audio));
        RecordingUploadQueue.Entry sent = reopened.enqueue("note:1", "test.opus", AUDIO.length, () -> {
            throw new AssertionError("Acknowledged recording must not be downloaded again");
        });
        assertEquals(entry.uploadId, sent.uploadId);
        assertEquals(RECEIPT, sent.receiptId);
        assertNull(sent.audio);
        assertTrue(other.pending().isEmpty());
    }

    private static RecordingUploadQueue.Entry enqueue(RecordingUploadQueue queue) throws IOException {
        return queue.enqueue("note:1", "test.opus", AUDIO.length, () -> new ByteArrayInputStream(AUDIO));
    }

    @Test
    public void sameWorkspaceIdsOnDifferentHostsDoNotShareRecordings() throws IOException {
        Path root = temporary.newFolder().toPath();
        RecordingUploadQueue production = RecordingUploadQueue.forOrigin(root, "https://workspace.example", "workspace-1",
                RecordingUploadQueueTest::syncDirectory);
        RecordingUploadQueue development = RecordingUploadQueue.forOrigin(root, "http://127.0.0.1:1234", "workspace-1",
                RecordingUploadQueueTest::syncDirectory);
        enqueue(production);
        assertTrue(development.pending().isEmpty());
    }

    @Test
    public void corruptMetadataAndIncompleteFoldersDoNotBlockValidAudio() throws IOException {
        Path root = temporary.newFolder().toPath();
        RecordingUploadQueue queue = queue(root, "workspace-1");
        RecordingUploadQueue.Entry valid = enqueue(queue);
        RecordingUploadQueue.Entry corrupt = queue.enqueue("corrupt", "corrupt.wav", AUDIO.length,
                () -> new ByteArrayInputStream(AUDIO));
        Files.write(corrupt.audio.getParent().resolve("entry"), new byte[]{0, 3, 'b', 'a', 'd'});
        Path missing = Files.createDirectory(valid.audio.getParent().getParent().resolve("a".repeat(64)));
        Files.write(missing.resolve("unexpected"), new byte[]{9});
        Path partial = Files.createTempDirectory(valid.audio.getParent().getParent(), "partial-");
        Files.write(partial.resolve("audio"), AUDIO);
        Files.write(partial.resolve("unexpected"), new byte[]{8});

        assertEquals(valid.uploadId, queue.pending().get(0).uploadId);
        assertEquals(1, queue.pending().size());
        RecordingUploadQueue.Diagnostics diagnostics = queue(root, "workspace-1").diagnostics();
        assertEquals(3, diagnostics.quarantined);
        assertEquals(0, diagnostics.failed);
        assertEquals(0, diagnostics.unavailable);
        assertEquals("3 quarantined, 0 failed, 0 unavailable", diagnostics.summary());
        try (Stream<Path> paths = Files.walk(valid.audio.getParent().getParent())) {
            List<Path> audio = paths.filter(path -> path.getFileName().toString().equals("audio")).toList();
            assertEquals(3, audio.size());
            for (Path path : audio) {
                assertArrayEquals(AUDIO, Files.readAllBytes(path));
            }
        }
    }

    @Test
    public void rejectsUnsupportedExtensionBeforeOpeningAudio() throws IOException {
        RecordingUploadQueue queue = queue(temporary.newFolder().toPath(), "workspace-1");
        for (String name : new String[]{"recording.exe", "recording", ".opus", "recording.opus.exe"}) {
            assertThrows(IllegalArgumentException.class, () -> queue.enqueue("bad", name, AUDIO.length,
                    () -> { throw new AssertionError("Unsupported audio must not be opened"); }));
        }
        assertTrue(queue.pending().isEmpty());
        assertEquals("録音.OPUS", queue.enqueue("good", "録音.OPUS", AUDIO.length,
                () -> new ByteArrayInputStream(AUDIO)).filename);
    }

    @Test
    public void completeStagingSurvivesSyncFailureAndIsRecoveredWithOriginalIdentity() throws IOException {
        Path root = temporary.newFolder().toPath();
        AtomicBoolean fail = new AtomicBoolean(true);
        RecordingUploadQueue queue = new RecordingUploadQueue(root, "workspace-1", path -> {
            if (path.getFileName().toString().startsWith("partial-") && fail.get()) {
                throw new IOException("Injected staging sync failure");
            }
            syncDirectory(path);
        });
        assertThrows(IOException.class, () -> enqueue(queue));
        Path staging;
        try (Stream<Path> paths = Files.walk(root)) {
            staging = paths.filter(path -> path.getFileName().toString().startsWith("partial-")).findFirst().orElseThrow();
        }
        byte[] metadata = Files.readAllBytes(staging.resolve("entry"));
        assertTrue(queue.pending().isEmpty());
        assertEquals(1, queue.diagnostics().unavailable);
        assertArrayEquals(AUDIO, Files.readAllBytes(staging.resolve("audio")));
        fail.set(false);
        RecordingUploadQueue.Entry recovered = queue.enqueue("note:1", "test.opus", AUDIO.length,
                () -> { throw new AssertionError("Complete staging must be recovered without downloading again"); });
        assertArrayEquals(metadata, Files.readAllBytes(recovered.audio.getParent().resolve("entry")));
        assertEquals(recovered.uploadId, enqueue(queue).uploadId);
        assertEquals(recovered.uploadId, queue.pending().get(0).uploadId);
        assertFalse(Files.exists(staging));
        assertArrayEquals(AUDIO, Files.readAllBytes(recovered.audio));
    }

    @Test
    public void enqueueRequiresParentSyncAfterRenameAndCanRecoverAfterFailure() throws IOException {
        Path root = temporary.newFolder().toPath();
        AtomicBoolean fail = new AtomicBoolean(true);
        RecordingUploadQueue queue = new RecordingUploadQueue(root, "workspace-1", path -> {
            if (fail.get()) {
                try (Stream<Path> children = Files.list(path)) {
                    if (children.anyMatch(child -> child.getFileName().toString().matches("[a-f0-9]{64}")
                            && Files.isRegularFile(child.resolve("entry")))) {
                        throw new IOException("Injected enqueue rename sync failure");
                    }
                }
            }
            syncDirectory(path);
        });
        assertThrows(IOException.class, () -> enqueue(queue));
        assertThrows(IOException.class, queue::pending);
        fail.set(false);
        RecordingUploadQueue.Entry recovered = queue.pending().get(0);
        assertEquals(recovered.uploadId, enqueue(queue).uploadId);
        assertArrayEquals(AUDIO, Files.readAllBytes(recovered.audio));
    }

    @Test
    public void receiptRenameIsSyncedBeforeAudioDeletionAndReopenRecovers() throws IOException {
        Path root = temporary.newFolder().toPath();
        AtomicBoolean fail = new AtomicBoolean(false);
        List<Path> synced = new ArrayList<>();
        RecordingUploadQueue queue = new RecordingUploadQueue(root, "workspace-1", path -> {
            synced.add(path);
            if (fail.get() && Files.exists(path.resolve("receipt"))) {
                assertTrue(Files.exists(path.resolve("audio")));
                throw new IOException("Injected receipt rename sync failure");
            }
            syncDirectory(path);
        });
        RecordingUploadQueue.Entry entry = enqueue(queue);
        assertTrue(synced.stream().anyMatch(path -> path.getFileName().toString().startsWith("partial-")));
        assertEquals(entry.audio.getParent().getParent(), synced.get(synced.size() - 1));
        fail.set(true);
        assertThrows(IOException.class, () -> queue.acknowledge(entry, RECEIPT));
        assertTrue(Files.exists(entry.audio));
        assertTrue(queue.pending().isEmpty());
        assertEquals(1, queue.diagnostics().unavailable);
        assertTrue(Files.exists(entry.audio));
        RecordingUploadQueue reopened = queue(root, "workspace-1");
        assertTrue(reopened.pending().isEmpty());
        assertFalse(Files.exists(entry.audio));
        assertEquals(RECEIPT, enqueue(reopened).receiptId);
    }

    @Test
    public void interruptedReceiptAndFailureMarkersRecoverWithoutResending() throws IOException {
        Path root = temporary.newFolder().toPath();
        RecordingUploadQueue queue = queue(root, "workspace-1");
        RecordingUploadQueue.Entry sent = enqueue(queue);
        Path receipt = sent.audio.getParent().resolve("receipt.partial");
        try (DataOutputStream marker = new DataOutputStream(Files.newOutputStream(receipt))) {
            marker.writeUTF(RECEIPT);
            marker.writeUTF(sent.uploadId.toString());
        }
        RecordingUploadQueue.Entry failed = queue.enqueue("failed", "failed.wav", AUDIO.length,
                () -> new ByteArrayInputStream(AUDIO));
        queue.markFailed(failed, 422);
        Files.move(failed.audio.getParent().resolve("failed"), failed.audio.getParent().resolve("failed.partial"),
                StandardCopyOption.ATOMIC_MOVE);

        RecordingUploadQueue reopened = queue(root, "workspace-1");
        assertTrue(reopened.pending().isEmpty());
        assertFalse(Files.exists(sent.audio));
        assertArrayEquals(AUDIO, Files.readAllBytes(failed.audio));
        assertEquals(1, reopened.diagnostics().failed);
        assertEquals(RECEIPT, enqueue(reopened).receiptId);
    }

    private static RecordingUploadQueue queue(Path root, String scope) {
        return new RecordingUploadQueue(root, scope, RecordingUploadQueueTest::syncDirectory);
    }

    static void syncDirectory(Path path) throws IOException {
        try (FileChannel channel = FileChannel.open(path, StandardOpenOption.READ)) {
            channel.force(true);
        }
    }
}
