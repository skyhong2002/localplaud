package org.localplaud.android;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import android.Manifest;
import android.bluetooth.le.ScanCallback;
import android.content.Context;
import android.content.pm.PackageManager;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Consumer;
import java.util.function.Predicate;

import org.junit.Test;

public final class DebugBleProbeTest {
    private static final String ADDRESS = "AA:BB:CC:DD:EE:01";
    private static final DebugBleProbe.Sighting MATCH = new DebugBleProbe.Sighting(ADDRESS, DebugBleProbe.SERVICE);

    @Test
    public void terminalScanFailureOnlyExplicitStartUsesRemainingAllowanceAndFreshAuthorization() {
        for (boolean startOrResume : List.of(false, true)) {
            var store = new MemoryStore();
            var transport = new FakeTransport();
            var clock = new ManualClock();
            var states = new ArrayList<BleConnectionManager.State>();
            var originalGrant = new AtomicBoolean(true);
            var retryGrant = new AtomicBoolean(false);
            var created = new ArrayList<DebugBleProbe>();
            var intent = new DebugBleReconnectIntent(store, () -> true, (address, authorized) -> {
                var probe = new DebugBleProbe(address, transport, clock, states::add, authorized);
                created.add(probe);
                return probe;
            });
            Predicate<String> originalAuthorization = address -> ADDRESS.equals(address) && originalGrant.get();
            Predicate<String> retryAuthorization = address -> ADDRESS.equals(address) && retryGrant.get();
            try {
                assertEquals(DebugBleReconnectIntent.Result.STARTED, intent.start(ADDRESS, originalAuthorization));
                var failed = transport.scans.get(0);
                failed.finished.accept(BleConnectionManager.ScanOutcome.FAILED);
                assertEquals(BleConnectionManager.State.RESCAN_COOLDOWN, states.get(states.size() - 1));
                assertEquals(DebugBleReconnectIntent.Result.ALREADY_ACTIVE, intent.start(ADDRESS, originalAuthorization));
                clock.advance(30_000);
                assertEquals(BleConnectionManager.State.RESCAN_READY, states.get(states.size() - 1));
                assertEquals(DebugBleReconnectIntent.Result.ALREADY_ACTIVE, intent.resume(originalAuthorization));
                clock.advance(60_000);
                assertEquals(1, transport.scans.size());
                assertEquals(DebugBleReconnectIntent.Result.NOT_AUTHORIZED,
                        startOrResume ? intent.startOrResume(ADDRESS, retryAuthorization)
                                : intent.start(ADDRESS, retryAuthorization));
                assertEquals(1, transport.scans.size());

                retryGrant.set(true);
                assertEquals(DebugBleReconnectIntent.Result.STARTED,
                        startOrResume ? intent.startOrResume(ADDRESS, retryAuthorization)
                                : intent.start(ADDRESS, retryAuthorization));
                assertEquals(1, created.size());
                assertEquals(2, transport.scans.size());
                assertEquals(DebugBleReconnectIntent.Result.ALREADY_ACTIVE, intent.start(ADDRESS, retryAuthorization));
                failed.seen.accept(MATCH);
                failed.finished.accept(BleConnectionManager.ScanOutcome.TIMED_OUT);
                assertTrue(transport.links.isEmpty());
                transport.scans.get(1).finished.accept(BleConnectionManager.ScanOutcome.TIMED_OUT);
                clock.advance(3_999);
                assertEquals(2, transport.scans.size());
                clock.advance(1);
                assertEquals(3, transport.scans.size());
                retryGrant.set(false);
                transport.scans.get(2).seen.accept(MATCH);
                assertEquals(BleConnectionManager.State.UNAUTHORIZED, states.get(states.size() - 1));
                assertTrue(originalGrant.get());
                assertTrue(transport.links.isEmpty());
                assertTrue(clock.pending.isEmpty());
            } finally {
                intent.pause();
            }
        }
    }

    @Test
    public void explicitStartAfterTerminalScanFailureReconnectsAndIgnoresFailedScanCallbacks() {
        var transport = new FakeTransport();
        var clock = new ManualClock();
        var states = new ArrayList<BleConnectionManager.State>();
        var intent = new DebugBleReconnectIntent(new MemoryStore(), () -> true, (address, authorized) ->
                new DebugBleProbe(address, transport, clock, states::add, authorized));
        try {
            assertEquals(DebugBleReconnectIntent.Result.STARTED, intent.start(ADDRESS, ADDRESS::equals));
            var failed = transport.scans.get(0);
            failed.finished.accept(BleConnectionManager.ScanOutcome.FAILED);
            clock.advance(30_000);
            assertEquals(DebugBleReconnectIntent.Result.STARTED, intent.start(ADDRESS, ADDRESS::equals));
            transport.scans.get(1).seen.accept(MATCH);
            var recovered = transport.links.get(0);
            recovered.ready();
            failed.seen.accept(MATCH);
            failed.finished.accept(BleConnectionManager.ScanOutcome.FAILED);
            clock.advance(60_000);
            assertEquals(BleConnectionManager.State.CONNECTED, states.get(states.size() - 1));
            assertEquals(2, transport.scans.size());
            assertEquals(1, transport.links.size());
            assertEquals(0, recovered.closed);
            assertTrue(clock.pending.isEmpty());
        } finally {
            intent.pause();
        }
    }

