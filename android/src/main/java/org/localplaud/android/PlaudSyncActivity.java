package org.localplaud.android;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothManager;
import android.bluetooth.le.BluetoothLeScanner;
import android.bluetooth.le.ScanRecord;
import android.bluetooth.le.ScanResult;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.hardware.usb.UsbConstants;
import android.hardware.usb.UsbDevice;
import android.hardware.usb.UsbManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.ParcelUuid;
import android.os.SystemClock;
import android.provider.Settings;
import android.util.Log;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.io.IOException;
import java.net.URI;
import java.util.List;
import java.util.Locale;
import java.util.Optional;
import java.util.function.Consumer;

public final class PlaudSyncActivity extends Activity {
    private static final String PLAUD_PACKAGE = "ai.plaud.android.plaud";
    private static final int BLUETOOTH_PERMISSION = 1;
    private static final String PLAUD_SERVICE_UUID = "00001910-0000-1000-8000-00805f9b34fb";
    private static final int NORDIC_COMPANY_ID = 0x0059;
    private static final String RESCAN_STATE = "plaud_rescan";
    private static final String RESCAN_TARGET = "target";
    private static final String RESCAN_DEADLINE = "deadline";
    private static final String RESCAN_BOOT_COUNT = "boot_count";
    private static final String RESCAN_SCANS_STARTED = "scans_started";
    private static final String RESCAN_REVISION = "revision";
    private static final String LEGACY_RESCAN_REVISION = "legacy";
    private static final BleLinkCoordinator<String> RECORDER_LINKS = new BleLinkCoordinator<>();

