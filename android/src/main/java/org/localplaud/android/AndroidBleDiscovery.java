package org.localplaud.android;

import android.annotation.SuppressLint;
import android.bluetooth.le.BluetoothLeScanner;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanFilter;
import android.bluetooth.le.ScanResult;
import android.bluetooth.le.ScanSettings;
import android.os.Handler;
import android.os.SystemClock;

import java.util.Collections;
import java.util.List;
import java.util.function.Consumer;
import java.util.function.IntFunction;

final class AndroidBleDiscovery {

    private final BluetoothLeScanner scanner;
    private final Handler handler;
    private final IntFunction<BleConnectionManager.ScanOutcome> startFailure;
    private Runnable timeout;
    private ScanCallback current;

    AndroidBleDiscovery(BluetoothLeScanner scanner, Handler handler) {
        this(scanner, handler, AndroidBleDiscovery::classifyStartFailure);
    }

    AndroidBleDiscovery(BluetoothLeScanner scanner, Handler handler,
            IntFunction<BleConnectionManager.ScanOutcome> startFailure) {
        this.scanner = scanner;
        this.handler = handler;
        this.startFailure = startFailure;
    }

    @SuppressLint("MissingPermission")
    void start(Consumer<ScanResult> seen, Runnable finished, Consumer<BleConnectionManager.ScanOutcome> failed) {
        start(Collections.emptyList(), seen, finished, failed);
    }

    @SuppressLint("MissingPermission")
    void start(List<ScanFilter> filters, Consumer<ScanResult> seen, Runnable finished,
            Consumer<BleConnectionManager.ScanOutcome> failed) {
        if (current != null) {
            return;
        }
        long startedAtNanos = SystemClock.elapsedRealtimeNanos();
        ScanCallback callback = new ScanCallback() {
            @Override
            public void onScanResult(int type, ScanResult result) {
                handler.post(() -> deliver(result));
            }

            @Override
            public void onBatchScanResults(List<ScanResult> results) {
                handler.post(() -> {
                    for (ScanResult result : results) {
                        deliver(result);
                    }
                });
            }

            private void deliver(ScanResult result) {
                if (current == this && result.getTimestampNanos() >= startedAtNanos) {
                    seen.accept(result);
                }
            }

            @Override
            public void onScanFailed(int errorCode) {
                handler.post(() -> {
                    if (current == this) {
                        stop();
                        failed.accept(startFailure.apply(errorCode));
                    }
                });
            }
        };
        ScanSettings settings = new ScanSettings.Builder()
                .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
                .build();
        current = callback;
        timeout = () -> {
            if (current == callback) {
                stop();
                finished.run();
            }
        };
        try {
            scanner.startScan(filters, settings, callback);
            handler.postDelayed(timeout, BleConnectionManager.SCAN_WINDOW_MS);
        } catch (SecurityException | IllegalStateException failure) {
            stop();
            throw failure;
        }
    }

    static BleConnectionManager.ScanOutcome classifyStartFailure(int errorCode) {
        return switch (errorCode) {
            case ScanCallback.SCAN_FAILED_OUT_OF_HARDWARE_RESOURCES -> BleConnectionManager.ScanOutcome.TEMPORARY_FAILURE;
            case ScanCallback.SCAN_FAILED_SCANNING_TOO_FREQUENTLY -> BleConnectionManager.ScanOutcome.THROTTLED;
            default -> BleConnectionManager.ScanOutcome.FAILED;
        };
    }

    @SuppressLint("MissingPermission")
    void stop() {
        if (timeout != null) {
            handler.removeCallbacks(timeout);
            timeout = null;
        }
        ScanCallback callback = current;
        current = null;
        if (callback != null) {
            try {
                scanner.stopScan(callback);
            } catch (SecurityException | IllegalStateException ignored) {
            }
        }
    }
}
