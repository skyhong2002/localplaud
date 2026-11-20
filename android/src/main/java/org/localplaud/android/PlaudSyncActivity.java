package org.localplaud.android;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.os.Bundle;

public final class PlaudSyncActivity extends Activity {
    private static final String PLAUD_PACKAGE = "ai.plaud.android.plaud";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
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
                .setPositiveButton(android.R.string.ok, (dialog, button) -> finish())
                .setOnCancelListener(dialog -> finish())
                .show();
    }
}
