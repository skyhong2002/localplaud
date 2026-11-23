package org.localplaud.android;

import android.Manifest;
import android.app.Activity;
import android.content.pm.PackageManager;
import android.net.ConnectivityManager;
import android.net.Network;
import android.os.Build;
import android.os.Handler;
import android.widget.TextView;

final class UploadNetworkMonitor {
    private final TextView status;
    private ConnectivityManager connectivity;
    private ConnectivityManager.NetworkCallback callback;

    private UploadNetworkMonitor(TextView status) {
        this.status = status;
    }

    static UploadNetworkMonitor start(Activity activity, TextView status, Handler handler) {
        UploadNetworkMonitor monitor = new UploadNetworkMonitor(status);
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q
                || activity.checkSelfPermission(Manifest.permission.ACCESS_NETWORK_STATE) != PackageManager.PERMISSION_GRANTED) {
            return monitor;
        }
        monitor.connectivity = activity.getSystemService(ConnectivityManager.class);
        if (monitor.connectivity == null) {
            return monitor;
        }
        ConnectivityManager.NetworkCallback callback = new ConnectivityManager.NetworkCallback() {
            @Override
            public void onBlockedStatusChanged(Network network, boolean blocked) {
                if (monitor.callback == this) {
                    status.setVisibility(blocked ? TextView.VISIBLE : TextView.GONE);
                }
            }

            @Override
            public void onLost(Network network) {
                if (monitor.callback == this) {
                    status.setVisibility(TextView.GONE);
                }
            }
        };
        monitor.callback = callback;
        try {
            monitor.connectivity.registerDefaultNetworkCallback(callback, handler);
        } catch (SecurityException error) {
            monitor.callback = null;
            status.setVisibility(TextView.GONE);
        }
        return monitor;
    }

    void stop() {
        ConnectivityManager.NetworkCallback previous = callback;
        callback = null;
        if (previous != null) {
            connectivity.unregisterNetworkCallback(previous);
        }
        status.setVisibility(TextView.GONE);
    }
}
