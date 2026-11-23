package org.localplaud.android;

import android.system.ErrnoException;
import android.system.Os;
import android.system.OsConstants;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.UUID;

final class RecordingUploadQueue {
    private static final long MAX_AUDIO_BYTES = 2L * 1024 * 1024 * 1024;
    private static final Object LOCK = new Object();
    private static final Set<String> EXTENSIONS = new HashSet<>(Arrays.asList(
            "mp3", "mp4", "m4a", "wav", "ogg", "opus", "webm", "flac", "aac", "amr"));
    private final Path directory;
    private final DirectorySync directorySync;
    private Diagnostics lastDiagnostics = new Diagnostics(0, 0, 0);
    final String captureScope;

    RecordingUploadQueue(Path privateDirectory, String captureScope) {
        this(privateDirectory, captureScope, RecordingUploadQueue::syncAndroidDirectory);
    }

    RecordingUploadQueue(Path privateDirectory, String captureScope, DirectorySync directorySync) {
        requireIdentifier(captureScope);
        this.captureScope = captureScope;
        this.directorySync = directorySync;
        directory = privateDirectory.toAbsolutePath().normalize().resolve(digest(captureScope));
    }

    static RecordingUploadQueue forOrigin(Path privateDirectory, String origin, String captureScope) {
        return new RecordingUploadQueue(privateDirectory.resolve(digest(origin)), captureScope);
    }

    static RecordingUploadQueue forOrigin(Path privateDirectory, String origin, String captureScope,
            DirectorySync directorySync) {
        return new RecordingUploadQueue(privateDirectory.resolve(digest(origin)), captureScope, directorySync);
    }

    interface OpenAudio {
        InputStream open() throws IOException;
    }

    interface DirectorySync {
        void sync(Path path) throws IOException;
    }

    static final class Diagnostics {
        final int quarantined;
        final int failed;
        final int unavailable;

        Diagnostics(int quarantined, int failed, int unavailable) {
            this.quarantined = quarantined;
            this.failed = failed;
            this.unavailable = unavailable;
        }

        String summary() {
            return quarantined + " quarantined, " + failed + " failed, " + unavailable + " unavailable";
        }
    }

    static final class Entry {
        final UUID uploadId;
        final String filename;
        final Path audio;
        final String receiptId;
        final Integer failureStatus;
        private final String storageKey;

        private Entry(UUID uploadId, String filename, Path audio, String receiptId,
                Integer failureStatus, String storageKey) {
            this.uploadId = uploadId;
            this.filename = filename;
            this.audio = audio;
            this.receiptId = receiptId;
            this.failureStatus = failureStatus;
            this.storageKey = storageKey;
        }
    }

    Entry enqueue(String recordingKey, String filename, long expectedBytes, OpenAudio audio)
            throws IOException {
        requireIdentifier(recordingKey);
        if (!validFilename(filename) || expectedBytes <= 0 || expectedBytes > MAX_AUDIO_BYTES) {
            throw new IllegalArgumentException("Invalid recording metadata");
        }
        synchronized (LOCK) {
            createDirectoriesDurably(directory);
            String storageKey = digest(recordingKey);
            Path target = directory.resolve(storageKey);
            if (!Files.exists(target)) {
                try (DirectoryStream<Path> paths = Files.newDirectoryStream(directory, "partial-*")) {
                    for (Path path : paths) {
                        Entry staged;
                        try {
                            staged = read(path);
                        } catch (IOException error) {
                            continue;
                        }
                        if (storageKey.equals(staged.storageKey)) {
                            publishStaging(path, staged);
                            break;
                        }
                    }
                }
            }
            if (Files.exists(target)) {
                recoverMarkers(target);
                syncDirectory(target);
                syncDirectory(directory);
                return read(target);
            }
            Path staging = Files.createTempDirectory(directory, "partial-");
            UUID uploadId = UUID.randomUUID();
            try (InputStream input = audio.open();
                    FileOutputStream output = new FileOutputStream(staging.resolve("audio").toFile())) {
                long total = 0;
                byte[] buffer = new byte[64 * 1024];
                int bytes;
                while ((bytes = input.read(buffer)) != -1) {
                    total += bytes;
                    if (total > expectedBytes) {
                        throw new IOException("Recorder sent more audio than expected");
                    }
                    output.write(buffer, 0, bytes);
                }
                if (total != expectedBytes) {
                    throw new IOException("Recording transfer incomplete");
                }
                output.getFD().sync();
            }
            writeMetadata(staging.resolve("entry"), uploadId.toString(), filename,
                    Long.toString(expectedBytes), storageKey);
            syncDirectory(staging);
            Files.move(staging, target, StandardCopyOption.ATOMIC_MOVE);
            syncDirectory(directory);
            return read(target);
        }
    }

