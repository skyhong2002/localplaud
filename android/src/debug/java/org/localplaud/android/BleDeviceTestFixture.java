package org.localplaud.android;

import android.Manifest;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothManager;
import android.content.Context;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Handler;
import android.text.InputType;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.util.Locale;
import java.util.function.Consumer;
import java.util.function.Predicate;

final class BleDeviceTestFixture {
    private static final String DEVICE = "synthetic-recorder";
    private static final long SIMULATED_HANDSHAKE_MS = 500;
    private static final long SIMULATED_SCAN_MS = 1_000;
    private static final long LAB_PERMISSION_CHECK_MS = 500;
    private static final BleLinkCoordinator<String> SIMULATED_LINKS = new BleLinkCoordinator<>();

    private final Handler handler;
    private final TextView status;
    private final Button start;
    private final Button disconnect;
    private final BleConnectionManager<String> connection;
    private final PlaudSyncActivity activity;
    private final EditText probeAddress;
    private final TextView probeStatus;
    private final Button probeRescan;
    private final Button probeRetryRead;
    private final DebugBleLab lab;
    private final DebugBleLab.ViewBinding probeBinding;
    private final DebugBleReconnectIntent reconnectIntent;
    private final Runnable probePermissionCheck = this::watchProbePermissions;
    private BleConnectionManager.Events active;
    private int attempts;
    private boolean adapterEnabled = true;

    static BleDeviceTestFixture install(PlaudSyncActivity activity, LinearLayout content, Handler handler, Object retained) {
        return new BleDeviceTestFixture(activity, content, handler, retained);
    }

    void adapterChanged(boolean enabled) {
        adapterEnabled = enabled;
        connection.adapterChanged(enabled);
        checkProbePermissions();
        reconnectIntent.adapterChanged(enabled);
        start.setEnabled(enabled && connection.state() == BleConnectionManager.State.STOPPED);
    }

    Object stop(boolean recreating) {
        connection.stop();
        handler.removeCallbacks(probePermissionCheck);
        if (recreating) {
            checkProbePermissions();
            if (probeBinding.detachForRecreation()) {
                return lab;
            }
        }
        reconnectIntent.pause();
        showPendingProbe();
        return null;
    }

    private BleDeviceTestFixture(PlaudSyncActivity activity, LinearLayout content, Handler handler, Object retained) {
        this.activity = activity;
        this.handler = handler;
        activity.text(content, R.string.plaud_simulation_heading, 20);
        status = activity.text(content, R.string.plaud_simulation_idle, 16);
        status.setAccessibilityLiveRegion(TextView.ACCESSIBILITY_LIVE_REGION_POLITE);
        start = activity.button(content, R.string.plaud_simulation_start, this::start);
        disconnect = activity.button(content, R.string.plaud_simulation_disconnect, this::disconnect);
        disconnect.setEnabled(false);
        connection = new BleConnectionManager<>(DEVICE,
                device -> new BleConnectionManager.AuthorizedConnection(this, this::connect),
                BleDeviceStatus.scheduler(handler), this::render, this::scan, SIMULATED_LINKS);
        activity.text(content, R.string.plaud_probe_heading, 20);
        activity.text(content, R.string.plaud_probe_description, 16);
        probeAddress = new EditText(activity);
        probeAddress.setHint(R.string.plaud_probe_address);
        probeAddress.setSingleLine(true);
        probeAddress.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        content.addView(probeAddress);
        probeStatus = activity.text(content, R.string.plaud_probe_idle, 16);
        probeStatus.setAccessibilityLiveRegion(TextView.ACCESSIBILITY_LIVE_REGION_POLITE);
        probeRetryRead = activity.button(content, R.string.plaud_probe_retry_read, this::retryProbeRead);
        probeRetryRead.setVisibility(Button.GONE);
        probeRescan = activity.button(content, R.string.plaud_probe_rescan, () -> {});
        probeRescan.setVisibility(Button.GONE);
        probeRescan.setEnabled(false);
        lab = DebugBleLab.fromNonConfigurationInstance(retained, () -> createLab(activity.getApplicationContext(), handler));
        reconnectIntent = lab.reconnectIntent;
        String savedAddress = reconnectIntent.pendingAddress();
        if (savedAddress != null) {
            probeAddress.setText(savedAddress);
        }
        probeBinding = lab.bind(this::renderProbe);
        if (reconnectIntent.hasActiveSession()) {
            watchProbePermissions();
        }
        activity.button(content, R.string.plaud_probe_start, this::startProbe);
        activity.button(content, R.string.plaud_probe_stop, () -> {
            handler.removeCallbacks(probePermissionCheck);
            if (reconnectIntent.forget()) {
                showPendingProbe();
            } else {
                probeStatus.setText(R.string.plaud_probe_storage_failed);
            }
        });
    }

