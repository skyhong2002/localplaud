package org.localplaud.android;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;
import static org.localplaud.android.BleConnectionManager.ScanOutcome.FAILED;
import static org.localplaud.android.BleConnectionManager.ScanOutcome.TEMPORARY_FAILURE;
import static org.localplaud.android.BleConnectionManager.ScanOutcome.THROTTLED;
import static org.localplaud.android.BleConnectionManager.ScanOutcome.TIMED_OUT;
import static org.localplaud.android.BleConnectionManager.State.BLUETOOTH_OFF;
import static org.localplaud.android.BleConnectionManager.State.CONNECTED;
import static org.localplaud.android.BleConnectionManager.State.CONNECTING;
import static org.localplaud.android.BleConnectionManager.State.RESCAN_COOLDOWN;
import static org.localplaud.android.BleConnectionManager.State.RESCAN_READY;
import static org.localplaud.android.BleConnectionManager.State.RETRYING;
import static org.localplaud.android.BleConnectionManager.State.SCAN_BUDGET_EMPTY;
import static org.localplaud.android.BleConnectionManager.State.SCAN_STORAGE_FAILED;
import static org.localplaud.android.BleConnectionManager.State.STOPPED;
import static org.localplaud.android.BleConnectionManager.State.UNAUTHORIZED;
import static org.localplaud.android.BleConnectionManager.State.WAITING_FOR_DEVICE;
import static org.localplaud.android.BleConnectionManager.State.WAITING_FOR_LINK;
import static org.localplaud.android.BleConnectionManager.State.WAITING_TO_RESCAN;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Consumer;

import org.junit.Test;

public final class BleConnectionManagerTest {
    @Test
    public void terminalScanFailureRetainsDebitUntilExplicitRetryAfterCooldown() {
        Fixture fixture = new Fixture();
        fixture.grant();
        fixture.manager.start();
        fixture.manager.requestDiscovery();
        var failed = fixture.scans.get(0);
        failed.finished.accept(FAILED);
        assertEquals(RESCAN_COOLDOWN, fixture.manager.state());
        assertEquals(1, fixture.journal.saved.scansStarted());
        assertEquals(1, failed.cancellations);
        fixture.manager.requestDiscovery();
        fixture.manager.requestManualRescan();
        fixture.manager.adapterChanged(false);
        fixture.manager.adapterChanged(true);
        assertEquals(1, fixture.scans.size());
        fixture.scheduler.fire(30_000);
        assertEquals(RESCAN_READY, fixture.manager.state());
        assertTrue(fixture.scheduler.pending.isEmpty());
        failed.seen.accept("target");
        failed.finished.accept(TIMED_OUT);
        fixture.manager.requestDiscovery();
        assertEquals(1, fixture.scans.size());

        fixture.manager.requestManualRescan(fixture.manager.manualRescanRevision());
        assertEquals(2, fixture.scans.size());
        assertEquals(List.of(1, 2), fixture.debitsObservedByScanner);
        fixture.scans.get(1).finished.accept(TIMED_OUT);
        fixture.scheduler.fire(4_000);
        fixture.scans.get(2).finished.accept(TIMED_OUT);
        fixture.scheduler.fire(30_000);
        assertEquals(RESCAN_READY, fixture.manager.state());
        assertEquals(3, fixture.scans.size());
        assertTrue(fixture.attempts.isEmpty());
        assertTrue(fixture.scheduler.pending.isEmpty());
    }

    @Test
    public void initialConnectionWithNoCallbackClosesAndExhaustsBoundedRediscovery() {
        Fixture fixture = new Fixture();
        fixture.startAuthorized();
        var silent = fixture.attempts.get(0);
        fixture.scheduler.fire(15_000);
        assertEquals(RETRYING, fixture.manager.state());
        assertEquals(List.of(silent), fixture.closedAttempts);
        assertFalse(silent.gattServiceDiscoveryStarted());
        assertFalse(silent.authorizedSessionOpened());
        silent.disconnected();
        fixture.manager.deviceSeen("target");
        fixture.scheduler.fire(1_000);
        assertEquals(1, fixture.scans.size());
        assertEquals(1, fixture.journal.saved.scansStarted());
        fixture.scans.get(0).finished.accept(TIMED_OUT);
        fixture.scheduler.fire(2_000);
        fixture.scans.get(1).finished.accept(TIMED_OUT);
        fixture.scheduler.fire(4_000);
        fixture.scans.get(2).finished.accept(TIMED_OUT);
        fixture.scheduler.fire(30_000);
        fixture.manager.deviceSeen("target");
        assertEquals(RESCAN_READY, fixture.manager.state());
        assertEquals(1, fixture.attempts.size());
        assertEquals(List.of(1, 2, 3), fixture.debitsObservedByScanner);
        assertTrue(fixture.scheduler.pending.isEmpty());
    }

    @Test
    public void initialSilentGattDiscoveryTimeoutExhaustsBoundedRediscovery() {
        Fixture fixture = new Fixture();
        fixture.startAuthorized();
        var silent = fixture.attempts.get(0);
        assertTrue(silent.gattServiceDiscoveryStarted());
        fixture.scheduler.fire(15_000);
        assertEquals(RETRYING, fixture.manager.state());
        assertEquals(List.of(silent), fixture.closedAttempts);
        fixture.scheduler.fire(1_000);
        assertEquals(1, fixture.attempts.size());
        assertEquals(1, fixture.scans.size());
        silent.gattServicesDiscovered();
        assertFalse(silent.authorizedSessionOpened());
        silent.disconnected();
        fixture.manager.deviceSeen("target");
        fixture.scans.get(0).finished.accept(TIMED_OUT);
        fixture.scheduler.fire(2_000);
        fixture.scans.get(1).finished.accept(TIMED_OUT);
        fixture.scheduler.fire(4_000);
        fixture.scans.get(2).finished.accept(TIMED_OUT);
        fixture.scheduler.fire(30_000);
        assertEquals(RESCAN_READY, fixture.manager.state());
        assertEquals(1, fixture.attempts.size());
        assertEquals(List.of(1, 2, 3), fixture.debitsObservedByScanner);
        assertTrue(fixture.scheduler.pending.isEmpty());
    }

    @Test
    public void initialSilentDiscoveryTimeoutRecoveryOnlyUsesOriginalAuthorization() {
        for (boolean reissue : List.of(false, true)) {
            for (int boundary : List.of(0, 1, 2)) {
                Fixture fixture = new Fixture();
                fixture.startAuthorized();
                var silent = fixture.attempts.get(0);
                assertTrue(silent.gattServiceDiscoveryStarted());
                if (boundary > 0) {
                    fixture.scheduler.fire(15_000);
                }
                if (boundary > 1) {
                    fixture.scheduler.fire(1_000);
                }
                if (reissue) {
                    fixture.grantIdentity = new Object();
                } else {
                    fixture.allowed = false;
                }
                if (boundary == 0) {
                    fixture.scheduler.fire(15_000);
                } else if (boundary == 1) {
                    fixture.scheduler.fire(1_000);
                } else {
                    fixture.scans.get(0).seen.accept("target");
                }
                assertEquals(UNAUTHORIZED, fixture.manager.state());
                assertEquals(boundary == 2 ? 1 : 0, fixture.scans.size());
                assertEquals(1, fixture.attempts.size());
                assertEquals(List.of(silent), fixture.closedAttempts);
                assertTrue(fixture.scheduler.pending.isEmpty());
            }
        }
    }

    @Test
    public void unauthorizedRadioCycleNeverImpliesWaitingOrStartsScanOrTransport() {
        for (boolean previouslyConnected : List.of(false, true)) {
            Fixture fixture = new Fixture();
            if (previouslyConnected) {
                fixture.startAuthorized();
                fixture.attempts.get(0).authorizedSessionOpened();
                fixture.allowed = false;
                fixture.manager.authorizationChanged();
            } else {
                fixture.manager.start();
                fixture.manager.deviceSeen("target");
            }
            assertEquals(UNAUTHORIZED, fixture.manager.state());
            for (int cycle = 0; cycle < 2; cycle++) {
                fixture.manager.adapterChanged(false);
                fixture.manager.adapterChanged(true);
                fixture.manager.adapterChanged(true);
                assertEquals(UNAUTHORIZED, fixture.manager.state());
                fixture.manager.requestDiscovery();
                fixture.manager.deviceSeen("target");
                assertEquals(UNAUTHORIZED, fixture.manager.state());
                assertTrue(fixture.scans.isEmpty());
                assertEquals(previouslyConnected ? 1 : 0, fixture.attempts.size());
                assertTrue(fixture.scheduler.pending.isEmpty());
            }
        }
    }

