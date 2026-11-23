package org.localplaud.android;

import android.Manifest;
import android.annotation.SuppressLint;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCallback;
import android.bluetooth.BluetoothProfile;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanFilter;
import android.content.Context;
import android.content.pm.PackageManager;
import android.os.Handler;
import android.os.ParcelUuid;

import java.util.List;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Consumer;

@SuppressLint("MissingPermission")
final class AndroidDebugBleTransport implements DebugBleProbe.Transport {
    private final Context context;
    private final BluetoothAdapter adapter;
    private final Handler handler;

    AndroidDebugBleTransport(Context context, BluetoothAdapter adapter, Handler handler) {
        this.context = context;
        this.adapter = adapter;
        this.handler = handler;
    }

    @Override
    public Runnable scan(String address, Consumer<DebugBleProbe.Sighting> seen,
            Consumer<BleConnectionManager.ScanOutcome> finished) {
        var outcome = BleConnectionManager.ScanOutcome.FAILED;
        try {
            var scanner = adapter.getBluetoothLeScanner();
            if (scanner != null) {
                var service = new ParcelUuid(DebugBleProbe.SERVICE);
                var discovery = new AndroidBleDiscovery(scanner, handler, this::classifyScanFailure);
                var filter = new ScanFilter.Builder().setDeviceAddress(address).setServiceUuid(service).build();
                discovery.start(List.of(filter), result -> {
                    var record = result.getScanRecord();
                    if (record != null && record.getServiceUuids() != null && record.getServiceUuids().contains(service)) {
                        seen.accept(new DebugBleProbe.Sighting(result.getDevice().getAddress(), DebugBleProbe.SERVICE));
                    }
                }, () -> finished.accept(BleConnectionManager.ScanOutcome.TIMED_OUT), finished);
                return discovery::stop;
            }
            if (canRetryScannerStartup()) {
                outcome = BleConnectionManager.ScanOutcome.TEMPORARY_FAILURE;
            }
        } catch (SecurityException | IllegalStateException ignored) {
        }
        finished.accept(outcome);
        return () -> {};
    }

    private BleConnectionManager.ScanOutcome classifyScanFailure(int errorCode) {
        if (errorCode == ScanCallback.SCAN_FAILED_APPLICATION_REGISTRATION_FAILED && canRetryScannerStartup()) {
            return BleConnectionManager.ScanOutcome.TEMPORARY_FAILURE;
        }
        return AndroidBleDiscovery.classifyStartFailure(errorCode);
    }

    private boolean canRetryScannerStartup() {
        return adapter.isEnabled()
                && context.checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN) == PackageManager.PERMISSION_GRANTED;
    }

    @Override
    public Runnable open(String address, DebugBleProbe.LinkEvents events) {
        var link = new Link(events);
        if (context.checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) != PackageManager.PERMISSION_GRANTED) {
            events.bluetoothPermissionDenied();
            return link::close;
        }
        try {
            link.active = adapter.getRemoteDevice(address)
                    .connectGatt(context, false, link, BluetoothDevice.TRANSPORT_LE);
        } catch (SecurityException denied) {
            events.bluetoothPermissionDenied();
            return link::close;
        }
        if (link.active == null) {
            events.lost();
        }
        return link::close;
    }

    private enum Discovery {
        NOT_STARTED, INITIAL_RUNNING, INITIAL_REPEAT_RUNNING, REVALIDATING, READY;

        boolean isFollowUp() {
            return this == INITIAL_REPEAT_RUNNING || this == REVALIDATING;
        }
    }

    private final class Link extends BluetoothGattCallback {
        private final DebugBleProbe.LinkEvents events;
        private final AtomicLong serviceChanges = new AtomicLong();
        private volatile BluetoothGatt active;
        private volatile boolean disconnectReported;
        private Discovery discovery = Discovery.NOT_STARTED;
        private long discoveryWatermark;

        Link(DebugBleProbe.LinkEvents events) {
            this.events = events;
        }

        @Override
        public void onConnectionStateChange(BluetoothGatt gatt, int status, int state) {
            boolean lost = status != BluetoothGatt.GATT_SUCCESS || state == BluetoothProfile.STATE_DISCONNECTED;
            if (lost && active == gatt) {
                disconnectReported = true;
            }
            handler.post(() -> {
                if (active != gatt) {
                    return;
                }
                if (lost || disconnectReported) {
                    events.lost();
                } else if (state == BluetoothProfile.STATE_CONNECTED && discovery == Discovery.NOT_STARTED) {
                    discover(gatt, Discovery.INITIAL_RUNNING);
                }
            });
        }

        @Override
        public void onServiceChanged(BluetoothGatt gatt) {
            if (active != gatt) {
                return;
            }
            long change = serviceChanges.incrementAndGet();
            handler.post(() -> {
                if (change <= discoveryWatermark || discardLostLink(gatt)) {
                    return;
                }
                if (discovery.isFollowUp()) {
                    events.lost();
                } else if (discovery == Discovery.READY && events.servicesChanged()) {
                    discover(gatt, Discovery.REVALIDATING);
                }
            });
        }

        private void discover(BluetoothGatt gatt, Discovery phase) {
            if (discardLostLink(gatt) || !events.discovering()) {
                return;
            }
            discovery = phase;
            discoveryWatermark = serviceChanges.get();
            if (discardLostLink(gatt)) {
                return;
            }
            boolean started;
            try {
                started = gatt.discoverServices();
            } catch (SecurityException ignored) {
                events.bluetoothPermissionDenied();
                return;
            } catch (IllegalStateException ignored) {
                events.services(false);
                return;
            }
            if (!started && !discardLostLink(gatt)) {
                events.discoveryRetryableFailure();
            }
        }

        @Override
        public void onServicesDiscovered(BluetoothGatt gatt, int status) {
            handler.post(() -> {
                if (discardLostLink(gatt)) {
                    return;
                }
                if ((discovery != Discovery.INITIAL_RUNNING && !discovery.isFollowUp())
                        || discardInvalidatedDiscovery(gatt)) {
                    return;
                }
                if (status == BluetoothGatt.GATT_FAILURE) {
                    events.discoveryRetryableFailure();
                } else {
                    boolean servicePresent = status == BluetoothGatt.GATT_SUCCESS
                            && gatt.getService(DebugBleProbe.SERVICE) != null;
                    if (discardLostLink(gatt) || discardInvalidatedDiscovery(gatt)) {
                        return;
                    }
                    if (events.services(servicePresent) && active == gatt) {
                        discovery = Discovery.READY;
                    }
                }
            });
        }

        private boolean discardLostLink(BluetoothGatt gatt) {
            if (active != gatt) {
                return true;
            }
            if (disconnectReported) {
                events.lost();
                return true;
            }
            return false;
        }

        private boolean discardInvalidatedDiscovery(BluetoothGatt gatt) {
            if (serviceChanges.get() <= discoveryWatermark) {
                return false;
            }
            if (discovery.isFollowUp()) {
                events.lost();
            } else if (events.canRepeatDiscovery()) {
                discover(gatt, Discovery.INITIAL_REPEAT_RUNNING);
            }
            return true;
        }

        void close() {
            BluetoothGatt previous = active;
            active = null;
            if (previous != null) {
                try {
                    previous.disconnect();
                } catch (SecurityException | IllegalStateException ignored) {
                }
                try {
                    previous.close();
                } catch (SecurityException | IllegalStateException ignored) {
                }
            }
        }
    }
}
