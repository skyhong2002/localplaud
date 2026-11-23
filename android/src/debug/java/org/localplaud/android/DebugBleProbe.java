package org.localplaud.android;

import java.util.UUID;
import java.util.function.BooleanSupplier;
import java.util.function.Consumer;
import java.util.function.Supplier;

final class DebugBleProbe {
    static final UUID SERVICE = UUID.fromString("85ffec0f-a47f-407b-b20c-b70260aea5d5");
    private static final BleLinkCoordinator<String> LINKS = new BleLinkCoordinator<>();

    record Sighting(String address, UUID service) {}

    static final class Grant {}

    interface Transport {
        Runnable scan(String address, Consumer<Sighting> seen, Consumer<BleConnectionManager.ScanOutcome> finished);
        Runnable open(String address, LinkEvents events);
    }

    interface LinkEvents {
        boolean discovering();
        boolean servicesChanged();
        boolean canRepeatDiscovery();
        void discoveryRetryableFailure();
        void bluetoothPermissionDenied();
        boolean services(boolean testServicePresent);
        void lost();
    }

    private final String address;
    private final Transport transport;
    private final BleConnectionManager<String> manager;
    private boolean allowed;

    DebugBleProbe(String address, Transport transport, BleConnectionManager.Scheduler scheduler,
            Consumer<BleConnectionManager.State> stateChanged) {
        this(address, transport, scheduler, stateChanged, () -> true);
    }

    DebugBleProbe(String address, Transport transport, BleConnectionManager.Scheduler scheduler,
            Consumer<BleConnectionManager.State> stateChanged, BooleanSupplier stillAuthorized) {
        this(address, transport, scheduler, stateChanged, sessionGrant(stillAuthorized));
    }

    DebugBleProbe(String address, Transport transport, BleConnectionManager.Scheduler scheduler,
            Consumer<BleConnectionManager.State> stateChanged, Supplier<Grant> currentGrant) {
        this.address = address;
        this.transport = transport;
        manager = new BleConnectionManager<>(address, device -> {
            Grant grant = allowed && address.equals(device) ? currentGrant.get() : null;
            return grant == null ? null : new BleConnectionManager.AuthorizedConnection(grant, this::open);
        }, scheduler, stateChanged, (seen, finished) -> transport.scan(address, sighting -> {
            if (address.equals(sighting.address()) && SERVICE.equals(sighting.service())) {
                seen.accept(address);
            }
        }, finished), LINKS);
    }

    private static Supplier<Grant> sessionGrant(BooleanSupplier stillAuthorized) {
        Grant grant = new Grant();
        return () -> stillAuthorized.getAsBoolean() ? grant : null;
    }

    void start(boolean testGrant) {
        allowed = testGrant;
        manager.start();
        manager.requestDiscovery();
    }

    void revoke() {
        allowed = false;
        manager.authorizationChanged();
    }

    void adapterChanged(boolean enabled) {
        manager.adapterChanged(enabled);
    }

    String manualRescanRevision() {
        return manager.manualRescanRevision();
    }

    void requestManualRescan(String expectedRevision) {
        manager.requestManualRescan(expectedRevision);
    }

    void stop() {
        allowed = false;
        manager.stop();
    }

    boolean permissionDenied() {
        return manager.state() == BleConnectionManager.State.BLUETOOTH_PERMISSION_DENIED;
    }

    boolean requiresExplicitRestart() {
        return permissionDenied() || manager.state() == BleConnectionManager.State.UNAUTHORIZED;
    }

    private Runnable open(BleConnectionManager.Events attempt) {
        return transport.open(address, new LinkEvents() {
            private boolean discovering;
            private boolean ready;

            @Override
            public boolean discovering() {
                discovering = attempt.gattServiceDiscoveryStarted();
                return discovering;
            }

            @Override
            public boolean servicesChanged() {
                if (!ready) {
                    return false;
                }
                ready = false;
                return attempt.gattServicesChanged();
            }

            @Override
            public boolean canRepeatDiscovery() {
                return attempt.canRepeatGattDiscovery();
            }

            @Override
            public void discoveryRetryableFailure() {
                if (discovering && !ready) {
                    attempt.disconnected();
                }
            }

            @Override
            public void bluetoothPermissionDenied() {
                attempt.bluetoothPermissionDenied();
            }

            @Override
            public boolean services(boolean testServicePresent) {
                if (!discovering || ready) {
                    return false;
                }
                if (!testServicePresent) {
                    attempt.connectionRejected();
                    return false;
                }
                attempt.gattServicesDiscovered();
                ready = attempt.authorizedSessionOpened();
                return ready;
            }

            @Override
            public void lost() {
                attempt.disconnectedAndRediscover();
            }
        });
    }
}