    @Test
    public void turningBluetoothOffCancelsRetryAndReopeningRequiresFreshMatchingSighting() {
        Fixture fixture = new Fixture();
        fixture.startAuthorized();
        var first = fixture.attempts.get(0);
        first.authorizedSessionOpened();
        first.disconnected();
        Runnable staleRetry = fixture.scheduler.onlyCallback(1_000);

        fixture.manager.adapterChanged(false);
        fixture.manager.adapterChanged(false);
        fixture.manager.deviceSeen("target");
        staleRetry.run();
        assertEquals(BLUETOOTH_OFF, fixture.manager.state());
        assertTrue(fixture.scheduler.pending.isEmpty());
        assertEquals(1, fixture.attempts.size());

        fixture.manager.adapterChanged(true);
        fixture.manager.adapterChanged(true);
        fixture.manager.deviceSeen("target");
        assertEquals(1, fixture.scans.size());
        var scan = fixture.scans.get(0);
        scan.seen.accept("other");
        assertEquals(WAITING_FOR_DEVICE, fixture.manager.state());
        assertEquals(1, fixture.attempts.size());
        scan.seen.accept("target");
        scan.seen.accept("target");
        assertEquals(1, scan.cancellations);
        assertEquals(CONNECTING, fixture.manager.state());
        assertEquals(2, fixture.attempts.size());
        fixture.attempts.get(1).authorizedSessionOpened();
        assertEquals(CONNECTED, fixture.manager.state());
        assertTrue(fixture.scheduler.pending.isEmpty());
    }

    @Test
    public void sharedLinkRadioCycleCancelsOldRetryAndOnlyOwnerReconnects() {
        var links = new BleLinkCoordinator<String>();
        Fixture owner = new Fixture(new JournalMemory(), links);
        Fixture waiting = new Fixture(new JournalMemory(), links);
        owner.startAuthorized();
        waiting.startAuthorized();
        var original = owner.attempts.get(0);
        original.authorizedSessionOpened();
        original.disconnected();
        Runnable staleRetry = owner.scheduler.onlyCallback(1_000);

        owner.manager.adapterChanged(false);
        assertTrue(waiting.scheduler.pending.isEmpty());
        waiting.manager.adapterChanged(false);
        assertEquals(BLUETOOTH_OFF, owner.manager.state());
        assertTrue(owner.scheduler.pending.isEmpty());
        assertTrue(waiting.scheduler.pending.isEmpty());

        waiting.manager.adapterChanged(true);
        waiting.manager.deviceSeen("target");
        assertEquals(WAITING_FOR_LINK, waiting.manager.state());
        owner.manager.adapterChanged(true);
        owner.manager.adapterChanged(true);
        staleRetry.run();
        original.disconnected();
        assertTrue(waiting.scans.isEmpty());
        assertTrue(waiting.attempts.isEmpty());
        assertTrue(waiting.scheduler.pending.isEmpty());
        assertEquals(1, owner.scans.size());
        assertEquals(1, owner.attempts.size());

        var fresh = owner.scans.get(0);
        fresh.seen.accept("target");
        fresh.seen.accept("target");
        assertEquals(2, owner.attempts.size());
        owner.attempts.get(1).authorizedSessionOpened();
        assertEquals(CONNECTED, owner.manager.state());
        assertTrue(owner.scheduler.pending.isEmpty());
        assertTrue(waiting.attempts.isEmpty());
    }

    @Test
    public void radioRecoveryRechecksOwnerAuthorizationBeforeDiscoveryAndBeforeConnecting() {
        for (boolean revokeBeforeRadioOn : List.of(true, false)) {
            var links = new BleLinkCoordinator<String>();
            Fixture owner = new Fixture(new JournalMemory(), links);
            Fixture waiting = new Fixture(new JournalMemory(), links);
            owner.startAuthorized();
            waiting.startAuthorized();
            owner.attempts.get(0).disconnected();
            Runnable staleRetry = owner.scheduler.onlyCallback(1_000);
            owner.manager.adapterChanged(false);
            waiting.manager.adapterChanged(false);
            owner.allowed = !revokeBeforeRadioOn;
            owner.manager.adapterChanged(true);
            waiting.manager.adapterChanged(true);
            assertEquals(revokeBeforeRadioOn ? 0 : 1, owner.scans.size());
            if (!revokeBeforeRadioOn) {
                owner.allowed = false;
                owner.scans.get(0).seen.accept("target");
            }
            staleRetry.run();
            assertEquals(UNAUTHORIZED, owner.manager.state());
            assertEquals(1, owner.attempts.size());
            assertTrue(owner.scheduler.pending.isEmpty());
            assertTrue(waiting.scans.isEmpty());
            assertTrue(waiting.attempts.isEmpty());

            waiting.manager.deviceSeen("target");
            assertEquals(1, waiting.attempts.size());
        }
    }

    @Test
    public void radioCycleDuringBackoffOnlyRecoversTheOriginalGrant() {
        for (var backoff : RadioBackoff.values()) {
            for (boolean reissueGrant : List.of(false, true)) {
                Fixture fixture = new Fixture();
                fixture.startAuthorized();
                var original = fixture.attempts.get(0);
                original.authorizedSessionOpened();
                if (backoff == RadioBackoff.DIRECT_RETRY) {
                    original.disconnected();
                } else {
                    original.disconnectedAndRediscover();
                }
                if (backoff == RadioBackoff.SCAN_TIMEOUT) {
                    fixture.scheduler.fire(1_000);
                    fixture.scans.get(0).finished.accept(TIMED_OUT);
                }
                long delay = backoff == RadioBackoff.SCAN_TIMEOUT ? 2_000 : 1_000;
                Runnable staleTimer = fixture.scheduler.onlyCallback(delay);
                int scansBeforeToggle = fixture.scans.size();

                fixture.manager.adapterChanged(false);
                fixture.scheduler.advance(delay);
                staleTimer.run();
                assertEquals(BLUETOOTH_OFF, fixture.manager.state());
                assertEquals(scansBeforeToggle, fixture.scans.size());
                assertTrue(fixture.scheduler.pending.isEmpty());

                if (reissueGrant) {
                    fixture.grantIdentity = new Object();
                }
                fixture.manager.adapterChanged(true);
                fixture.manager.adapterChanged(true);
                staleTimer.run();
                assertEquals(1, fixture.attempts.size());
                assertEquals(backoff.name(), scansBeforeToggle + (reissueGrant ? 0 : 1), fixture.scans.size());
                if (reissueGrant) {
                    assertEquals(UNAUTHORIZED, fixture.manager.state());
                } else {
                    var fresh = fixture.scans.get(scansBeforeToggle);
                    fresh.seen.accept("target");
                    fresh.seen.accept("target");
                    assertEquals(2, fixture.attempts.size());
                    fixture.attempts.get(1).authorizedSessionOpened();
                    assertEquals(CONNECTED, fixture.manager.state());
                }
                assertEquals(List.of(original), fixture.closedAttempts);
                assertTrue(fixture.scheduler.pending.isEmpty());
            }
        }
    }

    private enum RadioBackoff { DIRECT_RETRY, RECOVERY_SCAN, SCAN_TIMEOUT }

