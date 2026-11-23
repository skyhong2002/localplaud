package org.localplaud.android;

import static org.junit.Assert.assertEquals;
import static org.localplaud.android.BleConnectionManager.ScanOutcome.FAILED;
import static org.localplaud.android.BleConnectionManager.ScanOutcome.TEMPORARY_FAILURE;
import static org.localplaud.android.BleConnectionManager.ScanOutcome.THROTTLED;

import android.bluetooth.le.ScanCallback;

import org.junit.Test;

public final class AndroidBleDiscoveryTest {
    @Test
    public void classifiesOnlyExplicitlyTemporaryAndroidStartCodesAsRetryable() {
        assertEquals(TEMPORARY_FAILURE,
                AndroidBleDiscovery.classifyStartFailure(ScanCallback.SCAN_FAILED_OUT_OF_HARDWARE_RESOURCES));
        assertEquals(THROTTLED,
                AndroidBleDiscovery.classifyStartFailure(ScanCallback.SCAN_FAILED_SCANNING_TOO_FREQUENTLY));
        for (int code : new int[] {ScanCallback.SCAN_FAILED_ALREADY_STARTED,
                ScanCallback.SCAN_FAILED_APPLICATION_REGISTRATION_FAILED, ScanCallback.SCAN_FAILED_INTERNAL_ERROR,
                ScanCallback.SCAN_FAILED_FEATURE_UNSUPPORTED, 999}) {
            assertEquals(FAILED, AndroidBleDiscovery.classifyStartFailure(code));
        }
    }
}
