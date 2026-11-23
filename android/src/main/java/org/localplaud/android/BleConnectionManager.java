package org.localplaud.android;

import java.util.Objects;
import java.util.UUID;
import java.util.function.Consumer;
import java.util.function.Supplier;

final class BleConnectionManager<Device> {
    private static final long CONNECT_TIMEOUT_MS = 15_000;
    private static final long FIRST_RETRY_MS = 1_000;
    private static final long MAX_RETRY_MS = 30_000;
    private static final long FIRST_RESCAN_DELAY_MS = 2_000;
    private static final int MAX_REDISCOVERY_SCANS = 3;
    private static final long THROTTLED_RESCAN_DELAY_MS = 30_000;
    private static final long MANUAL_RESCAN_COOLDOWN_MS = 30_000;
    static final long SCAN_WINDOW_MS = 20_000;
    private static final long INTERRUPTED_SCAN_WAIT_MS = SCAN_WINDOW_MS + MANUAL_RESCAN_COOLDOWN_MS;

    @FunctionalInterface
    interface Authorization<Device> {
        AuthorizedConnection authorize(Device device);
    }

    record AuthorizedConnection(Object grantIdentity, AuthorizedConnector connector) {
        AuthorizedConnection {
            Objects.requireNonNull(grantIdentity);
            Objects.requireNonNull(connector);
        }
    }

    private record ScanAuthorization(Object grantIdentity) {
        boolean matches(AuthorizedConnection authorized) {
            return grantIdentity == (authorized == null ? null : authorized.grantIdentity());
        }
    }

    @FunctionalInterface
    interface AuthorizedConnector {
        Runnable connect(Events events);
    }

    interface Events {
        boolean gattServiceDiscoveryStarted();
        void gattServicesDiscovered();
        boolean gattServicesChanged();
        boolean canRepeatGattDiscovery();
        boolean authorizedSessionOpened();
        void disconnected();
        void disconnectedAndRediscover();
        void connectionRejected();
        void bluetoothPermissionDenied();
    }

    interface Scheduler {
        long nowMillis();
        void postDelayed(Runnable callback, long delayMillis);
        void remove(Runnable callback);
    }

    @FunctionalInterface
    interface Discovery<Device> {
        Runnable scan(Consumer<Device> seen, Consumer<ScanOutcome> finished);
    }

    @FunctionalInterface
    interface ScanJournal<Device> {
        BleScanJournal.Result replace(String expectedRevision, String nextRevision, Device target, int scansStarted,
                long availableAfter);
    }

    enum ScanOutcome { TIMED_OUT, TEMPORARY_FAILURE, THROTTLED, FAILED }

    record ManualRescan<Device>(Device target, int scansStarted, long availableAfter, boolean previouslyAuthorized,
            String revision) {}

    record SavedScanBudget<Device>(Device target, int scansStarted, long availableAfter, int bootCount, String revision) {
        ManualRescan<Device> restoreForBoot(int currentBootCount, long now) {
            if (bootCount < 0 || bootCount != currentBootCount || target == null || availableAfter <= 0
                    || scansStarted < 1 || scansStarted > MAX_REDISCOVERY_SCANS
                    || availableAfter - now > INTERRUPTED_SCAN_WAIT_MS) {
                return null;
            }
            return new ManualRescan<>(target, scansStarted, availableAfter, false, revision);
        }
    }

    enum State { STOPPED, BLUETOOTH_OFF, WAITING_FOR_DEVICE, WAITING_TO_RESCAN, RESCAN_COOLDOWN, RESCAN_READY,
        SCAN_STORAGE_FAILED, SCAN_BUDGET_CHANGED, SCAN_BUDGET_EMPTY, UNAUTHORIZED, BLUETOOTH_PERMISSION_DENIED, WAITING_FOR_LINK,
        CONNECTING, REVALIDATING_SERVICES, CONNECTED, RETRYING }

