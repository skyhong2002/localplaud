package org.localplaud.android;

import static android.bluetooth.BluetoothGatt.GATT_SUCCESS;
import static android.bluetooth.BluetoothProfile.STATE_CONNECTED;
import static android.bluetooth.BluetoothProfile.STATE_DISCONNECTED;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.mockConstruction;
import static org.mockito.Mockito.mockStatic;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import android.Manifest;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCallback;
import android.bluetooth.BluetoothGattService;
import android.bluetooth.BluetoothManager;
import android.bluetooth.le.BluetoothLeScanner;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanFilter;
import android.bluetooth.le.ScanRecord;
import android.bluetooth.le.ScanResult;
import android.bluetooth.le.ScanSettings;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.os.Handler;
import android.os.ParcelUuid;
import android.os.SystemClock;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.List;
import java.util.Queue;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Consumer;
import java.util.function.Supplier;

import org.junit.Before;
import org.junit.Test;

public final class AndroidDebugBleTransportTest {
    private static final String ADDRESS = "AA:BB:CC:DD:EE:01";
    private final DebugBleProbe.Sighting sighting = new DebugBleProbe.Sighting(ADDRESS, DebugBleProbe.SERVICE);
    private final BluetoothGatt first = mock(BluetoothGatt.class);
    private final BluetoothGatt replacement = mock(BluetoothGatt.class);
    private final BluetoothGatt newlyAuthorized = mock(BluetoothGatt.class);
    private final Handler handler = mock(Handler.class);
    private final BluetoothAdapter adapter = mock(BluetoothAdapter.class);
    private final Context context = mock(Context.class);
    private final BluetoothDevice device = mock(BluetoothDevice.class);
    private final Queue<Runnable> handlerWork = new ArrayDeque<>();
    private final List<BluetoothGattCallback> callbacks = new ArrayList<>();
    private final List<Consumer<DebugBleProbe.Sighting>> scans = new ArrayList<>();
    private final List<Consumer<BleConnectionManager.ScanOutcome>> scanCompletions = new ArrayList<>();
    private final List<Runnable> scanCancellations = new ArrayList<>();
    private final List<BleConnectionManager.State> states = new ArrayList<>();
    private final DebugBleProbeTest.ManualClock clock = new DebugBleProbeTest.ManualClock();
    private final AtomicBoolean grant = new AtomicBoolean(true);
    private final AtomicReference<DebugBleProbe.Grant> grantGeneration =
            new AtomicReference<>(new DebugBleProbe.Grant());
    private final AtomicReference<BleConnectionManager.State> state = new AtomicReference<>();
    private DebugBleProbe.Transport transport;
    private DebugBleProbe probe;

