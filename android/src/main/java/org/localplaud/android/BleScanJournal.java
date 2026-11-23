package org.localplaud.android;

import java.util.Objects;
import java.util.function.BooleanSupplier;
import java.util.function.Supplier;

final class BleScanJournal {
    enum Result { SAVED, CONFLICT, STORAGE_FAILURE }

    private BleScanJournal() {}

    static synchronized Result replace(String expectedRevision, Supplier<String> currentRevision,
            BooleanSupplier commit) {
        if (!Objects.equals(expectedRevision, currentRevision.get())) {
            return Result.CONFLICT;
        }
        return commit.getAsBoolean() ? Result.SAVED : Result.STORAGE_FAILURE;
    }
}
