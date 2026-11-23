package org.localplaud.android;

import java.util.ArrayDeque;
import java.util.HashMap;
import java.util.Map;

final class BleLinkCoordinator<Device> {
    private final Map<Device, ArrayDeque<Claim>> claims = new HashMap<>();

    Claim claim(Device target, Runnable promoted) {
        Claim claim = new Claim(target, promoted);
        claims.computeIfAbsent(target, ignored -> new ArrayDeque<>()).addLast(claim);
        return claim;
    }

    final class Claim {
        private final Device target;
        private final Runnable promoted;

        private Claim(Device target, Runnable promoted) {
            this.target = target;
            this.promoted = promoted;
        }

        boolean isOwner() {
            var queue = claims.get(target);
            return queue != null && queue.peekFirst() == this;
        }

        void release() {
            var queue = claims.get(target);
            if (queue == null) {
                return;
            }
            boolean outgoingOwner = queue.peekFirst() == this;
            if (!queue.remove(this)) {
                return;
            }
            if (queue.isEmpty()) {
                claims.remove(target);
            } else if (outgoingOwner) {
                queue.getFirst().promoted.run();
            }
        }
    }
}
