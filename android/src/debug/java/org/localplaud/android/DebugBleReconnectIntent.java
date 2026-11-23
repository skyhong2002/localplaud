package org.localplaud.android;

import java.util.function.BiFunction;
import java.util.function.BooleanSupplier;
import java.util.function.Predicate;

final class DebugBleReconnectIntent {
    interface Store {
        String readAddress();
        boolean writeAddress(String address);
    }

    enum Result { STARTED, ALREADY_ACTIVE, NO_INTENT, NOT_AUTHORIZED, STORAGE_FAILED }

    private final Store store;
    private final BooleanSupplier systemPermissions;
    private final BiFunction<String, BooleanSupplier, DebugBleProbe> createProbe;
    private DebugBleProbe active;
    private String activeAddress;
    private SessionPermissions activePermissions;
    private boolean intentReadFailed;
    private boolean intentWriteFailed;

    DebugBleReconnectIntent(Store store, BooleanSupplier systemPermissions,
            BiFunction<String, BooleanSupplier, DebugBleProbe> createProbe) {
        this.store = store;
        this.systemPermissions = systemPermissions;
        this.createProbe = createProbe;
        loadPermittedPendingAddress();
    }

    String pendingAddress() {
        if (storageFailed()) {
            return null;
        }
        try {
            return store.readAddress();
        } catch (RuntimeException error) {
            intentReadFailed = true;
            return null;
        }
    }

    boolean intentReadFailed() {
        return intentReadFailed;
    }

    String retryPendingAddress() {
        if (!intentReadFailed || active != null) {
            return null;
        }
        intentReadFailed = false;
        return loadPermittedPendingAddress();
    }

    private String loadPermittedPendingAddress() {
        String address = pendingAddress();
        if (address != null && !systemPermissions.getAsBoolean()) {
            clearSavedIntent();
            return null;
        }
        return address;
    }

    Result resume(Predicate<String> authorize) {
        invalidateIfPermissionLost();
        String address = pendingAddress();
        if (intentReadFailed) {
            return Result.STORAGE_FAILED;
        }
        return address == null ? Result.NO_INTENT : activate(address, authorize, false, false);
    }

    Result start(String address, Predicate<String> authorize) {
        invalidateIfPermissionLost();
        return intentReadFailed ? Result.STORAGE_FAILED : activate(address, authorize, true, true);
    }

    Result startOrResume(String address, Predicate<String> authorize) {
        invalidateIfPermissionLost();
        String savedAddress = pendingAddress();
        return intentReadFailed ? Result.STORAGE_FAILED : activate(address, authorize, !address.equals(savedAddress), true);
    }

    private Result activate(String address, Predicate<String> authorize, boolean remember, boolean explicit) {
        if (!systemPermissions.getAsBoolean()) {
            return Result.NOT_AUTHORIZED;
        }
        if (!authorize.test(address)) {
            return Result.NOT_AUTHORIZED;
        }
        if (address.equals(activeAddress) && !active.requiresExplicitRestart()) {
            String revision = active.manualRescanRevision();
            if (explicit && revision != null) {
                activePermissions.rescanAuthorized = () -> authorize.test(address);
                active.requestManualRescan(revision);
                return Result.STARTED;
            }
            return Result.ALREADY_ACTIVE;
        }
        if (remember && !writeIntent(address)) {
            return Result.STORAGE_FAILED;
        }
        pause();
        var permissions = new SessionPermissions();
        activePermissions = permissions;
        activeAddress = address;
        active = createProbe.apply(address,
                () -> permissions.granted() && authorize.test(address) && permissions.rescanAuthorized.getAsBoolean());
        active.start(true);
        return Result.STARTED;
    }

    void adapterChanged(boolean enabled) {
        invalidateIfPermissionLost();
        if (active != null) {
            active.adapterChanged(enabled);
        }
    }

    String manualRescanRevision() {
        return active == null ? null : active.manualRescanRevision();
    }

    void requestManualRescan(String expectedRevision) {
        if (active != null) {
            active.requestManualRescan(expectedRevision);
        }
    }

    boolean hasActiveSession() {
        return active != null;
    }

    boolean invalidateIfPermissionLost() {
        if (activePermissions == null || activePermissions.granted()) {
            return false;
        }
        pause();
        return true;
    }

    void pause() {
        DebugBleProbe previous = active;
        active = null;
        activeAddress = null;
        if (activePermissions != null) {
            activePermissions.lost = true;
            activePermissions = null;
        }
        if (previous != null) {
            previous.stop();
        }
    }

    boolean forget() {
        pause();
        return clearSavedIntent();
    }

    private boolean clearSavedIntent() {
        return writeIntent(null);
    }

    private boolean writeIntent(String address) {
        try {
            intentWriteFailed = !store.writeAddress(address);
        } catch (RuntimeException error) {
            intentWriteFailed = true;
        }
        if (!intentWriteFailed) {
            intentReadFailed = false;
        }
        return !intentWriteFailed;
    }

    boolean storageFailed() {
        return intentReadFailed || intentWriteFailed;
    }

    private final class SessionPermissions {
        private boolean lost;
        private BooleanSupplier rescanAuthorized = () -> true;

        boolean granted() {
            if (!lost && !systemPermissions.getAsBoolean()) {
                lost = true;
                clearSavedIntent();
            }
            return !lost;
        }
    }
}