    private final Device target;
    private final Authorization<Device> authorization;
    private final Scheduler scheduler;
    private final Consumer<State> stateChanged;
    private final Discovery<Device> discovery;
    private final ScanJournal<Device> journal;
    private final Supplier<ManualRescan<Device>> readSavedBudget;
    private final BleLinkCoordinator<Device> links;
    private State state = State.STOPPED;
    private boolean adapterEnabled = true;
    private boolean present;
    private ScanAuthorization sightingAuthorization;
    private boolean needsRediscovery;
    private ScanAuthorization rediscoveryAuthorization;
    private boolean hadAuthorization;
    private long retryDelay = FIRST_RETRY_MS;
    private Attempt current;
    private ConnectionRetry retry;
    private Runnable handoff;
    private BleLinkCoordinator<Device>.Claim linkClaim;
    private boolean openingTransport;
    private boolean releaseAfterOpen;
    private Search search;
    private Runnable rescan;
    private int scansStarted;
    private long manualRescanAfter;
    private String journalRevision;
    private boolean ownsDebit;
    private boolean refreshOnCooldownEnd;

    BleConnectionManager(Device target, Authorization<Device> authorization, Scheduler scheduler) {
        this(target, authorization, scheduler, ignored -> {}, (seen, finished) -> {
            finished.accept(ScanOutcome.FAILED);
            return () -> {};
        });
    }

    BleConnectionManager(Device target, Authorization<Device> authorization, Scheduler scheduler,
            Consumer<State> stateChanged, Discovery<Device> discovery) {
        this(target, authorization, scheduler, stateChanged, discovery, new BleLinkCoordinator<>());
    }

    BleConnectionManager(Device target, Authorization<Device> authorization, Scheduler scheduler,
            Consumer<State> stateChanged, Discovery<Device> discovery, BleLinkCoordinator<Device> links) {
        this(target, authorization, scheduler, stateChanged, discovery,
                (expected, next, device, started, deadline) -> BleScanJournal.Result.SAVED, () -> null, links);
    }

    BleConnectionManager(Device target, Authorization<Device> authorization, Scheduler scheduler,
            Consumer<State> stateChanged, Discovery<Device> discovery, ScanJournal<Device> journal,
            Supplier<ManualRescan<Device>> readSavedBudget, BleLinkCoordinator<Device> links) {
        this.target = Objects.requireNonNull(target);
        this.authorization = Objects.requireNonNull(authorization);
        this.scheduler = Objects.requireNonNull(scheduler);
        this.stateChanged = Objects.requireNonNull(stateChanged);
        this.discovery = Objects.requireNonNull(discovery);
        this.journal = Objects.requireNonNull(journal);
        this.readSavedBudget = Objects.requireNonNull(readSavedBudget);
        this.links = Objects.requireNonNull(links);
    }

    State state() {
        return state;
    }

    private void setState(State next) {
        if (state != next) {
            state = next;
            stateChanged.accept(next);
        }
    }

    void start() {
        if (state == State.STOPPED) {
            setState(adapterEnabled ? State.WAITING_FOR_DEVICE : State.BLUETOOTH_OFF);
        }
    }

    ManualRescan<Device> manualRescanForRecreation() {
        return manualRescanAfter == 0 ? null
                : new ManualRescan<>(target, scansStarted, manualRescanAfter, hadAuthorization, journalRevision);
    }

    void restoreManualRescan(ManualRescan<Device> snapshot) {
        if (state != State.STOPPED || !target.equals(snapshot.target())) {
            return;
        }
        hadAuthorization = snapshot.previouslyAuthorized();
        applyManualRescan(snapshot);
    }

    private void applyManualRescan(ManualRescan<Device> snapshot) {
        needsRediscovery = true;
        rediscoveryAuthorization = null;
        scansStarted = snapshot.scansStarted();
        manualRescanAfter = snapshot.availableAfter();
        journalRevision = snapshot.revision();
        if (!adapterEnabled) {
            setState(State.BLUETOOTH_OFF);
        } else if (!rediscoveryAuthorizationRevoked()) {
            updateManualRescanAvailability();
        }
    }

    void stop() {
        setState(State.STOPPED);
        present = false;
        clearRediscovery();
        hadAuthorization = false;
        manualRescanAfter = 0;
        journalRevision = null;
        ownsDebit = false;
        refreshOnCooldownEnd = false;
        resetRediscovery();
        resetConnection();
    }

    void deviceSeen(Device device) {
        deviceSeen(device, null);
    }

    private void deviceSeen(Device device, ScanAuthorization scanAuthorization) {
        if (state == State.STOPPED || state == State.BLUETOOTH_PERMISSION_DENIED
                || state == State.SCAN_BUDGET_CHANGED || state == State.SCAN_BUDGET_EMPTY
                || !adapterEnabled || needsRediscovery || !target.equals(device) || present) {
            return;
        }
        present = true;
        sightingAuthorization = scanAuthorization;
        connect();
    }

