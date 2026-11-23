package org.localplaud.android;

import java.util.function.BiConsumer;
import java.util.function.Consumer;
import java.util.function.Function;
import java.util.function.Supplier;

final class DebugBleLab {
    final DebugBleReconnectIntent reconnectIntent;
    private BleConnectionManager.State state = BleConnectionManager.State.STOPPED;
    private int readySessions;
    private ViewBinding view;

    static DebugBleLab fromNonConfigurationInstance(Object retained, Supplier<DebugBleLab> fresh) {
        return retained instanceof DebugBleLab lab ? lab : fresh.get();
    }

    DebugBleLab(Function<Consumer<BleConnectionManager.State>, DebugBleReconnectIntent> createIntent) {
        reconnectIntent = createIntent.apply(this::stateChanged);
    }

    ViewBinding bind(BiConsumer<BleConnectionManager.State, Integer> render) {
        view = new ViewBinding(render);
        view.refresh();
        return view;
    }

    private void stateChanged(BleConnectionManager.State next) {
        if (next == BleConnectionManager.State.CONNECTED && state != BleConnectionManager.State.REVALIDATING_SERVICES) {
            readySessions++;
        } else if (next == BleConnectionManager.State.STOPPED) {
            readySessions = 0;
        }
        state = next;
        if (view != null) {
            view.refresh();
        }
    }

    final class ViewBinding {
        private final BiConsumer<BleConnectionManager.State, Integer> render;

        private ViewBinding(BiConsumer<BleConnectionManager.State, Integer> render) {
            this.render = render;
        }

        private void refresh() {
            render.accept(state, readySessions);
        }

        boolean detachForRecreation() {
            if (view != this) {
                return false;
            }
            view = null;
            return state == BleConnectionManager.State.RESCAN_COOLDOWN || state == BleConnectionManager.State.RESCAN_READY;
        }

        void requestManualRescan(String revision) {
            if (view == this) {
                reconnectIntent.requestManualRescan(revision);
            }
        }

        void retryPendingAddress(Consumer<String> renderResult) {
            if (view == this && state == BleConnectionManager.State.STOPPED && reconnectIntent.intentReadFailed()) {
                renderResult.accept(reconnectIntent.retryPendingAddress());
            }
        }
    }
}