    private final Handler handler = new Handler(Looper.getMainLooper());
    private AndroidBleDiscovery discovery;
    private BleConnectionManager<String> recorderConnection;
    private BleConnectionManager.ManualRescan<String> retainedRescan;
    private Object retainedBleLab;
    private BleDeviceTestFixture bleFixture;
    private AndroidBluetoothState bluetoothState;
    private boolean screenStarted;
    private boolean adapterEnabled;
    private boolean sawAdvertising;
    private boolean sawNordic;
    private TextView usbStatus;
    private TextView bluetoothStatus;
    private Button scanButton;
    private Button rescanButton;
    private TextView connectionStatus;
    private TextView uploadStatus;
    private TextView uploadNetworkStatus;
    private UploadNetworkMonitor uploadNetworkMonitor;
    private Button startApprovalButton;
    private Button uploadPendingButton;
    private RecordingUploadApproval approval;
    private RecordingUploadApproval.Grant uploadGrant;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Object retainedLab = null;
        if (getLastNonConfigurationInstance() instanceof RetainedBluetooth saved) {
            retainedRescan = saved.recorderRescan();
            retainedLab = saved.debugLab();
        }
        ScrollView scroll = new ScrollView(this);
        LinearLayout content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        int padding = Math.round(24 * getResources().getDisplayMetrics().density);
        content.setPadding(padding, padding, padding, padding);
        scroll.addView(content, new ScrollView.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            scroll.setOnApplyWindowInsetsListener((view, insets) -> {
                var bars = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
                view.setPadding(bars.left, bars.top, bars.right, bars.bottom);
                return insets;
            });
        }
        setContentView(scroll);
        text(content, R.string.sync_plaud, 26);
        text(content, R.string.plaud_sync_diagnostics, 16);
        button(content, R.string.plaud_open_official_app, this::openPlaud);
        bluetoothStatus = text(content, R.string.plaud_scan_idle, 16);
        bluetoothStatus.setAccessibilityLiveRegion(TextView.ACCESSIBILITY_LIVE_REGION_POLITE);
        scanButton = button(content, R.string.plaud_scan, this::requestScan);
        connectionStatus = text(content, R.string.plaud_connection_unavailable, 16);
        connectionStatus.setAccessibilityLiveRegion(TextView.ACCESSIBILITY_LIVE_REGION_POLITE);
        rescanButton = button(content, R.string.plaud_rescan, this::requestRescan);
        refreshScanButtons();
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) {
            bluetoothStatus.setText(R.string.plaud_scan_android_version);
            scanButton.setEnabled(false);
        }
        usbStatus = text(content, R.string.plaud_usb_unavailable, 16);
        usbStatus.setAccessibilityLiveRegion(TextView.ACCESSIBILITY_LIVE_REGION_POLITE);
        button(content, R.string.plaud_check_usb, this::refreshUsb);
        bleFixture = BleDeviceTestFixture.install(this, content, handler, retainedLab);
        if (BuildConfig.DEBUG) {
            uploadNetworkStatus = text(content, R.string.plaud_upload_network_blocked, 16);
            uploadNetworkStatus.setAccessibilityLiveRegion(TextView.ACCESSIBILITY_LIVE_REGION_POLITE);
            uploadNetworkStatus.setVisibility(TextView.GONE);
            uploadStatus = text(content, R.string.plaud_approval_idle, 16);
            uploadStatus.setAccessibilityLiveRegion(TextView.ACCESSIBILITY_LIVE_REGION_POLITE);
            startApprovalButton = button(content, R.string.plaud_approval_start, this::startUploadApproval);
            uploadPendingButton = button(content, R.string.plaud_approval_check, this::uploadPendingRecordings);
            uploadPendingButton.setEnabled(false);
        }
    }

    @Override
    protected void onStart() {
        super.onStart();
        screenStarted = true;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            bluetoothState = new AndroidBluetoothState(this, this::adapterChanged);
            bluetoothState.start();
            restoreScanBudget();
        }
        if (BuildConfig.DEBUG) {
            uploadNetworkMonitor = UploadNetworkMonitor.start(this, uploadNetworkStatus, handler);
        }
    }

    private void startUploadApproval() {
        RecordingUploadApproval pending = new RecordingUploadApproval(URI.create(BuildConfig.WORKSPACE_ORIGIN), BuildConfig.DEBUG);
        new AlertDialog.Builder(this)
                .setTitle(R.string.plaud_approval_start)
                .setMessage(getString(R.string.plaud_approval_code, pending.comparisonCode()))
                .setNegativeButton(android.R.string.cancel, null)
                .setPositiveButton(R.string.plaud_approval_open_browser, (dialog, which) -> openUploadApproval(pending))
                .show();
    }

    private void openUploadApproval(RecordingUploadApproval pending) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(pending.browserUri().toString())));
            approval = pending;
            uploadGrant = null;
            uploadPendingButton.setEnabled(true);
            uploadStatus.setText(getString(R.string.plaud_approval_code, pending.comparisonCode()));
        } catch (ActivityNotFoundException error) {
            uploadStatus.setText(R.string.plaud_approval_browser_missing);
        }
    }

    private void uploadPendingRecordings() {
        RecordingUploadApproval pending = approval;
        RecordingUploadApproval.Grant existing = uploadGrant;
        if (pending == null || !uploadPendingButton.isEnabled()) {
            return;
        }
        uploadStatus.setText(R.string.plaud_approval_checking);
        startApprovalButton.setEnabled(false);
        uploadPendingButton.setEnabled(false);
        new Thread(() -> {
            RecordingUploadApproval.Grant grant = existing;
            int completed = 0;
            int message;
            String batchSummary = null;
            try {
                if (grant == null) {
                    grant = pending.claim();
                }
                if (grant == null) {
                    message = R.string.plaud_approval_waiting;
                } else if (grant.isExpired()) {
                    message = R.string.plaud_approval_expired;
                } else {
                    RecordingUploadQueue queue = RecordingUploadQueue.forOrigin(
                            getNoBackupFilesDir().toPath().resolve("recording-uploads"),
                            BuildConfig.WORKSPACE_ORIGIN, grant.scope);
                    RecordingUploader uploader = new RecordingUploader();
                    RecordingUploader.BatchResult batch = uploader.uploadPending(queue, grant.scope, grant.token);
                    completed = batch.uploaded;
                    if (batch.unauthorized) {
                        message = R.string.plaud_approval_expired;
                    } else if (batch.retryable + batch.diagnostics.failed
                            + batch.diagnostics.quarantined + batch.diagnostics.unavailable > 0) {
                        batchSummary = getString(R.string.plaud_approval_batch_result, completed,
                                batch.retryable, batch.diagnostics.failed
                                        + batch.diagnostics.quarantined, batch.diagnostics.unavailable);
                        message = R.string.plaud_approval_batch_result;
                    } else {
                        message = completed == 0 ? R.string.plaud_approval_no_queue : R.string.plaud_approval_uploaded;
                    }
                }
            } catch (RecordingUploader.Rejected error) {
                Log.w("LocalplaudUpload", "Workspace rejected upload authorization or audio: " + error.status);
                message = error.status == 401 ? R.string.plaud_approval_expired : R.string.plaud_approval_failed;
            } catch (IOException | IllegalArgumentException error) {
                Log.w("LocalplaudUpload", error.getClass().getSimpleName());
                message = R.string.plaud_approval_failed;
            }
            final RecordingUploadApproval.Grant result = grant;
            final int status = message;
            final int count = completed;
            final String summary = batchSummary;
            handler.post(() -> {
                if (isDestroyed()) {
                    return;
                }
                uploadGrant = result;
                startApprovalButton.setEnabled(true);
                uploadPendingButton.setEnabled(status != R.string.plaud_approval_expired);
                uploadStatus.setText(summary != null ? summary : status == R.string.plaud_approval_uploaded
                        ? getString(R.string.plaud_approval_uploaded, count) : getString(status));
            });
        }, "localplaud-upload-test").start();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (bluetoothState != null) {
            bluetoothState.refresh();
        }
        refreshUsb();
    }

    @Override
    protected void onStop() {
        screenStarted = false;
        boolean recreating = isChangingConfigurations();
        retainedRescan = recreating && recorderConnection != null
                ? recorderConnection.manualRescanForRecreation() : null;
        if (bluetoothState != null) {
            bluetoothState.stop();
            bluetoothState = null;
        }
        if (uploadNetworkMonitor != null) {
            uploadNetworkMonitor.stop();
            uploadNetworkMonitor = null;
        }
        if (discovery != null) {
            endScan(R.string.plaud_scan_idle);
        }
        clearRecorderConnection();
        retainedBleLab = bleFixture.stop(recreating);
        super.onStop();
    }

    private SharedPreferences rescanJournal() {
        return getSharedPreferences(RESCAN_STATE, MODE_PRIVATE);
    }

    private BleScanJournal.Result replaceScanDebit(String expectedRevision, String nextRevision, String target,
            int scansStarted, long availableAfter) {
        int bootCount = bootCount();
        if (bootCount < 0) {
            return BleScanJournal.Result.STORAGE_FAILURE;
        }
        var preferences = rescanJournal();
        return BleScanJournal.replace(expectedRevision,
                () -> savedScanBudget(preferences, bootCount).map(BleConnectionManager.SavedScanBudget::revision)
                        .orElse(null),
                () -> {
                    var editor = preferences.edit();
                    if (scansStarted == 0) {
                        return editor.clear().commit();
                    }
                    return editor.putString(RESCAN_TARGET, target).putInt(RESCAN_SCANS_STARTED, scansStarted)
                            .putLong(RESCAN_DEADLINE, availableAfter).putInt(RESCAN_BOOT_COUNT, bootCount)
                            .putString(RESCAN_REVISION, nextRevision).commit();
                });
    }

    private Optional<BleConnectionManager.SavedScanBudget<String>> savedScanBudget(
            SharedPreferences preferences, int bootCount) {
        var state = preferences.getAll();
        if (!(state.get(RESCAN_TARGET) instanceof String target) || !BluetoothAdapter.checkBluetoothAddress(target)
                || !(state.get(RESCAN_SCANS_STARTED) instanceof Integer scansStarted)
                || !(state.get(RESCAN_DEADLINE) instanceof Long deadline)
                || !(state.get(RESCAN_BOOT_COUNT) instanceof Integer savedBoot) || savedBoot != bootCount) {
            return Optional.empty();
        }
        String revision = state.get(RESCAN_REVISION) instanceof String value ? value : LEGACY_RESCAN_REVISION;
        return Optional.of(new BleConnectionManager.SavedScanBudget<>(target, scansStarted, deadline,
                savedBoot, revision));
    }

    private BleConnectionManager.ManualRescan<String> readSavedRescan() {
        int bootCount = bootCount();
        return savedScanBudget(rescanJournal(), bootCount)
                .map(saved -> saved.restoreForBoot(bootCount, SystemClock.elapsedRealtime())).orElse(null);
    }

    private int bootCount() {
        return Settings.Global.getInt(getContentResolver(), Settings.Global.BOOT_COUNT, -1);
    }

    @Override
    public Object onRetainNonConfigurationInstance() {
        return retainedRescan == null && retainedBleLab == null ? null
                : new RetainedBluetooth(retainedRescan, retainedBleLab);
    }

    private record RetainedBluetooth(BleConnectionManager.ManualRescan<String> recorderRescan, Object debugLab) {}

    private void restoreScanBudget() {
        var saved = retainedRescan != null ? retainedRescan : readSavedRescan();
        retainedRescan = null;
        if (saved != null) {
            prepareRecorderConnection(saved.target());
            recorderConnection.restoreManualRescan(saved);
        }
    }

    TextView text(LinearLayout parent, int resource, int size) {
        TextView view = new TextView(this);
        view.setText(resource);
        view.setTextSize(size);
        parent.addView(view);
        return view;
    }

    Button button(LinearLayout parent, int resource, Runnable action) {
        Button view = new Button(this);
        view.setText(resource);
        view.setAllCaps(false);
        view.setOnClickListener(ignored -> action.run());
        parent.addView(view);
        return view;
    }

    private void refreshUsb() {
        UsbManager usb = getSystemService(UsbManager.class);
        if (usb == null || !getPackageManager().hasSystemFeature(PackageManager.FEATURE_USB_HOST)) {
            usbStatus.setText(R.string.plaud_usb_unavailable);
            return;
        }
        boolean peripheralFound = false;
        for (UsbDevice device : usb.getDeviceList().values()) {
            peripheralFound = true;
            if (device.getDeviceClass() == UsbConstants.USB_CLASS_MASS_STORAGE) {
                usbStatus.setText(R.string.plaud_usb_storage);
                return;
            }
            for (int index = 0; index < device.getInterfaceCount(); index++) {
                if (device.getInterface(index).getInterfaceClass() == UsbConstants.USB_CLASS_MASS_STORAGE) {
                    usbStatus.setText(R.string.plaud_usb_storage);
                    return;
                }
            }
        }
        usbStatus.setText(peripheralFound ? R.string.plaud_usb_other : R.string.plaud_usb_no_device);
    }

    private boolean hasBluetoothScanPermission() {
        return checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN) == PackageManager.PERMISSION_GRANTED;
    }

    private void requestScan() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S || !adapterEnabled || managedRescanPending()) {
            return;
        }
        if (!hasBluetoothScanPermission()) {
            requestPermissions(new String[] {Manifest.permission.BLUETOOTH_SCAN}, BLUETOOTH_PERMISSION);
            return;
        }
        startScan();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == BLUETOOTH_PERMISSION) {
            if (hasBluetoothScanPermission()) {
                startScan();
            } else {
                bluetoothStatus.setText(R.string.plaud_scan_permission);
            }
        }
    }

    private void startScan() {
        if (!screenStarted || !adapterEnabled || !hasBluetoothScanPermission() || discovery != null
                || managedRescanPending()) {
            return;
        }
        clearRecorderConnection();
        sawAdvertising = false;
        sawNordic = false;
        beginScan(R.string.plaud_scanning,
                result -> observeAdvertisement(result.getScanRecord(), result.getDevice()),
                this::finishScan, ignored -> {});
    }

    @SuppressLint("MissingPermission")
    private Runnable beginScan(int status, Consumer<ScanResult> seen, Runnable completed,
            Consumer<BleConnectionManager.ScanOutcome> failed) {
        try {
            BluetoothManager manager = getSystemService(BluetoothManager.class);
            BluetoothAdapter adapter = manager == null ? null : manager.getAdapter();
            BluetoothLeScanner scanner = adapter == null ? null : adapter.getBluetoothLeScanner();
            if (scanner == null) {
                endScan(R.string.plaud_scan_unavailable);
                failed.accept(BleConnectionManager.ScanOutcome.FAILED);
                return () -> {};
            }
            AndroidBleDiscovery scan = new AndroidBleDiscovery(scanner, handler);
            discovery = scan;
            refreshScanButtons();
            bluetoothStatus.setText(status);
            getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            scan.start(result -> {
                if (discovery == scan) {
                    seen.accept(result);
                }
            }, () -> {
                if (discovery == scan) {
                    completed.run();
                }
            }, outcome -> {
                if (discovery != scan) {
                    return;
                }
                if (!hasBluetoothScanPermission()) {
                    endScan(R.string.plaud_scan_permission);
                    failed.accept(BleConnectionManager.ScanOutcome.FAILED);
                } else {
                    endScan(R.string.plaud_scan_failed);
                    failed.accept(outcome);
                }
            });
            return () -> {
                if (discovery == scan) {
                    endScan(R.string.plaud_scan_idle);
                }
            };
        } catch (SecurityException ignored) {
            endScan(R.string.plaud_scan_permission);
        } catch (IllegalStateException ignored) {
            endScan(R.string.plaud_scan_failed);
        }
        failed.accept(BleConnectionManager.ScanOutcome.FAILED);
        return () -> {};
    }

    private void observeAdvertisement(ScanRecord record, BluetoothDevice device) {
        sawAdvertising = true;
        if (record == null) {
            return;
        }
        List<ParcelUuid> services = record.getServiceUuids();
        if (services != null) {
            for (ParcelUuid service : services) {
                if (isPlaudAdvertisementService(service.toString())) {
                    recorderSeen(device, R.string.plaud_scan_service_match);
                    return;
                }
            }
        }
        if (isPlaudAdvertisementName(record.getDeviceName())) {
            recorderSeen(device, R.string.plaud_scan_match);
        } else if (record.getManufacturerSpecificData(NORDIC_COMPANY_ID) != null) {
            sawNordic = true;
        }
    }

    private void recorderSeen(BluetoothDevice device, int scanStatus) {
        endScan(scanStatus);
        String address = device.getAddress();
        prepareRecorderConnection(address);
        recorderConnection.start();
        recorderConnection.deviceSeen(address);
    }

    private void prepareRecorderConnection(String device) {
        recorderConnection = new BleConnectionManager<>(device, ignored -> null,
                BleDeviceStatus.scheduler(handler), this::renderConnection,
                (seen, finished) -> rescanRecorder(device, seen, finished), this::replaceScanDebit, this::readSavedRescan,
                RECORDER_LINKS);
        recorderConnection.adapterChanged(adapterEnabled);
    }

    private void renderConnection(BleConnectionManager.State state) {
        connectionStatus.setText(BleDeviceStatus.message(state));
        refreshScanButtons();
    }

    private boolean managedRescanPending() {
        if (recorderConnection == null) {
            return false;
        }
        return switch (recorderConnection.state()) {
            case WAITING_TO_RESCAN, RESCAN_COOLDOWN, RESCAN_READY, SCAN_STORAGE_FAILED, SCAN_BUDGET_CHANGED -> true;
            default -> false;
        };
    }

    private void refreshScanButtons() {
        var state = recorderConnection == null ? BleConnectionManager.State.STOPPED : recorderConnection.state();
        boolean otherCandidate = state == BleConnectionManager.State.SCAN_BUDGET_CHANGED;
        boolean manualRescan = state == BleConnectionManager.State.RESCAN_COOLDOWN
                || state == BleConnectionManager.State.RESCAN_READY || otherCandidate;
        boolean canScan = screenStarted && adapterEnabled && discovery == null;
        scanButton.setVisibility(manualRescan ? Button.GONE : Button.VISIBLE);
        scanButton.setEnabled(canScan && !managedRescanPending());
        rescanButton.setVisibility(manualRescan ? Button.VISIBLE : Button.GONE);
        rescanButton.setText(otherCandidate ? R.string.plaud_refresh_scan_budget : R.string.plaud_rescan);
        rescanButton.setEnabled(canScan && (state == BleConnectionManager.State.RESCAN_READY || otherCandidate));
    }

    private void requestRescan() {
        if (!screenStarted || !adapterEnabled || discovery != null || recorderConnection == null) {
            return;
        }
        if (recorderConnection.state() == BleConnectionManager.State.SCAN_BUDGET_CHANGED) {
            clearRecorderConnection();
            restoreScanBudget();
            return;
        }
        if (recorderConnection.state() != BleConnectionManager.State.RESCAN_READY) {
            return;
        }
        if (!hasBluetoothScanPermission()) {
            bluetoothStatus.setText(R.string.plaud_scan_permission);
            return;
        }
        recorderConnection.requestManualRescan();
    }

    private Runnable rescanRecorder(String device, Consumer<String> seen,
            Consumer<BleConnectionManager.ScanOutcome> finished) {
        if (!hasBluetoothScanPermission()) {
            endScan(R.string.plaud_scan_permission);
            finished.accept(BleConnectionManager.ScanOutcome.FAILED);
            return () -> {};
        }
        return beginScan(R.string.plaud_scan_reconfirming, result -> {
            if (device.equals(result.getDevice().getAddress())) {
                endScan(R.string.plaud_scan_reconfirmed);
                seen.accept(device);
            }
        }, () -> {
            endScan(R.string.plaud_scan_not_reconfirmed);
            finished.accept(BleConnectionManager.ScanOutcome.TIMED_OUT);
        }, finished);
    }

    private void adapterChanged(boolean supported, boolean enabled) {
        boolean wasEnabled = adapterEnabled;
        adapterEnabled = enabled;
        if (enabled && !wasEnabled) {
            bluetoothStatus.setText(R.string.plaud_scan_idle);
        }
        if (recorderConnection != null) {
            recorderConnection.adapterChanged(enabled);
        }
        bleFixture.adapterChanged(enabled);
        if (!enabled) {
            endScan(supported ? R.string.plaud_scan_bluetooth_off : R.string.plaud_scan_unavailable);
        } else {
            refreshScanButtons();
        }
    }

    private void clearRecorderConnection() {
        if (recorderConnection != null) {
            recorderConnection.stop();
            recorderConnection = null;
        }
    }

    private void finishScan() {
        if (sawNordic) {
            endScan(R.string.plaud_scan_nordic_only);
        } else {
            endScan(sawAdvertising ? R.string.plaud_scan_no_match : R.string.plaud_scan_no_advertisements);
        }
    }

    static boolean isPlaudAdvertisementService(String service) {
        return PLAUD_SERVICE_UUID.equalsIgnoreCase(service);
    }

    static boolean isPlaudAdvertisementName(String name) {
        if (name == null) {
            return false;
        }
        String normalized = name.trim().toLowerCase(Locale.ROOT);
        return normalized.equals("plaud") || normalized.startsWith("plaud ")
                || normalized.startsWith("plaud-") || normalized.startsWith("plaud_");
    }

    private void endScan(int status) {
        AndroidBleDiscovery previous = discovery;
        discovery = null;
        if (previous != null) {
            previous.stop();
        }
        getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        bluetoothStatus.setText(status);
        refreshScanButtons();
    }

    private void openPlaud() {
        Intent launch = getPackageManager().getLaunchIntentForPackage(PLAUD_PACKAGE);
        if (launch != null) {
            try {
                startActivity(launch);
                finish();
                return;
            } catch (ActivityNotFoundException ignored) {
            }
        }
        new AlertDialog.Builder(this)
                .setTitle(R.string.sync_plaud)
                .setMessage(R.string.plaud_app_unavailable)
                .setPositiveButton(android.R.string.ok, null)
                .show();
    }
}