    void deviceLost(Device device) {
        if (state == State.STOPPED || !target.equals(device) || !present) {
            return;
        }
        present = false;
        resetConnection();
        if (adapterEnabled) {
            setState(State.WAITING_FOR_DEVICE);
        }
    }

    void authorizationChanged() {
        if (state == State.STOPPED || rediscoveryAuthorizationRevoked()) {
            return;
        }
        if (adapterEnabled && present) {
            if (openingTransport) {
                if (current != null) {
                    current.confirmAuthorization();
                }
                return;
            }
            resetLinkWork();
            connect();
        }
    }

    private boolean rediscoveryAuthorizationRevoked() {
        return rediscoveryAuthorizationRevoked(rediscoveryAuthorization);
    }

    private boolean rediscoveryAuthorizationRevoked(ScanAuthorization expected) {
        if (!needsRediscovery || (expected == null && !hadAuthorization)) {
            return false;
        }
        var authorized = authorization.authorize(target);
        if (expected == null ? authorized != null : expected.matches(authorized)) {
            return false;
        }
        clearRediscovery();
        hadAuthorization = false;
        manualRescanAfter = 0;
        refreshOnCooldownEnd = false;
        resetRediscovery();
        resetConnection();
        if ((!ownsDebit || replaceDebit(0, 0)) && adapterEnabled) {
            setState(State.UNAUTHORIZED);
        }
        return true;
    }

    void adapterChanged(boolean enabled) {
        if (adapterEnabled == enabled) {
            return;
        }
        adapterEnabled = enabled;
        if (state == State.STOPPED || state == State.BLUETOOTH_PERMISSION_DENIED) {
            return;
        }
        if (!enabled) {
            boolean retainOwner = linkClaim != null && linkClaim.isOwner();
            if (retainOwner && rediscoveryAuthorization == null) {
                if (current != null) {
                    rediscoveryAuthorization = new ScanAuthorization(current.grantIdentity);
                } else if (retry != null) {
                    rediscoveryAuthorization = retry.scanAuthorization;
                }
            }
            needsRediscovery |= retainOwner;
            present = false;
            setState(State.BLUETOOTH_OFF);
            resetRediscovery();
            if (retainOwner) {
                cancelHandoff();
                resetLinkWork();
            } else {
                resetConnection();
            }
        } else if (!rediscoveryAuthorizationRevoked()) {
            if (refreshOnCooldownEnd) {
                refreshAfterConflict();
            } else if (manualRescanAfter != 0) {
                updateManualRescanAvailability();
            } else if (needsRediscovery && (linkClaim == null ? hadAuthorization : linkClaim.isOwner())) {
                rediscover();
            } else {
                setState(authorization.authorize(target) == null ? State.UNAUTHORIZED : State.WAITING_FOR_DEVICE);
            }
        }
    }

    String manualRescanRevision() {
        return state == State.RESCAN_READY ? journalRevision : null;
    }

    void requestManualRescan(String expectedRevision) {
        if (expectedRevision != null && expectedRevision.equals(manualRescanRevision())) {
            requestManualRescan();
        }
    }

    void requestManualRescan() {
        if (!adapterEnabled || state != State.RESCAN_READY || scheduler.nowMillis() < manualRescanAfter
                || rediscoveryAuthorizationRevoked()) {
            return;
        }
        manualRescanAfter = 0;
        refreshOnCooldownEnd = false;
        if (scansStarted >= MAX_REDISCOVERY_SCANS) {
            scansStarted = 0;
        }
        rediscover();
    }

    private void updateManualRescanAvailability() {
        long remaining = manualRescanAfter - scheduler.nowMillis();
        if (remaining <= 0) {
            refreshOnCooldownEnd = false;
            setState(State.RESCAN_READY);
            return;
        }
        setState(State.RESCAN_COOLDOWN);
        rescan = new Runnable() {
            @Override
            public void run() {
                if (rescan == this) {
                    rescan = null;
                    if (refreshOnCooldownEnd) {
                        refreshAfterConflict();
                    } else if (!rediscoveryAuthorizationRevoked()) {
                        updateManualRescanAvailability();
                    }
                }
            }
        };
        scheduler.postDelayed(rescan, remaining);
    }