    @Test
    public void radioCycleCancelsAnActiveRecoveryScanAndChecksTheOriginalGrant() {
        for (var grantChange : RadioGrantChange.values()) {
            Fixture fixture = new Fixture();
            fixture.startAuthorized();
            var original = fixture.attempts.get(0);
            original.authorizedSessionOpened();
            original.disconnectedAndRediscover();
            fixture.scheduler.fire(1_000);
            assertEquals(WAITING_FOR_DEVICE, fixture.manager.state());
            var interrupted = fixture.scans.get(0);
            Runnable lateSighting = () -> interrupted.seen.accept("target");
            Runnable lateTimeout = () -> interrupted.finished.accept(TIMED_OUT);

            fixture.manager.adapterChanged(false);
            assertEquals(1, interrupted.cancellations);
            lateSighting.run();
            lateTimeout.run();
            assertEquals(BLUETOOTH_OFF, fixture.manager.state());
            assertEquals(1, fixture.scans.size());
            assertEquals(1, fixture.attempts.size());
            assertTrue(fixture.scheduler.pending.isEmpty());

            if (grantChange == RadioGrantChange.REVOKED) {
                fixture.allowed = false;
            } else if (grantChange == RadioGrantChange.REISSUED) {
                fixture.grantIdentity = new Object();
            }
            fixture.manager.adapterChanged(true);
            fixture.manager.adapterChanged(true);
            lateSighting.run();
            lateTimeout.run();
            assertEquals(1, interrupted.cancellations);
            assertEquals(1, fixture.attempts.size());
            assertTrue(fixture.scheduler.pending.isEmpty());

            if (grantChange == RadioGrantChange.UNCHANGED) {
                assertEquals(WAITING_FOR_DEVICE, fixture.manager.state());
                assertEquals(2, fixture.scans.size());
                var fresh = fixture.scans.get(1);
                assertEquals(0, fresh.cancellations);
                fresh.seen.accept("target");
                fresh.seen.accept("target");
                assertEquals(2, fixture.attempts.size());
                fixture.attempts.get(1).authorizedSessionOpened();
                assertEquals(CONNECTED, fixture.manager.state());
            } else {
                assertEquals(grantChange.name(), UNAUTHORIZED, fixture.manager.state());
                assertEquals(1, fixture.scans.size());
            }
            assertEquals(List.of(original), fixture.closedAttempts);
            assertTrue(fixture.scheduler.pending.isEmpty());
        }
    }

    private enum RadioGrantChange { UNCHANGED, REVOKED, REISSUED }

    @Test
    public void duplicateRadioOnAfterRecoveryGattTimeoutPreservesBackoffAndScanBudget() {
        for (var grantChange : RadioGrantChange.values()) {
            Fixture fixture = new Fixture();
            fixture.startAuthorized();
            var original = fixture.attempts.get(0);
            original.authorizedSessionOpened();
            fixture.manager.adapterChanged(false);
            fixture.manager.adapterChanged(true);
            fixture.scans.get(0).seen.accept("target");
            var replacement = fixture.attempts.get(1);
            assertTrue(replacement.gattServiceDiscoveryStarted());
            fixture.scheduler.fire(15_000);
            assertEquals(RETRYING, fixture.manager.state());
            assertEquals(List.of(original, replacement), fixture.closedAttempts);
            Runnable retry = fixture.scheduler.onlyCallback(1_000);
            int writesBeforeRetry = fixture.journal.writes;
            assertNull(fixture.journal.saved);

            fixture.scheduler.advance(400);
            fixture.manager.adapterChanged(true);
            assertEquals(retry, fixture.scheduler.onlyCallback(600));
            assertEquals(1, fixture.scans.size());
            assertEquals(writesBeforeRetry, fixture.journal.writes);
            if (grantChange == RadioGrantChange.REVOKED) {
                fixture.allowed = false;
            } else if (grantChange == RadioGrantChange.REISSUED) {
                fixture.grantIdentity = new Object();
            }
            fixture.scheduler.advance(599);
            fixture.manager.adapterChanged(true);
            assertEquals(RETRYING, fixture.manager.state());
            assertEquals(retry, fixture.scheduler.onlyCallback(1));
            assertEquals(1, fixture.scans.size());
            assertEquals(writesBeforeRetry, fixture.journal.writes);

            fixture.scheduler.fire(1);
            assertEquals(2, fixture.attempts.size());
            assertTrue(fixture.scheduler.pending.isEmpty());
            if (grantChange == RadioGrantChange.UNCHANGED) {
                assertEquals(WAITING_FOR_DEVICE, fixture.manager.state());
                assertEquals(2, fixture.scans.size());
                assertEquals(writesBeforeRetry + 1, fixture.journal.writes);
                assertEquals(List.of(1, 1), fixture.debitsObservedByScanner);
            } else {
                assertEquals(UNAUTHORIZED, fixture.manager.state());
                assertEquals(1, fixture.scans.size());
                assertEquals(writesBeforeRetry, fixture.journal.writes);
            }
        }
    }

    @Test
    public void turningBluetoothOffClosesAttemptAndReopeningRechecksAuthorization() {
        Fixture fixture = new Fixture();
        fixture.startAuthorized();
        var first = fixture.attempts.get(0);
        fixture.manager.adapterChanged(false);
        assertEquals(1, fixture.closed);
        assertTrue(fixture.scheduler.pending.isEmpty());
        first.authorizedSessionOpened();
        first.disconnected();
        assertEquals(BLUETOOTH_OFF, fixture.manager.state());

        fixture.manager.adapterChanged(true);
        fixture.manager.adapterChanged(true);
        assertEquals(WAITING_FOR_DEVICE, fixture.manager.state());
        fixture.allowed = false;
        fixture.manager.authorizationChanged();
        assertEquals(1, fixture.scans.get(0).cancellations);
        fixture.scans.get(0).seen.accept("target");
        assertEquals(UNAUTHORIZED, fixture.manager.state());
        assertEquals(1, fixture.attempts.size());
        assertTrue(fixture.scheduler.pending.isEmpty());
    }

    @Test
    public void timedOutAndCancelledScansCannotConnectEvenAfterNextRadioCycle() {
        Fixture fixture = new Fixture();
        fixture.startAuthorized();
        fixture.manager.adapterChanged(false);
        fixture.manager.adapterChanged(true);
        var expired = fixture.scans.get(0);
        expired.finished.accept(TIMED_OUT);
        Runnable cancelledRescan = fixture.scheduler.onlyCallback(2_000);
        expired.seen.accept("target");
        assertEquals(WAITING_TO_RESCAN, fixture.manager.state());
        assertEquals(1, fixture.attempts.size());

        fixture.manager.adapterChanged(false);
        assertTrue(fixture.scheduler.pending.isEmpty());
        fixture.manager.adapterChanged(true);
        cancelledRescan.run();
        assertEquals(2, fixture.scans.size());
        var cancelled = fixture.scans.get(1);
        fixture.manager.adapterChanged(false);
        fixture.manager.adapterChanged(true);
        expired.seen.accept("target");
        cancelled.seen.accept("target");
        cancelled.finished.accept(TIMED_OUT);
        assertEquals(WAITING_FOR_DEVICE, fixture.manager.state());
        assertEquals(1, fixture.attempts.size());
        assertEquals(1, cancelled.cancellations);

        var lastScan = fixture.scans.get(2);
        fixture.manager.stop();
        lastScan.seen.accept("target");
        assertEquals(1, lastScan.cancellations);
        assertEquals(STOPPED, fixture.manager.state());
        assertEquals(1, fixture.attempts.size());
    }

    @Test
    public void staleRecoveryCallbacksCannotInterfereWithNextRadioCyclesScan() {
        for (boolean authorizationStillValid : new boolean[]{true, false}) {
            Fixture fixture = new Fixture();
            fixture.restartRadio();
            var firstScan = fixture.scans.get(0);
            fixture.manager.adapterChanged(false);
            firstScan.seen.accept("target");
            firstScan.finished.accept(TIMED_OUT);
            assertEquals(BLUETOOTH_OFF, fixture.manager.state());
            assertEquals(1, firstScan.cancellations);
            assertTrue(fixture.scheduler.pending.isEmpty());

            fixture.manager.adapterChanged(true);
            var currentScan = fixture.scans.get(1);
            firstScan.seen.accept("target");
            firstScan.finished.accept(FAILED);
            firstScan.finished.accept(TEMPORARY_FAILURE);
            firstScan.finished.accept(TIMED_OUT);
            assertEquals(WAITING_FOR_DEVICE, fixture.manager.state());
            assertEquals(0, currentScan.cancellations);
            assertEquals(1, fixture.attempts.size());
            assertEquals(2, fixture.scans.size());
            assertTrue(fixture.scheduler.pending.isEmpty());

            fixture.allowed = authorizationStillValid;
            currentScan.seen.accept("target");
            currentScan.seen.accept("target");
            assertEquals(1, currentScan.cancellations);
            assertEquals(authorizationStillValid ? CONNECTING : UNAUTHORIZED, fixture.manager.state());
            assertEquals(authorizationStillValid ? 2 : 1, fixture.attempts.size());
            if (authorizationStillValid) {
                fixture.scheduler.onlyCallback(15_000);
            } else {
                assertTrue(fixture.scheduler.pending.isEmpty());
            }
        }
    }

