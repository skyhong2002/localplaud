package org.localplaud.android;

import static org.junit.Assert.assertEquals;

import org.junit.Test;

public final class WorkspaceActivityTest {
    @Test
    public void launcherActionsMapOnlyToWorkspaceRoutes() {
        assertEquals("/", WorkspaceActivity.routeForAction("org.localplaud.android.FILES"));
        assertEquals("/search", WorkspaceActivity.routeForAction("org.localplaud.android.SEARCH"));
        assertEquals("/explore", WorkspaceActivity.routeForAction("org.localplaud.android.EXPLORE"));
    }

    @Test
    public void launcherAndUnsupportedActionsDefaultToHome() {
        assertEquals("/home", WorkspaceActivity.routeForAction(null));
        assertEquals("/home", WorkspaceActivity.routeForAction("android.intent.action.MAIN"));
        assertEquals("/home", WorkspaceActivity.routeForAction("https://other.example/"));
    }
}
