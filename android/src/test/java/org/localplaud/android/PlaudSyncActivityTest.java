package org.localplaud.android;

import org.junit.Test;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public final class PlaudSyncActivityTest {
    @Test
    public void recognizesNamedPlaudAdvertisementsWithoutClaimingDeviceIdentity() {
        assertTrue(PlaudSyncActivity.isPlaudAdvertisementName("PLAUD"));
        assertTrue(PlaudSyncActivity.isPlaudAdvertisementName(" Plaud Note "));
        assertTrue(PlaudSyncActivity.isPlaudAdvertisementName("Plaud-Note"));
        assertTrue(PlaudSyncActivity.isPlaudAdvertisementName("Plaud_Note"));
        assertFalse(PlaudSyncActivity.isPlaudAdvertisementName(null));
        assertFalse(PlaudSyncActivity.isPlaudAdvertisementName(""));
        assertFalse(PlaudSyncActivity.isPlaudAdvertisementName("applaud"));
        assertFalse(PlaudSyncActivity.isPlaudAdvertisementName("Plaudible"));
    }

    @Test
    public void requiresExactServiceUuidForAnUnnamedAdvertisement() {
        assertTrue(PlaudSyncActivity.isPlaudAdvertisementService("00001910-0000-1000-8000-00805F9B34FB"));
        assertFalse(PlaudSyncActivity.isPlaudAdvertisementService("00001910-FFFF-1000-8000-00805F9B34FB"));
        assertFalse(PlaudSyncActivity.isPlaudAdvertisementService(null));
    }
}