    @Before
    public void setUp() {
        var service = mock(BluetoothGattService.class);
        when(context.checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT))
                .thenReturn(PackageManager.PERMISSION_GRANTED);
        when(context.checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN))
                .thenReturn(PackageManager.PERMISSION_GRANTED);
        when(handler.post(any(Runnable.class))).thenAnswer(call -> handlerWork.add(call.getArgument(0)));
        when(adapter.getRemoteDevice(ADDRESS)).thenReturn(device);
        when(device.connectGatt(eq(context), eq(false), any(BluetoothGattCallback.class),
                eq(BluetoothDevice.TRANSPORT_LE))).thenAnswer(call -> {
                    callbacks.add(call.getArgument(2));
                    return List.of(first, replacement, newlyAuthorized).get(callbacks.size() - 1);
                });
        when(first.discoverServices()).thenReturn(true);
        when(replacement.discoverServices()).thenReturn(true);
        when(newlyAuthorized.discoverServices()).thenReturn(true);
        when(first.getService(DebugBleProbe.SERVICE)).thenReturn(service);
        when(replacement.getService(DebugBleProbe.SERVICE)).thenReturn(service);
        when(newlyAuthorized.getService(DebugBleProbe.SERVICE)).thenReturn(service);
        var androidTransport = new AndroidDebugBleTransport(context, adapter, handler);
        transport = new DebugBleProbe.Transport() {
            @Override
            public Runnable scan(String target, Consumer<DebugBleProbe.Sighting> seen,
                    Consumer<BleConnectionManager.ScanOutcome> finished) {
                assertEquals(ADDRESS, target);
                scans.add(seen);
                scanCompletions.add(finished);
                var cancel = mock(Runnable.class);
                scanCancellations.add(cancel);
                return cancel;
            }

            @Override
            public Runnable open(String target, DebugBleProbe.LinkEvents events) {
                return androidTransport.open(target, events);
            }
        };
        probe = new DebugBleProbe(ADDRESS, transport, clock, next -> {
            states.add(next);
            state.set(next);
        }, () -> grant.get() ? grantGeneration.get() : null);
    }

    @Test
    public void duplicateConnectedAndClosedGattResultsDoNotInterfereWithReconnect() {
        try {
            probe.start(true);
            scans.get(0).accept(sighting);
            var oldCallback = callbacks.get(0);
            oldCallback.onConnectionStateChange(first, GATT_SUCCESS, STATE_CONNECTED);
            oldCallback.onServicesDiscovered(first, GATT_SUCCESS);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.CONNECTED, state.get());

            oldCallback.onConnectionStateChange(first, GATT_SUCCESS, STATE_DISCONNECTED);
            oldCallback.onServicesDiscovered(first, GATT_SUCCESS);
            drain(handlerWork);
            verify(first).close();
            assertEquals(BleConnectionManager.State.RETRYING, state.get());
            clock.advance(1_000);
            scans.get(1).accept(sighting);
            assertEquals(2, callbacks.size());
            var currentCallback = callbacks.get(1);
            currentCallback.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
            currentCallback.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
            oldCallback.onServicesDiscovered(first, GATT_SUCCESS);
            currentCallback.onServicesDiscovered(first, GATT_SUCCESS);
            drain(handlerWork);
            verify(replacement).discoverServices();
            verify(first).getService(DebugBleProbe.SERVICE);
            verify(replacement, never()).getService(DebugBleProbe.SERVICE);
            assertEquals(BleConnectionManager.State.CONNECTING, state.get());

            currentCallback.onServicesDiscovered(replacement, GATT_SUCCESS);
            oldCallback.onServicesDiscovered(first, BluetoothGatt.GATT_FAILURE);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.CONNECTED, state.get());
            verify(replacement).getService(DebugBleProbe.SERVICE);
            verify(replacement, never()).close();

            grant.set(false);
            currentCallback.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_DISCONNECTED);
            drain(handlerWork);
            clock.advance(30_000);
            assertEquals(BleConnectionManager.State.UNAUTHORIZED, state.get());
            assertEquals(2, scans.size());
            assertEquals(2, callbacks.size());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void rejectedDiscoveryStartClosesImmediatelyAndRetriesOnlyOnce() {
        when(first.discoverServices()).thenReturn(false);
        try {
            probe.start(true);
            scans.get(0).accept(sighting);
            var oldCallback = callbacks.get(0);
            oldCallback.onConnectionStateChange(first, GATT_SUCCESS, STATE_CONNECTED);
            oldCallback.onConnectionStateChange(first, GATT_SUCCESS, STATE_CONNECTED);
            oldCallback.onServicesDiscovered(first, GATT_SUCCESS);
            drain(handlerWork);

            verify(first).discoverServices();
            verify(first).disconnect();
            verify(first).close();
            verify(first, never()).getService(DebugBleProbe.SERVICE);
            assertEquals(BleConnectionManager.State.RETRYING, state.get());
            assertEquals(1, clock.pendingCount());
            clock.advance(999);
            assertEquals(1, callbacks.size());

            clock.advance(1);
            assertEquals(2, callbacks.size());
            var currentCallback = callbacks.get(1);
            currentCallback.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
            currentCallback.onServicesDiscovered(replacement, GATT_SUCCESS);
            drain(handlerWork);
            clock.advance(15_000);
            assertEquals(BleConnectionManager.State.CONNECTED, state.get());
            assertEquals(2, callbacks.size());
            assertEquals(1, scans.size());
            assertEquals(0, clock.pendingCount());
            verify(replacement, never()).close();
        } finally {
            probe.stop();
        }
    }

    @Test
    public void rejectedDiscoveryStartDoesNotRetryAfterGrantRevocation() {
        when(first.discoverServices()).thenReturn(false);
        try {
            probe.start(true);
            scans.get(0).accept(sighting);
            callbacks.get(0).onConnectionStateChange(first, GATT_SUCCESS, STATE_CONNECTED);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.RETRYING, state.get());

            grant.set(false);
            clock.advance(1_000);
            assertEquals(BleConnectionManager.State.UNAUTHORIZED, state.get());
            assertEquals(1, callbacks.size());
            assertEquals(1, scans.size());
            assertEquals(0, clock.pendingCount());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void gattDiscoveryFailureClosesImmediatelyAndRetriesOnlyOnce() {
        try {
            probe.start(true);
            scans.get(0).accept(sighting);
            var failedCallback = callbacks.get(0);
            failedCallback.onConnectionStateChange(first, GATT_SUCCESS, STATE_CONNECTED);
            failedCallback.onServicesDiscovered(first, BluetoothGatt.GATT_FAILURE);
            failedCallback.onServicesDiscovered(first, BluetoothGatt.GATT_FAILURE);
            drain(handlerWork);

            verify(first).disconnect();
            verify(first).close();
            verify(first, never()).getService(DebugBleProbe.SERVICE);
            assertEquals(BleConnectionManager.State.RETRYING, state.get());
            assertEquals(1, clock.pendingCount());
            clock.advance(999);
            assertEquals(1, callbacks.size());

            clock.advance(1);
            assertEquals(2, callbacks.size());
            var currentCallback = callbacks.get(1);
            currentCallback.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
            failedCallback.onServicesDiscovered(first, GATT_SUCCESS);
            currentCallback.onServicesDiscovered(replacement, GATT_SUCCESS);
            currentCallback.onServicesDiscovered(replacement, BluetoothGatt.GATT_FAILURE);
            drain(handlerWork);
            verify(first, never()).getService(DebugBleProbe.SERVICE);
            verify(replacement, never()).close();
            assertEquals(BleConnectionManager.State.CONNECTED, state.get());
            assertEquals(1, scans.size());
            assertEquals(0, clock.pendingCount());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void successfulGattDiscoveryWithoutTestServiceRejectsWithoutRetry() {
        when(first.getService(DebugBleProbe.SERVICE)).thenReturn(null);
        try {
            probe.start(true);
            scans.get(0).accept(sighting);
            var callback = callbacks.get(0);
            callback.onConnectionStateChange(first, GATT_SUCCESS, STATE_CONNECTED);
            callback.onServicesDiscovered(first, GATT_SUCCESS);
            drain(handlerWork);

            verify(first).getService(DebugBleProbe.SERVICE);
            verify(first).close();
            assertEquals(BleConnectionManager.State.UNAUTHORIZED, state.get());
            assertEquals(0, clock.pendingCount());
            clock.advance(30_000);
            assertEquals(1, callbacks.size());
            assertEquals(1, scans.size());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void missingServiceDuringRecoveryRequiresExplicitRestartWhenServiceReturns() {
        when(replacement.getService(DebugBleProbe.SERVICE)).thenReturn(null);
        var session = new DebugBleReconnectIntent(new DebugBleProbeTest.MemoryStore(), () -> true,
                (address, authorized) -> new DebugBleProbe(address, transport, clock, state::set, authorized));
        try {
            assertEquals(DebugBleReconnectIntent.Result.STARTED, session.startOrResume(ADDRESS, ADDRESS::equals));
            scans.get(0).accept(sighting);
            completeLabGatt(0, first);
            callbacks.get(0).onConnectionStateChange(first, GATT_SUCCESS, STATE_DISCONNECTED);
            drain(handlerWork);
            clock.advance(1_000);
            scans.get(1).accept(sighting);
            var rejected = callbacks.get(1);
            rejected.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
            rejected.onServicesDiscovered(replacement, GATT_SUCCESS);
            drain(handlerWork);
            verify(replacement).close();
            assertEquals(BleConnectionManager.State.UNAUTHORIZED, state.get());
            assertEquals(0, clock.pendingCount());

            scans.get(1).accept(sighting);
            clock.advance(60_000);
            assertEquals(2, scans.size());
            assertEquals(2, callbacks.size());
            assertEquals(BleConnectionManager.State.UNAUTHORIZED, state.get());

            assertEquals(DebugBleReconnectIntent.Result.STARTED, session.startOrResume(ADDRESS, ADDRESS::equals));
            assertEquals(3, scans.size());
            scans.get(2).accept(sighting);
            scans.get(2).accept(sighting);
            assertEquals(3, callbacks.size());
            completeLabGatt(2, newlyAuthorized);
            assertEquals(0, clock.pendingCount());
        } finally {
            session.pause();
        }
    }

    @Test
    public void discoverySecurityExceptionClosesWithoutRetryAndRequiresFreshAuthorization() {
        when(first.discoverServices()).thenThrow(new SecurityException());
        doThrow(new SecurityException()).when(first).disconnect();
        var session = new DebugBleReconnectIntent(new DebugBleProbeTest.MemoryStore(), () -> true,
                (address, authorized) -> new DebugBleProbe(address, transport, clock, state::set, authorized));
        try {
            assertEquals(DebugBleReconnectIntent.Result.STARTED, session.start(ADDRESS, ADDRESS::equals));
            scans.get(0).accept(sighting);
            var oldCallback = callbacks.get(0);
            oldCallback.onConnectionStateChange(first, GATT_SUCCESS, STATE_CONNECTED);
            oldCallback.onConnectionStateChange(first, GATT_SUCCESS, STATE_CONNECTED);
            oldCallback.onServicesDiscovered(first, GATT_SUCCESS);
            drain(handlerWork);

            verify(first).discoverServices();
            verify(first).close();
            verify(first, never()).getService(DebugBleProbe.SERVICE);
            assertEquals(BleConnectionManager.State.BLUETOOTH_PERMISSION_DENIED, state.get());
            assertEquals(R.string.plaud_connection_bluetooth_permission_denied, BleDeviceStatus.message(state.get()));
            assertEquals(0, clock.pendingCount());

            session.adapterChanged(false);
            session.adapterChanged(true);
            clock.advance(30_000);
            assertEquals(BleConnectionManager.State.BLUETOOTH_PERMISSION_DENIED, state.get());
            assertEquals(1, scans.size());
            assertEquals(1, callbacks.size());
            assertEquals(DebugBleReconnectIntent.Result.NOT_AUTHORIZED, session.resume(ignored -> false));
            assertEquals(1, scans.size());

            assertEquals(DebugBleReconnectIntent.Result.STARTED, session.resume(ADDRESS::equals));
            scans.get(1).accept(sighting);
            var currentCallback = callbacks.get(1);
            currentCallback.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
            currentCallback.onServicesDiscovered(replacement, GATT_SUCCESS);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.CONNECTED, state.get());
            assertEquals(0, clock.pendingCount());
        } finally {
            session.pause();
        }
    }

    @Test
    public void queuedServiceChangesCoalesceIntoOneRevalidation() {
        when(first.discoverServices()).thenReturn(true).thenAnswer(call -> {
            assertEquals(BleConnectionManager.State.REVALIDATING_SERVICES, state.get());
            return true;
        });
        try {
            var callback = startReadyLink();
            callback.onServiceChanged(first);
            callback.onServiceChanged(first);
            callback.onConnectionStateChange(first, GATT_SUCCESS, STATE_CONNECTED);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.REVALIDATING_SERVICES, state.get());
            assertEquals(1, clock.pendingCount());
            verify(first, times(2)).discoverServices();

            callback.onServicesDiscovered(first, GATT_SUCCESS);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.CONNECTED, state.get());
            assertEquals(0, clock.pendingCount());
            assertEquals(1, scans.size());
            assertEquals(1, callbacks.size());
            verify(first, never()).close();
        } finally {
            probe.stop();
        }
    }

    @Test
    public void newServiceChangeDuringRevalidationClosesLinkAndRecoversThroughFreshScan() {
        try {
            var callback = startReadyLink();
            callback.onServiceChanged(first);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.REVALIDATING_SERVICES, state.get());
            callback.onServiceChanged(first);
            callback.onServiceChanged(first);
            callback.onServicesDiscovered(first, GATT_SUCCESS);
            drain(handlerWork);

            verify(first, times(2)).discoverServices();
            verify(first).getService(DebugBleProbe.SERVICE);
            verify(first).close();
            assertEquals(BleConnectionManager.State.RETRYING, state.get());
            assertEquals(1, clock.pendingCount());
            clock.advance(999);
            assertEquals(1, scans.size());
            clock.advance(1);
            assertEquals(2, scans.size());
            assertEquals(1, callbacks.size());

            scans.get(1).accept(sighting);
            scans.get(1).accept(sighting);
            assertEquals(2, callbacks.size());
            var currentCallback = callbacks.get(1);
            currentCallback.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
            callback.onServicesDiscovered(first, GATT_SUCCESS);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.CONNECTING, state.get());
            currentCallback.onServicesDiscovered(replacement, GATT_SUCCESS);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.CONNECTED, state.get());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void closedGattServiceChangesDoNotInvalidateReplacementRevalidation() {
        try {
            var oldCallback = startReadyLink();
            oldCallback.onConnectionStateChange(first, GATT_SUCCESS, STATE_DISCONNECTED);
            oldCallback.onServiceChanged(first);
            handlerWork.remove().run();
            Runnable queuedOldChange = handlerWork.remove();
            verify(first).close();
            assertEquals(BleConnectionManager.State.RETRYING, state.get());

            clock.advance(1_000);
            scans.get(1).accept(sighting);
            var currentCallback = callbacks.get(1);
            currentCallback.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
            currentCallback.onServicesDiscovered(replacement, GATT_SUCCESS);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.CONNECTED, state.get());

            currentCallback.onServiceChanged(replacement);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.REVALIDATING_SERVICES, state.get());
            currentCallback.onServicesDiscovered(replacement, GATT_SUCCESS);
            oldCallback.onServiceChanged(first);
            currentCallback.onServiceChanged(first);
            assertEquals(1, handlerWork.size());
            queuedOldChange.run();
            assertEquals(BleConnectionManager.State.REVALIDATING_SERVICES, state.get());

            drain(handlerWork);
            assertEquals(BleConnectionManager.State.CONNECTED, state.get());
            verify(first).discoverServices();
            verify(replacement, times(2)).discoverServices();
            verify(replacement, never()).close();
            assertEquals(0, clock.pendingCount());
            clock.advance(30_000);
            assertEquals(2, scans.size());
            assertEquals(2, callbacks.size());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void queuedRevalidationResultCannotPublishReadinessAfterANewServiceChange() {
        try {
            var callback = startReadyLink();
            callback.onServiceChanged(first);
            drain(handlerWork);
            callback.onServicesDiscovered(first, GATT_SUCCESS);
            callback.onServiceChanged(first);
            handlerWork.remove().run();
            assertEquals(BleConnectionManager.State.RETRYING, state.get());
            drain(handlerWork);
            verify(first).getService(DebugBleProbe.SERVICE);
            verify(first).close();
            assertEquals(1, clock.pendingCount());

            grant.set(false);
            clock.advance(1_000);
            assertEquals(BleConnectionManager.State.UNAUTHORIZED, state.get());
            assertEquals(1, scans.size());
            assertEquals(1, callbacks.size());
            assertEquals(0, clock.pendingCount());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void serviceChangeDuringServiceLookupStillInvalidatesReadiness() {
        try {
            var callback = startReadyLink();
            callback.onServiceChanged(first);
            drain(handlerWork);
            var service = mock(BluetoothGattService.class);
            when(first.getService(DebugBleProbe.SERVICE)).thenAnswer(call -> {
                callback.onServiceChanged(first);
                return service;
            });

            callback.onServicesDiscovered(first, GATT_SUCCESS);
            handlerWork.remove().run();
            assertEquals(BleConnectionManager.State.RETRYING, state.get());
            drain(handlerWork);
            verify(first, times(2)).discoverServices();
            verify(first).close();
            assertEquals(1, clock.pendingCount());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void serviceChangeDoesNotRediscoverAfterGrantRevocation() {
        try {
            var callback = startReadyLink();
            grant.set(false);
            callback.onServiceChanged(first);
            callback.onServiceChanged(first);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.UNAUTHORIZED, state.get());
            verify(first).discoverServices();
            verify(first).close();
            assertEquals(0, clock.pendingCount());
            assertEquals(1, callbacks.size());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void serviceChangeCannotRestoreReadinessIfGrantIsRevokedDuringDiscovery() {
        try {
            var callback = startReadyLink();
            callback.onServiceChanged(first);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.REVALIDATING_SERVICES, state.get());

            grant.set(false);
            callback.onServicesDiscovered(first, GATT_SUCCESS);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.UNAUTHORIZED, state.get());
            verify(first).close();
            assertEquals(0, clock.pendingCount());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void lateSuccessFromTimedOutRecoveryGattDoesNotInterruptActiveRescan() {
        assertRecoveryGattTimeoutDoesNotInterruptRescan(false);
    }

    @Test
    public void recoveryGattSuccessAtDeadlineCannotBecomeReadyOrDuplicateRescan() {
        assertRecoveryGattTimeoutDoesNotInterruptRescan(true);
    }

    @Test
    public void retiredRecoveryScanFailuresDoNotAffectReadyGatt() {
        try {
            var original = startReadyLink();
            original.onConnectionStateChange(first, GATT_SUCCESS, STATE_DISCONNECTED);
            drain(handlerWork);
            clock.advance(1_000);
            scans.get(1).accept(sighting);
            callbacks.get(1).onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
            drain(handlerWork);
            verify(replacement).discoverServices();
            clock.advance(15_000);
            verify(replacement).close();

            clock.advance(2_000);
            scans.get(2).accept(sighting);
            var fresh = callbacks.get(2);
            fresh.onConnectionStateChange(newlyAuthorized, GATT_SUCCESS, STATE_CONNECTED);
            fresh.onServicesDiscovered(newlyAuthorized, GATT_SUCCESS);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.CONNECTED, state.get());
            states.clear();

            scanCompletions.get(1).accept(BleConnectionManager.ScanOutcome.FAILED);
            scanCompletions.get(2).accept(BleConnectionManager.ScanOutcome.THROTTLED);
            drain(handlerWork);
            assertEquals(0, clock.pendingCount());
            clock.advance(30_000);

            assertEquals(List.of(), states);
            assertEquals(BleConnectionManager.State.CONNECTED, state.get());
            verify(newlyAuthorized, never()).close();
            verify(scanCancellations.get(1)).run();
            verify(scanCancellations.get(2)).run();
            assertEquals(3, scans.size());
            assertEquals(3, callbacks.size());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void retiredScanThrottleDoesNotInterruptActiveRescan() {
        try {
            var original = startReadyLink();
            original.onConnectionStateChange(first, GATT_SUCCESS, STATE_DISCONNECTED);
            drain(handlerWork);
            clock.advance(1_000);
            var retiredScan = scanCompletions.get(1);
            retiredScan.accept(BleConnectionManager.ScanOutcome.TIMED_OUT);
            assertEquals(BleConnectionManager.State.WAITING_TO_RESCAN, state.get());
            clock.advance(2_000);
            assertEquals(BleConnectionManager.State.WAITING_FOR_DEVICE, state.get());
            assertEquals(3, scans.size());
            var cancelRescan = scanCancellations.get(2);
            states.clear();

            retiredScan.accept(BleConnectionManager.ScanOutcome.THROTTLED);
            drain(handlerWork);

            assertEquals(List.of(), states);
            assertEquals(0, clock.pendingCount());
            assertEquals(1, callbacks.size());
            verify(scanCancellations.get(1)).run();
            verify(cancelRescan, never()).run();

            scans.get(2).accept(sighting);
            verify(cancelRescan).run();
            var fresh = callbacks.get(1);
            fresh.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
            fresh.onServicesDiscovered(replacement, GATT_SUCCESS);
            drain(handlerWork);
            clock.advance(30_000);

            assertEquals(BleConnectionManager.State.CONNECTED, state.get());
            verify(replacement).discoverServices();
            verify(replacement, never()).close();
            assertEquals(0, clock.pendingCount());
            assertEquals(3, scans.size());
            assertEquals(2, callbacks.size());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void queuedAndroidScanResultIsNotDeliveredAfterRecoveryScanRestarts() {
        assertOnlyFreshAndroidScanResultReconnects(ScanDelivery.RETIRED_SINGLE);
    }

    @Test
    public void recoveryScanRejectsEarlierTimestampAndAcceptsExactStartTime() {
        assertOnlyFreshAndroidScanResultReconnects(ScanDelivery.ACTIVE_SINGLE);
    }

    @Test
    public void recoveryBatchRejectsStaleResultsAndReconnectsOnlyOnce() {
        assertOnlyFreshAndroidScanResultReconnects(ScanDelivery.ACTIVE_BATCH);
    }

    @Test
    public void queuedBatchFromRetiredScanIsNotDeliveredToRecovery() {
        assertOnlyFreshAndroidScanResultReconnects(ScanDelivery.RETIRED_BATCH);
    }

    @Test
    public void queuedRecoveryBatchCannotConnectAfterGrantIsRevoked() {
        assertOnlyFreshAndroidScanResultReconnects(ScanDelivery.REVOKED_BATCH);
    }

    @Test
    public void queuedAndroidScanFailureCannotInterruptActiveRecoveryScan() {
        assertOnlyFreshAndroidScanResultReconnects(ScanDelivery.RETIRED_FAILURE);
    }

    @Test
    public void recoveryBatchDeliveredBeforeThrottleConnectsOnceWithoutBackoff() {
        assertOnlyFreshAndroidScanResultReconnects(ScanDelivery.BATCH_BEFORE_THROTTLE);
    }

    @Test
    public void recoveryThrottleDeliveredBeforeBatchBacksOffOnceWithoutConnecting() {
        assertOnlyFreshAndroidScanResultReconnects(ScanDelivery.THROTTLE_BEFORE_BATCH);
    }

    @Test
    public void radioCycleReplacesThrottleBackoffUnderOriginalGrant() {
        assertOnlyFreshAndroidScanResultReconnects(ScanDelivery.THROTTLE_BEFORE_BATCH, RadioGrant.UNCHANGED);
    }

    @Test
    public void radioCycleCannotResumeThrottleBackoffAfterGrantRevoked() {
        assertOnlyFreshAndroidScanResultReconnects(ScanDelivery.THROTTLE_BEFORE_BATCH, RadioGrant.REVOKED);
    }

    @Test
    public void radioCycleCannotResumeThrottleBackoffWithReissuedGrant() {
        assertOnlyFreshAndroidScanResultReconnects(ScanDelivery.THROTTLE_BEFORE_BATCH, RadioGrant.REISSUED);
    }

    @Test
    public void duplicateRadioOnKeepsThrottleDeadlineAndScanBudget() {
        assertOnlyFreshAndroidScanResultReconnects(ScanDelivery.THROTTLE_DUPLICATE_ON, RadioGrant.UNCHANGED);
    }

    @Test
    public void duplicateRadioOnCannotResumeThrottleBackoffAfterGrantRevoked() {
        assertOnlyFreshAndroidScanResultReconnects(ScanDelivery.THROTTLE_DUPLICATE_ON, RadioGrant.REVOKED);
    }

    @Test
    public void duplicateRadioOnCannotResumeThrottleBackoffWithReissuedGrant() {
        assertOnlyFreshAndroidScanResultReconnects(ScanDelivery.THROTTLE_DUPLICATE_ON, RadioGrant.REISSUED);
    }

    @Test
    public void nearbyPermissionLossCancelsThrottleBackoffDespiteValidDeviceGrant() {
        assertOnlyFreshAndroidScanResultReconnects(ScanDelivery.THROTTLE_PERMISSION_LOST);
    }

    @Test
    public void queuedRecoveryResultCannotOpenGattAfterConnectPermissionIsRevoked() {
        assertOnlyFreshAndroidScanResultReconnects(ScanDelivery.CONNECT_PERMISSION_LOST);
    }

    @Test
    public void connectGattPermissionRejectionAfterPrecheckStopsRecovery() {
        try {
            var original = startReadyLink();
            original.onConnectionStateChange(first, GATT_SUCCESS, STATE_DISCONNECTED);
            drain(handlerWork);
            clock.advance(1_000);
            assertEquals(BleConnectionManager.State.WAITING_FOR_DEVICE, state.get());
            var rejectedCallback = new AtomicReference<BluetoothGattCallback>();
            doAnswer(call -> {
                rejectedCallback.set(call.getArgument(2));
                throw new SecurityException("Connect permission revoked after precheck");
            }).when(device).connectGatt(eq(context), eq(false), any(BluetoothGattCallback.class),
                    eq(BluetoothDevice.TRANSPORT_LE));

            scans.get(1).accept(sighting);

            verify(context, times(2)).checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT);
            verify(device, times(2)).connectGatt(eq(context), eq(false), any(BluetoothGattCallback.class),
                    eq(BluetoothDevice.TRANSPORT_LE));
            assertTrue(probe.permissionDenied());
            assertEquals(R.string.plaud_connection_bluetooth_permission_denied, BleDeviceStatus.message(state.get()));
            verify(scanCancellations.get(1)).run();
            verify(first).close();
            assertEquals(0, clock.pendingCount());

            rejectedCallback.get().onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
            rejectedCallback.get().onServicesDiscovered(replacement, GATT_SUCCESS);
            drain(handlerWork);
            clock.advance(60_000);
            verify(replacement, never()).discoverServices();
            assertTrue(probe.permissionDenied());
            assertEquals(2, scans.size());
            assertEquals(1, callbacks.size());
            assertEquals(0, clock.pendingCount());
        } finally {
            probe.stop();
        }
    }

    private enum ScanDelivery {
        RETIRED_SINGLE, ACTIVE_SINGLE, ACTIVE_BATCH, RETIRED_BATCH, REVOKED_BATCH, RETIRED_FAILURE,
        BATCH_BEFORE_THROTTLE, THROTTLE_BEFORE_BATCH, THROTTLE_DUPLICATE_ON, THROTTLE_PERMISSION_LOST,
        CONNECT_PERMISSION_LOST, SYNCHRONOUS_RADIO_RESULT
    }

    private void assertOnlyFreshAndroidScanResultReconnects(ScanDelivery delivery) {
        assertOnlyFreshAndroidScanResultReconnects(delivery, null);
    }

    private void assertOnlyFreshAndroidScanResultReconnects(ScanDelivery delivery, RadioGrant radioGrant) {
        boolean fromRetiredCallback = delivery == ScanDelivery.RETIRED_SINGLE || delivery == ScanDelivery.RETIRED_BATCH;
        var scanner = mock(BluetoothLeScanner.class);
        var scanCallbacks = new ArrayList<ScanCallback>();
        var scanTimeouts = new ArrayList<Runnable>();
        var delivered = new ArrayList<ScanResult>();
        var failures = new ArrayList<BleConnectionManager.ScanOutcome>();
        doAnswer(call -> {
            scanCallbacks.add(call.getArgument(2));
            return null;
        }).when(scanner).startScan(anyList(), any(ScanSettings.class), any(ScanCallback.class));
        when(handler.postDelayed(any(Runnable.class), eq(BleConnectionManager.SCAN_WINDOW_MS)))
                .thenAnswer(call -> scanTimeouts.add(call.getArgument(0)));
        var discovery = new AndroidBleDiscovery(scanner, handler);
        var platformTransport = new DebugBleProbe.Transport() {
            @Override
            public Runnable scan(String target, Consumer<DebugBleProbe.Sighting> seen,
                    Consumer<BleConnectionManager.ScanOutcome> finished) {
                assertEquals(ADDRESS, target);
                discovery.start(result -> {
                    delivered.add(result);
                    seen.accept(sighting);
                }, () -> finished.accept(BleConnectionManager.ScanOutcome.TIMED_OUT), failure -> {
                    failures.add(failure);
                    finished.accept(failure);
                });
                return discovery::stop;
            }

            @Override
            public Runnable open(String target, DebugBleProbe.LinkEvents events) {
                return transport.open(target, events);
            }
        };
        var platformProbe = new DebugBleProbe(ADDRESS, platformTransport, clock, state::set, grantGeneration::get);
        var nearbyPermission = new AtomicBoolean(true);
        var permissionSession = new DebugBleReconnectIntent(new DebugBleProbeTest.MemoryStore(),
                nearbyPermission::get, (address, authorized) ->
                        new DebugBleProbe(address, platformTransport, clock, state::set, authorized));
        var initial = mock(ScanResult.class);
        var stale = mock(ScanResult.class);
        var fresh = mock(ScanResult.class);
        when(initial.getTimestampNanos()).thenReturn(100L);
        long rescanStartedAtNanos = 300L;
        when(stale.getTimestampNanos()).thenReturn(
                fromRetiredCallback ? rescanStartedAtNanos : rescanStartedAtNanos - 1);
        when(fresh.getTimestampNanos()).thenReturn(rescanStartedAtNanos);

        try (var systemClock = mockStatic(SystemClock.class);
                var settings = mockConstruction(ScanSettings.Builder.class, (builder, context) -> {
                    when(builder.setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)).thenReturn(builder);
                    when(builder.build()).thenReturn(mock(ScanSettings.class));
                })) {
            systemClock.when(SystemClock::elapsedRealtimeNanos).thenReturn(100L, 200L, rescanStartedAtNanos);
            if (delivery == ScanDelivery.THROTTLE_PERMISSION_LOST) {
                assertEquals(DebugBleReconnectIntent.Result.STARTED,
                        permissionSession.start(ADDRESS, address -> ADDRESS.equals(address) && grant.get()));
            } else {
                platformProbe.start(true);
            }
            scanCallbacks.get(0).onScanResult(ScanSettings.CALLBACK_TYPE_ALL_MATCHES, initial);
            drain(handlerWork);
            var original = callbacks.get(0);
            original.onConnectionStateChange(first, GATT_SUCCESS, STATE_CONNECTED);
            original.onServicesDiscovered(first, GATT_SUCCESS);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.CONNECTED, state.get());

            if (delivery == ScanDelivery.SYNCHRONOUS_RADIO_RESULT) {
                var receiver = bluetoothStateReceiver(platformProbe::adapterChanged);
                try (var filters = mockConstruction(IntentFilter.class)) {
                    receiver.start();
                    sendBluetoothState(receiver, BluetoothAdapter.STATE_OFF);
                    assertEquals(BleConnectionManager.State.BLUETOOTH_OFF, state.get());
                    verify(first).close();
                    when(handler.post(any(Runnable.class))).thenAnswer(call -> {
                        call.<Runnable>getArgument(0).run();
                        return true;
                    });
                    doAnswer(call -> {
                        ScanCallback resumed = call.getArgument(2);
                        scanCallbacks.add(resumed);
                        resumed.onScanResult(ScanSettings.CALLBACK_TYPE_ALL_MATCHES, fresh);
                        resumed.onScanResult(ScanSettings.CALLBACK_TYPE_ALL_MATCHES, fresh);
                        assertEquals(2, callbacks.size());
                        return null;
                    }).when(scanner).startScan(anyList(), any(ScanSettings.class), any(ScanCallback.class));
                    sendBluetoothState(receiver, BluetoothAdapter.STATE_ON);
                    sendBluetoothState(receiver, BluetoothAdapter.STATE_ON);

                    assertEquals(2, scanCallbacks.size());
                    assertEquals(2, callbacks.size());
                    verify(scanner).stopScan(scanCallbacks.get(1));
                    var scanDeadline = scanTimeouts.get(1);
                    verify(handler).removeCallbacks(scanDeadline);
                    var recovered = callbacks.get(1);
                    recovered.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
                    recovered.onServicesDiscovered(replacement, GATT_SUCCESS);
                    scanDeadline.run();
                    clock.advance(BleConnectionManager.SCAN_WINDOW_MS);
                    assertEquals(BleConnectionManager.State.CONNECTED, state.get());
                    assertEquals(2, scanCallbacks.size());
                    assertEquals(2, callbacks.size());
                    assertEquals(0, clock.pendingCount());
                } finally {
                    receiver.stop();
                }
                return;
            }

            original.onConnectionStateChange(first, GATT_SUCCESS, STATE_DISCONNECTED);
            drain(handlerWork);
            clock.advance(1_000);
            var oldScan = scanCallbacks.get(1);
            switch (delivery) {
                case RETIRED_BATCH -> oldScan.onBatchScanResults(List.of(stale));
                case RETIRED_SINGLE -> oldScan.onScanResult(ScanSettings.CALLBACK_TYPE_ALL_MATCHES, stale);
                case RETIRED_FAILURE -> oldScan.onScanFailed(ScanCallback.SCAN_FAILED_SCANNING_TOO_FREQUENTLY);
                default -> { }
            }
            assertEquals(fromRetiredCallback || delivery == ScanDelivery.RETIRED_FAILURE ? 1 : 0, handlerWork.size());
            scanTimeouts.get(1).run();
            clock.advance(2_000);
            assertEquals(3, scanCallbacks.size());
            var newScan = scanCallbacks.get(2);
            verify(scanner).stopScan(oldScan);

            if (delivery == ScanDelivery.ACTIVE_SINGLE) {
                newScan.onScanResult(ScanSettings.CALLBACK_TYPE_ALL_MATCHES, stale);
                assertEquals(1, handlerWork.size());
            }

            drain(handlerWork);
            assertEquals(List.of(initial), delivered);
            assertEquals(List.of(), failures);
            assertEquals(1, callbacks.size());
            assertEquals(BleConnectionManager.State.WAITING_FOR_DEVICE, state.get());
            assertEquals(0, clock.pendingCount());
            verify(scanner, never()).stopScan(newScan);

            if (delivery == ScanDelivery.THROTTLE_BEFORE_BATCH || delivery == ScanDelivery.THROTTLE_DUPLICATE_ON
                    || delivery == ScanDelivery.THROTTLE_PERMISSION_LOST) {
                newScan.onScanFailed(ScanCallback.SCAN_FAILED_SCANNING_TOO_FREQUENTLY);
                newScan.onBatchScanResults(List.of(fresh, fresh));
                assertEquals(2, handlerWork.size());
                handlerWork.remove().run();
                assertEquals(BleConnectionManager.State.WAITING_TO_RESCAN, state.get());
                var retry = clock.onlyPendingCallback();
                drain(handlerWork);

                assertEquals(List.of(initial), delivered);
                assertEquals(List.of(BleConnectionManager.ScanOutcome.THROTTLED), failures);
                assertEquals(1, callbacks.size());
                assertEquals(BleConnectionManager.State.WAITING_TO_RESCAN, state.get());
                assertEquals(retry, clock.onlyPendingCallback());
                verify(scanner).stopScan(newScan);
                if (delivery == ScanDelivery.THROTTLE_PERMISSION_LOST) {
                    clock.advance(10_000);
                    nearbyPermission.set(false);
                    assertTrue(permissionSession.invalidateIfPermissionLost());
                    assertFalse(permissionSession.hasActiveSession());
                    assertEquals(BleConnectionManager.State.STOPPED, state.get());
                    assertEquals(0, clock.pendingCount());

                    clock.advance(20_000);
                    retry.run();
                    assertEquals(DebugBleReconnectIntent.Result.NO_INTENT,
                            permissionSession.resume(address -> ADDRESS.equals(address) && grant.get()));
                    assertTrue(grant.get());
                    nearbyPermission.set(true);
                    clock.advance(30_000);
                    assertEquals(3, scanCallbacks.size());
                    assertEquals(1, callbacks.size());
                    assertEquals(0, clock.pendingCount());
                    return;
                }
                if (delivery == ScanDelivery.THROTTLE_DUPLICATE_ON) {
                    clock.advance(10_000);
                    platformProbe.adapterChanged(true);
                    assertEquals(retry, clock.onlyPendingCallback());
                    if (radioGrant == RadioGrant.REVOKED) {
                        grantGeneration.set(null);
                    } else if (radioGrant == RadioGrant.REISSUED) {
                        grantGeneration.set(new DebugBleProbe.Grant());
                    }
                    clock.advance(19_999);
                    platformProbe.adapterChanged(true);
                    assertEquals(retry, clock.onlyPendingCallback());
                    assertEquals(BleConnectionManager.State.WAITING_TO_RESCAN, state.get());
                    assertEquals(3, scanCallbacks.size());

                    clock.advance(1);
                    assertEquals(0, clock.pendingCount());
                    assertEquals(1, callbacks.size());
                    if (radioGrant == RadioGrant.UNCHANGED) {
                        assertEquals(4, scanCallbacks.size());
                        assertEquals(BleConnectionManager.State.WAITING_FOR_DEVICE, state.get());
                        scanTimeouts.get(3).run();
                        assertEquals(BleConnectionManager.State.RESCAN_COOLDOWN, state.get());
                        clock.advance(30_000);
                        assertEquals(BleConnectionManager.State.RESCAN_READY, state.get());
                        assertEquals(4, scanCallbacks.size());
                    } else {
                        assertEquals(3, scanCallbacks.size());
                        assertEquals(BleConnectionManager.State.UNAUTHORIZED, state.get());
                    }
                    assertEquals(0, clock.pendingCount());
                    return;
                }
                if (radioGrant != null) {
                    clock.advance(1_000);
                    platformProbe.adapterChanged(false);
                    assertEquals(BleConnectionManager.State.BLUETOOTH_OFF, state.get());
                    assertEquals(0, clock.pendingCount());
                    retry.run();
                    assertEquals(3, scanCallbacks.size());
                    if (radioGrant == RadioGrant.REVOKED) {
                        grantGeneration.set(null);
                    } else if (radioGrant == RadioGrant.REISSUED) {
                        grantGeneration.set(null);
                        grantGeneration.set(new DebugBleProbe.Grant());
                    }

                    platformProbe.adapterChanged(true);
                    boolean grantRetained = radioGrant == RadioGrant.UNCHANGED;
                    int expectedScans = grantRetained ? 4 : 3;
                    assertEquals(expectedScans, scanCallbacks.size());
                    clock.advance(29_000);
                    retry.run();
                    assertEquals(expectedScans, scanCallbacks.size());
                    assertEquals(1, callbacks.size());
                    assertEquals(0, clock.pendingCount());
                    assertEquals(grantRetained ? BleConnectionManager.State.WAITING_FOR_DEVICE
                            : BleConnectionManager.State.UNAUTHORIZED, state.get());
                    if (grantRetained) {
                        var resumed = scanCallbacks.get(3);
                        verify(scanner, never()).stopScan(resumed);
                        resumed.onBatchScanResults(List.of(fresh, fresh));
                        drain(handlerWork);
                        assertEquals(List.of(initial, fresh), delivered);
                        assertEquals(2, callbacks.size());
                        var recovered = callbacks.get(1);
                        recovered.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
                        recovered.onServicesDiscovered(replacement, GATT_SUCCESS);
                        drain(handlerWork);
                        assertEquals(BleConnectionManager.State.CONNECTED, state.get());
                        assertEquals(0, clock.pendingCount());
                    }
                    return;
                }
                clock.advance(29_999);
                assertEquals(3, scanCallbacks.size());
                clock.advance(1);
                assertEquals(4, scanCallbacks.size());
                assertEquals(BleConnectionManager.State.WAITING_FOR_DEVICE, state.get());
                assertEquals(0, clock.pendingCount());
                return;
            }
            if (delivery == ScanDelivery.BATCH_BEFORE_THROTTLE) {
                newScan.onBatchScanResults(List.of(fresh, fresh));
                newScan.onScanFailed(ScanCallback.SCAN_FAILED_SCANNING_TOO_FREQUENTLY);
                assertEquals(2, handlerWork.size());
                handlerWork.remove().run();
                assertEquals(BleConnectionManager.State.CONNECTING, state.get());
            } else if (delivery == ScanDelivery.ACTIVE_BATCH || delivery == ScanDelivery.REVOKED_BATCH) {
                newScan.onBatchScanResults(List.of(stale, fresh, fresh));
                assertEquals(1, handlerWork.size());
            } else {
                newScan.onScanResult(ScanSettings.CALLBACK_TYPE_ALL_MATCHES, fresh);
                newScan.onScanResult(ScanSettings.CALLBACK_TYPE_ALL_MATCHES, fresh);
            }
            if (delivery == ScanDelivery.REVOKED_BATCH) {
                grantGeneration.set(null);
            }
            if (delivery == ScanDelivery.CONNECT_PERMISSION_LOST) {
                assertEquals(2, handlerWork.size());
                when(context.checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT))
                        .thenReturn(PackageManager.PERMISSION_DENIED);
            }
            drain(handlerWork);
            assertEquals(List.of(initial, fresh), delivered);
            assertEquals(List.of(), failures);
            verify(scanner).stopScan(newScan);
            if (delivery == ScanDelivery.CONNECT_PERMISSION_LOST) {
                assertEquals(PackageManager.PERMISSION_GRANTED,
                        context.checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN));
                assertTrue(grantGeneration.get() != null);
                assertTrue(platformProbe.permissionDenied());
                assertEquals(R.string.plaud_connection_bluetooth_permission_denied, BleDeviceStatus.message(state.get()));
                assertEquals(0, clock.pendingCount());
                clock.advance(60_000);
                assertEquals(3, scanCallbacks.size());
                assertEquals(1, callbacks.size());
                verify(device).connectGatt(eq(context), eq(false), any(BluetoothGattCallback.class),
                        eq(BluetoothDevice.TRANSPORT_LE));
                assertEquals(0, clock.pendingCount());
                return;
            }
            if (delivery == ScanDelivery.REVOKED_BATCH) {
                assertEquals(1, callbacks.size());
                assertEquals(BleConnectionManager.State.UNAUTHORIZED, state.get());
                assertEquals(0, clock.pendingCount());
                return;
            }
            assertEquals(2, callbacks.size());
            var recovered = callbacks.get(1);
            recovered.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
            recovered.onServicesDiscovered(replacement, GATT_SUCCESS);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.CONNECTED, state.get());
            verify(replacement).discoverServices();
            assertEquals(3, scanCallbacks.size());
            assertEquals(0, clock.pendingCount());
        } finally {
            if (delivery == ScanDelivery.THROTTLE_PERMISSION_LOST) {
                permissionSession.pause();
            } else {
                platformProbe.stop();
            }
        }
    }

    private void assertRecoveryGattTimeoutDoesNotInterruptRescan(boolean successAtDeadline) {
        try {
            var original = startReadyLink();
            original.onConnectionStateChange(first, GATT_SUCCESS, STATE_DISCONNECTED);
            drain(handlerWork);
            clock.advance(1_000);
            scans.get(1).accept(sighting);
            var timedOut = callbacks.get(1);
            timedOut.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
            drain(handlerWork);
            verify(replacement).discoverServices();
            Runnable watchdog = clock.onlyPendingCallback();
            states.clear();
            if (successAtDeadline) {
                clock.elapse(15_000);
                timedOut.onServicesDiscovered(replacement, GATT_SUCCESS);
                assertEquals(BleConnectionManager.State.CONNECTING, state.get());
                assertEquals(watchdog, clock.onlyPendingCallback());
                drain(handlerWork);
                Runnable rescan = clock.onlyPendingCallback();
                watchdog.run();
                assertEquals(rescan, clock.onlyPendingCallback());
            } else {
                clock.advance(15_000);
            }
            assertEquals(List.of(BleConnectionManager.State.RETRYING), states);
            verify(replacement).close();
            assertEquals(BleConnectionManager.State.RETRYING, state.get());
            assertEquals(2, scans.size());

            clock.advance(1_999);
            assertEquals(2, scans.size());
            clock.advance(1);
            assertEquals(3, scans.size());
            assertEquals(BleConnectionManager.State.WAITING_FOR_DEVICE, state.get());
            states.clear();
            var cancelRescan = scanCancellations.get(2);

            watchdog.run();
            timedOut.onServicesDiscovered(replacement, GATT_SUCCESS);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.WAITING_FOR_DEVICE, state.get());
            assertFalse(states.contains(BleConnectionManager.State.CONNECTED));
            verify(cancelRescan, never()).run();
            verify(replacement, times(successAtDeadline ? 1 : 0)).getService(DebugBleProbe.SERVICE);
            assertEquals(3, scans.size());
            assertEquals(2, callbacks.size());
            assertEquals(0, clock.pendingCount());

            scans.get(2).accept(sighting);
            scans.get(2).accept(sighting);
            verify(cancelRescan).run();
            assertEquals(3, callbacks.size());
            var fresh = callbacks.get(2);
            fresh.onConnectionStateChange(newlyAuthorized, GATT_SUCCESS, STATE_CONNECTED);
            fresh.onServicesDiscovered(newlyAuthorized, GATT_SUCCESS);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.CONNECTED, state.get());
            verify(replacement).close();
            verify(newlyAuthorized, never()).close();
            assertEquals(0, clock.pendingCount());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void serviceChangeDiscoveryTimeoutRescansWhenWatchdogRunsFirst() {
        assertRevalidationTimeoutRecovery(true, false);
    }

    @Test
    public void serviceChangeDiscoveryTimeoutRescansWhenLateSuccessRunsFirst() {
        assertRevalidationTimeoutRecovery(false, false);
    }

    @Test
    public void serviceChangeDiscoveryWatchdogRecoveryRechecksAuthorization() {
        assertRevalidationTimeoutRecovery(true, true);
    }

    @Test
    public void radioOffCancelsRecoveryGattBeforeConnectedCallbackRuns() {
        assertRadioCycleDuringOpening(false, RadioGrant.UNCHANGED);
    }

    @Test
    public void radioOffCancelsRecoveryGattBeforeDiscoveryCallbackRuns() {
        assertRadioCycleDuringOpening(true, RadioGrant.UNCHANGED);
    }

    @Test
    public void radioOnDoesNotRecoverInterruptedGattWithRevokedGrant() {
        assertRadioCycleDuringOpening(true, RadioGrant.REVOKED);
    }

    @Test
    public void radioOnDoesNotRecoverInterruptedGattWithReissuedGrant() {
        assertRadioCycleDuringOpening(true, RadioGrant.REISSUED);
    }

    @Test
    public void twoRadioCyclesOnlyAllowTheLastGattToBecomeReady() {
        assertTwoRadioCycles(RadioGrant.UNCHANGED);
    }

    @Test
    public void secondRadioRecoveryStopsWhenOriginalGrantIsRevoked() {
        assertTwoRadioCycles(RadioGrant.REVOKED);
    }

    @Test
    public void secondRadioRecoveryStopsWhenOriginalGrantIsReissued() {
        assertTwoRadioCycles(RadioGrant.REISSUED);
    }

    @Test
    public void duplicateRadioOnKeepsAuthorizedRecoveryDiscoveryRunning() {
        assertDuplicateRadioOnDuringDiscovery(RadioGrant.UNCHANGED);
    }

    @Test
    public void duplicateRadioOnDoesNotRestoreRevokedRecoveryGrant() {
        assertDuplicateRadioOnDuringDiscovery(RadioGrant.REVOKED);
    }

    @Test
    public void duplicateRadioOnDoesNotAdoptReissuedRecoveryGrant() {
        assertDuplicateRadioOnDuringDiscovery(RadioGrant.REISSUED);
    }

    @Test
    public void delayedBluetoothBroadcastsRecoverOnceWhenOriginalGrantStillApplies() {
        assertDelayedBluetoothBroadcastRecovery(RadioGrant.UNCHANGED);
    }

    @Test
    public void delayedBluetoothBroadcastsCannotRecoverWithRevokedGrant() {
        assertDelayedBluetoothBroadcastRecovery(RadioGrant.REVOKED);
    }

    @Test
    public void delayedBluetoothBroadcastsCannotRecoverWithReissuedGrant() {
        assertDelayedBluetoothBroadcastRecovery(RadioGrant.REISSUED);
    }

    @Test
    public void synchronousResultDuringRadioRecoveryClosesScanAndClearsDeadline() {
        assertOnlyFreshAndroidScanResultReconnects(ScanDelivery.SYNCHRONOUS_RADIO_RESULT);
    }

    @Test
    public void radioRecoveryRetriesMissingScannerWithoutAnotherOnBroadcast() {
        assertMissingScannerRecovery(ScannerRecovery.AVAILABLE);
    }

    @Test
    public void radioRecoveryStopsAfterThreeMissingScannerAttempts() {
        assertMissingScannerRecovery(ScannerRecovery.UNAVAILABLE);
    }

    @Test
    public void radioRecoveryCannotRetryMissingScannerAfterGrantRevoked() {
        assertMissingScannerRecovery(ScannerRecovery.REVOKED);
    }

    @Test
    public void radioRecoveryCannotRetryMissingScannerWithReissuedGrant() {
        assertMissingScannerRecovery(ScannerRecovery.REISSUED);
    }

    @Test
    public void radioRecoverySharesBudgetAcrossMissingScannerAndRegistrationFailure() {
        assertMissingScannerRecovery(ScannerRecovery.MIXED);
    }

    @Test
    public void radioRecoveryRechecksOriginalGrantAfterRegistrationFailure() {
        assertMissingScannerRecovery(ScannerRecovery.MIXED_REISSUED);
    }

    @Test
    public void coldRestoredSessionReconnectsAfterRegistrationFailureWithOriginalGrant() {
        assertColdRestoredRecovery(ColdRecovery.CONNECT);
    }

    @Test
    public void coldRestoredSessionStopsAfterRegistrationFailuresExhaustScanBudget() {
        assertColdRestoredRecovery(ColdRecovery.EXHAUST);
    }

    @Test
    public void coldRestoredSessionDoesNotRetryRegistrationFailureAfterGrantRevocation() {
        assertColdRestoredRecovery(ColdRecovery.REVOKE);
    }

    @Test
    public void coldRestoredSessionCannotUseQueuedRecoveryResultAfterConnectPermissionLoss() {
        assertColdRestoredRecovery(ColdRecovery.CONNECT_PERMISSION_LOST);
    }

    @Test
    public void coldRestoredSessionCannotResumeRevokedIntentInNewProcess() {
        assertColdRestoredRecovery(ColdRecovery.RESTART_AFTER_PERMISSION_LOSS);
    }

    @Test
    public void switchingBackToSameAddressRejectsOldBatchWithFreshTimestamp() {
        assertSwitchBackRejectsRetiredScanCallback(RetiredScanEvent.BATCH);
    }

    @Test
    public void switchingBackToSameAddressOnlyRetriesCurrentRegistrationFailure() {
        assertSwitchBackRejectsRetiredScanCallback(RetiredScanEvent.REGISTRATION_FAILURE);
    }

    @Test
    public void switchingBackToSameAddressOnlyRetriesCurrentScanTimeout() {
        assertSwitchBackRejectsRetiredScanCallback(RetiredScanEvent.TIMEOUT);
    }

    @Test
    public void switchingBackToSameAddressIgnoresDequeuedScanBackoffRetry() {
        assertSwitchBackRejectsRetiredScanCallback(RetiredScanEvent.BACKOFF_RETRY);
    }

    private enum RetiredScanEvent { BATCH, REGISTRATION_FAILURE, TIMEOUT, BACKOFF_RETRY }

    private void assertSwitchBackRejectsRetiredScanCallback(RetiredScanEvent event) {
        boolean scanTimeout = event == RetiredScanEvent.TIMEOUT || event == RetiredScanEvent.BACKOFF_RETRY;
        String otherAddress = "AA:BB:CC:DD:EE:02";
        var otherDevice = mock(BluetoothDevice.class);
        when(device.getAddress()).thenReturn(ADDRESS);
        when(otherDevice.getAddress()).thenReturn(otherAddress);
        when(adapter.getRemoteDevice(otherAddress)).thenReturn(otherDevice);
        when(otherDevice.connectGatt(eq(context), eq(false), any(BluetoothGattCallback.class),
                eq(BluetoothDevice.TRANSPORT_LE))).thenAnswer(call -> {
                    callbacks.add(call.getArgument(2));
                    return replacement;
                });
        var scanner = mock(BluetoothLeScanner.class);
        var registrations = new ArrayList<ScanCallback>();
        var scanTimeouts = new ArrayDeque<Runnable>();
        when(handler.postDelayed(any(Runnable.class), eq(BleConnectionManager.SCAN_WINDOW_MS)))
                .thenAnswer(call -> scanTimeouts.add(call.getArgument(0)));
        doAnswer(call -> {
            scanTimeouts.remove(call.getArgument(0));
            return null;
        }).when(handler).removeCallbacks(any(Runnable.class));
        when(adapter.isEnabled()).thenReturn(true);
        when(adapter.getBluetoothLeScanner()).thenReturn(scanner);
        doAnswer(call -> registrations.add(call.getArgument(2)))
                .when(scanner).startScan(anyList(), any(ScanSettings.class), any(ScanCallback.class));
        var transport = new AndroidDebugBleTransport(context, adapter, handler);
        var intent = new DebugBleReconnectIntent(new DebugBleProbeTest.MemoryStore(), () -> true,
                (address, authorized) -> new DebugBleProbe(address, transport, clock, state::set, authorized));
        var record = mock(ScanRecord.class);
        var initialResult = labScanResult(device, record, 100L);
        var otherResult = labScanResult(otherDevice, record, 300L);
        var sameWindowResult = labScanResult(device, record, 301L);

        try (var systemClock = mockStatic(SystemClock.class);
                var uuids = mockConstruction(ParcelUuid.class);
                var filters = mockConstruction(ScanFilter.Builder.class, (builder, construction) -> {
                    when(builder.setDeviceAddress(any(String.class))).thenReturn(builder);
                    when(builder.setServiceUuid(any(ParcelUuid.class))).thenReturn(builder);
                    when(builder.build()).thenReturn(mock(ScanFilter.class));
                });
                var settings = mockConstruction(ScanSettings.Builder.class, (builder, construction) -> {
                    when(builder.setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)).thenReturn(builder);
                    when(builder.build()).thenReturn(mock(ScanSettings.class));
                })) {
            systemClock.when(SystemClock::elapsedRealtimeNanos).thenReturn(100L, 200L, 300L);
            when(record.getServiceUuids()).thenAnswer(call -> uuids.constructed());
            assertEquals(DebugBleReconnectIntent.Result.STARTED, intent.startOrResume(ADDRESS, ADDRESS::equals));
            var firstScan = registrations.get(0);
            firstScan.onBatchScanResults(List.of(initialResult));
            drain(handlerWork);
            completeLabGatt(0, first);
            Runnable retiredTimer = null;
            if (scanTimeout) {
                callbacks.get(0).onConnectionStateChange(first, GATT_SUCCESS, STATE_DISCONNECTED);
                drain(handlerWork);
                clock.advance(1_000);
                assertEquals(2, registrations.size());
                assertEquals(1, scanTimeouts.size());
                clock.advance(BleConnectionManager.SCAN_WINDOW_MS);
                retiredTimer = scanTimeouts.remove();
                if (event == RetiredScanEvent.BACKOFF_RETRY) {
                    retiredTimer.run();
                    assertEquals(BleConnectionManager.State.WAITING_TO_RESCAN, state.get());
                    retiredTimer = clock.onlyPendingCallback();
                    clock.elapse(2_000);
                    clock.remove(retiredTimer);
                    assertEquals(2, registrations.size());
                }
            }
            assertEquals(DebugBleReconnectIntent.Result.STARTED, intent.startOrResume(otherAddress, otherAddress::equals));
            registrations.get(registrations.size() - 1).onBatchScanResults(List.of(otherResult));
            drain(handlerWork);
            completeLabGatt(1, replacement);

            assertEquals(DebugBleReconnectIntent.Result.STARTED, intent.startOrResume(ADDRESS, ADDRESS::equals));
            int initialScanCount = scanTimeout ? 4 : 3;
            assertEquals(initialScanCount, registrations.size());
            var currentScan = registrations.get(initialScanCount - 1);
            assertEquals(1, scanTimeouts.size());
            var currentTimeout = scanTimeouts.element();
            int registrationError = ScanCallback.SCAN_FAILED_APPLICATION_REGISTRATION_FAILED;
            switch (event) {
                case BATCH -> firstScan.onBatchScanResults(List.of(sameWindowResult));
                case REGISTRATION_FAILURE -> firstScan.onScanFailed(registrationError);
                case TIMEOUT -> retiredTimer.run();
                case BACKOFF_RETRY -> {}
            }
            drain(handlerWork);
            verify(sameWindowResult, never()).getScanRecord();
            assertEquals(2, callbacks.size());
            assertEquals(BleConnectionManager.State.WAITING_FOR_DEVICE, state.get());
            verify(scanner, never()).stopScan(currentScan);
            assertEquals(0, clock.pendingCount());
            assertEquals(initialScanCount, registrations.size());
            assertEquals(List.of(currentTimeout), List.copyOf(scanTimeouts));

            if (event != RetiredScanEvent.BATCH) {
                if (scanTimeout) {
                    clock.advance(BleConnectionManager.SCAN_WINDOW_MS);
                    scanTimeouts.remove().run();
                    currentTimeout.run();
                } else {
                    currentScan.onScanFailed(registrationError);
                    currentScan.onScanFailed(registrationError);
                    drain(handlerWork);
                }
                verify(scanner).stopScan(currentScan);
                assertEquals(BleConnectionManager.State.WAITING_TO_RESCAN, state.get());
                assertEquals(1, clock.pendingCount());
                assertEquals(2, callbacks.size());
                if (event == RetiredScanEvent.BACKOFF_RETRY) {
                    var currentRetry = clock.onlyPendingCallback();
                    retiredTimer.run();
                    assertEquals(currentRetry, clock.onlyPendingCallback());
                    assertEquals(BleConnectionManager.State.WAITING_TO_RESCAN, state.get());
                    assertEquals(initialScanCount, registrations.size());
                    assertEquals(2, callbacks.size());
                    assertTrue(scanTimeouts.isEmpty());
                }
                clock.advance(1_999);
                assertEquals(initialScanCount, registrations.size());
                clock.advance(1);
                assertEquals(initialScanCount + 1, registrations.size());
                assertEquals(BleConnectionManager.State.WAITING_FOR_DEVICE, state.get());
                assertEquals(0, clock.pendingCount());
                currentScan = registrations.get(initialScanCount);
            }

            currentScan.onBatchScanResults(List.of(sameWindowResult, sameWindowResult));
            drain(handlerWork);
            assertEquals(3, callbacks.size());
            completeLabGatt(2, newlyAuthorized);
            verify(scanner).stopScan(currentScan);
            verify(first).close();
            verify(replacement).close();
            verify(newlyAuthorized, never()).close();
            assertEquals(initialScanCount + (event == RetiredScanEvent.BATCH ? 0 : 1), registrations.size());
            assertEquals(0, clock.pendingCount());
            assertTrue(scanTimeouts.isEmpty());
        } finally {
            intent.pause();
        }
    }

    private static ScanResult labScanResult(BluetoothDevice device, ScanRecord record, long timestamp) {
        var result = mock(ScanResult.class);
        when(result.getScanRecord()).thenReturn(record);
        when(result.getDevice()).thenReturn(device);
        when(result.getTimestampNanos()).thenReturn(timestamp);
        return result;
    }

    private void completeLabGatt(int index, BluetoothGatt gatt) {
        var callback = callbacks.get(index);
        callback.onConnectionStateChange(gatt, GATT_SUCCESS, STATE_CONNECTED);
        callback.onServicesDiscovered(gatt, GATT_SUCCESS);
        drain(handlerWork);
        assertEquals(BleConnectionManager.State.CONNECTED, state.get());
    }

    @Test
    public void recoveryIgnoresSameNameWithoutServiceUuidAndReconnectsOnceAfterItReturns() {
        var scanner = mock(BluetoothLeScanner.class);
        var registrations = new ArrayList<ScanCallback>();
        when(adapter.getBluetoothLeScanner()).thenReturn(scanner);
        when(device.getAddress()).thenReturn(ADDRESS);
        doAnswer(call -> registrations.add(call.getArgument(2)))
                .when(scanner).startScan(anyList(), any(ScanSettings.class), any(ScanCallback.class));
        var androidTransport = new AndroidDebugBleTransport(context, adapter, handler);
        var lab = new DebugBleLab(render ->
                new DebugBleReconnectIntent(new DebugBleProbeTest.MemoryStore(), () -> true,
                        (address, authorized) -> new DebugBleProbe(address, androidTransport, clock, render, authorized)));
        var readySessions = new ArrayList<Integer>();
        lab.bind((next, ready) -> {
            state.set(next);
            if (next == BleConnectionManager.State.CONNECTED) {
                readySessions.add(ready);
            }
        });
        var correct = mock(ScanRecord.class);
        var decoy = mock(ScanRecord.class);
        String localName = "LPLab";
        when(correct.getDeviceName()).thenReturn(localName);
        when(decoy.getDeviceName()).thenReturn(localName);
        when(decoy.getServiceUuids()).thenReturn(null);
        var initial = labScanResult(device, correct, 100L);
        var wrongAdvertisement = labScanResult(device, decoy, 200L);
        var restoredAdvertisement = labScanResult(device, correct, 201L);

        try (var systemClock = mockStatic(SystemClock.class);
                var uuids = mockConstruction(ParcelUuid.class);
                var filters = mockConstruction(ScanFilter.Builder.class, (builder, construction) -> {
                    when(builder.setDeviceAddress(ADDRESS)).thenReturn(builder);
                    when(builder.setServiceUuid(any(ParcelUuid.class))).thenReturn(builder);
                    when(builder.build()).thenReturn(mock(ScanFilter.class));
                });
                var settings = mockConstruction(ScanSettings.Builder.class, (builder, construction) -> {
                    when(builder.setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)).thenReturn(builder);
                    when(builder.build()).thenReturn(mock(ScanSettings.class));
                })) {
            systemClock.when(SystemClock::elapsedRealtimeNanos).thenReturn(100L, 200L);
            when(correct.getServiceUuids()).thenAnswer(call -> uuids.constructed());
            assertEquals(DebugBleReconnectIntent.Result.STARTED,
                    lab.reconnectIntent.startOrResume(ADDRESS, address -> ADDRESS.equals(address) && grant.get()));
            registrations.get(0).onScanResult(ScanSettings.CALLBACK_TYPE_ALL_MATCHES, initial);
            drain(handlerWork);
            completeLabGatt(0, first);
            assertEquals(List.of(1), readySessions);

            callbacks.get(0).onConnectionStateChange(first, GATT_SUCCESS, STATE_DISCONNECTED);
            drain(handlerWork);
            clock.advance(1_000);
            var recovery = registrations.get(1);
            recovery.onScanResult(ScanSettings.CALLBACK_TYPE_ALL_MATCHES, wrongAdvertisement);
            drain(handlerWork);
            verify(decoy).getServiceUuids();
            verify(scanner, never()).stopScan(recovery);
            assertEquals(BleConnectionManager.State.WAITING_FOR_DEVICE, state.get());
            assertEquals(1, callbacks.size());
            assertEquals(List.of(1), readySessions);

            recovery.onBatchScanResults(List.of(restoredAdvertisement, restoredAdvertisement));
            drain(handlerWork);
            assertEquals(2, callbacks.size());
            completeLabGatt(1, replacement);
            verify(scanner).stopScan(recovery);
            assertEquals(List.of(1, 2), readySessions);
            assertEquals(2, registrations.size());
            assertEquals(0, clock.pendingCount());
        } finally {
            lab.reconnectIntent.pause();
        }
    }

    private enum ColdRecovery { CONNECT, EXHAUST, REVOKE, CONNECT_PERMISSION_LOST, RESTART_AFTER_PERMISSION_LOSS }

    private void assertColdRestoredRecovery(ColdRecovery recovery) {
        var store = new DebugBleProbeTest.MemoryStore();
        store.writeAddress(ADDRESS);
        var scanner = mock(BluetoothLeScanner.class);
        var registrations = new ArrayList<ScanCallback>();
        when(adapter.isEnabled()).thenReturn(true);
        when(adapter.getBluetoothLeScanner()).thenReturn(scanner);
        doAnswer(call -> registrations.add(call.getArgument(2)))
                .when(scanner).startScan(anyList(), any(ScanSettings.class), any(ScanCallback.class));
        var transport = new AndroidDebugBleTransport(context, adapter, handler);
        Supplier<DebugBleLab> createLab = () -> new DebugBleLab(render ->
                new DebugBleReconnectIntent(store, () -> BleDeviceTestFixture.hasProbePermissions(context), (address, authorized) ->
                        new DebugBleProbe(address, transport, clock, render, authorized)));
        var lab = DebugBleLab.fromNonConfigurationInstance(null, createLab);
        var intent = lab.reconnectIntent;
        var result = mock(ScanResult.class);
        var record = mock(ScanRecord.class);
        when(result.getScanRecord()).thenReturn(record);
        when(result.getDevice()).thenReturn(device);
        when(device.getAddress()).thenReturn(ADDRESS);
        when(result.getTimestampNanos()).thenReturn(100L);

        try (var systemClock = mockStatic(SystemClock.class);
                var uuids = mockConstruction(ParcelUuid.class);
                var filters = mockConstruction(ScanFilter.Builder.class, (builder, construction) -> {
                    when(builder.setDeviceAddress(ADDRESS)).thenReturn(builder);
                    when(builder.setServiceUuid(any(ParcelUuid.class))).thenReturn(builder);
                    when(builder.build()).thenReturn(mock(ScanFilter.class));
                });
                var settings = mockConstruction(ScanSettings.Builder.class, (builder, construction) -> {
                    when(builder.setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)).thenReturn(builder);
                    when(builder.build()).thenReturn(mock(ScanSettings.class));
                })) {
            systemClock.when(SystemClock::elapsedRealtimeNanos).thenReturn(100L);
            when(record.getServiceUuids()).thenAnswer(call -> List.of(uuids.constructed().get(uuids.constructed().size() - 1)));
            lab.bind((next, ready) -> state.set(next));
            assertEquals(BleConnectionManager.State.STOPPED, state.get());
            assertTrue(registrations.isEmpty());
            assertEquals(DebugBleReconnectIntent.Result.STARTED,
                    intent.resume(address -> ADDRESS.equals(address) && grant.get()));
            registrations.get(0).onScanResult(ScanSettings.CALLBACK_TYPE_ALL_MATCHES, result);
            drain(handlerWork);
            var initial = callbacks.get(0);
            initial.onConnectionStateChange(first, GATT_SUCCESS, STATE_CONNECTED);
            initial.onServicesDiscovered(first, GATT_SUCCESS);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.CONNECTED, state.get());
            initial.onConnectionStateChange(first, GATT_SUCCESS, STATE_DISCONNECTED);
            drain(handlerWork);
            clock.advance(1_000);

            var recoveryScan = registrations.get(1);
            if (recovery == ColdRecovery.RESTART_AFTER_PERMISSION_LOSS) {
                queueRecoveryResultAndRevokeConnectPermission(recoveryScan, result);
                assertEquals(BleConnectionManager.State.UNAUTHORIZED, state.get());
                verify(scanner).stopScan(recoveryScan);
                intent.pause();
                restoreConnectPermission();
                var restarted = DebugBleLab.fromNonConfigurationInstance(null, createLab);
                var restoredIntent = restarted.reconnectIntent;
                try {
                    restarted.bind((next, ready) -> state.set(next));
                    restoredIntent.adapterChanged(false);
                    restoredIntent.adapterChanged(true);
                    assertTrue(grant.get());
                    assertEquals(DebugBleReconnectIntent.Result.NO_INTENT,
                            restoredIntent.resume(address -> ADDRESS.equals(address) && grant.get()));
                    clock.advance(60_000);
                    assertEquals(BleConnectionManager.State.STOPPED, state.get());
                    assertEquals(2, registrations.size());
                    assertEquals(1, callbacks.size());
                    assertEquals(0, clock.pendingCount());
                    assertEquals(DebugBleReconnectIntent.Result.STARTED,
                            restoredIntent.start(ADDRESS, address -> ADDRESS.equals(address) && grant.get()));
                    assertEquals(3, registrations.size());
                } finally {
                    restoredIntent.pause();
                }
                return;
            }
            if (recovery == ColdRecovery.CONNECT_PERMISSION_LOST) {
                assertConnectPermissionLossStopsColdRecovery(intent, recoveryScan, scanner, result);
                assertEquals(2, registrations.size());
                return;
            }
            failScanRegistration(recoveryScan, scanner, result);
            if (recovery == ColdRecovery.REVOKE) {
                grant.set(false);
            }
            clock.advance(1_999);
            assertEquals(2, registrations.size());
            clock.advance(1);
            if (recovery == ColdRecovery.REVOKE) {
                assertEquals(BleConnectionManager.State.UNAUTHORIZED, state.get());
                assertEquals(0, clock.pendingCount());
                assertEquals(2, registrations.size());
                assertEquals(1, callbacks.size());
                return;
            }

            failScanRegistration(registrations.get(2), scanner, result);
            clock.advance(3_999);
            assertEquals(3, registrations.size());
            clock.advance(1);
            var lastScan = registrations.get(3);
            if (recovery == ColdRecovery.EXHAUST) {
                lastScan.onScanFailed(ScanCallback.SCAN_FAILED_APPLICATION_REGISTRATION_FAILED);
                drain(handlerWork);
                assertEquals(BleConnectionManager.State.RESCAN_COOLDOWN, state.get());
                clock.advance(29_999);
                assertEquals(BleConnectionManager.State.RESCAN_COOLDOWN, state.get());
                clock.advance(1);
                assertEquals(BleConnectionManager.State.RESCAN_READY, state.get());
                assertEquals(1, callbacks.size());
            } else {
                lastScan.onScanResult(ScanSettings.CALLBACK_TYPE_ALL_MATCHES, result);
                lastScan.onScanResult(ScanSettings.CALLBACK_TYPE_ALL_MATCHES, result);
                drain(handlerWork);
                assertEquals(2, callbacks.size());
                callbacks.get(1).onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
                callbacks.get(1).onServicesDiscovered(replacement, GATT_SUCCESS);
                drain(handlerWork);
                assertEquals(BleConnectionManager.State.CONNECTED, state.get());
            }
            assertEquals(4, registrations.size());
            assertEquals(0, clock.pendingCount());
        } finally {
            intent.pause();
        }
    }

    private void assertConnectPermissionLossStopsColdRecovery(DebugBleReconnectIntent intent,
            ScanCallback callback, BluetoothLeScanner scanner, ScanResult result) {
        queueRecoveryResultAndRevokeConnectPermission(callback, result);

        assertEquals(BleConnectionManager.State.UNAUTHORIZED, state.get());
        assertEquals(R.string.plaud_probe_permission_revoked, BleDeviceTestFixture.probeMessage(state.get(), context));
        verify(scanner).stopScan(callback);
        verify(first).close();
        assertEquals(1, callbacks.size());
        assertEquals(0, clock.pendingCount());

        restoreConnectPermission();
        callback.onScanResult(ScanSettings.CALLBACK_TYPE_ALL_MATCHES, result);
        drain(handlerWork);
        assertTrue(intent.invalidateIfPermissionLost());
        assertFalse(intent.hasActiveSession());
        intent.adapterChanged(false);
        intent.adapterChanged(true);
        clock.advance(60_000);
        assertEquals(1, callbacks.size());
        assertEquals(0, clock.pendingCount());
    }

    private void queueRecoveryResultAndRevokeConnectPermission(ScanCallback callback, ScanResult result) {
        assertEquals(BleConnectionManager.State.WAITING_FOR_DEVICE, state.get());
        callback.onScanResult(ScanSettings.CALLBACK_TYPE_ALL_MATCHES, result);
        assertEquals(1, handlerWork.size());
        when(context.checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT))
                .thenReturn(PackageManager.PERMISSION_DENIED);
        assertEquals(PackageManager.PERMISSION_GRANTED, context.checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN));
        assertTrue(grant.get());
        drain(handlerWork);
    }

    private void restoreConnectPermission() {
        when(context.checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT))
                .thenReturn(PackageManager.PERMISSION_GRANTED);
    }

    private void failScanRegistration(ScanCallback callback, BluetoothLeScanner scanner, ScanResult result) {
        callback.onScanFailed(ScanCallback.SCAN_FAILED_APPLICATION_REGISTRATION_FAILED);
        callback.onScanFailed(ScanCallback.SCAN_FAILED_APPLICATION_REGISTRATION_FAILED);
        callback.onScanResult(ScanSettings.CALLBACK_TYPE_ALL_MATCHES, result);
        drain(handlerWork);
        verify(scanner).stopScan(callback);
        assertEquals(BleConnectionManager.State.WAITING_TO_RESCAN, state.get());
        assertEquals(1, callbacks.size());
        assertEquals(1, clock.pendingCount());
    }

    private enum ScannerRecovery { AVAILABLE, UNAVAILABLE, REVOKED, REISSUED, MIXED, MIXED_REISSUED }

    private void assertMissingScannerRecovery(ScannerRecovery recovery) {
        boolean mixedFailures = recovery == ScannerRecovery.MIXED || recovery == ScannerRecovery.MIXED_REISSUED;
        var scanner = mock(BluetoothLeScanner.class);
        var activeScan = new AtomicReference<ScanCallback>();
        when(adapter.isEnabled()).thenReturn(true);
        when(adapter.getBluetoothLeScanner()).thenReturn(scanner, null,
                recovery == ScannerRecovery.UNAVAILABLE ? null : scanner,
                recovery == ScannerRecovery.UNAVAILABLE || mixedFailures ? null : scanner);
        doAnswer(call -> {
            activeScan.set(call.getArgument(2));
            return null;
        }).when(scanner).startScan(anyList(), any(ScanSettings.class), any(ScanCallback.class));
        var platformProbe = new DebugBleProbe(ADDRESS, new AndroidDebugBleTransport(context, adapter, handler),
                clock, state::set, grantGeneration::get);
        var receiver = bluetoothStateReceiver(adapter, platformProbe::adapterChanged);
        var result = mock(ScanResult.class);
        var record = mock(ScanRecord.class);
        when(result.getScanRecord()).thenReturn(record);
        when(result.getDevice()).thenReturn(device);
        when(device.getAddress()).thenReturn(ADDRESS);
        when(result.getTimestampNanos()).thenReturn(100L);

        try (var filters = mockConstruction(IntentFilter.class);
                var systemClock = mockStatic(SystemClock.class);
                var uuids = mockConstruction(ParcelUuid.class);
                var scanFilters = mockConstruction(ScanFilter.Builder.class, (builder, construction) -> {
                    when(builder.setDeviceAddress(ADDRESS)).thenReturn(builder);
                    when(builder.setServiceUuid(any(ParcelUuid.class))).thenReturn(builder);
                    when(builder.build()).thenReturn(mock(ScanFilter.class));
                });
                var settings = mockConstruction(ScanSettings.Builder.class, (builder, construction) -> {
                    when(builder.setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)).thenReturn(builder);
                    when(builder.build()).thenReturn(mock(ScanSettings.class));
                })) {
            when(record.getServiceUuids()).thenAnswer(call -> List.of(uuids.constructed().get(uuids.constructed().size() - 1)));
            systemClock.when(SystemClock::elapsedRealtimeNanos).thenReturn(100L);
            receiver.start();
            platformProbe.start(true);
            activeScan.get().onScanResult(ScanSettings.CALLBACK_TYPE_ALL_MATCHES, result);
            drain(handlerWork);
            var original = callbacks.get(0);
            original.onConnectionStateChange(first, GATT_SUCCESS, STATE_CONNECTED);
            original.onServicesDiscovered(first, GATT_SUCCESS);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.CONNECTED, state.get());

            sendBluetoothState(receiver, BluetoothAdapter.STATE_OFF);
            verify(first).close();
            sendBluetoothState(receiver, BluetoothAdapter.STATE_ON);
            assertEquals(BleConnectionManager.State.WAITING_TO_RESCAN, state.get());
            assertEquals(1, clock.pendingCount());
            if (recovery == ScannerRecovery.REVOKED) {
                grantGeneration.set(null);
            } else if (recovery == ScannerRecovery.REISSUED) {
                grantGeneration.set(new DebugBleProbe.Grant());
            }
            clock.advance(1_999);
            verify(adapter, times(2)).getBluetoothLeScanner();
            clock.advance(1);

            if (recovery == ScannerRecovery.AVAILABLE) {
                assertEquals(BleConnectionManager.State.WAITING_FOR_DEVICE, state.get());
                verify(scanner, times(2)).startScan(anyList(), any(ScanSettings.class), any(ScanCallback.class));
                var resumedScan = activeScan.get();
                resumedScan.onScanResult(ScanSettings.CALLBACK_TYPE_ALL_MATCHES, result);
                resumedScan.onScanResult(ScanSettings.CALLBACK_TYPE_ALL_MATCHES, result);
                drain(handlerWork);
                assertEquals(2, callbacks.size());
                var resumed = callbacks.get(1);
                resumed.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
                resumed.onServicesDiscovered(replacement, GATT_SUCCESS);
                drain(handlerWork);
                assertEquals(BleConnectionManager.State.CONNECTED, state.get());
                verify(adapter, times(3)).getBluetoothLeScanner();
            } else if (mixedFailures) {
                assertEquals(BleConnectionManager.State.WAITING_FOR_DEVICE, state.get());
                assertEquals(0, clock.pendingCount());
                var registration = activeScan.get();
                registration.onScanFailed(ScanCallback.SCAN_FAILED_APPLICATION_REGISTRATION_FAILED);
                registration.onScanFailed(ScanCallback.SCAN_FAILED_APPLICATION_REGISTRATION_FAILED);
                assertEquals(2, handlerWork.size());
                drain(handlerWork);
                verify(scanner).stopScan(registration);
                assertEquals(BleConnectionManager.State.WAITING_TO_RESCAN, state.get());
                assertEquals(1, clock.pendingCount());
                if (recovery == ScannerRecovery.MIXED_REISSUED) {
                    grantGeneration.set(new DebugBleProbe.Grant());
                }

                clock.advance(3_999);
                verify(adapter, times(3)).getBluetoothLeScanner();
                clock.advance(1);
                if (recovery == ScannerRecovery.MIXED_REISSUED) {
                    assertEquals(BleConnectionManager.State.UNAUTHORIZED, state.get());
                    verify(adapter, times(3)).getBluetoothLeScanner();
                } else {
                    assertEquals(BleConnectionManager.State.RESCAN_COOLDOWN, state.get());
                    clock.advance(30_000);
                    assertEquals(BleConnectionManager.State.RESCAN_READY, state.get());
                    verify(adapter, times(4)).getBluetoothLeScanner();
                }
                verify(scanner, times(2)).startScan(anyList(), any(ScanSettings.class), any(ScanCallback.class));
                assertEquals(1, callbacks.size());
            } else if (recovery == ScannerRecovery.UNAVAILABLE) {
                assertEquals(BleConnectionManager.State.WAITING_TO_RESCAN, state.get());
                clock.advance(3_999);
                verify(adapter, times(3)).getBluetoothLeScanner();
                clock.advance(1);
                assertEquals(BleConnectionManager.State.RESCAN_COOLDOWN, state.get());
                clock.advance(30_000);
                assertEquals(BleConnectionManager.State.RESCAN_READY, state.get());
                verify(adapter, times(4)).getBluetoothLeScanner();
                verify(scanner).startScan(anyList(), any(ScanSettings.class), any(ScanCallback.class));
                assertEquals(1, callbacks.size());
            } else {
                assertEquals(BleConnectionManager.State.UNAUTHORIZED, state.get());
                verify(adapter, times(2)).getBluetoothLeScanner();
                verify(scanner).startScan(anyList(), any(ScanSettings.class), any(ScanCallback.class));
                assertEquals(1, callbacks.size());
            }
            assertEquals(0, clock.pendingCount());
        } finally {
            receiver.stop();
            platformProbe.stop();
        }
    }

    private enum RadioGrant { UNCHANGED, REVOKED, REISSUED }

    private AndroidBluetoothState bluetoothStateReceiver(Consumer<Boolean> changed) {
        var adapter = mock(BluetoothAdapter.class);
        when(adapter.isEnabled()).thenReturn(true);
        return bluetoothStateReceiver(adapter, changed);
    }

    private AndroidBluetoothState bluetoothStateReceiver(BluetoothAdapter adapter, Consumer<Boolean> changed) {
        var manager = mock(BluetoothManager.class);
        when(context.getSystemService(BluetoothManager.class)).thenReturn(manager);
        when(manager.getAdapter()).thenReturn(adapter);
        return new AndroidBluetoothState(context, (supported, enabled) -> {
            assertTrue(supported);
            changed.accept(enabled);
        });
    }

    private void assertDelayedBluetoothBroadcastRecovery(RadioGrant grantChange) {
        var receiver = bluetoothStateReceiver(probe::adapterChanged);
        try (var filters = mockConstruction(IntentFilter.class)) {
            receiver.start();
            var original = startReadyLink();
            sendBluetoothState(receiver, BluetoothAdapter.STATE_TURNING_OFF);
            sendBluetoothState(receiver, BluetoothAdapter.STATE_OFF);
            assertEquals(BleConnectionManager.State.BLUETOOTH_OFF, state.get());
            verify(first).close();
            assertEquals(1, scans.size());

            if (grantChange == RadioGrant.REVOKED) {
                grantGeneration.set(null);
            } else if (grantChange == RadioGrant.REISSUED) {
                grantGeneration.set(new DebugBleProbe.Grant());
            }
            sendBluetoothState(receiver, BluetoothAdapter.STATE_ON);
            sendBluetoothState(receiver, BluetoothAdapter.STATE_ON);
            original.onConnectionStateChange(first, GATT_SUCCESS, STATE_CONNECTED);
            original.onServicesDiscovered(first, GATT_SUCCESS);
            drain(handlerWork);
            assertEquals(1, callbacks.size());
            verify(first).discoverServices();

            if (grantChange == RadioGrant.UNCHANGED) {
                assertEquals(2, scans.size());
                assertEquals(BleConnectionManager.State.WAITING_FOR_DEVICE, state.get());
                scans.get(1).accept(sighting);
                scans.get(1).accept(sighting);
                assertEquals(2, callbacks.size());
                var recovered = callbacks.get(1);
                recovered.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
                recovered.onServicesDiscovered(replacement, GATT_SUCCESS);
                drain(handlerWork);
                assertEquals(BleConnectionManager.State.CONNECTED, state.get());
                verify(replacement).discoverServices();
                assertEquals(2, scans.size());
            } else {
                assertEquals(1, scans.size());
                assertEquals(BleConnectionManager.State.UNAUTHORIZED, state.get());
            }
            assertEquals(0, clock.pendingCount());
        } finally {
            receiver.stop();
            probe.stop();
        }
    }

    private void sendBluetoothState(AndroidBluetoothState receiver, int state) {
        var intent = mock(Intent.class);
        when(intent.getAction()).thenReturn(BluetoothAdapter.ACTION_STATE_CHANGED);
        when(intent.getIntExtra(BluetoothAdapter.EXTRA_STATE, BluetoothAdapter.ERROR)).thenReturn(state);
        receiver.onReceive(context, intent);
    }

    private void assertDuplicateRadioOnDuringDiscovery(RadioGrant grantChange) {
        try {
            startReadyLink();
            probe.adapterChanged(false);
            probe.adapterChanged(true);
            assertEquals(2, scans.size());
            scans.get(1).accept(sighting);
            var recovery = callbacks.get(1);
            recovery.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
            drain(handlerWork);
            verify(replacement).discoverServices();
            assertEquals(BleConnectionManager.State.CONNECTING, state.get());
            assertEquals(1, clock.pendingCount());
            states.clear();

            recovery.onServicesDiscovered(replacement, GATT_SUCCESS);
            if (grantChange == RadioGrant.REVOKED) {
                grantGeneration.set(null);
            } else if (grantChange == RadioGrant.REISSUED) {
                grantGeneration.set(new DebugBleProbe.Grant());
            }
            probe.adapterChanged(true);
            assertEquals(BleConnectionManager.State.CONNECTING, state.get());
            assertEquals(1, clock.pendingCount());
            assertEquals(2, scans.size());
            assertEquals(2, callbacks.size());
            verify(replacement, never()).close();

            drain(handlerWork);
            if (grantChange == RadioGrant.UNCHANGED) {
                assertEquals(List.of(BleConnectionManager.State.CONNECTED), states);
                verify(replacement, never()).close();
            } else {
                assertEquals(List.of(BleConnectionManager.State.UNAUTHORIZED), states);
                verify(replacement).close();
            }
            clock.advance(15_000);
            verify(replacement).discoverServices();
            assertEquals(2, scans.size());
            assertEquals(2, callbacks.size());
            assertEquals(0, clock.pendingCount());
        } finally {
            probe.stop();
        }
    }

    private void assertTwoRadioCycles(RadioGrant grantChange) {
        try {
            var original = startDiscoveringLink();
            original.onServicesDiscovered(first, GATT_SUCCESS);
            probe.adapterChanged(false);
            verify(first).close();
            assertEquals(BleConnectionManager.State.BLUETOOTH_OFF, state.get());
            assertEquals(0, clock.pendingCount());
            probe.adapterChanged(true);
            assertEquals(2, scans.size());
            scans.get(1).accept(sighting);
            var interrupted = callbacks.get(1);
            interrupted.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
            drain(handlerWork);
            verify(replacement).discoverServices();
            assertEquals(BleConnectionManager.State.CONNECTING, state.get());

            interrupted.onServicesDiscovered(replacement, GATT_SUCCESS);
            probe.adapterChanged(false);
            verify(replacement).close();
            assertEquals(BleConnectionManager.State.BLUETOOTH_OFF, state.get());
            assertEquals(0, clock.pendingCount());
            if (grantChange == RadioGrant.REVOKED) {
                grantGeneration.set(null);
            } else if (grantChange == RadioGrant.REISSUED) {
                grantGeneration.set(new DebugBleProbe.Grant());
            }
            probe.adapterChanged(true);
            probe.adapterChanged(true);
            assertEquals(grantChange == RadioGrant.UNCHANGED ? 3 : 2, scans.size());
            assertEquals(2, callbacks.size());

            if (grantChange == RadioGrant.UNCHANGED) {
                scans.get(2).accept(sighting);
                scans.get(2).accept(sighting);
                assertEquals(3, callbacks.size());
            }
            original.onConnectionStateChange(first, GATT_SUCCESS, STATE_CONNECTED);
            interrupted.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
            drain(handlerWork);
            assertFalse(states.contains(BleConnectionManager.State.CONNECTED));
            verify(first).discoverServices();
            verify(replacement).discoverServices();
            verify(first, never()).getService(DebugBleProbe.SERVICE);
            verify(replacement, never()).getService(DebugBleProbe.SERVICE);

            if (grantChange == RadioGrant.UNCHANGED) {
                assertEquals(BleConnectionManager.State.CONNECTING, state.get());
                var fresh = callbacks.get(2);
                fresh.onConnectionStateChange(newlyAuthorized, GATT_SUCCESS, STATE_CONNECTED);
                fresh.onServicesDiscovered(newlyAuthorized, GATT_SUCCESS);
                drain(handlerWork);
                assertEquals(BleConnectionManager.State.CONNECTED, state.get());
            }
            original.onConnectionStateChange(first, GATT_SUCCESS, STATE_DISCONNECTED);
            interrupted.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_DISCONNECTED);
            drain(handlerWork);
            clock.advance(30_000);
            assertEquals(0, clock.pendingCount());
            assertEquals(grantChange == RadioGrant.UNCHANGED ? 3 : 2, scans.size());
            assertEquals(grantChange == RadioGrant.UNCHANGED ? 3 : 2, callbacks.size());
            assertEquals(grantChange == RadioGrant.UNCHANGED ? BleConnectionManager.State.CONNECTED
                    : BleConnectionManager.State.UNAUTHORIZED, state.get());
            verify(first).close();
            verify(replacement).close();
            verify(newlyAuthorized, never()).close();
        } finally {
            probe.stop();
        }
    }

    private void assertRadioCycleDuringOpening(boolean discoveryStarted, RadioGrant grantChange) {
        try {
            var original = startReadyLink();
            original.onConnectionStateChange(first, GATT_SUCCESS, STATE_DISCONNECTED);
            drain(handlerWork);
            clock.advance(1_000);
            scans.get(1).accept(sighting);
            var interrupted = callbacks.get(1);
            interrupted.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
            if (discoveryStarted) {
                drain(handlerWork);
            }
            interrupted.onServicesDiscovered(replacement, GATT_SUCCESS);
            assertEquals(BleConnectionManager.State.CONNECTING, state.get());
            assertEquals(1, clock.pendingCount());
            states.clear();

            probe.adapterChanged(false);
            verify(replacement).close();
            assertEquals(0, clock.pendingCount());
            drain(handlerWork);
            clock.advance(15_000);
            assertEquals(BleConnectionManager.State.BLUETOOTH_OFF, state.get());
            assertEquals(2, scans.size());
            assertEquals(2, callbacks.size());
            verify(replacement, times(discoveryStarted ? 1 : 0)).discoverServices();
            verify(replacement, never()).getService(DebugBleProbe.SERVICE);

            if (grantChange == RadioGrant.REVOKED) {
                grantGeneration.set(null);
            } else if (grantChange == RadioGrant.REISSUED) {
                grantGeneration.set(new DebugBleProbe.Grant());
            }
            probe.adapterChanged(true);
            probe.adapterChanged(true);
            assertEquals(2, callbacks.size());
            if (grantChange == RadioGrant.UNCHANGED) {
                assertEquals(3, scans.size());
                scans.get(2).accept(sighting);
                scans.get(2).accept(sighting);
                assertEquals(3, callbacks.size());
            } else {
                assertEquals(2, scans.size());
            }

            interrupted.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
            interrupted.onServicesDiscovered(replacement, GATT_SUCCESS);
            drain(handlerWork);
            assertFalse(states.contains(BleConnectionManager.State.CONNECTED));
            verify(replacement).close();
            if (grantChange == RadioGrant.UNCHANGED) {
                assertEquals(BleConnectionManager.State.CONNECTING, state.get());
                var fresh = callbacks.get(2);
                fresh.onConnectionStateChange(newlyAuthorized, GATT_SUCCESS, STATE_CONNECTED);
                fresh.onServicesDiscovered(newlyAuthorized, GATT_SUCCESS);
                drain(handlerWork);
                assertEquals(BleConnectionManager.State.CONNECTED, state.get());
                verify(newlyAuthorized, never()).close();
            } else {
                assertEquals(BleConnectionManager.State.UNAUTHORIZED, state.get());
            }
            assertEquals(0, clock.pendingCount());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void recoveryBackoffCannotScanUnderAReissuedGrant() {
        assertBackoffRejectsReissuedGrant(false);
    }

    @Test
    public void scanTimeoutBackoffCannotScanUnderAReissuedGrant() {
        assertBackoffRejectsReissuedGrant(true);
    }

    private void assertBackoffRejectsReissuedGrant(boolean scanTimedOut) {
        try {
            var original = startReadyLink();
            original.onConnectionStateChange(first, GATT_SUCCESS, STATE_DISCONNECTED);
            drain(handlerWork);
            if (scanTimedOut) {
                clock.advance(1_000);
                scanCompletions.get(1).accept(BleConnectionManager.ScanOutcome.TIMED_OUT);
                assertEquals(BleConnectionManager.State.WAITING_TO_RESCAN, state.get());
            } else {
                assertEquals(BleConnectionManager.State.RETRYING, state.get());
            }
            assertEquals(1, clock.pendingCount());

            int scansBeforeReissue = scans.size();
            grantGeneration.set(null);
            grantGeneration.set(new DebugBleProbe.Grant());
            clock.advance(scanTimedOut ? 2_000 : 1_000);
            assertEquals(scansBeforeReissue, scans.size());
            assertEquals(1, callbacks.size());
            assertEquals(BleConnectionManager.State.UNAUTHORIZED, state.get());
            assertEquals(0, clock.pendingCount());

            probe.stop();
            probe.start(true);
            scans.get(scansBeforeReissue).accept(sighting);
            var freshLink = callbacks.get(1);
            freshLink.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
            freshLink.onServicesDiscovered(replacement, GATT_SUCCESS);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.CONNECTED, state.get());
            freshLink.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_DISCONNECTED);
            drain(handlerWork);
            clock.advance(999);
            assertEquals(scansBeforeReissue + 1, scans.size());
            clock.advance(1);
            assertEquals(scansBeforeReissue + 2, scans.size());
            assertEquals(2, callbacks.size());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void queuedRecoverySightingCannotUseAReissuedGrant() {
        try {
            var original = startReadyLink();
            original.onServiceChanged(first);
            drain(handlerWork);
            clock.advance(15_000);
            clock.advance(1_000);
            assertEquals(2, scans.size());
            assertEquals(1, callbacks.size());

            var oldScan = scans.get(1);
            handlerWork.add(() -> oldScan.accept(sighting));
            grantGeneration.set(null);
            grantGeneration.set(new DebugBleProbe.Grant());
            drain(handlerWork);
            assertEquals(1, callbacks.size());
            assertEquals(BleConnectionManager.State.UNAUTHORIZED, state.get());
            assertEquals(0, clock.pendingCount());
            assertEquals(2, scans.size());

            probe.stop();
            probe.start(true);
            assertEquals(3, scans.size());
            oldScan.accept(sighting);
            assertEquals(1, callbacks.size());
            scans.get(2).accept(sighting);
            var freshLink = callbacks.get(1);
            freshLink.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
            freshLink.onServicesDiscovered(replacement, GATT_SUCCESS);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.CONNECTED, state.get());
            assertEquals(2, callbacks.size());
            assertEquals(0, clock.pendingCount());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void queuedDiscoveryCannotUseReissuedGrantAfterRevalidationTimeoutRecovery() {
        try {
            var original = startReadyLink();
            original.onServiceChanged(first);
            drain(handlerWork);
            clock.advance(15_000);
            clock.advance(1_000);
            scans.get(1).accept(sighting);

            var previousGrantLink = callbacks.get(1);
            previousGrantLink.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
            drain(handlerWork);
            verify(replacement).discoverServices();
            assertEquals(BleConnectionManager.State.CONNECTING, state.get());
            states.clear();
            previousGrantLink.onServicesDiscovered(replacement, GATT_SUCCESS);
            grantGeneration.set(null);
            grantGeneration.set(new DebugBleProbe.Grant());
            drain(handlerWork);
            assertFalse(states.contains(BleConnectionManager.State.CONNECTED));
            assertEquals(BleConnectionManager.State.UNAUTHORIZED, state.get());
            verify(replacement).close();
            assertEquals(0, clock.pendingCount());
            assertEquals(2, scans.size());
            assertEquals(2, callbacks.size());

            probe.stop();
            probe.start(true);
            scans.get(2).accept(sighting);
            var freshLink = callbacks.get(2);
            freshLink.onConnectionStateChange(newlyAuthorized, GATT_SUCCESS, STATE_CONNECTED);
            previousGrantLink.onServicesDiscovered(replacement, GATT_SUCCESS);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.CONNECTING, state.get());
            freshLink.onServicesDiscovered(newlyAuthorized, GATT_SUCCESS);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.CONNECTED, state.get());
            verify(newlyAuthorized, never()).close();
            assertEquals(0, clock.pendingCount());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void serviceChangeDiscoveryLateSuccessRecoveryRechecksAuthorization() {
        assertRevalidationTimeoutRecovery(false, true);
    }

    private void assertRevalidationTimeoutRecovery(boolean watchdogFirst, boolean revokeBeforeRetry) {
        try {
            var callback = startReadyLink();
            states.clear();
            clock.elapse(15_001);
            callback.onServiceChanged(first);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.REVALIDATING_SERVICES, state.get());
            assertEquals(1, clock.pendingCount());

            callback.onServicesDiscovered(first, GATT_SUCCESS);
            if (watchdogFirst) {
                clock.advance(15_000);
            } else {
                clock.elapse(15_000);
                assertEquals(BleConnectionManager.State.REVALIDATING_SERVICES, state.get());
                assertEquals(1, clock.pendingCount());
            }
            drain(handlerWork);
            verify(first, times(watchdogFirst ? 1 : 2)).getService(DebugBleProbe.SERVICE);
            callback.onServiceChanged(first);
            callback.onServicesDiscovered(first, GATT_SUCCESS);
            drain(handlerWork);
            assertFalse(states.contains(BleConnectionManager.State.CONNECTED));
            verify(first, times(2)).discoverServices();
            verify(first).close();
            assertEquals(BleConnectionManager.State.RETRYING, state.get());
            assertEquals(1, clock.pendingCount());
            clock.advance(999);
            assertEquals(1, scans.size());
            assertEquals(1, callbacks.size());

            if (revokeBeforeRetry) {
                grant.set(false);
                clock.advance(1);
                assertEquals(BleConnectionManager.State.UNAUTHORIZED, state.get());
                assertEquals(1, scans.size());
                assertEquals(1, callbacks.size());
            } else {
                clock.advance(1);
                assertEquals(2, scans.size());
                assertEquals(1, callbacks.size());
                scans.get(1).accept(sighting);
                scans.get(1).accept(sighting);
                var currentCallback = callbacks.get(1);
                currentCallback.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
                currentCallback.onServicesDiscovered(replacement, GATT_SUCCESS);
                drain(handlerWork);
                assertEquals(BleConnectionManager.State.CONNECTED, state.get());
                assertEquals(2, callbacks.size());

                callback.onConnectionStateChange(first, GATT_SUCCESS, STATE_DISCONNECTED);
                drain(handlerWork);
                assertEquals(BleConnectionManager.State.CONNECTED, state.get());
                assertEquals(0, clock.pendingCount());
                clock.advance(30_000);
                assertEquals(BleConnectionManager.State.CONNECTED, state.get());
                assertEquals(2, scans.size());
                assertEquals(2, callbacks.size());
                verify(first).close();
                verify(replacement, never()).close();
            }
            assertEquals(0, clock.pendingCount());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void initialDiscoveryInvalidatedByServiceChangeIsRepeatedSerially() {
        try {
            var callback = startDiscoveringLink();
            callback.onServiceChanged(first);
            callback.onServiceChanged(first);
            drain(handlerWork);
            verify(first).discoverServices();

            callback.onServicesDiscovered(first, GATT_SUCCESS);
            callback.onServiceChanged(first);
            drain(handlerWork);
            verify(first, times(2)).discoverServices();
            verify(first, never()).getService(DebugBleProbe.SERVICE);
            assertEquals(BleConnectionManager.State.CONNECTING, state.get());
            assertEquals(1, clock.pendingCount());

            callback.onServicesDiscovered(first, GATT_SUCCESS);
            drain(handlerWork);
            verify(first).getService(DebugBleProbe.SERVICE);
            verify(first, never()).close();
            assertEquals(BleConnectionManager.State.CONNECTED, state.get());
            assertEquals(1, callbacks.size());
            assertEquals(0, clock.pendingCount());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void expiredInvalidatedInitialDiscoveryDoesNotRepeatBeforeWatchdogRuns() {
        try {
            var callback = startDiscoveringLink();
            callback.onServicesDiscovered(first, GATT_SUCCESS);
            callback.onServiceChanged(first);
            callback.onServicesDiscovered(first, GATT_SUCCESS);
            clock.elapse(15_001);
            assertEquals(BleConnectionManager.State.CONNECTING, state.get());
            assertEquals(1, clock.pendingCount());

            drain(handlerWork);
            verify(first).discoverServices();
            verify(first, never()).getService(DebugBleProbe.SERVICE);
            verify(first).close();
            assertFalse(states.contains(BleConnectionManager.State.CONNECTED));
            assertEquals(BleConnectionManager.State.RETRYING, state.get());
            assertEquals(1, clock.pendingCount());
            clock.advance(999);
            assertEquals(1, scans.size());
            clock.advance(1);
            assertEquals(2, scans.size());
            assertEquals(1, callbacks.size());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void queuedConnectedSkipsDiscoveryWhenWatchdogHasNotRunButDeadlinePassed() {
        try {
            probe.start(true);
            scans.get(0).accept(sighting);
            var callback = callbacks.get(0);
            callback.onConnectionStateChange(first, GATT_SUCCESS, STATE_CONNECTED);
            clock.elapse(15_001);
            assertEquals(BleConnectionManager.State.CONNECTING, state.get());
            assertEquals(1, clock.pendingCount());

            drain(handlerWork);
            verify(first, never()).discoverServices();
            verify(first).close();
            assertEquals(BleConnectionManager.State.RETRYING, state.get());
            assertEquals(1, clock.pendingCount());
            clock.advance(999);
            assertEquals(1, scans.size());
            assertEquals(1, callbacks.size());

            clock.advance(1);
            assertEquals(2, scans.size());
            assertEquals(1, callbacks.size());
            scans.get(1).accept(sighting);
            scans.get(1).accept(sighting);
            var currentCallback = callbacks.get(1);
            currentCallback.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
            currentCallback.onServicesDiscovered(replacement, GATT_SUCCESS);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.CONNECTED, state.get());
            verify(replacement).discoverServices();
            assertEquals(2, callbacks.size());
            assertEquals(0, clock.pendingCount());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void queuedDiscoverySuccessCannotBecomeReadyAfterDeadlineBeforeWatchdog() {
        try {
            var callback = startDiscoveringLink();
            callback.onServicesDiscovered(first, GATT_SUCCESS);
            callback.onServicesDiscovered(first, GATT_SUCCESS);
            clock.elapse(15_001);
            assertEquals(BleConnectionManager.State.CONNECTING, state.get());
            assertEquals(1, clock.pendingCount());

            handlerWork.remove().run();
            assertEquals(BleConnectionManager.State.RETRYING, state.get());
            drain(handlerWork);
            assertFalse(states.contains(BleConnectionManager.State.CONNECTED));
            verify(first).close();
            assertEquals(1, clock.pendingCount());
            clock.advance(999);
            assertEquals(1, scans.size());
            clock.advance(1);
            assertEquals(2, scans.size());
            assertEquals(1, callbacks.size());

            scans.get(1).accept(sighting);
            scans.get(1).accept(sighting);
            var currentCallback = callbacks.get(1);
            currentCallback.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
            currentCallback.onServicesDiscovered(replacement, GATT_SUCCESS);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.CONNECTED, state.get());
            assertEquals(2, callbacks.size());
            assertEquals(0, clock.pendingCount());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void queuedConnectedDoesNotStartDiscoveryAfterDisconnectArrives() {
        try {
            probe.start(true);
            scans.get(0).accept(sighting);
            var callback = callbacks.get(0);
            callback.onConnectionStateChange(first, GATT_SUCCESS, STATE_CONNECTED);
            callback.onConnectionStateChange(first, GATT_SUCCESS, STATE_DISCONNECTED);
            handlerWork.remove().run();
            verify(first, never()).discoverServices();
            verify(first).close();
            assertEquals(BleConnectionManager.State.RETRYING, state.get());
            drain(handlerWork);
            assertEquals(1, clock.pendingCount());
            clock.advance(999);
            assertEquals(1, scans.size());
            clock.advance(1);
            assertEquals(2, scans.size());
            assertEquals(1, callbacks.size());

            scans.get(1).accept(sighting);
            scans.get(1).accept(sighting);
            var currentCallback = callbacks.get(1);
            currentCallback.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
            currentCallback.onServicesDiscovered(replacement, GATT_SUCCESS);
            drain(handlerWork);
            verify(replacement).discoverServices();
            assertEquals(BleConnectionManager.State.CONNECTED, state.get());
            assertEquals(2, callbacks.size());
            assertEquals(0, clock.pendingCount());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void queuedInitialSuccessCannotPublishReadinessAfterDisconnect() {
        try {
            var callback = startDiscoveringLink();
            callback.onServicesDiscovered(first, GATT_SUCCESS);
            callback.onConnectionStateChange(first, GATT_SUCCESS, STATE_DISCONNECTED);
            callback.onConnectionStateChange(first, GATT_SUCCESS, STATE_DISCONNECTED);
            handlerWork.remove().run();
            assertEquals(BleConnectionManager.State.RETRYING, state.get());
            drain(handlerWork);
            verify(first).close();
            verify(first, never()).getService(DebugBleProbe.SERVICE);
            assertEquals(1, clock.pendingCount());
            clock.advance(999);
            assertEquals(1, scans.size());
            clock.advance(1);
            assertEquals(2, scans.size());
            assertEquals(1, callbacks.size());

            scans.get(1).accept(sighting);
            scans.get(1).accept(sighting);
            var currentCallback = callbacks.get(1);
            currentCallback.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
            callback.onConnectionStateChange(first, GATT_SUCCESS, STATE_DISCONNECTED);
            currentCallback.onServicesDiscovered(replacement, GATT_SUCCESS);
            currentCallback.onConnectionStateChange(first, GATT_SUCCESS, STATE_DISCONNECTED);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.CONNECTED, state.get());
            assertEquals(2, callbacks.size());
            assertEquals(0, clock.pendingCount());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void disconnectDuringServiceLookupCannotRestoreRevalidationReadiness() {
        try {
            var callback = startReadyLink();
            callback.onServiceChanged(first);
            drain(handlerWork);
            var service = mock(BluetoothGattService.class);
            when(first.getService(DebugBleProbe.SERVICE)).thenAnswer(call -> {
                callback.onConnectionStateChange(first, BluetoothGatt.GATT_FAILURE, STATE_DISCONNECTED);
                return service;
            });

            callback.onServicesDiscovered(first, GATT_SUCCESS);
            handlerWork.remove().run();
            assertEquals(BleConnectionManager.State.RETRYING, state.get());
            drain(handlerWork);
            verify(first).close();
            assertEquals(1, clock.pendingCount());
            grant.set(false);
            clock.advance(1_000);
            assertEquals(BleConnectionManager.State.UNAUTHORIZED, state.get());
            assertEquals(1, scans.size());
            assertEquals(1, callbacks.size());
            assertEquals(0, clock.pendingCount());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void queuedInitialSuccessCannotPublishReadinessAfterServiceChange() {
        try {
            var callback = startDiscoveringLink();
            callback.onServicesDiscovered(first, GATT_SUCCESS);
            callback.onServiceChanged(first);
            handlerWork.remove().run();
            assertEquals(BleConnectionManager.State.CONNECTING, state.get());
            verify(first, times(2)).discoverServices();
            verify(first, never()).getService(DebugBleProbe.SERVICE);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.CONNECTING, state.get());
            assertEquals(1, clock.pendingCount());

            callback.onServicesDiscovered(first, GATT_SUCCESS);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.CONNECTED, state.get());
            verify(first, times(2)).discoverServices();
            verify(first).getService(DebugBleProbe.SERVICE);
            verify(first, never()).close();
            assertEquals(1, scans.size());
            assertEquals(1, callbacks.size());
            assertEquals(0, clock.pendingCount());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void invalidatedInitialDiscoveryCannotRepeatAfterGrantRevocation() {
        try {
            var callback = startDiscoveringLink();
            callback.onServiceChanged(first);
            drain(handlerWork);
            grant.set(false);
            callback.onServicesDiscovered(first, GATT_SUCCESS);
            drain(handlerWork);

            verify(first).discoverServices();
            verify(first, never()).getService(DebugBleProbe.SERVICE);
            verify(first).close();
            assertEquals(BleConnectionManager.State.UNAUTHORIZED, state.get());
            assertEquals(0, clock.pendingCount());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void repeatingInitialDiscoveryKeepsTheOriginalConnectionDeadline() {
        try {
            var callback = startDiscoveringLink();
            callback.onServiceChanged(first);
            clock.advance(14_999);
            callback.onServicesDiscovered(first, BluetoothGatt.GATT_FAILURE);
            drain(handlerWork);
            verify(first, times(2)).discoverServices();
            verify(first, never()).close();
            assertEquals(BleConnectionManager.State.CONNECTING, state.get());

            clock.advance(1);
            callback.onServicesDiscovered(first, GATT_SUCCESS);
            drain(handlerWork);
            verify(first).close();
            verify(first, never()).getService(DebugBleProbe.SERVICE);
            assertEquals(BleConnectionManager.State.RETRYING, state.get());
            assertEquals(1, clock.pendingCount());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void serviceChangeDuringInitialRepeatClosesLinkAndRecoversThroughFreshScan() {
        try {
            var callback = startRepeatingInitialDiscovery();
            callback.onServiceChanged(first);
            callback.onServiceChanged(first);
            callback.onServicesDiscovered(first, GATT_SUCCESS);
            drain(handlerWork);

            verify(first, times(2)).discoverServices();
            verify(first, never()).getService(DebugBleProbe.SERVICE);
            verify(first).close();
            assertEquals(BleConnectionManager.State.RETRYING, state.get());
            assertEquals(1, clock.pendingCount());
            clock.advance(999);
            assertEquals(1, scans.size());

            clock.advance(1);
            assertEquals(2, scans.size());
            assertEquals(1, callbacks.size());
            scans.get(0).accept(sighting);
            assertEquals(1, callbacks.size());
            scans.get(1).accept(sighting);
            scans.get(1).accept(sighting);
            assertEquals(2, callbacks.size());

            var currentCallback = callbacks.get(1);
            currentCallback.onConnectionStateChange(replacement, GATT_SUCCESS, STATE_CONNECTED);
            callback.onServicesDiscovered(first, GATT_SUCCESS);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.CONNECTING, state.get());
            currentCallback.onServicesDiscovered(replacement, GATT_SUCCESS);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.CONNECTED, state.get());
            assertEquals(0, clock.pendingCount());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void queuedInitialRepeatSuccessCannotPublishReadinessAfterANewServiceChange() {
        try {
            var callback = startRepeatingInitialDiscovery();
            callback.onServicesDiscovered(first, GATT_SUCCESS);
            callback.onServiceChanged(first);
            handlerWork.remove().run();
            assertEquals(BleConnectionManager.State.RETRYING, state.get());
            drain(handlerWork);
            verify(first, times(2)).discoverServices();
            verify(first, never()).getService(DebugBleProbe.SERVICE);
            verify(first).close();
            assertEquals(1, clock.pendingCount());

            grant.set(false);
            clock.advance(1_000);
            assertEquals(BleConnectionManager.State.UNAUTHORIZED, state.get());
            assertEquals(1, scans.size());
            assertEquals(1, callbacks.size());
            assertEquals(0, clock.pendingCount());
        } finally {
            probe.stop();
        }
    }

    @Test
    public void serviceChangeDuringInitialRepeatDoesNotRecoverAfterGrantRevocation() {
        try {
            var callback = startRepeatingInitialDiscovery();
            callback.onServiceChanged(first);
            drain(handlerWork);
            assertEquals(BleConnectionManager.State.RETRYING, state.get());

            grant.set(false);
            clock.advance(1_000);
            assertEquals(BleConnectionManager.State.UNAUTHORIZED, state.get());
            assertEquals(1, scans.size());
            assertEquals(1, callbacks.size());
            assertEquals(0, clock.pendingCount());
        } finally {
            probe.stop();
        }
    }

    private BluetoothGattCallback startRepeatingInitialDiscovery() {
        var callback = startDiscoveringLink();
        callback.onServiceChanged(first);
        callback.onServicesDiscovered(first, GATT_SUCCESS);
        drain(handlerWork);
        verify(first, times(2)).discoverServices();
        return callback;
    }

    private BluetoothGattCallback startDiscoveringLink() {
        probe.start(true);
        scans.get(0).accept(sighting);
        var callback = callbacks.get(0);
        callback.onConnectionStateChange(first, GATT_SUCCESS, STATE_CONNECTED);
        drain(handlerWork);
        return callback;
    }

    private BluetoothGattCallback startReadyLink() {
        var callback = startDiscoveringLink();
        callback.onServicesDiscovered(first, GATT_SUCCESS);
        drain(handlerWork);
        assertEquals(BleConnectionManager.State.CONNECTED, state.get());
        return callback;
    }

    private static void drain(Queue<Runnable> work) {
        while (!work.isEmpty()) {
            work.remove().run();
        }
    }
}