    private static DebugBleLab createLab(Context context, Handler handler) {
        return new DebugBleLab(render -> new DebugBleReconnectIntent(new DebugBleReconnectIntent.Store() {
            @Override
            public String readAddress() {
                var preferences = context.getSharedPreferences("debug_ble_lab_intent", Context.MODE_PRIVATE);
                String address = preferences.getString("peer_address", null);
                return BluetoothAdapter.checkBluetoothAddress(address) ? address : null;
            }

            @Override
            public boolean writeAddress(String address) {
                var preferences = context.getSharedPreferences("debug_ble_lab_intent", Context.MODE_PRIVATE);
                return preferences.edit().putString("peer_address", address).commit();
            }
        }, () -> hasProbePermissions(context), (address, stillAuthorized) -> {
            var adapter = context.getSystemService(BluetoothManager.class).getAdapter();
            return new DebugBleProbe(address, new AndroidDebugBleTransport(context, adapter, handler),
                    BleDeviceStatus.scheduler(handler), render, stillAuthorized);
        }));
    }

    private void renderProbe(BleConnectionManager.State state, int readySessions) {
        if (state == BleConnectionManager.State.STOPPED) {
            showPendingProbe();
        } else {
            int message = probeMessage(state, activity);
            probeStatus.setText(activity.getString(R.string.plaud_probe_status, activity.getString(message), readySessions));
        }
        updateProbeRescan(state);
    }

    static int probeMessage(BleConnectionManager.State state, Context context) {
        return state == BleConnectionManager.State.UNAUTHORIZED && !hasProbePermissions(context)
                ? R.string.plaud_probe_permission_revoked : probeMessage(state);
    }

    static int probeMessage(BleConnectionManager.State state) {
        return switch (state) {
            case RESCAN_COOLDOWN -> R.string.plaud_probe_rescan_cooldown;
            case RESCAN_READY -> R.string.plaud_probe_rescan_ready;
            default -> BleDeviceStatus.message(state);
        };
    }

    private void updateProbeRescan(BleConnectionManager.State state) {
        String revision = reconnectIntent.manualRescanRevision();
        probeRescan.setVisibility(revision != null || state == BleConnectionManager.State.RESCAN_COOLDOWN
                ? Button.VISIBLE : Button.GONE);
        probeRescan.setEnabled(revision != null);
        probeRescan.setOnClickListener(revision == null ? null : ignored -> probeBinding.requestManualRescan(revision));
    }