    void requestDiscovery() {
        if (adapterEnabled && state == State.WAITING_FOR_DEVICE && !present && !needsRediscovery) {
            hadAuthorization = authorization.authorize(target) != null;
            needsRediscovery = true;
            rediscover();
        }
    }

    private void rediscover() {
        rediscover(rediscoveryAuthorization);
    }

    private void rediscover(ScanAuthorization expected) {
        if (rediscoveryAuthorizationRevoked(expected)) {
            return;
        }
        var authorized = authorization.authorize(target);
        ScanAuthorization scanAuthorization = expected != null ? expected
                : new ScanAuthorization(authorized == null ? null : authorized.grantIdentity());
        int debit = scansStarted + 1;
        if (!replaceDebit(debit, scheduler.nowMillis() + INTERRUPTED_SCAN_WAIT_MS)) {
            return;
        }
        scansStarted = debit;
        rediscoveryAuthorization = scanAuthorization;
        setState(State.WAITING_FOR_DEVICE);
        if (rediscoveryAuthorization != scanAuthorization || rediscoveryAuthorizationRevoked(scanAuthorization)) {
            return;
        }
        Search next = new Search(scanAuthorization);
        search = next;
        Runnable cancel = discovery.scan(next::seen, next::finished);
        if (search == next) {
            next.cancel = cancel;
        } else {
            cancel.run();
        }
    }

    private boolean replaceDebit(int debit, long nextScanAfter) {
        if (debit == 0 && journalRevision == null) {
            return true;
        }
        String nextRevision = debit == 0 ? null : UUID.randomUUID().toString();
        var result = journal.replace(journalRevision, nextRevision, target, debit, nextScanAfter);
        if (result == BleScanJournal.Result.SAVED) {
            journalRevision = nextRevision;
            ownsDebit = debit != 0;
            return true;
        }
        if (result == BleScanJournal.Result.CONFLICT) {
            refreshAfterConflict();
            return false;
        }
        abandonScanBudget();
        resetConnection();
        setState(State.SCAN_STORAGE_FAILED);
        return false;
    }

    private void abandonScanBudget() {
        clearRediscovery();
        manualRescanAfter = 0;
        journalRevision = null;
        ownsDebit = false;
        refreshOnCooldownEnd = false;
        resetRediscovery();
    }

    private void refreshAfterConflict() {
        abandonScanBudget();
        present = false;
        resetConnection();
        var saved = readSavedBudget.get();
        if (saved != null && target.equals(saved.target())) {
            refreshOnCooldownEnd = true;
            applyManualRescan(saved);
        } else {
            setState(saved == null ? State.SCAN_BUDGET_EMPTY : State.SCAN_BUDGET_CHANGED);
        }
    }

    private void scheduleRescan(ScanOutcome result, ScanAuthorization scanAuthorization) {
        if (scansStarted >= MAX_REDISCOVERY_SCANS) {
            waitForManualRescan();
            return;
        }
        Runnable pending = new Runnable() {
            @Override
            public void run() {
                if (rescan == this) {
                    rescan = null;
                    rediscover(scanAuthorization);
                }
            }
        };
        rescan = pending;
        setState(State.WAITING_TO_RESCAN);
        if (rescan != pending) {
            return;
        }
        long delay = FIRST_RESCAN_DELAY_MS << (scansStarted - 1);
        if (result == ScanOutcome.THROTTLED) {
            delay = THROTTLED_RESCAN_DELAY_MS;
        }
        if (replaceDebit(scansStarted, scheduler.nowMillis() + delay)) {
            scheduler.postDelayed(pending, delay);
        }
    }

    private void waitForManualRescan() {
        manualRescanAfter = scheduler.nowMillis() + MANUAL_RESCAN_COOLDOWN_MS;
        if (replaceDebit(scansStarted, manualRescanAfter)) {
            updateManualRescanAvailability();
        }
    }

    private void clearRediscovery() {
        needsRediscovery = false;
        rediscoveryAuthorization = null;
    }

    private void resetRediscovery() {
        cancelSearch();
        if (rescan != null) {
            Runnable previous = rescan;
            rescan = null;
            scheduler.remove(previous);
        }
        if (manualRescanAfter == 0) {
            scansStarted = 0;
        }
    }

    private void cancelSearch() {
        Search previous = search;
        search = null;
        if (previous != null && previous.cancel != null) {
            previous.cancel.run();
        }
    }