    @Test
    public void firstTimeoutBacksOffAndLateMatchingDeviceReconnectsOnlyOnce() {
        Fixture fixture = new Fixture();
        fixture.restartRadio();
        var initialScan = fixture.scans.get(0);
        initialScan.finished.accept(TIMED_OUT);
        initialScan.finished.accept(TIMED_OUT);
        fixture.manager.adapterChanged(true);
        assertEquals(1, fixture.scans.size());
        fixture.scheduler.fire(2_000);
        assertEquals(2, fixture.scans.size());
        var rescan = fixture.scans.get(1);
        initialScan.seen.accept("target");
        rescan.seen.accept("other");
        assertEquals(1, fixture.attempts.size());
        rescan.seen.accept("target");
        rescan.seen.accept("target");
        rescan.finished.accept(TIMED_OUT);
        fixture.attempts.get(1).authorizedSessionOpened();
        assertEquals(CONNECTED, fixture.manager.state());
        assertEquals(2, fixture.attempts.size());
        assertTrue(fixture.scheduler.pending.isEmpty());
    }

    @Test
    public void exhaustedScansRequireAnExplicitRescanAfterCooldown() {
        Fixture fixture = new Fixture();
        fixture.exhaustScans();
        fixture.scans.get(2).seen.accept("target");
        fixture.manager.deviceSeen("target");
        assertEquals(RESCAN_COOLDOWN, fixture.manager.state());
        fixture.scheduler.advance(29_999);
        fixture.manager.requestManualRescan();
        assertEquals(3, fixture.scans.size());
        fixture.scheduler.fire(1);
        assertEquals(RESCAN_READY, fixture.manager.state());
        assertEquals(3, fixture.scans.size());
        fixture.manager.requestManualRescan();
        fixture.manager.requestManualRescan();
        assertEquals(4, fixture.scans.size());
        fixture.scans.get(2).seen.accept("target");
        var fresh = fixture.scans.get(3);
        fresh.seen.accept("other");
        assertEquals(1, fixture.attempts.size());
        fresh.seen.accept("target");
        fresh.seen.accept("target");
        assertEquals(2, fixture.attempts.size());
    }

    @Test
    public void recreationKeepsOriginalDeadlineAndRequiresExplicitRescan() {
        Fixture original = new Fixture();
        original.exhaustScans();
        original.scheduler.advance(10_000);
        Runnable staleTimer = original.scheduler.onlyCallback(20_000);

        Fixture recreated = original.recreate(5_000, true, true);
        staleTimer.run();
        assertEquals(STOPPED, original.manager.state());
        assertEquals(RESCAN_COOLDOWN, recreated.manager.state());
        recreated.manager.requestManualRescan();
        recreated.manager.deviceSeen("target");
        assertTrue(recreated.scans.isEmpty());
        assertTrue(recreated.attempts.isEmpty());

        recreated.scheduler.fire(15_000);
        assertEquals(RESCAN_READY, recreated.manager.state());
        assertTrue(recreated.scans.isEmpty());
        recreated.manager.requestManualRescan();
        recreated.manager.requestManualRescan();
        assertEquals(1, recreated.scans.size());
        recreated.scans.get(0).finished.accept(TIMED_OUT);
        recreated.scheduler.fire(2_000);
        recreated.scans.get(1).finished.accept(TIMED_OUT);
        recreated.scheduler.fire(4_000);
        recreated.scans.get(2).finished.accept(TIMED_OUT);
        assertEquals(RESCAN_COOLDOWN, recreated.manager.state());
        assertEquals(3, recreated.scans.size());
    }

    @Test
    public void recreationAfterDeadlineAndRadioOnStillRequireATapAndFreshAuthorizedMatch() {
        Fixture original = new Fixture();
        original.exhaustScans();
        original.manager.adapterChanged(false);

        Fixture recreated = original.recreate(30_000, false, true);
        assertEquals(BLUETOOTH_OFF, recreated.manager.state());
        recreated.manager.adapterChanged(true);
        assertEquals(RESCAN_READY, recreated.manager.state());
        assertTrue(recreated.scans.isEmpty());
        assertTrue(recreated.scheduler.pending.isEmpty());

        recreated.manager.requestManualRescan();
        recreated.scans.get(0).seen.accept("other");
        assertTrue(recreated.attempts.isEmpty());
        recreated.allowed = false;
        recreated.scans.get(0).seen.accept("target");
        assertEquals(UNAUTHORIZED, recreated.manager.state());
        assertTrue(recreated.attempts.isEmpty());
    }

    @Test
    public void recreationDoesNotCarryForwardRevokedAuthorization() {
        Fixture original = new Fixture();
        original.exhaustScans();

        Fixture recreated = original.recreate(30_000, true, false);
        assertEquals(UNAUTHORIZED, recreated.manager.state());
        recreated.manager.requestManualRescan();
        assertTrue(recreated.scans.isEmpty());
        assertTrue(recreated.scheduler.pending.isEmpty());
    }

    @Test
    public void newProcessRestoresExhaustionWithoutRestoringAuthorization() {
        Fixture original = new Fixture();
        original.exhaustScans();
        var saved = original.journal.saved;
        original.manager.stop();

        Fixture replacement = new Fixture(original.journal);
        replacement.scheduler.advance(saved.availableAfter() - 1);
        var restored = saved.restoreForBoot(17, replacement.scheduler.nowMillis());
        replacement.manager.restoreManualRescan(restored);
        assertEquals(RESCAN_COOLDOWN, replacement.manager.state());
        replacement.manager.requestManualRescan();
        replacement.manager.deviceSeen("target");
        assertTrue(replacement.scans.isEmpty());
        replacement.scheduler.fire(1);
        assertEquals(RESCAN_READY, replacement.manager.state());
        assertTrue(replacement.scans.isEmpty());
        assertTrue(replacement.attempts.isEmpty());

        replacement.manager.requestManualRescan();
        assertEquals(1, replacement.scans.size());
        replacement.scans.get(0).seen.accept("target");
        assertEquals(UNAUTHORIZED, replacement.manager.state());
        assertTrue(replacement.attempts.isEmpty());
    }

    @Test
    public void sameBootExpiredDeadlineWaitsForUserAndOtherBootsDiscardSavedState() {
        Fixture original = new Fixture();
        original.exhaustScans();
        var saved = original.journal.saved;
        assertNull(saved.restoreForBoot(18, 0));
        assertNull(saved.restoreForBoot(-1, 0));
        assertNull(new BleConnectionManager.SavedScanBudget<>("target", 3, 50_001L, 17, "test").restoreForBoot(17, 0));
        original.manager.stop();

        Fixture replacement = new Fixture(original.journal);
        replacement.scheduler.advance(saved.availableAfter() + 5_000);
        replacement.manager.restoreManualRescan(saved.restoreForBoot(17, replacement.scheduler.nowMillis()));
        assertEquals(RESCAN_READY, replacement.manager.state());
        assertTrue(replacement.scheduler.pending.isEmpty());
        assertTrue(replacement.scans.isEmpty());
        assertTrue(replacement.attempts.isEmpty());
    }

    @Test
    public void interruptedSecondScanCannotRefundAnAttemptAfterProcessRecovery() {
        Fixture original = new Fixture();
        original.restartRadio();
        original.scans.get(0).finished.accept(TIMED_OUT);
        original.scheduler.fire(2_000);
        assertEquals(List.of(1, 2), original.debitsObservedByScanner);
        var saved = original.journal.saved;
        original.manager.stop();
        assertEquals(saved, original.journal.saved);

        Fixture replacement = new Fixture(original.journal);
        long restoredAt = original.scheduler.nowMillis() + 10_000;
        replacement.scheduler.advance(restoredAt);
        replacement.manager.restoreManualRescan(saved.restoreForBoot(17, restoredAt));
        replacement.manager.requestManualRescan();
        assertEquals(RESCAN_COOLDOWN, replacement.manager.state());
        assertTrue(replacement.scans.isEmpty());
        replacement.scheduler.fire(40_000);
        assertEquals(RESCAN_READY, replacement.manager.state());
        assertTrue(replacement.scans.isEmpty());

        replacement.manager.requestManualRescan();
        replacement.manager.requestManualRescan();
        replacement.scans.get(0).seen.accept("other");
        assertEquals(List.of(3), replacement.debitsObservedByScanner);
        assertTrue(replacement.attempts.isEmpty());
        replacement.scans.get(0).finished.accept(TIMED_OUT);
        assertEquals(RESCAN_COOLDOWN, replacement.manager.state());
        assertEquals(3, replacement.journal.saved.scansStarted());
        assertEquals(replacement.scheduler.nowMillis() + 30_000, replacement.journal.saved.availableAfter());
    }

