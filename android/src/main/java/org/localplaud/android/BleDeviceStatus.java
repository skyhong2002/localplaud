package org.localplaud.android;

import android.os.Handler;
import android.os.SystemClock;

final class BleDeviceStatus {
    private BleDeviceStatus() {}

    static int message(BleConnectionManager.State state) {
        return switch (state) {
            case STOPPED, UNAUTHORIZED -> R.string.plaud_connection_unavailable;
            case BLUETOOTH_OFF -> R.string.plaud_connection_bluetooth_off;
            case BLUETOOTH_PERMISSION_DENIED -> R.string.plaud_connection_bluetooth_permission_denied;
            case WAITING_FOR_DEVICE -> R.string.plaud_connection_waiting;
            case WAITING_TO_RESCAN -> R.string.plaud_connection_rescan_pending;
            case RESCAN_COOLDOWN -> R.string.plaud_connection_rescan_cooldown;
            case RESCAN_READY -> R.string.plaud_connection_rescan_ready;
            case SCAN_STORAGE_FAILED -> R.string.plaud_connection_scan_storage_failed;
            case SCAN_BUDGET_CHANGED -> R.string.plaud_connection_scan_budget_changed;
            case SCAN_BUDGET_EMPTY -> R.string.plaud_connection_scan_budget_empty;
            case WAITING_FOR_LINK -> R.string.plaud_connection_waiting_for_link;
            case CONNECTING -> R.string.plaud_connection_connecting;
            case REVALIDATING_SERVICES -> R.string.plaud_connection_revalidating_services;
            case CONNECTED -> R.string.plaud_connection_connected;
            case RETRYING -> R.string.plaud_connection_retrying;
        };
    }

    static BleConnectionManager.Scheduler scheduler(Handler handler) {
        return new BleConnectionManager.Scheduler() {
            @Override
            public long nowMillis() {
                return SystemClock.elapsedRealtime();
            }

            @Override
            public void postDelayed(Runnable callback, long delayMillis) {
                handler.postDelayed(callback, delayMillis);
            }

            @Override
            public void remove(Runnable callback) {
                handler.removeCallbacks(callback);
            }
        };
    }
}
