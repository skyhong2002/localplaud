package org.localplaud.android;

import android.net.Uri;

import com.google.androidbrowserhelper.trusted.LauncherActivity;

public final class WorkspaceActivity extends LauncherActivity {
    @Override
    protected Uri getLaunchingUrl() {
        return Uri.parse(BuildConfig.WORKSPACE_ORIGIN + routeForAction(getIntent().getAction()));
    }

    static String routeForAction(String action) {
        if ("org.localplaud.android.FILES".equals(action)) {
            return "/";
        }
        if ("org.localplaud.android.SEARCH".equals(action)) {
            return "/search";
        }
        if ("org.localplaud.android.EXPLORE".equals(action)) {
            return "/explore";
        }
        return "/home";
    }
}