    @Test
    public void reconnectsOnlyOnceAfterFreshAdvertisementAndDiscovery() {
        FakeTransport transport = new FakeTransport();
        ManualClock clock = new ManualClock();
        List<BleConnectionManager.State> states = new ArrayList<>();
        var probe = new DebugBleProbe(ADDRESS, transport, clock, states::add);
        try {
            probe.start(true);
            var initialScan = transport.scans.get(0);
            initialScan.seen.accept(new DebugBleProbe.Sighting(ADDRESS, new UUID(0, 0)));
            initialScan.seen.accept(new DebugBleProbe.Sighting("AA:BB:CC:DD:EE:02", DebugBleProbe.SERVICE));
            assertTrue(transport.links.isEmpty());
            initialScan.seen.accept(MATCH);
            Link first = transport.links.get(0);
            first.events.services(true);
            assertEquals(BleConnectionManager.State.CONNECTING, states.get(states.size() - 1));
            first.ready();
            first.events.lost();
            first.events.lost();
            initialScan.seen.accept(MATCH);
            clock.advance(1_000);
            assertEquals(1, first.closed);
            assertEquals(1, transport.links.size());
            assertEquals(2, transport.scans.size());
            transport.scans.get(1).finished.accept(BleConnectionManager.ScanOutcome.TIMED_OUT);
            clock.advance(2_000);
            assertEquals(1, transport.links.size());

            var freshScan = transport.scans.get(2);
            freshScan.seen.accept(MATCH);
            freshScan.seen.accept(MATCH);
            assertEquals(2, transport.links.size());
            Link second = transport.links.get(1);
            first.ready();
            assertEquals(BleConnectionManager.State.CONNECTING, states.get(states.size() - 1));
            second.ready();
            second.ready();
            assertEquals(0, second.closed);
            assertEquals(2, states.stream().filter(state -> state == BleConnectionManager.State.CONNECTED).count());
            assertTrue(clock.pending.isEmpty());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void recoveryDoesNotSubstituteSameNameAndServiceAtDifferentAddress() {
        var authorized = new Peripheral("Localplaud BLE lab", MATCH);
        var other = new Peripheral(authorized.name(),
                new DebugBleProbe.Sighting("AA:BB:CC:DD:EE:02", authorized.sighting().service()));
        FakeTransport transport = new FakeTransport();
        ManualClock clock = new ManualClock();
        List<BleConnectionManager.State> states = new ArrayList<>();
        var session = new DebugBleReconnectIntent(new MemoryStore(), () -> true, (address, grant) ->
                new DebugBleProbe(address, transport, clock, states::add, grant));
        try {
            assertEquals(DebugBleReconnectIntent.Result.STARTED,
                    session.start(authorized.sighting().address(), authorized.sighting().address()::equals));
            var initialScan = transport.scans.get(0);
            initialScan.seen.accept(authorized.sighting());
            Link initialGatt = transport.links.get(0);
            initialGatt.ready();
            initialGatt.events.lost();
            assertEquals(1, initialGatt.closed);
            clock.advance(1_000);

            var firstRecovery = transport.scans.get(1);
            firstRecovery.seen.accept(other.sighting());
            firstRecovery.seen.accept(other.sighting());
            assertEquals(1, transport.links.size());
            firstRecovery.finished.accept(BleConnectionManager.ScanOutcome.TIMED_OUT);
            clock.advance(2_000);
            var currentRecovery = transport.scans.get(2);
            firstRecovery.seen.accept(authorized.sighting());
            currentRecovery.seen.accept(other.sighting());
            assertEquals(1, transport.links.size());
            assertEquals(BleConnectionManager.State.WAITING_FOR_DEVICE, states.get(states.size() - 1));

            currentRecovery.seen.accept(authorized.sighting());
            currentRecovery.seen.accept(other.sighting());
            currentRecovery.seen.accept(authorized.sighting());
            Link newGatt = transport.links.get(1);
            newGatt.ready();
            assertEquals(2, transport.links.size());
            assertEquals(0, newGatt.closed);
            assertEquals(2, states.stream().filter(state -> state == BleConnectionManager.State.CONNECTED).count());
            assertTrue(clock.pending.isEmpty());
        } finally {
            session.pause();
        }
    }

    private record Peripheral(String name, DebugBleProbe.Sighting sighting) {}

    @Test
    public void throttledRecoveryQueuesOneRetryAndOnlyReconnectsAuthorizedPeripheral() {
        FakeTransport transport = new FakeTransport();
        ManualClock clock = new ManualClock();
        List<BleConnectionManager.State> states = new ArrayList<>();
        var session = new DebugBleReconnectIntent(new MemoryStore(), () -> true, (address, grant) ->
                new DebugBleProbe(address, transport, clock, states::add, grant));
        var throttled = AndroidBleDiscovery.classifyStartFailure(ScanCallback.SCAN_FAILED_SCANNING_TOO_FREQUENTLY);
        var differentPeripheral = new DebugBleProbe.Sighting("AA:BB:CC:DD:EE:02", DebugBleProbe.SERVICE);
        try {
            assertEquals(DebugBleReconnectIntent.Result.STARTED, session.start(ADDRESS, ADDRESS::equals));
            transport.scans.get(0).seen.accept(MATCH);
            Link firstGatt = transport.links.get(0);
            firstGatt.ready();
            firstGatt.events.lost();

            clock.advance(1_000);
            var rejectedScan = transport.scans.get(1);
            rejectedScan.finished.accept(throttled);
            rejectedScan.finished.accept(throttled);
            rejectedScan.finished.accept(BleConnectionManager.ScanOutcome.TIMED_OUT);
            rejectedScan.seen.accept(MATCH);
            assertEquals(BleConnectionManager.State.WAITING_TO_RESCAN, states.get(states.size() - 1));
            assertEquals(List.of(31_000L), new ArrayList<>(clock.pending.values()));
            Runnable oldRetry = clock.pending.keySet().iterator().next();
            clock.advance(29_999);
            assertEquals(2, transport.scans.size());
            assertEquals(1, transport.links.size());

            clock.advance(1);
            oldRetry.run();
            rejectedScan.finished.accept(throttled);
            assertEquals(3, transport.scans.size());
            assertTrue(clock.pending.isEmpty());
            var recovery = transport.scans.get(2);
            recovery.seen.accept(differentPeripheral);
            assertEquals(1, transport.links.size());
            recovery.seen.accept(MATCH);
            recovery.seen.accept(MATCH);
            assertEquals(2, transport.links.size());
            Link newGatt = transport.links.get(1);
            newGatt.ready();
            assertEquals(BleConnectionManager.State.CONNECTED, states.get(states.size() - 1));
            assertEquals(1, firstGatt.closed);
            assertEquals(0, newGatt.closed);
            assertTrue(clock.pending.isEmpty());
        } finally {
            session.pause();
        }
    }

    @Test
    public void manualStopDuringThrottleBackoffPreventsScanAndGattAfterDeadline() {
        FakeTransport transport = new FakeTransport();
        ManualClock clock = new ManualClock();
        List<BleConnectionManager.State> states = new ArrayList<>();
        var session = new DebugBleReconnectIntent(new MemoryStore(), () -> true, (address, grant) ->
                new DebugBleProbe(address, transport, clock, states::add, grant));
        var throttled = AndroidBleDiscovery.classifyStartFailure(ScanCallback.SCAN_FAILED_SCANNING_TOO_FREQUENTLY);
        try {
            assertEquals(DebugBleReconnectIntent.Result.STARTED, session.start(ADDRESS, ADDRESS::equals));
            transport.scans.get(0).seen.accept(MATCH);
            Link firstGatt = transport.links.get(0);
            firstGatt.ready();
            firstGatt.events.lost();
            clock.advance(1_000);
            Scan rejectedScan = transport.scans.get(1);
            rejectedScan.finished.accept(throttled);
            assertEquals(List.of(31_000L), new ArrayList<>(clock.pending.values()));
            Runnable cancelledRetry = clock.pending.keySet().iterator().next();

            clock.advance(15_000);
            assertTrue(session.forget());
            assertTrue(clock.pending.isEmpty());
            assertNull(session.pendingAddress());
            assertEquals(BleConnectionManager.State.STOPPED, states.get(states.size() - 1));

            clock.advance(16_000);
            cancelledRetry.run();
            rejectedScan.seen.accept(MATCH);
            rejectedScan.finished.accept(throttled);
            firstGatt.events.lost();
            assertEquals(2, transport.scans.size());
            assertEquals(1, transport.links.size());
            assertEquals(1, firstGatt.closed);
            assertEquals(BleConnectionManager.State.STOPPED, states.get(states.size() - 1));
            assertTrue(clock.pending.isEmpty());
        } finally {
            session.pause();
        }
    }

    @Test
    public void manualStopWhileBluetoothIsOffCancelsRecoveryUntilAnExplicitRequest() {
        FakeTransport transport = new FakeTransport();
        ManualClock clock = new ManualClock();
        List<BleConnectionManager.State> states = new ArrayList<>();
        var session = new DebugBleReconnectIntent(new MemoryStore(), () -> true, (address, grant) ->
                new DebugBleProbe(address, transport, clock, states::add, grant));
        try {
            assertEquals(DebugBleReconnectIntent.Result.STARTED, session.start(ADDRESS, ADDRESS::equals));
            Scan oldScan = transport.scans.get(0);
            oldScan.seen.accept(MATCH);
            Link original = transport.links.get(0);
            original.ready();
            original.events.lost();
            assertEquals(BleConnectionManager.State.RETRYING, states.get(states.size() - 1));
            Runnable staleRetry = clock.pending.keySet().iterator().next();

            session.adapterChanged(false);
            assertEquals(BleConnectionManager.State.BLUETOOTH_OFF, states.get(states.size() - 1));
            assertTrue(session.forget());
            assertNull(session.pendingAddress());

            session.adapterChanged(true);
            session.adapterChanged(true);
            clock.advance(30_000);
            staleRetry.run();
            oldScan.seen.accept(MATCH);
            assertEquals(DebugBleReconnectIntent.Result.NO_INTENT, session.resume(ADDRESS::equals));
            assertEquals(BleConnectionManager.State.STOPPED, states.get(states.size() - 1));
            assertEquals(1, transport.scans.size());
            assertEquals(1, transport.links.size());
            assertEquals(1, original.closed);
            assertTrue(clock.pending.isEmpty());

            assertEquals(DebugBleReconnectIntent.Result.STARTED, session.start(ADDRESS, ADDRESS::equals));
            assertEquals(2, transport.scans.size());
            staleRetry.run();
            oldScan.seen.accept(MATCH);
            assertEquals(1, transport.links.size());
            transport.scans.get(1).seen.accept(MATCH);
            transport.scans.get(1).seen.accept(MATCH);
            assertEquals(2, transport.links.size());
            transport.links.get(1).ready();
            assertEquals(BleConnectionManager.State.CONNECTED, states.get(states.size() - 1));
            assertTrue(clock.pending.isEmpty());
        } finally {
            session.pause();
        }
    }

    @Test
    public void exhaustedRecoveryOffersOneManualBoundedRoundAfterCooldown() {
        assertManualRescanAfterExhaustion(true);
    }

    @Test
    public void manualRescanAfterExhaustionCannotUseRevokedGrant() {
        assertManualRescanAfterExhaustion(false);
    }

    private void assertManualRescanAfterExhaustion(boolean keepGrant) {
        var transport = new FakeTransport();
        var clock = new ManualClock();
        var grant = new AtomicBoolean(true);
        var states = new ArrayList<BleConnectionManager.State>();
        var session = new DebugBleReconnectIntent(new MemoryStore(), () -> true, (address, authorized) ->
                new DebugBleProbe(address, transport, clock, states::add, authorized));
        try {
            assertEquals(DebugBleReconnectIntent.Result.STARTED,
                    session.start(ADDRESS, address -> ADDRESS.equals(address) && grant.get()));
            transport.scans.get(0).seen.accept(MATCH);
            var first = transport.links.get(0);
            first.ready();
            first.events.lost();
            clock.advance(1_000);
            exhaustRecoveryScans(transport, clock, 1);
            assertEquals(BleConnectionManager.State.RESCAN_COOLDOWN, states.get(states.size() - 1));
            assertEquals(R.string.plaud_probe_rescan_cooldown, BleDeviceTestFixture.probeMessage(states.get(states.size() - 1)));
            clock.advance(29_999);
            assertNull(session.manualRescanRevision());
            session.requestManualRescan(null);
            assertEquals(4, transport.scans.size());

            clock.advance(1);
            assertEquals(BleConnectionManager.State.RESCAN_READY, states.get(states.size() - 1));
            assertEquals(R.string.plaud_probe_rescan_ready, BleDeviceTestFixture.probeMessage(states.get(states.size() - 1)));
            assertEquals(4, transport.scans.size());
            assertEquals(0, clock.pendingCount());

            String firstRevision = session.manualRescanRevision();
            assertNotNull(firstRevision);
            grant.set(keepGrant);
            session.requestManualRescan(firstRevision);
            session.requestManualRescan(firstRevision);
            if (keepGrant) {
                assertEquals(5, transport.scans.size());
                assertEquals(BleConnectionManager.State.WAITING_FOR_DEVICE, states.get(states.size() - 1));
                exhaustRecoveryScans(transport, clock, 4);
                assertEquals(BleConnectionManager.State.RESCAN_COOLDOWN, states.get(states.size() - 1));
                clock.advance(30_000);
                assertEquals(BleConnectionManager.State.RESCAN_READY, states.get(states.size() - 1));
                assertEquals(7, transport.scans.size());
                String nextRevision = session.manualRescanRevision();
                assertNotNull(nextRevision);
                assertNotEquals(firstRevision, nextRevision);

                session.requestManualRescan(firstRevision);
                assertEquals(BleConnectionManager.State.RESCAN_READY, states.get(states.size() - 1));
                assertEquals(nextRevision, session.manualRescanRevision());
                assertEquals(7, transport.scans.size());
                assertEquals(0, clock.pendingCount());

                session.requestManualRescan(nextRevision);
                assertEquals(BleConnectionManager.State.WAITING_FOR_DEVICE, states.get(states.size() - 1));
                assertEquals(8, transport.scans.size());
            } else {
                assertEquals(BleConnectionManager.State.UNAUTHORIZED, states.get(states.size() - 1));
                assertEquals(4, transport.scans.size());
            }
            assertEquals(1, transport.links.size());
            assertEquals(0, clock.pendingCount());
        } finally {
            session.pause();
        }
    }

    private static void exhaustRecoveryScans(FakeTransport transport, ManualClock clock, int firstScan) {
        transport.scans.get(firstScan).finished.accept(BleConnectionManager.ScanOutcome.TIMED_OUT);
        clock.advance(2_000);
        transport.scans.get(firstScan + 1).finished.accept(BleConnectionManager.ScanOutcome.TEMPORARY_FAILURE);
        clock.advance(4_000);
        transport.scans.get(firstScan + 2).finished.accept(BleConnectionManager.ScanOutcome.TIMED_OUT);
    }

    @Test
    public void recreatedLabViewKeepsOriginalCooldownAndDoesNotStartScanning() {
        var transport = new FakeTransport();
        var clock = new ManualClock();
        var lab = labWithFakeTransport(transport, clock);
        var originalStates = new ArrayList<BleConnectionManager.State>();
        var original = lab.bind((state, count) -> originalStates.add(state));
        try {
            startExhaustedLab(lab, transport, clock, new AtomicBoolean(true));
            var originalDeadline = clock.onlyPendingCallback();
            clock.advance(10_000);
            assertTrue(original.detachForRecreation());
            clock.advance(5_000);
            var replacementStates = new ArrayList<BleConnectionManager.State>();
            DebugBleLab.fromNonConfigurationInstance(lab, () -> labWithFakeTransport(transport, clock))
                    .bind((state, count) -> replacementStates.add(state));
            assertEquals(List.of(BleConnectionManager.State.RESCAN_COOLDOWN), replacementStates);
            assertEquals(originalDeadline, clock.onlyPendingCallback());
            clock.advance(14_999);
            assertNull(lab.reconnectIntent.manualRescanRevision());
            clock.advance(1);
            assertEquals(List.of(BleConnectionManager.State.RESCAN_COOLDOWN, BleConnectionManager.State.RESCAN_READY),
                    replacementStates);
            assertEquals(BleConnectionManager.State.RESCAN_COOLDOWN, originalStates.get(originalStates.size() - 1));
            assertEquals(4, transport.scans.size());
            assertEquals(0, clock.pendingCount());
        } finally {
            lab.reconnectIntent.pause();
        }
    }

    @Test
    public void recreatedLabViewRejectsOldViewClickForSameRevision() {
        assertRestoredLabRequest(true);
    }

    @Test
    public void recreatingLabViewDoesNotRefreshRevokedAuthorization() {
        assertRestoredLabRequest(false);
    }

    private void assertRestoredLabRequest(boolean keepGrant) {
        var transport = new FakeTransport();
        var clock = new ManualClock();
        var grant = new AtomicBoolean(true);
        var lab = labWithFakeTransport(transport, clock);
        var original = lab.bind((state, count) -> {});
        try {
            startExhaustedLab(lab, transport, clock, grant);
            clock.advance(30_000);
            String revision = lab.reconnectIntent.manualRescanRevision();
            assertNotNull(revision);
            Runnable delayedClick = () -> original.requestManualRescan(revision);
            assertTrue(original.detachForRecreation());
            grant.set(keepGrant);
            delayedClick.run();
            var states = new ArrayList<BleConnectionManager.State>();
            var replacement = lab.bind((state, count) -> states.add(state));
            assertEquals(List.of(BleConnectionManager.State.RESCAN_READY), states);
            assertEquals(revision, lab.reconnectIntent.manualRescanRevision());
            delayedClick.run();
            assertEquals(4, transport.scans.size());
            assertEquals(0, clock.pendingCount());

            replacement.requestManualRescan(revision);
            if (keepGrant) {
                assertEquals(5, transport.scans.size());
                exhaustRecoveryScans(transport, clock, 4);
                assertEquals(7, transport.scans.size());
                assertEquals(BleConnectionManager.State.RESCAN_COOLDOWN, states.get(states.size() - 1));
            } else {
                assertEquals(4, transport.scans.size());
                assertEquals(BleConnectionManager.State.UNAUTHORIZED, states.get(states.size() - 1));
                assertEquals(0, clock.pendingCount());
            }
        } finally {
            lab.reconnectIntent.pause();
        }
    }

    @Test
    public void coldRestoredLabRejectsOldRescanAndWaitsForFreshAuthorization() {
        var store = new MemoryStore();
        var oldTransport = new FakeTransport();
        var oldClock = new ManualClock();
        var previous = labWithFakeTransport(store, oldTransport, oldClock);
        var oldView = previous.bind((state, count) -> {});
        var oldGrant = new AtomicBoolean(true);
        startExhaustedLab(previous, oldTransport, oldClock, oldGrant);
        oldClock.advance(30_000);
        String oldRevision = previous.reconnectIntent.manualRescanRevision();
        assertNotNull(oldRevision);
        assertTrue(oldView.detachForRecreation());
        previous.reconnectIntent.pause();

        var transport = new FakeTransport();
        var clock = new ManualClock();
        var restored = DebugBleLab.fromNonConfigurationInstance(null, () -> labWithFakeTransport(store, transport, clock));
        var states = new ArrayList<BleConnectionManager.State>();
        var view = restored.bind((state, count) -> states.add(state));
        try {
            assertEquals(ADDRESS, restored.reconnectIntent.pendingAddress());
            assertNull(restored.reconnectIntent.manualRescanRevision());
            oldView.requestManualRescan(oldRevision);
            view.requestManualRescan(oldRevision);
            restored.reconnectIntent.adapterChanged(false);
            restored.reconnectIntent.adapterChanged(true);
            clock.advance(30_000);

            var newGrant = new AtomicBoolean(false);
            assertEquals(DebugBleReconnectIntent.Result.NOT_AUTHORIZED,
                    restored.reconnectIntent.resume(address -> ADDRESS.equals(address) && newGrant.get()));
            assertEquals(List.of(BleConnectionManager.State.STOPPED), states);
            assertTrue(transport.scans.isEmpty());
            assertTrue(transport.links.isEmpty());
            assertEquals(0, clock.pendingCount());
            assertEquals(4, oldTransport.scans.size());

            newGrant.set(true);
            assertEquals(DebugBleReconnectIntent.Result.STARTED,
                    restored.reconnectIntent.resume(address -> ADDRESS.equals(address) && newGrant.get()));
            view.requestManualRescan(oldRevision);
            assertEquals(1, transport.scans.size());
            transport.scans.get(0).seen.accept(MATCH);
            transport.links.get(0).ready();
            assertEquals(BleConnectionManager.State.CONNECTED, states.get(states.size() - 1));
        } finally {
            restored.reconnectIntent.pause();
        }
    }

    private static DebugBleLab labWithFakeTransport(FakeTransport transport, ManualClock clock) {
        return labWithFakeTransport(new MemoryStore(), transport, clock);
    }

    private static DebugBleLab labWithFakeTransport(MemoryStore store, FakeTransport transport, ManualClock clock) {
        return new DebugBleLab(render -> new DebugBleReconnectIntent(store, () -> true,
                (address, granted) -> new DebugBleProbe(address, transport, clock, render, granted)));
    }

    private static void startExhaustedLab(DebugBleLab lab, FakeTransport transport, ManualClock clock,
            AtomicBoolean grant) {
        assertEquals(DebugBleReconnectIntent.Result.STARTED,
                lab.reconnectIntent.start(ADDRESS, address -> ADDRESS.equals(address) && grant.get()));
        transport.scans.get(0).seen.accept(MATCH);
        var first = transport.links.get(0);
        first.ready();
        first.events.lost();
        clock.advance(1_000);
        exhaustRecoveryScans(transport, clock, 1);
    }

    @Test
    public void staleGattCallbacksAfterManualStopCannotAffectSeparatelyAuthorizedSession() {
        FakeTransport transport = new FakeTransport();
        ManualClock clock = new ManualClock();
        List<BleConnectionManager.State> states = new ArrayList<>();
        var session = new DebugBleReconnectIntent(new MemoryStore(), () -> true, (address, grant) ->
                new DebugBleProbe(address, transport, clock, states::add, grant));
        var originalGrant = new AtomicBoolean(true);
        var replacementGrant = new AtomicBoolean(false);
        try {
            assertEquals(DebugBleReconnectIntent.Result.STARTED,
                    session.start(ADDRESS, address -> ADDRESS.equals(address) && originalGrant.get()));
            Scan oldScan = transport.scans.get(0);
            oldScan.seen.accept(MATCH);
            Link oldGatt = transport.links.get(0);
            oldGatt.events.discovering();
            assertTrue(session.forget());
            assertEquals(1, oldGatt.closed);

            assertEquals(DebugBleReconnectIntent.Result.NOT_AUTHORIZED,
                    session.start(ADDRESS, address -> ADDRESS.equals(address) && replacementGrant.get()));
            assertEquals(1, transport.scans.size());
            replacementGrant.set(true);
            assertEquals(DebugBleReconnectIntent.Result.STARTED,
                    session.start(ADDRESS, address -> ADDRESS.equals(address) && replacementGrant.get()));
            transport.scans.get(1).seen.accept(MATCH);
            Link newGatt = transport.links.get(1);
            newGatt.ready();
            assertEquals(BleConnectionManager.State.CONNECTED, states.get(states.size() - 1));
            List<BleConnectionManager.State> beforeLateCallbacks = List.copyOf(states);

            oldGatt.events.services(true);
            oldGatt.events.lost();
            oldGatt.events.lost();
            oldScan.seen.accept(MATCH);
            clock.advance(60_000);
            assertEquals(beforeLateCallbacks, states);
            assertEquals(2, transport.scans.size());
            assertEquals(2, transport.links.size());
            assertEquals(1, oldGatt.closed);
            assertEquals(0, newGatt.closed);
            assertTrue(clock.pending.isEmpty());
        } finally {
            session.pause();
        }
    }

    @Test
    public void repeatedThrottlingExhaustsRecoveryBudgetWithoutAnotherAutomaticScan() {
        FakeTransport transport = new FakeTransport();
        ManualClock clock = new ManualClock();
        List<BleConnectionManager.State> states = new ArrayList<>();
        var probe = new DebugBleProbe(ADDRESS, transport, clock, states::add);
        var throttled = AndroidBleDiscovery.classifyStartFailure(ScanCallback.SCAN_FAILED_SCANNING_TOO_FREQUENTLY);
        try {
            probe.start(true);
            transport.scans.get(0).seen.accept(MATCH);
            Link firstGatt = transport.links.get(0);
            firstGatt.ready();
            firstGatt.events.lost();
            clock.advance(1_000);
            for (int recoveryRound = 1; recoveryRound <= 3; recoveryRound++) {
                Scan recovery = transport.scans.get(recoveryRound);
                recovery.finished.accept(throttled);
                recovery.finished.accept(throttled);
                assertEquals(recoveryRound + 1, transport.scans.size());
                assertEquals(1, clock.pending.size());
                clock.advance(30_000);
            }
            assertEquals(BleConnectionManager.State.RESCAN_READY, states.get(states.size() - 1));
            clock.advance(60_000);
            assertEquals(4, transport.scans.size());
            assertEquals(1, transport.links.size());
            assertTrue(clock.pending.isEmpty());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void missingOrRevokedTestGrantPreventsOpeningGatt() {
        for (boolean initiallyAllowed : new boolean[]{false, true}) {
            FakeTransport transport = new FakeTransport();
            List<BleConnectionManager.State> states = new ArrayList<>();
            ManualClock clock = new ManualClock();
            var probe = new DebugBleProbe(ADDRESS, transport, clock, states::add);
            try {
                probe.start(initiallyAllowed);
                transport.scans.get(0).seen.accept(MATCH);
                if (initiallyAllowed) {
                    Link first = transport.links.get(0);
                    first.ready();
                    first.events.lost();
                    probe.revoke();
                    clock.advance(1_000);
                    assertEquals(1, transport.scans.size());
                    assertTrue(clock.pending.isEmpty());
                }
                assertEquals(initiallyAllowed ? 1 : 0, transport.links.size());
                assertEquals(BleConnectionManager.State.UNAUTHORIZED, states.get(states.size() - 1));
            } finally {
                probe.stop();
            }
        }
    }

    @Test
    public void bluetoothRecoveryRequiresFreshSightingAndStillValidGrant() {
        for (boolean grantStillValid : new boolean[]{true, false}) {
            FakeTransport transport = new FakeTransport();
            ManualClock clock = new ManualClock();
            List<BleConnectionManager.State> states = new ArrayList<>();
            var grant = new AtomicBoolean(true);
            var probe = new DebugBleProbe(ADDRESS, transport, clock, states::add, grant::get);
            try {
                probe.start(true);
                var oldScan = transport.scans.get(0);
                oldScan.seen.accept(MATCH);
                Link oldGatt = transport.links.get(0);
                oldGatt.ready();

                probe.adapterChanged(false);
                assertEquals(1, oldGatt.closed);
                assertEquals(BleConnectionManager.State.BLUETOOTH_OFF, states.get(states.size() - 1));
                assertTrue(clock.pending.isEmpty());
                grant.set(grantStillValid);
                probe.adapterChanged(true);
                probe.adapterChanged(true);
                oldScan.seen.accept(MATCH);
                oldGatt.events.lost();
                assertEquals(1, transport.links.size());
                assertEquals(grantStillValid ? 2 : 1, transport.scans.size());

                if (grantStillValid) {
                    var recovery = transport.scans.get(1);
                    recovery.seen.accept(MATCH);
                    recovery.seen.accept(MATCH);
                    transport.links.get(1).ready();
                    assertEquals(2, transport.links.size());
                    assertEquals(BleConnectionManager.State.CONNECTED, states.get(states.size() - 1));
                } else {
                    assertEquals(BleConnectionManager.State.UNAUTHORIZED, states.get(states.size() - 1));
                }
                assertTrue(clock.pending.isEmpty());
            } finally {
                probe.stop();
            }
        }
    }

    @Test
    public void bluetoothToggleInvalidatesDisconnectBackoffAndRequiresFreshAuthorizedSighting() {
        for (boolean grantStillValid : new boolean[]{true, false}) {
            FakeTransport transport = new FakeTransport();
            ManualClock clock = new ManualClock();
            List<BleConnectionManager.State> states = new ArrayList<>();
            var grant = new AtomicBoolean(true);
            var session = new DebugBleReconnectIntent(new MemoryStore(), () -> true, (address, authorized) ->
                    new DebugBleProbe(address, transport, clock, states::add, authorized));
            try {
                assertEquals(DebugBleReconnectIntent.Result.STARTED,
                        session.start(ADDRESS, address -> grant.get() && ADDRESS.equals(address)));
                Scan oldScan = transport.scans.get(0);
                oldScan.seen.accept(MATCH);
                Link oldGatt = transport.links.get(0);
                oldGatt.ready();
                oldGatt.events.lost();
                assertEquals(1, oldGatt.closed);
                assertEquals(List.of(1_000L), new ArrayList<>(clock.pending.values()));
                Runnable oldRetry = clock.pending.keySet().iterator().next();

                clock.advance(250);
                session.adapterChanged(false);
                assertTrue(clock.pending.isEmpty());
                assertEquals(BleConnectionManager.State.BLUETOOTH_OFF, states.get(states.size() - 1));
                session.adapterChanged(true);
                assertEquals(2, transport.scans.size());
                Scan recovery = transport.scans.get(1);

                clock.advance(750);
                oldRetry.run();
                oldScan.seen.accept(MATCH);
                recovery.seen.accept(new DebugBleProbe.Sighting("AA:BB:CC:DD:EE:02", DebugBleProbe.SERVICE));
                assertEquals(2, transport.scans.size());
                assertEquals(1, transport.links.size());
                assertTrue(clock.pending.isEmpty());

                grant.set(grantStillValid);
                recovery.seen.accept(MATCH);
                recovery.seen.accept(MATCH);
                if (grantStillValid) {
                    assertEquals(2, transport.links.size());
                    Link newGatt = transport.links.get(1);
                    newGatt.ready();
                    oldRetry.run();
                    assertEquals(0, newGatt.closed);
                    assertEquals(BleConnectionManager.State.CONNECTED, states.get(states.size() - 1));
                } else {
                    assertEquals(1, transport.links.size());
                    assertEquals(BleConnectionManager.State.UNAUTHORIZED, states.get(states.size() - 1));
                }
                assertEquals(2, transport.scans.size());
                assertTrue(clock.pending.isEmpty());
            } finally {
                session.pause();
            }
        }
    }

    @Test
    public void bluetoothOffCancelsInitialScanBackoffAndRestartsWithFreshScan() {
        FakeTransport transport = new FakeTransport();
        ManualClock clock = new ManualClock();
        var probe = new DebugBleProbe(ADDRESS, transport, clock, ignored -> {});
        try {
            probe.start(true);
            var firstScan = transport.scans.get(0);
            firstScan.finished.accept(BleConnectionManager.ScanOutcome.TIMED_OUT);
            Runnable oldRetry = clock.pending.keySet().iterator().next();
            probe.adapterChanged(false);
            assertTrue(clock.pending.isEmpty());
            probe.adapterChanged(true);
            oldRetry.run();
            firstScan.seen.accept(MATCH);
            assertEquals(2, transport.scans.size());
            assertTrue(transport.links.isEmpty());
            transport.scans.get(1).seen.accept(MATCH);
            assertEquals(1, transport.links.size());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void revokedGrantDoesNotRestartAnInterruptedInitialScan() {
        FakeTransport transport = new FakeTransport();
        ManualClock clock = new ManualClock();
        List<BleConnectionManager.State> states = new ArrayList<>();
        var grant = new AtomicBoolean(true);
        var probe = new DebugBleProbe(ADDRESS, transport, clock, states::add, grant::get);
        try {
            probe.start(true);
            probe.adapterChanged(false);
            grant.set(false);
            probe.adapterChanged(true);
            transport.scans.get(0).seen.accept(MATCH);
            assertEquals(BleConnectionManager.State.UNAUTHORIZED, states.get(states.size() - 1));
            assertEquals(1, transport.scans.size());
            assertTrue(transport.links.isEmpty());
            assertTrue(clock.pending.isEmpty());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void coldStartClearsSavedIntentWhenConnectPermissionWasRevokedWhileStopped() {
        MemoryStore store = new MemoryStore();
        FakeTransport previous = new FakeTransport();
        var deviceGrant = new AtomicBoolean(true);
        Predicate<String> authorize = address -> ADDRESS.equals(address) && deviceGrant.get();
        var before = controller(store, previous, new ArrayList<>());
        assertEquals(DebugBleReconnectIntent.Result.STARTED, before.start(ADDRESS, authorize));
        previous.scans.get(0).seen.accept(MATCH);
        Link oldGatt = previous.links.get(0);
        oldGatt.ready();
        before.pause();
        assertEquals(1, oldGatt.closed);
        assertEquals(ADDRESS, store.address);

        FakeTransport current = new FakeTransport();
        List<BleConnectionManager.State> states = new ArrayList<>();
        var context = mock(Context.class);
        when(context.checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN))
                .thenReturn(PackageManager.PERMISSION_GRANTED);
        when(context.checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT))
                .thenReturn(PackageManager.PERMISSION_DENIED);
        var clock = new ManualClock();
        var after = new DebugBleReconnectIntent(store, () -> BleDeviceTestFixture.hasProbePermissions(context),
                (address, authorized) -> new DebugBleProbe(address, current, clock, states::add, authorized));
        try {
            assertNull(store.address);
            assertNull(after.pendingAddress());
            assertEquals(DebugBleReconnectIntent.Result.NO_INTENT, after.resume(authorize));
            assertTrue(current.scans.isEmpty());
            assertTrue(current.links.isEmpty());
            when(context.checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT))
                    .thenReturn(PackageManager.PERMISSION_GRANTED);
            after.adapterChanged(false);
            after.adapterChanged(true);
            clock.advance(60_000);
            assertTrue(deviceGrant.get());
            assertEquals(DebugBleReconnectIntent.Result.NO_INTENT, after.resume(authorize));
            assertEquals(DebugBleReconnectIntent.Result.NO_INTENT,
                    controller(store, current, states).resume(authorize));
            assertTrue(current.scans.isEmpty());
            assertTrue(current.links.isEmpty());
            assertEquals(0, clock.pendingCount());

            assertEquals(DebugBleReconnectIntent.Result.STARTED, after.start(ADDRESS, authorize));
            assertEquals(DebugBleReconnectIntent.Result.ALREADY_ACTIVE, after.resume(authorize));
            assertEquals(1, current.scans.size());
            oldGatt.events.lost();
            oldGatt.ready();
            assertTrue(current.links.isEmpty());
            current.scans.get(0).seen.accept(MATCH);
            current.scans.get(0).seen.accept(MATCH);
            Link newGatt = current.links.get(0);
            newGatt.ready();
            assertEquals(1, current.links.size());
            assertEquals(0, newGatt.closed);
            assertEquals(1, states.stream().filter(state -> state == BleConnectionManager.State.CONNECTED).count());
            assertEquals(3, store.writes);
        } finally {
            assertTrue(after.forget());
        }
        assertNull(store.address);
        assertEquals(DebugBleReconnectIntent.Result.NO_INTENT,
                controller(store, new FakeTransport(), new ArrayList<>()).resume(ADDRESS::equals));
    }

    @Test
    public void nearbyDevicesPermissionLossDuringRecoveryRequiresExplicitReauthorization() {
        for (boolean retryPending : new boolean[]{false, true}) {
            MemoryStore store = new MemoryStore();
            FakeTransport transport = new FakeTransport();
            ManualClock clock = new ManualClock();
            var permissions = new AtomicBoolean(true);
            var lab = new DebugBleLab(render -> new DebugBleReconnectIntent(store, permissions::get,
                    (address, grant) -> new DebugBleProbe(address, transport, clock, render, grant)));
            var resume = lab.reconnectIntent;
            var states = new ArrayList<BleConnectionManager.State>();
            var sessions = new ArrayList<Integer>();
            lab.bind((state, ready) -> {
                states.add(state);
                sessions.add(ready);
            });
            try {
                assertEquals(DebugBleReconnectIntent.Result.STARTED, resume.startOrResume(ADDRESS, ADDRESS::equals));
                Scan firstScan = transport.scans.get(0);
                firstScan.seen.accept(MATCH);
                Link oldGatt = transport.links.get(0);
                oldGatt.ready();
                assertEquals(1, sessions.get(sessions.size() - 1).intValue());
                oldGatt.events.lost();
                clock.advance(1_000);
                Scan oldRecovery = transport.scans.get(1);
                Runnable abandonedRetry = null;
                if (retryPending) {
                    oldRecovery.finished.accept(BleConnectionManager.ScanOutcome.TIMED_OUT);
                    abandonedRetry = clock.onlyPendingCallback();
                }
                assertEquals(retryPending ? BleConnectionManager.State.WAITING_TO_RESCAN
                        : BleConnectionManager.State.WAITING_FOR_DEVICE, states.get(states.size() - 1));
                assertEquals(retryPending ? 1 : 0, transport.stoppedScans.stream()
                        .filter(scan -> scan == oldRecovery).count());

                permissions.set(false);
                assertTrue(resume.invalidateIfPermissionLost());
                assertFalse(resume.hasActiveSession());
                assertEquals(BleConnectionManager.State.STOPPED, states.get(states.size() - 1));
                assertEquals(0, sessions.get(sessions.size() - 1).intValue());
                assertEquals(1, oldGatt.closed);
                assertEquals(List.of(firstScan, oldRecovery), transport.stoppedScans);
                assertTrue(clock.pending.isEmpty());
                assertNull(resume.pendingAddress());
                assertEquals(DebugBleReconnectIntent.Result.NO_INTENT, resume.resume(ADDRESS::equals));
                assertEquals(DebugBleReconnectIntent.Result.NOT_AUTHORIZED,
                        resume.startOrResume(ADDRESS, ADDRESS::equals));

                permissions.set(true);
                resume.adapterChanged(false);
                resume.adapterChanged(true);
                assertEquals(DebugBleReconnectIntent.Result.NO_INTENT, resume.resume(ADDRESS::equals));
                if (abandonedRetry != null) {
                    abandonedRetry.run();
                }
                oldRecovery.seen.accept(MATCH);
                oldRecovery.finished.accept(BleConnectionManager.ScanOutcome.TIMED_OUT);
                firstScan.seen.accept(MATCH);
                oldGatt.events.services(true);
                oldGatt.events.lost();
                clock.advance(60_000);
                assertEquals(2, transport.scans.size());
                assertEquals(1, transport.links.size());
                assertTrue(clock.pending.isEmpty());

                assertEquals(DebugBleReconnectIntent.Result.STARTED, resume.startOrResume(ADDRESS, ADDRESS::equals));
                assertEquals(3, transport.scans.size());
                Scan currentScan = transport.scans.get(2);
                currentScan.seen.accept(MATCH);
                currentScan.seen.accept(MATCH);
                Link newGatt = transport.links.get(1);
                newGatt.ready();
                oldGatt.events.lost();
                assertEquals(BleConnectionManager.State.CONNECTED, states.get(states.size() - 1));
                assertEquals(1, sessions.get(sessions.size() - 1).intValue());
                assertEquals(2, transport.links.size());
                assertEquals(List.of(firstScan, oldRecovery, currentScan), transport.stoppedScans);
                assertEquals(0, newGatt.closed);
                assertTrue(clock.pending.isEmpty());
            } finally {
                resume.pause();
            }
        }
    }

    @Test
    public void permissionLossObservedByGattIsLatchedDespiteImmediateRestoration() {
        var permissions = new AtomicBoolean(true);
        var transport = new FakeTransport();
        var resume = new DebugBleReconnectIntent(new MemoryStore(), permissions::get, (address, grant) ->
                new DebugBleProbe(address, transport, new ManualClock(), ignored -> {}, grant));
        try {
            assertEquals(DebugBleReconnectIntent.Result.STARTED, resume.start(ADDRESS, ADDRESS::equals));
            transport.scans.get(0).seen.accept(MATCH);
            Link oldGatt = transport.links.get(0);
            permissions.set(false);
            oldGatt.ready();
            permissions.set(true);
            assertTrue(resume.invalidateIfPermissionLost());
            assertEquals(1, oldGatt.closed);
            assertEquals(DebugBleReconnectIntent.Result.NO_INTENT, resume.resume(ignored -> false));
            assertEquals(1, transport.scans.size());
        } finally {
            resume.pause();
        }
    }

    @Test
    public void recoveredIntentDoesNotOverrideAuthorizationRevokedBeforeOpeningGatt() {
        MemoryStore store = new MemoryStore();
        store.address = ADDRESS;
        FakeTransport transport = new FakeTransport();
        List<BleConnectionManager.State> states = new ArrayList<>();
        var resume = controller(store, transport, states);
        var grant = new AtomicBoolean(true);
        try {
            assertEquals(DebugBleReconnectIntent.Result.STARTED, resume.resume(ignored -> grant.get()));
            grant.set(false);
            transport.scans.get(0).seen.accept(MATCH);
            assertTrue(transport.links.isEmpty());
            assertEquals(BleConnectionManager.State.UNAUTHORIZED, states.get(states.size() - 1));
        } finally {
            resume.pause();
        }
    }

    @Test
    public void failedIntentWriteCannotStartOrResumeLabSession() {
        var store = new MemoryStore();
        store.writeSucceeds = false;
        var transport = new FakeTransport();
        var clock = new ManualClock();
        var states = new ArrayList<BleConnectionManager.State>();
        var deviceGrant = new AtomicBoolean(true);
        Predicate<String> authorize = address -> ADDRESS.equals(address) && deviceGrant.get();
        var session = new DebugBleReconnectIntent(store, () -> true, (address, granted) ->
                new DebugBleProbe(address, transport, clock, states::add, granted));
        try {
            assertEquals(DebugBleReconnectIntent.Result.STORAGE_FAILED, session.start(ADDRESS, authorize));
            assertEquals(1, store.writes);
            assertEquals(ADDRESS, store.readAddress());
            assertNull(session.pendingAddress());
            assertEquals(DebugBleReconnectIntent.Result.NO_INTENT, session.resume(authorize));
            session.adapterChanged(false);
            session.adapterChanged(true);
            clock.advance(60_000);
            assertTrue(authorize.test(ADDRESS));
            assertTrue(states.isEmpty());
            assertTrue(transport.scans.isEmpty());
            assertTrue(transport.links.isEmpty());
            assertEquals(0, clock.pendingCount());

            store.writeSucceeds = true;
            assertEquals(DebugBleReconnectIntent.Result.STARTED, session.start(ADDRESS, authorize));
            assertEquals(2, store.writes);
            assertEquals(1, transport.scans.size());
            transport.scans.get(0).seen.accept(MATCH);
            assertEquals(1, transport.links.size());
        } finally {
            session.pause();
        }
    }

    @Test
    public void readRetryUsesOneResultForAddressAndStatusWithoutStartingBle() {
        var store = new MemoryStore();
        store.address = ADDRESS;
        store.readError = new IllegalStateException("Cannot read intent");
        var transport = new FakeTransport();
        var clock = new ManualClock();
        var lab = labWithFakeTransport(store, transport, clock);
        var intent = lab.reconnectIntent;
        var messages = new ArrayList<Integer>();
        var addresses = new ArrayList<String>();
        Consumer<String> renderResult = address -> {
            if (address != null) {
                addresses.add(address);
            }
            messages.add(BleDeviceTestFixture.pendingProbeMessage(address, intent.storageFailed()));
        };
        var deviceGrant = new AtomicBoolean(true);
        Predicate<String> authorize = address -> ADDRESS.equals(address) && deviceGrant.get();
        try {
            var view = lab.bind((state, count) -> renderResult.accept(intent.pendingAddress()));
            assertEquals(List.of(R.string.plaud_probe_storage_failed), messages);
            assertTrue(intent.intentReadFailed());
            assertEquals(DebugBleReconnectIntent.Result.STORAGE_FAILED, intent.resume(authorize));
            assertEquals(DebugBleReconnectIntent.Result.STORAGE_FAILED, intent.start(ADDRESS, authorize));
            intent.adapterChanged(false);
            intent.adapterChanged(true);
            clock.advance(60_000);
            assertTrue(authorize.test(ADDRESS));
            assertEquals(ADDRESS, store.address);
            assertEquals(0, store.writes);
            assertTrue(transport.scans.isEmpty());
            assertTrue(transport.links.isEmpty());
            assertEquals(0, clock.pendingCount());

            view.retryPendingAddress(renderResult);
            assertTrue(intent.intentReadFailed());
            store.readError = null;
            store.readsBeforeFailure = 1;
            int readsBeforeRetry = store.reads;
            view.retryPendingAddress(renderResult);
            view.retryPendingAddress(renderResult);
            assertEquals(readsBeforeRetry + 1, store.reads);
            assertEquals(List.of(ADDRESS), addresses);
            assertEquals(List.of(R.string.plaud_probe_storage_failed, R.string.plaud_probe_storage_failed,
                    R.string.plaud_probe_saved), messages);
            assertFalse(intent.intentReadFailed());
            assertTrue(authorize.test(ADDRESS));
            assertEquals(0, store.writes);
            assertTrue(transport.scans.isEmpty());
            assertTrue(transport.links.isEmpty());
            assertEquals(0, clock.pendingCount());
        } finally {
            intent.pause();
        }
    }

    @Test
    public void explicitStartReadsOnceAndOnlyPersistsADifferentAddress() {
        assertExplicitStartWithReadLimit(ADDRESS, true, 1, DebugBleReconnectIntent.Result.STARTED, 0);
        assertExplicitStartWithReadLimit("AA:BB:CC:DD:EE:02", true, 1, DebugBleReconnectIntent.Result.STARTED, 1);
    }

    @Test
    public void explicitStartWithNoDeviceGrantDoesNotScanOrWrite() {
        assertExplicitStartWithReadLimit(ADDRESS, false, 1, DebugBleReconnectIntent.Result.NOT_AUTHORIZED, 0);
    }

    @Test
    public void explicitStartWithReadFailureDoesNotScanOrWrite() {
        assertExplicitStartWithReadLimit(ADDRESS, true, 0, DebugBleReconnectIntent.Result.STORAGE_FAILED, 0);
    }

    @Test
    public void switchingTargetsKeepsOriginalLinkUntilExplicitRetryPersistsNewIntent() {
        String newAddress = "AA:BB:CC:DD:EE:02";
        var store = new MemoryStore();
        var originalTransport = new FakeTransport();
        var replacementTransport = new FakeTransport(newAddress);
        var clock = new ManualClock();
        var states = new ArrayList<BleConnectionManager.State>();
        var originalGrant = new AtomicBoolean(true);
        var replacementGrant = new AtomicBoolean(true);
        Predicate<String> authorizeOriginal = address -> ADDRESS.equals(address) && originalGrant.get();
        Predicate<String> authorizeReplacement = address -> newAddress.equals(address) && replacementGrant.get();
        var intent = new DebugBleReconnectIntent(store, () -> true, (address, authorized) -> {
            var transport = ADDRESS.equals(address) ? originalTransport : replacementTransport;
            if (transport == replacementTransport) {
                assertEquals(1, originalTransport.links.get(0).closed);
            }
            return new DebugBleProbe(address, transport, clock, states::add, authorized);
        });
        try {
            assertEquals(DebugBleReconnectIntent.Result.STARTED, intent.startOrResume(ADDRESS, authorizeOriginal));
            originalTransport.scans.get(0).seen.accept(MATCH);
            var originalLink = originalTransport.links.get(0);
            originalLink.ready();
            assertEquals(BleConnectionManager.State.CONNECTED, states.get(states.size() - 1));
            var connectedStates = List.copyOf(states);

            store.writeSucceeds = false;
            assertEquals(DebugBleReconnectIntent.Result.STORAGE_FAILED,
                    intent.startOrResume(newAddress, authorizeReplacement));
            assertEquals(2, store.writes);
            assertTrue(intent.storageFailed());
            assertEquals(0, originalLink.closed);
            assertEquals(connectedStates, states);
            assertTrue(replacementTransport.scans.isEmpty());
            assertTrue(replacementTransport.links.isEmpty());

            store.writeSucceeds = true;
            intent.adapterChanged(true);
            clock.advance(60_000);
            assertEquals(0, originalLink.closed);
            assertEquals(connectedStates, states);
            assertTrue(replacementTransport.scans.isEmpty());
            assertTrue(replacementTransport.links.isEmpty());

            assertEquals(DebugBleReconnectIntent.Result.STARTED, intent.startOrResume(newAddress, authorizeReplacement));
            assertFalse(intent.storageFailed());
            assertEquals(3, store.writes);
            assertEquals(newAddress, store.address);
            assertEquals(1, originalLink.closed);
            assertEquals(1, replacementTransport.scans.size());
            var replacementScan = replacementTransport.scans.get(0);
            replacementScan.seen.accept(MATCH);
            assertTrue(replacementTransport.links.isEmpty());
            var replacementSighting = new DebugBleProbe.Sighting(newAddress, DebugBleProbe.SERVICE);
            replacementScan.seen.accept(replacementSighting);
            replacementScan.seen.accept(replacementSighting);
            var replacementLink = replacementTransport.links.get(0);
            replacementLink.ready();
            originalLink.events.lost();
            assertEquals(DebugBleReconnectIntent.Result.ALREADY_ACTIVE,
                    intent.startOrResume(newAddress, authorizeReplacement));
            clock.advance(60_000);
            assertEquals(BleConnectionManager.State.CONNECTED, states.get(states.size() - 1));
            assertEquals(1, originalTransport.scans.size());
            assertEquals(1, replacementTransport.scans.size());
            assertEquals(1, replacementTransport.links.size());
            assertEquals(0, replacementLink.closed);
            assertEquals(0, clock.pendingCount());
            assertTrue(authorizeOriginal.test(ADDRESS));
            assertTrue(authorizeReplacement.test(newAddress));
        } finally {
            intent.pause();
        }
    }

    @Test
    public void switchingTargetsIgnoresOriginalRetryAlreadyDequeuedByScheduler() {
        String replacementAddress = "AA:BB:CC:DD:EE:02";
        var originalTransport = new FakeTransport();
        var replacementTransport = new FakeTransport(replacementAddress);
        var clock = new ManualClock();
        var states = new ArrayList<BleConnectionManager.State>();
        Predicate<String> authorizeOriginal = ADDRESS::equals;
        Predicate<String> authorizeReplacement = replacementAddress::equals;
        var intent = new DebugBleReconnectIntent(new MemoryStore(), () -> true, (address, authorized) ->
                new DebugBleProbe(address, ADDRESS.equals(address) ? originalTransport : replacementTransport,
                        clock, states::add, authorized));
        try {
            assertEquals(DebugBleReconnectIntent.Result.STARTED, intent.startOrResume(ADDRESS, authorizeOriginal));
            originalTransport.scans.get(0).seen.accept(MATCH);
            var originalLink = originalTransport.links.get(0);
            originalLink.ready();
            originalLink.events.lost();
            assertEquals(BleConnectionManager.State.RETRYING, states.get(states.size() - 1));
            assertEquals(1, originalLink.closed);
            var dequeuedRetry = clock.onlyPendingCallback();
            clock.elapse(1_000);
            assertEquals(Long.valueOf(clock.nowMillis()), clock.pending.remove(dequeuedRetry));

            assertEquals(DebugBleReconnectIntent.Result.STARTED,
                    intent.startOrResume(replacementAddress, authorizeReplacement));
            assertEquals(1, replacementTransport.scans.size());
            var replacementStates = List.copyOf(states);
            dequeuedRetry.run();
            originalTransport.scans.get(0).seen.accept(MATCH);
            assertEquals(replacementStates, states);
            assertEquals(1, originalTransport.scans.size());
            assertEquals(1, originalTransport.links.size());
            assertEquals(0, clock.pendingCount());

            var replacementScan = replacementTransport.scans.get(0);
            replacementScan.seen.accept(MATCH);
            assertTrue(replacementTransport.links.isEmpty());
            var replacementSighting = new DebugBleProbe.Sighting(replacementAddress, DebugBleProbe.SERVICE);
            replacementScan.seen.accept(replacementSighting);
            replacementScan.seen.accept(replacementSighting);
            replacementTransport.links.get(0).ready();
            clock.advance(60_000);
            assertEquals(BleConnectionManager.State.CONNECTED, states.get(states.size() - 1));
            assertEquals(1, replacementTransport.scans.size());
            assertEquals(1, replacementTransport.links.size());
            assertEquals(0, replacementTransport.links.get(0).closed);
            assertEquals(0, clock.pendingCount());
        } finally {
            intent.pause();
        }
    }

    @Test
    public void switchingBackToSameAddressIgnoresFirstSessionsDequeuedRetry() {
        String otherAddress = "AA:BB:CC:DD:EE:02";
        var originalTransport = new FakeTransport();
        var otherTransport = new FakeTransport(otherAddress);
        var clock = new ManualClock();
        var states = new ArrayList<BleConnectionManager.State>();
        Predicate<String> authorizeOriginal = ADDRESS::equals;
        Predicate<String> authorizeOther = otherAddress::equals;
        var intent = new DebugBleReconnectIntent(new MemoryStore(), () -> true, (address, authorized) ->
                new DebugBleProbe(address, ADDRESS.equals(address) ? originalTransport : otherTransport,
                        clock, states::add, authorized));
        try {
            assertEquals(DebugBleReconnectIntent.Result.STARTED, intent.startOrResume(ADDRESS, authorizeOriginal));
            originalTransport.scans.get(0).seen.accept(MATCH);
            var firstLink = originalTransport.links.get(0);
            firstLink.ready();
            firstLink.events.lost();
            assertEquals(BleConnectionManager.State.RETRYING, states.get(states.size() - 1));
            var firstRetry = clock.onlyPendingCallback();
            clock.elapse(1_000);
            assertEquals(Long.valueOf(clock.nowMillis()), clock.pending.remove(firstRetry));

            assertEquals(DebugBleReconnectIntent.Result.STARTED, intent.startOrResume(otherAddress, authorizeOther));
            otherTransport.scans.get(0).seen.accept(new DebugBleProbe.Sighting(otherAddress, DebugBleProbe.SERVICE));
            var otherLink = otherTransport.links.get(0);
            otherLink.ready();
            assertEquals(BleConnectionManager.State.CONNECTED, states.get(states.size() - 1));
            assertEquals(DebugBleReconnectIntent.Result.STARTED, intent.startOrResume(ADDRESS, authorizeOriginal));
            originalTransport.scans.get(1).seen.accept(MATCH);
            var currentLink = originalTransport.links.get(1);
            assertTrue(currentLink.events.discovering());
            var currentDeadline = clock.onlyPendingCallback();
            var beforeOldRetry = List.copyOf(states);

            firstRetry.run();
            assertEquals(beforeOldRetry, states);
            assertEquals(currentDeadline, clock.onlyPendingCallback());
            assertEquals(0, currentLink.closed);
            assertTrue(currentLink.events.services(true));
            clock.advance(60_000);
            assertEquals(BleConnectionManager.State.CONNECTED, states.get(states.size() - 1));
            assertEquals(2, originalTransport.scans.size());
            assertEquals(2, originalTransport.links.size());
            assertEquals(1, firstLink.closed);
            assertEquals(0, currentLink.closed);
            assertEquals(1, otherTransport.scans.size());
            assertEquals(1, otherTransport.links.size());
            assertEquals(1, otherLink.closed);
            assertEquals(0, clock.pendingCount());
        } finally {
            intent.pause();
        }
    }

    @Test
    public void switchingBackToSameAddressOnlyHonorsCurrentGattDiscoveryTimeout() {
        String otherAddress = "AA:BB:CC:DD:EE:02";
        var originalTransport = new FakeTransport();
        var otherTransport = new FakeTransport(otherAddress);
        var clock = new ManualClock();
        var states = new ArrayList<BleConnectionManager.State>();
        Predicate<String> authorizeOriginal = ADDRESS::equals;
        Predicate<String> authorizeOther = otherAddress::equals;
        var intent = new DebugBleReconnectIntent(new MemoryStore(), () -> true, (address, authorized) ->
                new DebugBleProbe(address, ADDRESS.equals(address) ? originalTransport : otherTransport,
                        clock, states::add, authorized));
        try {
            assertEquals(DebugBleReconnectIntent.Result.STARTED, intent.startOrResume(ADDRESS, authorizeOriginal));
            originalTransport.scans.get(0).seen.accept(MATCH);
            var firstLink = originalTransport.links.get(0);
            assertTrue(firstLink.events.discovering());
            var oldTimeout = clock.onlyPendingCallback();
            clock.elapse(15_000);
            assertEquals(Long.valueOf(clock.nowMillis()), clock.pending.remove(oldTimeout));

            assertEquals(DebugBleReconnectIntent.Result.STARTED, intent.startOrResume(otherAddress, authorizeOther));
            otherTransport.scans.get(0).seen.accept(new DebugBleProbe.Sighting(otherAddress, DebugBleProbe.SERVICE));
            var otherLink = otherTransport.links.get(0);
            otherLink.ready();
            assertEquals(BleConnectionManager.State.CONNECTED, states.get(states.size() - 1));
            assertEquals(DebugBleReconnectIntent.Result.STARTED, intent.startOrResume(ADDRESS, authorizeOriginal));
            originalTransport.scans.get(1).seen.accept(MATCH);
            var currentLink = originalTransport.links.get(1);
            assertTrue(currentLink.events.discovering());
            var currentTimeout = clock.onlyPendingCallback();
            var beforeOldTimeout = List.copyOf(states);

            oldTimeout.run();
            assertEquals(beforeOldTimeout, states);
            assertEquals(currentTimeout, clock.onlyPendingCallback());
            assertEquals(0, currentLink.closed);
            assertEquals(2, originalTransport.scans.size());
            assertEquals(2, originalTransport.links.size());

            clock.advance(14_999);
            assertEquals(0, currentLink.closed);
            clock.advance(1);
            assertEquals(BleConnectionManager.State.RETRYING, states.get(states.size() - 1));
            assertEquals(1, currentLink.closed);
            assertEquals(1, clock.pendingCount());
            clock.advance(999);
            assertEquals(2, originalTransport.links.size());
            clock.advance(1);
            assertEquals(2, originalTransport.links.size());
            assertEquals(3, originalTransport.scans.size());
            originalTransport.scans.get(2).seen.accept(MATCH);
            assertEquals(3, originalTransport.links.size());
            var retriedLink = originalTransport.links.get(2);
            retriedLink.ready();
            assertEquals(BleConnectionManager.State.CONNECTED, states.get(states.size() - 1));
            assertEquals(0, retriedLink.closed);
            assertEquals(1, firstLink.closed);
            assertEquals(1, otherLink.closed);
            assertEquals(3, originalTransport.scans.size());
            assertEquals(1, otherTransport.scans.size());
            assertEquals(0, clock.pendingCount());
        } finally {
            intent.pause();
        }
    }

    private static void assertExplicitStartWithReadLimit(String savedAddress, boolean granted, int readLimit,
            DebugBleReconnectIntent.Result expected, int expectedWrites) {
        var store = new MemoryStore();
        store.address = savedAddress;
        var transport = new FakeTransport();
        var clock = new ManualClock();
        var intent = labWithFakeTransport(store, transport, clock).reconnectIntent;
        store.readsBeforeFailure = readLimit;
        int readsBeforeStart = store.reads;
        try {
            assertEquals(expected, intent.startOrResume(ADDRESS, address -> granted && ADDRESS.equals(address)));
            assertEquals(readsBeforeStart + 1, store.reads);
            assertEquals(expectedWrites, store.writes);
            assertEquals(expectedWrites == 0 ? savedAddress : ADDRESS, store.address);
            clock.advance(60_000);
            assertEquals(expected == DebugBleReconnectIntent.Result.STARTED ? 1 : 0, transport.scans.size());
            assertTrue(transport.links.isEmpty());
            assertEquals(0, clock.pendingCount());
        } finally {
            intent.pause();
        }
    }

    private static DebugBleReconnectIntent controller(MemoryStore store, FakeTransport transport,
            List<BleConnectionManager.State> states) {
        return new DebugBleReconnectIntent(store, () -> true, (address, authorized) ->
                new DebugBleProbe(address, transport, new ManualClock(), states::add, authorized));
    }

    static final class MemoryStore implements DebugBleReconnectIntent.Store {
        private String address;
        private int writes;
        private boolean writeSucceeds = true;
        private RuntimeException readError;
        private int reads;
        private int readsBeforeFailure = Integer.MAX_VALUE;

        @Override
        public String readAddress() {
            reads++;
            if (readError != null) {
                throw readError;
            }
            if (readsBeforeFailure-- <= 0) {
                throw new IllegalStateException("Read limit reached");
            }
            return address;
        }

        @Override
        public boolean writeAddress(String address) {
            this.address = address;
            writes++;
            return writeSucceeds;
        }
    }

    @Test
    public void missingGattServiceStopsWithoutRetry() {
        FakeTransport transport = new FakeTransport();
        ManualClock clock = new ManualClock();
        var probe = new DebugBleProbe(ADDRESS, transport, clock, ignored -> {});
        try {
            probe.start(true);
            transport.scans.get(0).seen.accept(MATCH);
            Link first = transport.links.get(0);
            first.events.discovering();
            first.events.services(false);
            first.ready();
            first.events.lost();
            assertEquals(1, first.closed);
            assertEquals(1, transport.scans.size());
            assertTrue(clock.pending.isEmpty());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void silentGattDiscoveryRequiresFreshScanAndSuccessfulRecoveryResetsBackoff() {
        FakeTransport transport = new FakeTransport();
        ManualClock clock = new ManualClock();
        List<BleConnectionManager.State> states = new ArrayList<>();
        var session = new DebugBleReconnectIntent(new MemoryStore(), () -> true, (address, grant) ->
                new DebugBleProbe(address, transport, clock, states::add, grant));
        try {
            assertEquals(DebugBleReconnectIntent.Result.STARTED, session.start(ADDRESS, ADDRESS::equals));
            transport.scans.get(0).seen.accept(MATCH);
            Link first = transport.links.get(0);
            clock.advance(10_000);
            first.events.discovering();
            clock.advance(4_999);
            assertEquals(0, first.closed);
            assertEquals(BleConnectionManager.State.CONNECTING, states.get(states.size() - 1));

            clock.advance(1);
            assertEquals(1, first.closed);
            first.events.services(true);
            assertEquals(BleConnectionManager.State.RETRYING, states.get(states.size() - 1));
            clock.advance(999);
            assertEquals(1, transport.links.size());
            clock.advance(1);

            assertEquals(1, transport.links.size());
            assertEquals(2, transport.scans.size());
            transport.scans.get(1).seen.accept(MATCH);
            Link second = transport.links.get(1);
            second.events.discovering();
            clock.advance(15_000);
            assertEquals(1, second.closed);
            clock.advance(1_999);
            assertEquals(2, transport.links.size());
            clock.advance(1);

            assertEquals(2, transport.links.size());
            assertEquals(3, transport.scans.size());
            transport.scans.get(2).seen.accept(MATCH);
            Link replacement = transport.links.get(2);
            replacement.events.discovering();
            second.events.services(true);
            assertEquals(BleConnectionManager.State.CONNECTING, states.get(states.size() - 1));
            replacement.events.services(true);
            first.events.lost();
            second.events.lost();
            clock.advance(30_000);
            assertEquals(BleConnectionManager.State.CONNECTED, states.get(states.size() - 1));
            assertEquals(0, replacement.closed);
            assertEquals(3, transport.scans.size());
            assertEquals(3, transport.links.size());
            assertTrue(clock.pending.isEmpty());

            replacement.events.lost();
            replacement.events.lost();
            assertEquals(1, replacement.closed);
            assertEquals(BleConnectionManager.State.RETRYING, states.get(states.size() - 1));
            assertEquals(List.of(clock.nowMillis() + 1_000), new ArrayList<>(clock.pending.values()));
            clock.advance(999);
            assertEquals(3, transport.scans.size());
            assertEquals(3, transport.links.size());

            clock.advance(1);
            assertEquals(4, transport.scans.size());
            assertEquals(3, transport.links.size());
            Scan recovery = transport.scans.get(3);
            recovery.seen.accept(MATCH);
            recovery.seen.accept(MATCH);
            transport.links.get(3).ready();
            assertEquals(BleConnectionManager.State.CONNECTED, states.get(states.size() - 1));
            assertEquals(4, transport.links.size());
            assertTrue(clock.pending.isEmpty());
        } finally {
            session.pause();
        }
    }

    private record Scan(Consumer<DebugBleProbe.Sighting> seen, Consumer<BleConnectionManager.ScanOutcome> finished) {}

    private static final class FakeTransport implements DebugBleProbe.Transport {
        private final String target;
        private final List<Scan> scans = new ArrayList<>();
        private final List<Scan> stoppedScans = new ArrayList<>();
        private final List<Link> links = new ArrayList<>();

        FakeTransport() {
            this(ADDRESS);
        }

        FakeTransport(String target) {
            this.target = target;
        }

        @Override
        public Runnable scan(String address, Consumer<DebugBleProbe.Sighting> seen,
                Consumer<BleConnectionManager.ScanOutcome> finished) {
            assertEquals(target, address);
            var scan = new Scan(seen, finished);
            scans.add(scan);
            return () -> stoppedScans.add(scan);
        }

        @Override
        public Runnable open(String address, DebugBleProbe.LinkEvents events) {
            assertEquals(target, address);
            var link = new Link(events);
            links.add(link);
            return () -> link.closed++;
        }
    }

    private static final class Link {
        private final DebugBleProbe.LinkEvents events;
        private int closed;

        Link(DebugBleProbe.LinkEvents events) {
            this.events = events;
        }

        void ready() {
            events.discovering();
            events.services(true);
        }
    }

    static final class ManualClock implements BleConnectionManager.Scheduler {
        private final Map<Runnable, Long> pending = new LinkedHashMap<>();
        private long now;

        @Override
        public long nowMillis() {
            return now;
        }

        @Override
        public void postDelayed(Runnable callback, long delayMillis) {
            pending.put(callback, now + delayMillis);
        }

        @Override
        public void remove(Runnable callback) {
            pending.remove(callback);
        }

        int pendingCount() {
            return pending.size();
        }

        Runnable onlyPendingCallback() {
            assertEquals(1, pending.size());
            return pending.keySet().iterator().next();
        }

        void advance(long millis) {
            elapse(millis);
            for (var entry : new LinkedHashMap<>(pending).entrySet()) {
                if (entry.getValue() <= now && pending.remove(entry.getKey()) != null) {
                    entry.getKey().run();
                }
            }
        }

        void elapse(long millis) {
            now += millis;
        }
    }
}
