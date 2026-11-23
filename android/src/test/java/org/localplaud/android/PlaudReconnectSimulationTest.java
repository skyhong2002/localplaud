package org.localplaud.android;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.List;

public final class PlaudReconnectSimulationTest {
    private final FakeTransport transport = new FakeTransport();
    private final ArrayDeque<Runnable> retryTimer = new ArrayDeque<>();
    private final SimulatedReconnect controller = new SimulatedReconnect(transport, retryTimer);

    @Test
    public void returningToRangeConnectsOnceWithoutAnAdditionalAction() {
        connectInitially();
        controller.presenceChanged(false);
        assertTrue(transport.attempts.get(0).closed);
        assertFalse(controller.connected);

        controller.presenceChanged(true);
        controller.presenceChanged(true);
        assertEquals(2, transport.attempts.size());
        transport.attempts.get(1).connected();
        controller.presenceChanged(true);
        assertTrue(controller.connected);
        assertEquals(2, transport.attempts.size());
    }

    @Test
    public void disconnectWhilePresentArmsOneRetryDespiteDuplicateNotifications() {
        Attempt first = connectInitially();
        first.disconnected();
        first.disconnected();
        controller.presenceChanged(true);
        assertFalse(controller.connected);
        assertEquals(1, transport.attempts.size());
        assertEquals(1, retryTimer.size());

        retryTimer.remove().run();
        controller.presenceChanged(true);
        transport.attempts.get(1).connected();
        assertTrue(controller.connected);
        assertEquals(2, transport.attempts.size());
        assertTrue(retryTimer.isEmpty());
    }

    @Test
    public void missingOrRevokedAuthorizationBlocksPresenceAndPendingRetry() {
        controller.presenceChanged(true);
        controller.presenceChanged(true);
        assertTrue(transport.attempts.isEmpty());

        controller.authorizationChanged(true);
        Attempt first = transport.attempts.get(0);
        first.connected();
        first.disconnected();
        controller.authorizationChanged(false);
        controller.presenceChanged(false);
        controller.presenceChanged(true);
        retryTimer.remove().run();
        assertFalse(controller.connected);
        assertEquals(1, transport.attempts.size());
    }

    @Test
    public void delayedCallbacksFromDepartedAttemptCannotReplaceNewState() {
        controller.authorizationChanged(true);
        controller.presenceChanged(true);
        Attempt stale = transport.attempts.get(0);
        controller.presenceChanged(false);
        stale.connected();
        assertFalse(controller.connected);
        assertTrue(stale.closed);

        controller.presenceChanged(true);
        Attempt current = transport.attempts.get(1);
        stale.connected();
        assertFalse(controller.connected);
        current.connected();
        stale.disconnected();
        assertTrue(controller.connected);
        assertFalse(current.closed);
        assertTrue(retryTimer.isEmpty());
        assertEquals(2, transport.attempts.size());
    }

    @Test
    public void revocationClosesAnInFlightAttemptAndIgnoresItsLateSuccess() {
        controller.authorizationChanged(true);
        controller.presenceChanged(true);
        Attempt attempt = transport.attempts.get(0);
        controller.authorizationChanged(false);
        attempt.connected();
        controller.presenceChanged(true);
        assertTrue(attempt.closed);
        assertFalse(controller.connected);
        assertEquals(1, transport.attempts.size());
    }

    private Attempt connectInitially() {
        controller.authorizationChanged(true);
        controller.presenceChanged(true);
        Attempt attempt = transport.attempts.get(0);
        attempt.connected();
        assertTrue(controller.connected);
        return attempt;
    }

    private static final class SimulatedReconnect {
        private final FakeTransport transport;
        private final ArrayDeque<Runnable> timer;
        private boolean authorized;
        private boolean present;
        private boolean connected;
        private Attempt active;
        private Object retryToken;

        SimulatedReconnect(FakeTransport transport, ArrayDeque<Runnable> timer) {
            this.transport = transport;
            this.timer = timer;
        }

        void authorizationChanged(boolean granted) {
            authorized = granted;
            reevaluate();
        }

        void presenceChanged(boolean nearby) {
            present = nearby;
            reevaluate();
        }

        private void reevaluate() {
            if (!authorized || !present) {
                retryToken = null;
                clearConnection();
                return;
            }
            if (active == null && retryToken == null) {
                active = new Attempt(this);
                transport.connect(active);
            }
        }

        private void clearConnection() {
            Attempt previous = active;
            active = null;
            connected = false;
            if (previous != null) {
                transport.close(previous);
            }
        }

        void onConnected(Attempt attempt) {
            if (attempt == active && authorized && present) {
                connected = true;
            }
        }

        void onDisconnected(Attempt attempt) {
            if (attempt != active) {
                return;
            }
            clearConnection();
            if (authorized && present) {
                Object scheduled = new Object();
                retryToken = scheduled;
                timer.add(() -> {
                    if (retryToken == scheduled) {
                        retryToken = null;
                        reevaluate();
                    }
                });
            }
        }
    }

    private static final class FakeTransport {
        private final List<Attempt> attempts = new ArrayList<>();

        void connect(Attempt attempt) {
            attempts.add(attempt);
        }

        void close(Attempt attempt) {
            attempt.closed = true;
        }
    }

    private static final class Attempt {
        private final SimulatedReconnect controller;
        private boolean closed;

        Attempt(SimulatedReconnect controller) {
            this.controller = controller;
        }

        void connected() {
            controller.onConnected(this);
        }

        void disconnected() {
            controller.onDisconnected(this);
        }
    }
}