    @Test
    public void noScanStartsWhenItsDebitCannotBeSaved() {
        Fixture fixture = new Fixture();
        fixture.journal.writable = false;
        fixture.restartRadio();
        assertEquals(SCAN_STORAGE_FAILED, fixture.manager.state());
        assertTrue(fixture.scans.isEmpty());
        assertTrue(fixture.scheduler.pending.isEmpty());
    }

    @Test
    public void twoControllersWithTheSameLastDebitCanStartOnlyOneScan() {
        for (boolean finishBeforeSecondTap : List.of(false, true)) {
            Fixture original = new Fixture();
            original.restartRadio();
            original.scans.get(0).finished.accept(TIMED_OUT);
            original.scheduler.fire(2_000);
            original.manager.stop();
            var snapshot = original.journal.saved;

            Fixture winner = new Fixture(original.journal);
            Fixture contender = new Fixture(original.journal);
            for (Fixture restored : List.of(winner, contender)) {
                restored.scheduler.advance(snapshot.availableAfter());
                restored.manager.restoreManualRescan(snapshot.restoreForBoot(17, restored.scheduler.nowMillis()));
                assertEquals(RESCAN_READY, restored.manager.state());
                assertTrue(restored.scans.isEmpty());
                assertTrue(restored.attempts.isEmpty());
            }

            winner.manager.requestManualRescan();
            assertEquals(List.of(3), winner.debitsObservedByScanner);
            var winningDebit = original.journal.saved;
            if (finishBeforeSecondTap) {
                winner.scans.get(0).seen.accept("target");
                assertEquals(UNAUTHORIZED, winner.manager.state());
            }
            contender.manager.requestManualRescan();
            assertEquals(finishBeforeSecondTap ? SCAN_BUDGET_EMPTY : RESCAN_COOLDOWN, contender.manager.state());
            assertEquals(finishBeforeSecondTap ? null : winningDebit, original.journal.saved);
            assertTrue(contender.scans.isEmpty());
            assertTrue(contender.attempts.isEmpty());
            assertTrue(winner.attempts.isEmpty());

            if (!finishBeforeSecondTap) {
                assertEquals(3, contender.manager.manualRescanForRecreation().scansStarted());
                assertEquals(winningDebit.availableAfter(), contender.manager.manualRescanForRecreation().availableAfter());
                winner.scans.get(0).seen.accept("target");
                contender.scheduler.fire(winningDebit.availableAfter() - contender.scheduler.nowMillis());
                assertEquals(SCAN_BUDGET_EMPTY, contender.manager.state());
            }
            contender.manager.deviceSeen("target");
            assertTrue(contender.scans.isEmpty());
            assertTrue(contender.attempts.isEmpty());
        }
    }

    @Test
    public void refreshedCooldownUnlocksWithoutScanningAndNextTapUsesLatestBudget() {
        Fixture original = new Fixture();
        original.restartRadio();
        original.scans.get(0).finished.accept(TIMED_OUT);
        original.scheduler.fire(2_000);
        original.manager.stop();
        var snapshot = original.journal.saved;

        Fixture contender = new Fixture(original.journal);
        Fixture winner = new Fixture(original.journal);
        for (Fixture restored : List.of(contender, winner)) {
            restored.scheduler.advance(snapshot.availableAfter());
            restored.manager.restoreManualRescan(snapshot.restoreForBoot(17, restored.scheduler.nowMillis()));
        }
        winner.manager.requestManualRescan();
        winner.scans.get(0).finished.accept(TIMED_OUT);

        contender.manager.requestManualRescan();
        assertEquals(RESCAN_COOLDOWN, contender.manager.state());
        contender.scheduler.advance(29_999);
        contender.manager.requestManualRescan();
        assertTrue(contender.scans.isEmpty());
        contender.scheduler.fire(1);
        assertEquals(RESCAN_READY, contender.manager.state());
        assertTrue(contender.scans.isEmpty());
        assertTrue(contender.attempts.isEmpty());

        contender.manager.requestManualRescan();
        assertEquals(List.of(1), contender.debitsObservedByScanner);
        contender.scans.get(0).seen.accept("target");
        assertEquals(UNAUTHORIZED, contender.manager.state());
        assertTrue(contender.attempts.isEmpty());
    }

    @Test
    public void revocationInRefreshedControllerDoesNotClearWinningDebit() {
        Fixture original = new Fixture();
        original.exhaustScans();
        var snapshot = original.manager.manualRescanForRecreation();
        original.manager.stop();
        Fixture winner = new Fixture(original.journal);
        Fixture contender = new Fixture(original.journal);
        for (Fixture restored : List.of(winner, contender)) {
            restored.allowed = true;
            restored.scheduler.advance(snapshot.availableAfter());
            restored.manager.restoreManualRescan(snapshot);
        }
        winner.manager.requestManualRescan();
        var winningDebit = original.journal.saved;
        contender.manager.requestManualRescan();
        assertEquals(RESCAN_COOLDOWN, contender.manager.state());

        contender.allowed = false;
        contender.manager.authorizationChanged();
        assertEquals(UNAUTHORIZED, contender.manager.state());
        assertEquals(winningDebit, original.journal.saved);
        assertTrue(contender.scheduler.pending.isEmpty());
        assertTrue(contender.scans.isEmpty());
        assertTrue(contender.attempts.isEmpty());
    }

    @Test
    public void radioTogglePreservesCooldownAndManualRescanGetsOneFullBudget() {
        Fixture fixture = new Fixture();
        fixture.exhaustScans();
        Runnable abandoned = fixture.scheduler.onlyCallback(30_000);
        fixture.scheduler.advance(10_000);
        fixture.manager.adapterChanged(false);
        fixture.manager.adapterChanged(true);
        abandoned.run();
        assertEquals(RESCAN_COOLDOWN, fixture.manager.state());
        fixture.scheduler.fire(20_000);
        assertEquals(3, fixture.scans.size());
        fixture.manager.requestManualRescan();
        fixture.scans.get(3).finished.accept(TIMED_OUT);
        fixture.scheduler.fire(2_000);
        fixture.scans.get(4).finished.accept(TEMPORARY_FAILURE);
        fixture.scheduler.fire(4_000);
        fixture.scans.get(5).finished.accept(THROTTLED);
        assertEquals(RESCAN_COOLDOWN, fixture.manager.state());
        fixture.scheduler.fire(30_000);
        assertEquals(RESCAN_READY, fixture.manager.state());
        assertEquals(6, fixture.scans.size());
    }

    @Test
    public void manualRescanChecksAuthorizationAtTapAndOnFreshMatch() {
        for (boolean revokeBeforeTap : List.of(true, false)) {
            Fixture fixture = new Fixture();
            fixture.exhaustScans();
            fixture.scheduler.fire(30_000);
            fixture.allowed = !revokeBeforeTap;
            fixture.manager.requestManualRescan();
            assertEquals(revokeBeforeTap ? 3 : 4, fixture.scans.size());
            if (!revokeBeforeTap) {
                fixture.allowed = false;
                fixture.scans.get(3).seen.accept("target");
            }
            assertEquals(UNAUTHORIZED, fixture.manager.state());
            assertEquals(1, fixture.attempts.size());
        }
    }

    @Test
    public void leavingDuringCooldownCancelsReadinessAndIgnoresLateTimer() {
        Fixture fixture = new Fixture();
        fixture.exhaustScans();
        Runnable abandoned = fixture.scheduler.onlyCallback(30_000);
        fixture.manager.stop();
        fixture.scheduler.advance(30_000);
        abandoned.run();
        fixture.manager.requestManualRescan();
        assertEquals(STOPPED, fixture.manager.state());
        assertEquals(3, fixture.scans.size());
        assertTrue(fixture.scheduler.pending.isEmpty());
    }

    @Test
    public void revocationCancelsPendingRescanAndItsLateTimerCannotRun() {
        Fixture fixture = new Fixture();
        fixture.restartRadio();
        fixture.scans.get(0).finished.accept(TIMED_OUT);
        Runnable staleTimer = fixture.scheduler.onlyCallback(2_000);
        fixture.allowed = false;
        fixture.manager.authorizationChanged();
        staleTimer.run();
        assertEquals(UNAUTHORIZED, fixture.manager.state());
        assertEquals(1, fixture.scans.size());
        assertEquals(1, fixture.attempts.size());
        assertTrue(fixture.scheduler.pending.isEmpty());
    }