    List<Entry> pending() throws IOException {
        synchronized (LOCK) {
            List<Entry> entries = new ArrayList<>();
            int quarantined = 0;
            int failed = 0;
            int unavailable = 0;
            if (!Files.isDirectory(directory)) {
                lastDiagnostics = new Diagnostics(0, 0, 0);
                return entries;
            }
            syncDirectory(directory);
            List<Path> folders = new ArrayList<>();
            try (DirectoryStream<Path> paths = Files.newDirectoryStream(directory)) {
                for (Path path : paths) {
                    folders.add(path);
                }
            }
            folders.sort(Path::compareTo);
            for (Path path : folders) {
                String name = path.getFileName().toString();
                if (name.startsWith("quarantine-")) {
                    quarantined++;
                    continue;
                }
                if (!name.startsWith("partial-") && !name.matches("[a-f0-9]{64}")) {
                    continue;
                }
                try {
                    recoverMarkers(path);
                    Entry entry = read(path);
                    if (name.startsWith("partial-")) {
                        path = publishStaging(path, entry);
                        entry = read(path);
                    }
                    syncDirectory(path);
                    if (entry.receiptId != null) {
                        deleteAcknowledgedAudio(path.resolve("audio"));
                        syncDirectory(path);
                    } else if (entry.failureStatus != null) {
                        failed++;
                    } else {
                        entries.add(entry);
                    }
                } catch (DurabilityException error) {
                    unavailable++;
                } catch (IOException error) {
                    try {
                        quarantine(path);
                        quarantined++;
                    } catch (IOException quarantineError) {
                        unavailable++;
                    }
                }
            }
            lastDiagnostics = new Diagnostics(quarantined, failed, unavailable);
            return entries;
        }
    }

    Diagnostics diagnostics() throws IOException {
        synchronized (LOCK) {
            pending();
            return lastDiagnostics;
        }
    }

    static boolean isPermanentFailure(int status) {
        return status == 413 || status == 415 || status == 422 || status == 409;
    }