    private void startProbe() {
        checkProbePermissions();
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S || !adapterEnabled) {
            probeStatus.setText(R.string.plaud_probe_unavailable);
            return;
        }
        if (!hasProbePermissions(activity)) {
            activity.requestPermissions(new String[]{Manifest.permission.BLUETOOTH_SCAN,
                    Manifest.permission.BLUETOOTH_CONNECT}, 91);
            probeStatus.setText(R.string.plaud_probe_permission);
            return;
        }
        String address = probeAddress.getText().toString().trim().toUpperCase(Locale.ROOT);
        if (!BluetoothAdapter.checkBluetoothAddress(address)) {
            probeAddress.setError(activity.getString(R.string.plaud_probe_address));
            return;
        }
        var bluetooth = activity.getSystemService(BluetoothManager.class);
        var adapter = bluetooth == null ? null : bluetooth.getAdapter();
        if (adapter == null) {
            probeStatus.setText(R.string.plaud_probe_unavailable);
            return;
        }
        Predicate<String> grant = address::equals;
        var result = reconnectIntent.startOrResume(address, grant);
        if (result == DebugBleReconnectIntent.Result.STARTED) {
            handler.removeCallbacks(probePermissionCheck);
            handler.postDelayed(probePermissionCheck, LAB_PERMISSION_CHECK_MS);
        } else if (result == DebugBleReconnectIntent.Result.STORAGE_FAILED) {
            probeStatus.setText(R.string.plaud_probe_storage_failed);
            updateProbeRetryRead();
        } else if (result == DebugBleReconnectIntent.Result.NOT_AUTHORIZED) {
            probeStatus.setText(R.string.plaud_probe_permission);
        }
    }

    private void watchProbePermissions() {
        checkProbePermissions();
        if (reconnectIntent.hasActiveSession()) {
            handler.postDelayed(probePermissionCheck, LAB_PERMISSION_CHECK_MS);
        }
    }

    private void checkProbePermissions() {
        if (reconnectIntent.invalidateIfPermissionLost()) {
            handler.removeCallbacks(probePermissionCheck);
            probeStatus.setText(reconnectIntent.storageFailed()
                    ? R.string.plaud_probe_storage_failed : R.string.plaud_probe_permission_revoked);
        }
    }

    static boolean hasProbePermissions(Context context) {
        return context.checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED
                && context.checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN) == PackageManager.PERMISSION_GRANTED;
    }

    private void showPendingProbe() {
        showPendingProbe(reconnectIntent.pendingAddress());
    }

    private void showPendingProbe(String address) {
        probeStatus.setText(pendingProbeMessage(address, reconnectIntent.storageFailed()));
        updateProbeRetryRead();
    }

    private void updateProbeRetryRead() {
        probeRetryRead.setVisibility(reconnectIntent.intentReadFailed() && !reconnectIntent.hasActiveSession()
                ? Button.VISIBLE : Button.GONE);
    }

    private void retryProbeRead() {
        probeBinding.retryPendingAddress(address -> {
            if (address != null && probeAddress.length() == 0) {
                probeAddress.setText(address);
            }
            showPendingProbe(address);
        });
    }

    static int pendingProbeMessage(String address, boolean storageFailed) {
        if (storageFailed) {
            return R.string.plaud_probe_storage_failed;
        }
        return address == null ? R.string.plaud_probe_idle : R.string.plaud_probe_saved;
    }

    private void start() {
        if (adapterEnabled && connection.state() == BleConnectionManager.State.STOPPED) {
            attempts = 0;
            connection.start();
            connection.deviceSeen(DEVICE);
        }
    }

    private Runnable scan(Consumer<String> seen, Consumer<BleConnectionManager.ScanOutcome> finished) {
        Runnable result = () -> seen.accept(DEVICE);
        handler.postDelayed(result, SIMULATED_SCAN_MS);
        return () -> handler.removeCallbacks(result);
    }

    private Runnable connect(BleConnectionManager.Events events) {
        active = events;
        attempts++;
        Runnable opened = events::authorizedSessionOpened;
        handler.postDelayed(opened, SIMULATED_HANDSHAKE_MS);
        return () -> {
            handler.removeCallbacks(opened);
            if (active == events) {
                active = null;
            }
        };
    }

    private void disconnect() {
        if (connection.state() == BleConnectionManager.State.CONNECTED && active != null) {
            active.disconnected();
        }
    }

    private void render(BleConnectionManager.State state) {
        int message = switch (state) {
            case STOPPED -> R.string.plaud_simulation_idle;
            case CONNECTED -> attempts > 1 ? R.string.plaud_simulation_reconnected : BleDeviceStatus.message(state);
            default -> BleDeviceStatus.message(state);
        };
        status.setText(message);
        start.setEnabled(adapterEnabled && state == BleConnectionManager.State.STOPPED);
        disconnect.setEnabled(state == BleConnectionManager.State.CONNECTED);
    }
}