    @Test
    public void reissuedGrantCancelsPendingReconnectWithoutOverwritingNewRetry() {
        assertReissuedGrantCancelsBackoff(false);
    }

    @Test
    public void reissuedGrantCancelsPendingRescanWithoutOverwritingNewRetry() {
        assertReissuedGrantCancelsBackoff(true);
    }

    private void assertReissuedGrantCancelsBackoff(boolean scanTimedOut) {
        Fixture fixture = new Fixture();
        fixture.startAuthorized();
        var original = fixture.attempts.get(0);
        original.authorizedSessionOpened();
        original.disconnectedAndRediscover();
        if (scanTimedOut) {
            fixture.scheduler.fire(1_000);
            fixture.scans.get(0).finished.accept(TIMED_OUT);
        }
        assertEquals(scanTimedOut ? WAITING_TO_RESCAN : RETRYING, fixture.manager.state());
        Runnable staleTimer = fixture.scheduler.onlyCallback(scanTimedOut ? 2_000 : 1_000);

        fixture.grantIdentity = new Object();
        fixture.manager.authorizationChanged();
        assertEquals(UNAUTHORIZED, fixture.manager.state());
        assertTrue(fixture.scheduler.pending.isEmpty());
        assertEquals(scanTimedOut ? 1 : 0, fixture.scans.size());

        fixture.manager.stop();
        fixture.startAuthorized();
        var reauthorized = fixture.attempts.get(1);
        reauthorized.authorizedSessionOpened();
        reauthorized.disconnectedAndRediscover();
        Runnable freshTimer = fixture.scheduler.onlyCallback(1_000);
        staleTimer.run();
        assertEquals(RETRYING, fixture.manager.state());
        assertEquals(freshTimer, fixture.scheduler.onlyCallback(1_000));
        assertEquals(scanTimedOut ? 1 : 0, fixture.scans.size());

        fixture.scheduler.fire(1_000);
        assertEquals(WAITING_FOR_DEVICE, fixture.manager.state());
        assertEquals(scanTimedOut ? 2 : 1, fixture.scans.size());
        assertEquals(List.of(original, reauthorized), fixture.closedAttempts);
    }

    @Test
    public void transientFailuresAndTimeoutsShareTheSameThreeScanBudget() {
        Fixture fixture = new Fixture();
        fixture.restartRadio();
        fixture.scans.get(0).finished.accept(TEMPORARY_FAILURE);
        fixture.scheduler.fire(2_000);
        fixture.scans.get(1).finished.accept(TIMED_OUT);
        fixture.scheduler.fire(4_000);
        fixture.scans.get(2).finished.accept(THROTTLED);
        assertEquals(3, fixture.scans.size());
        assertEquals(1, fixture.attempts.size());
        assertEquals(RESCAN_COOLDOWN, fixture.manager.state());
        fixture.scheduler.onlyCallback(30_000);
    }

    @Test
    public void throttledScanUsesCooldownThenRequiresAMatchingFreshResultAndAuthorization() {
        Fixture fixture = new Fixture();
        fixture.restartRadio();
        var throttled = fixture.scans.get(0);
        throttled.finished.accept(THROTTLED);
        throttled.seen.accept("target");
        assertEquals(1, fixture.attempts.size());
        fixture.scheduler.fire(30_000);
        var fresh = fixture.scans.get(1);
        fresh.seen.accept("other");
        assertEquals(1, fixture.attempts.size());
        fresh.seen.accept("target");
        fresh.seen.accept("target");
        assertEquals(2, fixture.attempts.size());
    }

    @Test
    public void retryRechecksAuthorizationEvenWithoutRevocationNotification() {
        Fixture fixture = new Fixture();
        fixture.restartRadio();
        fixture.scans.get(0).finished.accept(TEMPORARY_FAILURE);
        fixture.allowed = false;
        fixture.scheduler.fire(2_000);
        assertEquals(UNAUTHORIZED, fixture.manager.state());
        assertEquals(1, fixture.scans.size());
        assertEquals(1, fixture.attempts.size());
        assertTrue(fixture.scheduler.pending.isEmpty());
    }

    @Test
    public void temporaryFailureFollowedByTerminalFailureDoesNotScheduleAnotherScan() {
        Fixture fixture = new Fixture();
        fixture.restartRadio();
        fixture.scans.get(0).finished.accept(TEMPORARY_FAILURE);
        fixture.scheduler.fire(2_000);
        fixture.scans.get(1).finished.accept(FAILED);
        fixture.scans.get(1).seen.accept("target");
        assertEquals(2, fixture.scans.size());
        assertEquals(1, fixture.attempts.size());
        assertEquals(RESCAN_COOLDOWN, fixture.manager.state());
        fixture.scheduler.fire(30_000);
        assertEquals(RESCAN_READY, fixture.manager.state());
        assertEquals(2, fixture.scans.size());
        assertTrue(fixture.scheduler.pending.isEmpty());
    }

    @Test
    public void leavingWhileBackoffIsPendingAndScanFailureDoNotRescan() {
        Fixture fixture = new Fixture();
        fixture.restartRadio();
        fixture.scans.get(0).finished.accept(TIMED_OUT);
        Runnable abandoned = fixture.scheduler.onlyCallback(2_000);
        fixture.manager.stop();
        abandoned.run();
        assertEquals(STOPPED, fixture.manager.state());
        assertEquals(1, fixture.scans.size());
        assertTrue(fixture.scheduler.pending.isEmpty());

        Fixture replacement = new Fixture(fixture.journal);
        replacement.scheduler.advance(fixture.journal.saved.availableAfter());
        replacement.manager.restoreManualRescan(fixture.journal.saved.restoreForBoot(17, replacement.scheduler.nowMillis()));
        replacement.manager.requestManualRescan();
        replacement.scans.get(0).finished.accept(FAILED);
        replacement.scans.get(0).seen.accept("target");
        assertEquals(RESCAN_COOLDOWN, replacement.manager.state());
        replacement.scheduler.fire(30_000);
        assertEquals(RESCAN_READY, replacement.manager.state());
        assertEquals(1, replacement.scans.size());
        assertTrue(replacement.attempts.isEmpty());
        assertTrue(replacement.scheduler.pending.isEmpty());
    }

    @Test
    public void reopeningWithoutPriorSightingOrAfterLeavingScreenDoesNotConnect() {
        Fixture fixture = new Fixture();
        fixture.grant();
        fixture.manager.adapterChanged(false);
        fixture.manager.start();
        fixture.manager.deviceSeen("target");
        fixture.manager.adapterChanged(true);
        assertEquals(WAITING_FOR_DEVICE, fixture.manager.state());
        assertTrue(fixture.attempts.isEmpty());

        fixture.manager.deviceSeen("target");
        fixture.attempts.get(0).authorizedSessionOpened();
        fixture.manager.adapterChanged(false);
        assertEquals(1, fixture.closed);
        fixture.manager.stop();
        fixture.manager.adapterChanged(true);
        assertEquals(STOPPED, fixture.manager.state());
        assertEquals(1, fixture.attempts.size());
        assertTrue(fixture.scheduler.pending.isEmpty());
    }

    @Test
    public void onlyAuthorizedTargetConnectsAndDuplicateSightingsAreIgnored() {
        Fixture fixture = new Fixture();
        fixture.manager.deviceSeen("target");
        fixture.manager.start();
        fixture.manager.deviceSeen("other");
        assertEquals(WAITING_FOR_DEVICE, fixture.manager.state());
        fixture.manager.deviceSeen("target");
        fixture.manager.deviceSeen("target");
        assertEquals(UNAUTHORIZED, fixture.manager.state());
        assertTrue(fixture.attempts.isEmpty());

        fixture.grant();
        fixture.manager.deviceSeen("target");
        assertEquals(CONNECTING, fixture.manager.state());
        assertEquals(1, fixture.attempts.size());
        fixture.attempts.get(0).authorizedSessionOpened();
        assertEquals(CONNECTED, fixture.manager.state());
        assertTrue(fixture.scheduler.pending.isEmpty());
    }

    @Test
    public void authorizedSessionReconnectsWhenDeviceReturns() {
        Fixture fixture = new Fixture();
        fixture.startAuthorized();
        fixture.attempts.get(0).authorizedSessionOpened();
        assertEquals(CONNECTED, fixture.manager.state());
        fixture.manager.deviceLost("target");
        assertEquals(WAITING_FOR_DEVICE, fixture.manager.state());
        assertTrue(fixture.scheduler.pending.isEmpty());
        fixture.manager.deviceSeen("target");
        fixture.manager.deviceSeen("target");
        assertEquals(2, fixture.attempts.size());
        fixture.attempts.get(1).authorizedSessionOpened();
        assertEquals(CONNECTED, fixture.manager.state());
    }

