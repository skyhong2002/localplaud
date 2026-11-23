package org.localplaud.android;

import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.os.Build;

final class AndroidBluetoothState extends BroadcastReceiver {
    private final Context context;
    private final BluetoothManager manager;
    private final Listener listener;
    private boolean started;

    interface Listener {
        void changed(boolean supported, boolean enabled);
    }

    AndroidBluetoothState(Context context, Listener listener) {
        this.context = context;
        this.manager = context.getSystemService(BluetoothManager.class);
        this.listener = listener;
    }

    void start() {
        IntentFilter filter = new IntentFilter(BluetoothAdapter.ACTION_STATE_CHANGED);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            context.registerReceiver(this, filter, Context.RECEIVER_EXPORTED);
        } else {
            context.registerReceiver(this, filter);
        }
        started = true;
        refresh();
    }

    void refresh() {
        refresh(false);
    }

    private void refresh(boolean shuttingDown) {
        if (started) {
            BluetoothAdapter adapter = manager == null ? null : manager.getAdapter();
            listener.changed(adapter != null, adapter != null && !shuttingDown && adapter.isEnabled());
        }
    }

    void stop() {
        if (started) {
            started = false;
            context.unregisterReceiver(this);
        }
    }

    @Override
    public void onReceive(Context context, Intent intent) {
        if (BluetoothAdapter.ACTION_STATE_CHANGED.equals(intent.getAction())) {
            int state = intent.getIntExtra(BluetoothAdapter.EXTRA_STATE, BluetoothAdapter.ERROR);
            refresh(state == BluetoothAdapter.STATE_TURNING_OFF || state == BluetoothAdapter.STATE_OFF);
        }
    }
}
