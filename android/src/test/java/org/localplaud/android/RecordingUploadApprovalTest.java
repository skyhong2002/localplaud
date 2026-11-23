package org.localplaud.android;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertTrue;

import java.net.URI;
import java.time.Instant;
import org.junit.Test;

public class RecordingUploadApprovalTest {
    @Test
    public void grantExpiryAcceptsServerOffsetAndUtcForms() {
        Instant expires = Instant.ofEpochSecond(1795424400);
        assertEquals(expires, RecordingUploadApproval.parseExpiry("2026-11-23T09:00:00+00:00"));
        assertEquals(expires, RecordingUploadApproval.parseExpiry("2026-11-23T09:00:00Z"));
        assertEquals(expires, RecordingUploadApproval.parseExpiry("2026-11-23T17:00:00+08:00"));
    }

    @Test
    public void proofIsHashedAsBase64UrlAndBrowserReceivesOnlyTheChallenge() {
        assertEquals("E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
                RecordingUploadApproval.challengeForProof("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"));
        URI origin = URI.create("https://workspace.example");
        RecordingUploadApproval approval = new RecordingUploadApproval(origin, false);
        URI browser = approval.browserUri();
        assertEquals(origin.getHost(), browser.getHost());
        assertEquals("/account/android-upload", browser.getPath());
        assertTrue(browser.getQuery().matches("challenge=[A-Za-z0-9_-]{43}"));
        assertEquals(browser.getQuery().substring(10, 18), approval.comparisonCode());
        assertNotEquals(browser, new RecordingUploadApproval(origin, false).browserUri());
    }
}