    @Test
    public void returnToRangeReconnectsAndIgnoresLateCallbacksFromOldAttempt() {
        Fixture fixture = new Fixture();
        fixture.startAuthorized();
        var first = fixture.attempts.get(0);
        fixture.manager.deviceLost("target");
        assertEquals(WAITING_FOR_DEVICE, fixture.manager.state());
        assertEquals(1, fixture.closed);

        fixture.manager.deviceSeen("target");
        fixture.manager.deviceSeen("target");
        first.authorizedSessionOpened();
        first.disconnected();
        assertEquals(CONNECTING, fixture.manager.state());
        assertEquals(2, fixture.attempts.size());
        fixture.attempts.get(1).authorizedSessionOpened();
        assertEquals(CONNECTED, fixture.manager.state());
    }

    @Test
    public void disconnectAndTimeoutRetryOnceWithBackoffUntilSessionOpens() {
        Fixture fixture = new Fixture();
        fixture.startAuthorized();
        var first = fixture.attempts.get(0);
        first.authorizedSessionOpened();
        first.disconnected();
        first.disconnected();
        fixture.manager.deviceSeen("target");
        assertEquals(RETRYING, fixture.manager.state());
        fixture.scheduler.fire(1_000);
        assertEquals(2, fixture.attempts.size());
        assertTrue(fixture.attempts.get(1).gattServiceDiscoveryStarted());
        fixture.scheduler.fire(15_000);
        assertEquals(2, fixture.closed);
        fixture.scheduler.fire(2_000);
        assertEquals(2, fixture.attempts.size());
        assertEquals(1, fixture.scans.size());
        fixture.scans.get(0).seen.accept("target");
        var recovered = fixture.attempts.get(2);
        recovered.authorizedSessionOpened();
        assertEquals(CONNECTED, fixture.manager.state());
        recovered.disconnected();
        fixture.scheduler.fire(1_000);
        assertEquals(4, fixture.attempts.size());
    }

    @Test
    public void gattDiscoveryTimeoutClosesOnlyItsAttemptAndIgnoresLateSuccess() {
        Fixture fixture = new Fixture();
        fixture.startAuthorized();
        var expiredGatt = fixture.attempts.get(0);
        expiredGatt.gattServiceDiscoveryStarted();
        expiredGatt.authorizedSessionOpened();
        assertEquals(CONNECTING, fixture.manager.state());
        Runnable expiredWatchdog = fixture.scheduler.onlyCallback(15_000);
        fixture.scheduler.fire(15_000);
        assertEquals(List.of(expiredGatt), fixture.closedAttempts);
        assertEquals(RETRYING, fixture.manager.state());

        fixture.scheduler.fire(1_000);
        assertEquals(1, fixture.attempts.size());
        fixture.scans.get(0).seen.accept("target");
        var currentGatt = fixture.attempts.get(1);
        currentGatt.gattServiceDiscoveryStarted();
        expiredGatt.gattServicesDiscovered();
        expiredGatt.authorizedSessionOpened();
        expiredWatchdog.run();
        currentGatt.authorizedSessionOpened();
        assertEquals(CONNECTING, fixture.manager.state());
        fixture.scheduler.onlyCallback(15_000);

        currentGatt.gattServicesDiscovered();
        assertEquals(CONNECTING, fixture.manager.state());
        currentGatt.authorizedSessionOpened();
        expiredGatt.gattServicesDiscovered();
        expiredGatt.disconnected();
        expiredWatchdog.run();
        assertEquals(CONNECTED, fixture.manager.state());
        assertEquals(List.of(expiredGatt), fixture.closedAttempts);
        assertTrue(fixture.scheduler.pending.isEmpty());
    }

    @Test
    public void expiredGattCannotRepeatDiscoveryBeforeWatchdogRuns() {
        Fixture fixture = new Fixture();
        fixture.startAuthorized();
        var gatt = fixture.attempts.get(0);
        assertTrue(gatt.gattServiceDiscoveryStarted());
        Runnable watchdog = fixture.scheduler.onlyCallback(15_000);
        fixture.scheduler.advance(15_001);
        assertEquals(CONNECTING, fixture.manager.state());

        assertFalse(gatt.canRepeatGattDiscovery());
        watchdog.run();
        assertFalse(gatt.canRepeatGattDiscovery());
        assertEquals(List.of(gatt), fixture.closedAttempts);
        assertEquals(RETRYING, fixture.manager.state());
        assertTrue(fixture.scans.isEmpty());

        fixture.scheduler.fire(1_000);
        assertEquals(1, fixture.scans.size());
        assertEquals(1, fixture.attempts.size());
    }

    @Test
    public void discoveredGattServicesCannotRestoreRevokedAuthorization() {
        Fixture fixture = new Fixture();
        fixture.startAuthorized();
        var gatt = fixture.attempts.get(0);
        gatt.gattServiceDiscoveryStarted();
        fixture.allowed = false;
        gatt.gattServicesDiscovered();
        gatt.authorizedSessionOpened();
        assertEquals(UNAUTHORIZED, fixture.manager.state());
        assertEquals(List.of(gatt), fixture.closedAttempts);
        assertTrue(fixture.scheduler.pending.isEmpty());
    }

    @Test
    public void revocationClosesSessionAndPreventsQueuedAndLateWork() {
        Fixture fixture = new Fixture();
        fixture.startAuthorized();
        fixture.attempts.get(0).disconnected();
        Runnable staleRetry = fixture.scheduler.onlyCallback(1_000);
        fixture.allowed = false;
        fixture.manager.authorizationChanged();
        staleRetry.run();
        fixture.manager.deviceLost("target");
        fixture.manager.deviceSeen("target");
        assertEquals(UNAUTHORIZED, fixture.manager.state());
        assertEquals(1, fixture.attempts.size());

        fixture.grant();
        var next = fixture.attempts.get(1);
        fixture.allowed = false;
        next.authorizedSessionOpened();
        assertEquals(UNAUTHORIZED, fixture.manager.state());
        assertEquals(2, fixture.closed);
        assertTrue(fixture.scheduler.pending.isEmpty());

        fixture.grant();
        fixture.attempts.get(2).authorizedSessionOpened();
        fixture.allowed = false;
        fixture.manager.authorizationChanged();
        assertEquals(UNAUTHORIZED, fixture.manager.state());
        assertEquals(3, fixture.closed);
    }

    @Test
    public void stopDisarmsTimersAndIgnoresCallbacksAcrossRestarts() {
        Fixture fixture = new Fixture();
        fixture.startAuthorized();
        Runnable staleTimeout = fixture.scheduler.onlyCallback(15_000);
        fixture.manager.stop();
        assertEquals(STOPPED, fixture.manager.state());
        assertEquals(1, fixture.closed);
        assertTrue(fixture.scheduler.pending.isEmpty());
        fixture.manager.start();
        fixture.manager.deviceSeen("target");
        staleTimeout.run();
        assertEquals(CONNECTING, fixture.manager.state());
        assertEquals(2, fixture.attempts.size());
    }

    @Test
    public void sharedLinkHandoffCancelsOldRetryAndTriggersExactlyOneNewAttempt() {
        var links = new BleLinkCoordinator<String>();
        Fixture outgoing = new Fixture(new JournalMemory(), links);
        Fixture incoming = new Fixture(new JournalMemory(), links);
        outgoing.startAuthorized();
        incoming.startAuthorized();
        incoming.manager.deviceSeen("target");
        assertEquals(WAITING_FOR_LINK, incoming.manager.state());
        assertTrue(incoming.attempts.isEmpty());
        assertTrue(incoming.scheduler.pending.isEmpty());

        var outgoingAttempt = outgoing.attempts.get(0);
        outgoingAttempt.authorizedSessionOpened();
        outgoingAttempt.disconnected();
        Runnable staleRetry = outgoing.scheduler.onlyCallback(1_000);
        assertTrue(incoming.scheduler.pending.isEmpty());
        outgoing.manager.stop();
        assertEquals(1, outgoing.closed);
        assertTrue(outgoing.scheduler.pending.isEmpty());

        Runnable handoff = incoming.scheduler.onlyCallback(0);
        incoming.scheduler.fire(0);
        outgoingAttempt.disconnected();
        staleRetry.run();
        handoff.run();
        assertEquals(1, outgoing.attempts.size());
        assertEquals(1, incoming.attempts.size());
        incoming.attempts.get(0).authorizedSessionOpened();
        assertEquals(CONNECTED, incoming.manager.state());
        assertTrue(incoming.scheduler.pending.isEmpty());
    }