    private final class Search {
        private final ScanAuthorization scanAuthorization;
        private Runnable cancel;

        private Search(ScanAuthorization scanAuthorization) {
            this.scanAuthorization = scanAuthorization;
        }

        private void seen(Device device) {
            if (search == this && target.equals(device) && !rediscoveryAuthorizationRevoked()) {
                cancelSearch();
                clearRediscovery();
                if (replaceDebit(0, 0)) {
                    scansStarted = 0;
                    deviceSeen(device, scanAuthorization);
                }
            }
        }

        private void finished(ScanOutcome result) {
            if (search == this && !rediscoveryAuthorizationRevoked()) {
                cancelSearch();
                if (result == ScanOutcome.FAILED) {
                    resetConnection();
                    waitForManualRescan();
                } else {
                    scheduleRescan(result, scanAuthorization);
                }
            }
        }
    }

    private void connect() {
        AuthorizedConnection authorized = authorization.authorize(target);
        if (sightingAuthorization != null && !sightingAuthorization.matches(authorized)) {
            present = false;
            sightingAuthorization = null;
            denyConnection();
            return;
        }
        hadAuthorization = authorized != null;
        if (authorized == null) {
            denyConnection();
            return;
        }
        if (linkClaim == null) {
            linkClaim = links.claim(target, this::scheduleHandoff);
        }
        if (!linkClaim.isOwner() || handoff != null) {
            setState(State.WAITING_FOR_LINK);
            return;
        }
        if (openingTransport) {
            return;
        }
        Attempt attempt = new Attempt(authorized.grantIdentity());
        current = attempt;
        openingTransport = true;
        try {
            setState(State.CONNECTING);
            if (current != attempt || !attempt.confirmAuthorization()) {
                return;
            }
            attempt.armTimeout();
            Runnable close;
            try {
                close = authorized.connector().connect(attempt);
            } catch (SecurityException | IllegalStateException failure) {
                attempt.disconnected();
                return;
            }
            if (current == attempt) {
                attempt.close = close;
            } else {
                close.run();
            }
        } finally {
            openingTransport = false;
            if (releaseAfterOpen) {
                releaseAfterOpen = false;
                releaseLink();
            }
        }
    }

    private void denyConnection() {
        hadAuthorization = false;
        setState(State.UNAUTHORIZED);
        resetConnection();
    }

    private void scheduleHandoff() {
        handoff = new Runnable() {
            @Override
            public void run() {
                if (handoff == this) {
                    handoff = null;
                    if (state == State.WAITING_FOR_LINK && adapterEnabled && present
                            && linkClaim != null && linkClaim.isOwner()) {
                        connect();
                    }
                }
            }
        };
        scheduler.postDelayed(handoff, 0);
    }

    private void resetConnection() {
        resetLinkWork();
        cancelHandoff();
        if (openingTransport) {
            releaseAfterOpen = true;
        } else {
            releaseLink();
        }
    }

    private void cancelHandoff() {
        if (handoff != null) {
            Runnable previous = handoff;
            handoff = null;
            scheduler.remove(previous);
        }
    }

    private void releaseLink() {
        var previous = linkClaim;
        linkClaim = null;
        if (previous != null) {
            previous.release();
        }
    }

    private void resetLinkWork() {
        if (retry != null) {
            Runnable previous = retry;
            retry = null;
            scheduler.remove(previous);
        }
        closeCurrent();
        retryDelay = FIRST_RETRY_MS;
    }

    private void closeCurrent() {
        Attempt previous = current;
        current = null;
        if (previous != null) {
            scheduler.remove(previous.timeout);
            if (previous.close != null) {
                previous.close.run();
            }
        }
    }

    private void scheduleConnectionRetry(ScanAuthorization scanAuthorization, Runnable recover) {
        var pending = new ConnectionRetry(scanAuthorization, recover);
        retry = pending;
        setState(State.RETRYING);
        if (retry == pending) {
            scheduler.postDelayed(pending, retryDelay);
            retryDelay = Math.min(MAX_RETRY_MS, retryDelay * 2);
        }
    }

    private final class ConnectionRetry implements Runnable {
        private final ScanAuthorization scanAuthorization;
        private final Runnable recover;

        private ConnectionRetry(ScanAuthorization scanAuthorization, Runnable recover) {
            this.scanAuthorization = scanAuthorization;
            this.recover = recover;
        }

