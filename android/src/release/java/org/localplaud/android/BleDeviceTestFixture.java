package org.localplaud.android;

import android.os.Handler;
import android.widget.LinearLayout;

final class BleDeviceTestFixture {
    private BleDeviceTestFixture() {}

    static BleDeviceTestFixture install(PlaudSyncActivity activity, LinearLayout content, Handler handler, Object retained) {
        return new BleDeviceTestFixture();
    }

    void adapterChanged(boolean enabled) {}

    Object stop(boolean recreating) {
        return null;
    }
}
