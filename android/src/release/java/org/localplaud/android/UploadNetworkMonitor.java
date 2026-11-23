package org.localplaud.android;

import android.app.Activity;
import android.os.Handler;
import android.widget.TextView;

final class UploadNetworkMonitor {
    private UploadNetworkMonitor() {}

    static UploadNetworkMonitor start(Activity activity, TextView status, Handler handler) {
        return new UploadNetworkMonitor();
    }

    void stop() {}
}