        @Override
        public void run() {
            if (retry == this) {
                retry = null;
                recover.run();
            }
        }
    }

    private enum DiscoveryPhase { NOT_STARTED, DISCOVERING, COMPLETE }

    private final class Attempt implements Events {
        private final Object grantIdentity;
        private final Runnable timeout = this::onTimeout;
        private DiscoveryPhase discoveryPhase = DiscoveryPhase.NOT_STARTED;
        private long openingDeadline;
        private Runnable close;

        private Attempt(Object grantIdentity) {
            this.grantIdentity = grantIdentity;
        }

        private void onTimeout() {
            if (current != this) {
                return;
            }
            if (isOpening()) {
                disconnectedAndRediscover();
            }
        }

        private void armTimeout() {
            openingDeadline = scheduler.nowMillis() + CONNECT_TIMEOUT_MS;
            scheduler.postDelayed(timeout, CONNECT_TIMEOUT_MS);
        }

        private boolean isOpening() {
            return current == this && (state == State.CONNECTING || state == State.REVALIDATING_SERVICES);
        }

        private boolean recoverIfOpeningExpired() {
            if (scheduler.nowMillis() < openingDeadline) {
                return false;
            }
            disconnectedAndRediscover();
            return true;
        }

        @Override
        public boolean gattServiceDiscoveryStarted() {
            if (!isOpening() || discoveryPhase == DiscoveryPhase.COMPLETE || !confirmAuthorization()
                    || recoverIfOpeningExpired()) {
                return false;
            }
            discoveryPhase = DiscoveryPhase.DISCOVERING;
            return true;
        }

        @Override
        public void gattServicesDiscovered() {
            if (isOpening() && discoveryPhase == DiscoveryPhase.DISCOVERING) {
                discoveryPhase = DiscoveryPhase.COMPLETE;
            }
        }

        @Override
        public boolean gattServicesChanged() {
            if (current != this || state != State.CONNECTED || discoveryPhase != DiscoveryPhase.COMPLETE) {
                return false;
            }
            discoveryPhase = DiscoveryPhase.DISCOVERING;
            setState(State.REVALIDATING_SERVICES);
            if (!isOpening() || !confirmAuthorization()) {
                return false;
            }
            armTimeout();
            return true;
        }

        @Override
        public boolean canRepeatGattDiscovery() {
            return isOpening() && discoveryPhase == DiscoveryPhase.DISCOVERING
                    && confirmAuthorization() && !recoverIfOpeningExpired();
        }

        private boolean confirmAuthorization() {
            AuthorizedConnection authorized = authorization.authorize(target);
            if (authorized == null || authorized.grantIdentity() != grantIdentity) {
                denyConnection();
                return false;
            }
            return true;
        }

        @Override
        public boolean authorizedSessionOpened() {
            if (!isOpening() || discoveryPhase == DiscoveryPhase.DISCOVERING) {
                return false;
            }
            if (!confirmAuthorization() || recoverIfOpeningExpired()) {
                return false;
            }
            scheduler.remove(timeout);
            retryDelay = FIRST_RETRY_MS;
            setState(State.CONNECTED);
            return true;
        }

        @Override
        public void connectionRejected() {
            if (isOpening()) {
                denyConnection();
            }
        }

        @Override
        public void bluetoothPermissionDenied() {
            if (isOpening()) {
                present = false;
                clearRediscovery();
                hadAuthorization = false;
                resetConnection();
                setState(State.BLUETOOTH_PERMISSION_DENIED);
            }
        }

        @Override
        public void disconnectedAndRediscover() {
            if (current != this) {
                return;
            }
            closeCurrent();
            present = false;
            needsRediscovery = true;
            var recoveryAuthorization = new ScanAuthorization(grantIdentity);
            rediscoveryAuthorization = recoveryAuthorization;
            if (!rediscoveryAuthorizationRevoked(recoveryAuthorization)) {
                scheduleConnectionRetry(recoveryAuthorization, () -> rediscover(recoveryAuthorization));
            }
        }

        @Override
        public void disconnected() {
            if (current != this) {
                return;
            }
            closeCurrent();
            if (authorization.authorize(target) == null) {
                denyConnection();
                return;
            }
            scheduleConnectionRetry(new ScanAuthorization(grantIdentity), BleConnectionManager.this::connect);
        }
    }
}