    @Test
    public void pendingLinkHandoffCannotUseSightingFromReplacedGrant() {
        var links = new BleLinkCoordinator<String>();
        Fixture outgoing = new Fixture(new JournalMemory(), links);
        Fixture incoming = new Fixture(new JournalMemory(), links);
        outgoing.startAuthorized();
        incoming.grant();
        incoming.manager.start();
        incoming.manager.requestDiscovery();
        incoming.scans.get(0).seen.accept("target");
        assertEquals(WAITING_FOR_LINK, incoming.manager.state());

        outgoing.manager.stop();
        incoming.grantIdentity = new Object();
        incoming.scheduler.fire(0);
        assertEquals(UNAUTHORIZED, incoming.manager.state());
        assertTrue(incoming.attempts.isEmpty());
        assertEquals(1, incoming.scans.size());
        assertTrue(incoming.scheduler.pending.isEmpty());
    }

    @Test
    public void promotedControllerStillNeedsIndependentAuthorizationAndALiveScreen() {
        for (boolean stopBeforePromotion : List.of(true, false)) {
            var links = new BleLinkCoordinator<String>();
            Fixture outgoing = new Fixture(new JournalMemory(), links);
            Fixture incoming = new Fixture(new JournalMemory(), links);
            outgoing.startAuthorized();
            incoming.startAuthorized();
            outgoing.manager.stop();
            assertEquals(1, outgoing.closed);
            Runnable promoted = incoming.scheduler.onlyCallback(0);

            if (stopBeforePromotion) {
                incoming.manager.stop();
                promoted.run();
                assertEquals(STOPPED, incoming.manager.state());
            } else {
                incoming.allowed = false;
                incoming.scheduler.fire(0);
                assertEquals(UNAUTHORIZED, incoming.manager.state());
            }
            assertTrue(incoming.attempts.isEmpty());
            assertTrue(incoming.scheduler.pending.isEmpty());
        }
    }

    @Test
    public void synchronousRevocationClosesReturnedTransportBeforeHandoff() {
        var links = new BleLinkCoordinator<String>();
        var allowed = new AtomicBoolean(true);
        var closed = new AtomicBoolean(false);
        Fixture incoming = new Fixture(new JournalMemory(), links);
        var authorized = new BleConnectionManager.AuthorizedConnection(allowed, events -> {
            allowed.set(false);
            events.authorizedSessionOpened();
            assertFalse(closed.get());
            incoming.startAuthorized();
            assertEquals(WAITING_FOR_LINK, incoming.manager.state());
            assertTrue(incoming.scheduler.pending.isEmpty());
            return () -> closed.set(true);
        });
        var outgoing = new BleConnectionManager<>("target", device -> allowed.get() ? authorized : null,
                new FakeScheduler(), ignored -> {}, (seen, finished) -> () -> {}, links);

        outgoing.start();
        outgoing.deviceSeen("target");
        assertTrue(closed.get());
        assertEquals(UNAUTHORIZED, outgoing.state());
        incoming.scheduler.fire(0);
        assertEquals(1, incoming.attempts.size());
    }

    @Test
    public void immediateTransportFailureClosesHandleAndSchedulesRetry() {
        FakeScheduler scheduler = new FakeScheduler();
        List<Boolean> closed = new ArrayList<>();
        var authorized = new BleConnectionManager.AuthorizedConnection(closed, events -> {
            events.disconnected();
            return () -> closed.add(true);
        });
        var manager = new BleConnectionManager<>("target", device -> authorized, scheduler);
        manager.start();
        manager.deviceSeen("target");
        assertEquals(RETRYING, manager.state());
        assertEquals(1, closed.size());
        scheduler.fire(1_000);
        assertEquals(2, closed.size());
        scheduler.onlyCallback(2_000);
    }

    private static final class Fixture {
        final FakeScheduler scheduler = new FakeScheduler();
        final List<BleConnectionManager.Events> attempts = new ArrayList<>();
        final List<BleConnectionManager.Events> closedAttempts = new ArrayList<>();
        final List<FakeScan> scans = new ArrayList<>();
        final List<Integer> debitsObservedByScanner = new ArrayList<>();
        final JournalMemory journal;
        boolean allowed;
        Object grantIdentity = new Object();
        int closed;
        final BleConnectionManager<String> manager;

        Fixture() {
            this(new JournalMemory());
        }

        Fixture(JournalMemory journal) {
            this(journal, new BleLinkCoordinator<>());
        }

        Fixture(JournalMemory journal, BleLinkCoordinator<String> links) {
            this.journal = journal;
            BleConnectionManager.AuthorizedConnector connector = events -> {
                attempts.add(events);
                return () -> {
                    closed++;
                    closedAttempts.add(events);
                };
            };
            manager = new BleConnectionManager<>("target",
                    device -> allowed ? new BleConnectionManager.AuthorizedConnection(grantIdentity, connector) : null,
                    scheduler, ignored -> {}, (seen, finished) -> {
                debitsObservedByScanner.add(journal.saved.scansStarted());
                FakeScan scan = new FakeScan(seen, finished);
                scans.add(scan);
                return () -> scan.cancellations++;
            }, journal::replace, () -> journal.saved == null ? null
                    : journal.saved.restoreForBoot(17, scheduler.nowMillis()), links);
        }

        void grant() {
            allowed = true;
            manager.authorizationChanged();
        }

        void startAuthorized() {
            grant();
            manager.start();
            manager.deviceSeen("target");
        }

        void restartRadio() {
            startAuthorized();
            manager.adapterChanged(false);
            manager.adapterChanged(true);
        }

        void exhaustScans() {
            restartRadio();
            scans.get(0).finished.accept(TIMED_OUT);
            scheduler.fire(2_000);
            scans.get(1).finished.accept(TIMED_OUT);
            scheduler.fire(4_000);
            scans.get(2).finished.accept(TIMED_OUT);
        }

        Fixture recreate(long elapsed, boolean adapterEnabled, boolean authorized) {
            var snapshot = manager.manualRescanForRecreation();
            manager.stop();
            assertTrue(scheduler.pending.isEmpty());
            Fixture replacement = new Fixture(journal);
            replacement.scheduler.advance(scheduler.nowMillis() + elapsed);
            replacement.allowed = authorized;
            replacement.manager.adapterChanged(adapterEnabled);
            replacement.manager.restoreManualRescan(snapshot);
            return replacement;
        }
    }

    private static final class JournalMemory {
        BleConnectionManager.SavedScanBudget<String> saved;
        boolean writable = true;
        int writes;

        BleScanJournal.Result replace(String expected, String next, String target, int count, long deadline) {
            return BleScanJournal.replace(expected, () -> saved == null ? null : saved.revision(), () -> {
                if (!writable) {
                    return false;
                }
                saved = count == 0 ? null : new BleConnectionManager.SavedScanBudget<>(target, count, deadline,
                        17, next);
                writes++;
                return true;
            });
        }
    }

    private static final class FakeScan {
        final Consumer<String> seen;
        final Consumer<BleConnectionManager.ScanOutcome> finished;
        int cancellations;

        FakeScan(Consumer<String> seen, Consumer<BleConnectionManager.ScanOutcome> finished) {
            this.seen = seen;
            this.finished = finished;
        }
    }

    private static final class FakeScheduler implements BleConnectionManager.Scheduler {
        final Map<Runnable, Long> pending = new LinkedHashMap<>();
        private long now;

        @Override
        public long nowMillis() {
            return now;
        }

        void advance(long elapsed) {
            now += elapsed;
        }

        @Override
        public void postDelayed(Runnable callback, long delayMillis) {
            pending.put(callback, now + delayMillis);
        }

        @Override
        public void remove(Runnable callback) {
            pending.remove(callback);
        }

        Runnable onlyCallback(long delay) {
            assertEquals(1, pending.size());
            var entry = pending.entrySet().iterator().next();
            assertEquals(delay, entry.getValue() - now);
            return entry.getKey();
        }

        void fire(long delay) {
            Runnable callback = onlyCallback(delay);
            pending.remove(callback);
            advance(delay);
            callback.run();
        }
    }
}