    void markFailed(Entry entry, int status) throws IOException {
        if (!isPermanentFailure(status)) {
            throw new IllegalArgumentException("Not a permanent upload failure");
        }
        synchronized (LOCK) {
            requireOwnedAudio(entry);
            Path folder = entry.audio.getParent();
            Entry current = read(folder);
            if (!entry.uploadId.equals(current.uploadId) || current.receiptId != null) {
                throw new IOException("Failure does not match queued recording");
            }
            writeMetadata(folder.resolve("failed.partial"), Integer.toString(status), entry.uploadId.toString());
            Files.move(folder.resolve("failed.partial"), folder.resolve("failed"),
                    StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
            syncDirectory(folder);
        }
    }

    void acknowledge(Entry entry, String receiptId) throws IOException {
        if (receiptId == null || !receiptId.matches("local-sync-[0-9a-f]{32}")) {
            throw new IllegalArgumentException("Invalid upload receipt");
        }
        synchronized (LOCK) {
            requireOwnedAudio(entry);
            Path folder = entry.audio.getParent();
            Entry current = read(folder);
            if (!entry.uploadId.equals(current.uploadId)
                    || (current.receiptId != null && !receiptId.equals(current.receiptId))) {
                throw new IOException("Upload receipt does not match the queued recording");
            }
            if (current.receiptId == null) {
                Path marker = folder.resolve("receipt.partial");
                writeMetadata(marker, receiptId, entry.uploadId.toString());
                Files.move(marker, folder.resolve("receipt"), StandardCopyOption.ATOMIC_MOVE);
            }
            syncDirectory(directory);
            syncDirectory(folder);
            deleteAcknowledgedAudio(entry.audio);
            syncDirectory(folder);
        }
    }

    void requireOwnedAudio(Entry entry) {
        if (entry.audio == null || entry.failureStatus != null
                || !directory.equals(entry.audio.getParent().getParent())) {
            throw new IllegalArgumentException("Entry belongs to a different queue or is already sent");
        }
    }

    void requirePending(Entry entry) throws IOException {
        synchronized (LOCK) {
            requireOwnedAudio(entry);
            recoverMarkers(entry.audio.getParent());
            Entry current = read(entry.audio.getParent());
            if (!entry.uploadId.equals(current.uploadId) || current.receiptId != null
                    || current.failureStatus != null) {
                throw new IOException("Recording is no longer pending upload");
            }
        }
    }

    private void recoverMarkers(Path folder) throws IOException {
        if (!Files.isDirectory(folder, LinkOption.NOFOLLOW_LINKS)
                || !Files.isRegularFile(folder.resolve("entry"), LinkOption.NOFOLLOW_LINKS)) {
            return;
        }
        String uploadId;
        try (DataInputStream metadata = new DataInputStream(Files.newInputStream(folder.resolve("entry")))) {
            uploadId = metadata.readUTF();
        }
        for (String name : new String[]{"receipt", "failed"}) {
            Path partial = folder.resolve(name + ".partial");
            Path target = folder.resolve(name);
            if (Files.exists(target) || !Files.isRegularFile(partial, LinkOption.NOFOLLOW_LINKS)) {
                continue;
            }
            boolean valid;
            try (DataInputStream marker = new DataInputStream(Files.newInputStream(partial))) {
                String value = marker.readUTF();
                valid = "receipt".equals(name) ? value.matches("local-sync-[0-9a-f]{32}")
                        : isPermanentFailure(Integer.parseInt(value));
                valid = valid && uploadId.equals(marker.readUTF()) && marker.read() == -1;
            } catch (IOException | IllegalArgumentException error) {
                continue;
            }
            if (valid) {
                syncFile(partial);
                publish(partial, target);
                syncDirectory(folder);
            }
        }
    }

    private static Entry read(Path folder) throws IOException {
        if (!Files.isDirectory(folder, LinkOption.NOFOLLOW_LINKS)
                || !Files.isRegularFile(folder.resolve("entry"), LinkOption.NOFOLLOW_LINKS)) {
            throw new IOException("Invalid queued recording directory");
        }
        try (DataInputStream metadata = new DataInputStream(Files.newInputStream(folder.resolve("entry")))) {
            UUID uploadId = UUID.fromString(metadata.readUTF());
            String filename = metadata.readUTF();
            if (!validFilename(filename)) {
                throw new IOException("Invalid queued recording filename");
            }
            long expectedBytes = -1;
            String storageKey = digest(uploadId.toString());
            if (metadata.available() > 0) {
                expectedBytes = Long.parseLong(metadata.readUTF());
                storageKey = metadata.readUTF();
                if (expectedBytes <= 0 || expectedBytes > MAX_AUDIO_BYTES
                        || !storageKey.matches("[a-f0-9]{64}") || metadata.read() != -1) {
                    throw new IOException("Invalid queued recording metadata");
                }
            }
            Path receipt = folder.resolve("receipt");
            if (Files.exists(receipt)) {
                if (!Files.isRegularFile(receipt, LinkOption.NOFOLLOW_LINKS)) {
                    throw new IOException("Invalid saved upload receipt");
                }
                try (DataInputStream sent = new DataInputStream(Files.newInputStream(receipt))) {
                    String receiptId = sent.readUTF();
                    if (!receiptId.matches("local-sync-[0-9a-f]{32}")
                            || (sent.available() > 0 && !uploadId.toString().equals(sent.readUTF()))
                            || sent.read() != -1) {
                        throw new IOException("Invalid saved upload receipt");
                    }
                    return new Entry(uploadId, filename, null, receiptId, null, storageKey);
                }
            }
            Path audio = folder.resolve("audio");
            if (!Files.isRegularFile(audio, LinkOption.NOFOLLOW_LINKS) || Files.size(audio) <= 0
                    || Files.size(audio) > MAX_AUDIO_BYTES
                    || (expectedBytes != -1 && Files.size(audio) != expectedBytes)) {
                throw new IOException("Queued audio is missing or incomplete");
            }
            Integer failureStatus = null;
            Path failure = folder.resolve("failed");
            if (Files.exists(failure)) {
                if (!Files.isRegularFile(failure, LinkOption.NOFOLLOW_LINKS)) {
                    throw new IOException("Invalid saved upload failure");
                }
                try (DataInputStream failed = new DataInputStream(Files.newInputStream(failure))) {
                    failureStatus = Integer.parseInt(failed.readUTF());
                    if (!isPermanentFailure(failureStatus)
                            || !uploadId.toString().equals(failed.readUTF()) || failed.read() != -1) {
                        throw new IOException("Invalid saved upload failure");
                    }
                }
            }
            return new Entry(uploadId, filename, audio, null, failureStatus, storageKey);
        } catch (IllegalArgumentException error) {
            throw new IOException("Invalid queued recording metadata", error);
        }
    }

    private static void writeMetadata(Path path, String... values) throws IOException {
        try (FileOutputStream output = new FileOutputStream(path.toFile());
                DataOutputStream data = new DataOutputStream(output)) {
            for (String value : values) {
                data.writeUTF(value);
            }
            output.getFD().sync();
        }
    }

    private void quarantine(Path path) throws IOException {
        Path target = directory.resolve("quarantine-" + UUID.randomUUID());
        Files.move(path, target, StandardCopyOption.ATOMIC_MOVE);
        syncDirectory(directory);
        if (Files.isDirectory(target, LinkOption.NOFOLLOW_LINKS)) {
            writeMetadata(target.resolve("diagnostic"), "Queued recording is incomplete or invalid; audio retained");
            syncDirectory(target);
        }
    }

    private Path publishStaging(Path staging, Entry entry) throws IOException {
        Path target = directory.resolve(entry.storageKey);
        if (Files.exists(target)) {
            throw new IOException("Conflicting staged recording");
        }
        if (entry.audio != null) {
            syncFile(entry.audio);
        }
        syncFile(staging.resolve("entry"));
        syncDirectory(staging);
        publish(staging, target);
        syncDirectory(directory);
        return target;
    }

    private static boolean validFilename(String filename) {
        return filename != null && !filename.trim().isEmpty() && filename.length() <= 255
                && filename.indexOf('/') < 0 && filename.indexOf('\\') < 0
                && filename.chars().noneMatch(Character::isISOControl)
                && EXTENSIONS.contains(filename.substring(filename.lastIndexOf('.') + 1).toLowerCase(Locale.ROOT))
                && filename.lastIndexOf('.') > 0;
    }

    private void createDirectoriesDurably(Path path) throws IOException {
        if (!Files.exists(path)) {
            createDirectoriesDurably(path.getParent());
            Files.createDirectory(path);
        }
        syncDirectory(path);
        if (path.getParent() != null) {
            syncDirectory(path.getParent());
        }
    }

    private static void syncFile(Path path) throws IOException {
        try (FileOutputStream file = new FileOutputStream(path.toFile(), true)) {
            file.getFD().sync();
        } catch (IOException error) {
            throw new DurabilityException(error);
        }
    }

    private static void publish(Path source, Path target) throws IOException {
        try {
            Files.move(source, target, StandardCopyOption.ATOMIC_MOVE);
        } catch (IOException error) {
            throw new DurabilityException(error);
        }
    }

    private static void deleteAcknowledgedAudio(Path audio) throws IOException {
        try {
            Files.deleteIfExists(audio);
        } catch (IOException error) {
            throw new DurabilityException(error);
        }
    }

    private void syncDirectory(Path path) throws IOException {
        try {
            directorySync.sync(path);
        } catch (IOException error) {
            throw new DurabilityException(error);
        }
    }

    private static void syncAndroidDirectory(Path path) throws IOException {
        try {
            FileDescriptor descriptor = Os.open(path.toString(), OsConstants.O_RDONLY, 0);
            try {
                if (!OsConstants.S_ISDIR(Os.fstat(descriptor).st_mode)) {
                    throw new IOException("Queue path is not a directory");
                }
                Os.fsync(descriptor);
            } finally {
                Os.close(descriptor);
            }
        } catch (ErrnoException error) {
            throw new IOException("Queue directory could not be synchronized", error);
        }
    }

    private static final class DurabilityException extends IOException {
        DurabilityException(IOException cause) {
            super("Queue durability could not be confirmed", cause);
        }
    }

    private static void requireIdentifier(String value) {
        if (value == null || value.trim().isEmpty() || value.length() > 1024) {
            throw new IllegalArgumentException("Missing or invalid identifier");
        }
    }

    private static String digest(String value) {
        try {
            byte[] bytes = MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8));
            StringBuilder hex = new StringBuilder(bytes.length * 2);
            for (byte part : bytes) {
                hex.append(Character.forDigit((part >> 4) & 15, 16));
                hex.append(Character.forDigit(part & 15, 16));
            }
            return hex.toString();
        } catch (NoSuchAlgorithmException error) {
            throw new AssertionError(error);
        }
    }
}
