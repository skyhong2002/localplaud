from scripts.ble_lab_peripheral import (
    AD_PATH,
    DECOY_PATH,
    DEVICE,
    ROOT,
    UUID,
    PeripheralCycle,
    advertisement_properties,
)


class FakeBusError(Exception):
    pass


class FakeBlueZ:
    def __init__(self):
        self.connected = True
        self.calls = []
        self.reply = None

    def RegisterAdvertisement(self, path, options, reply_handler, error_handler):
        assert options == {}
        self.calls.append("register" if path == AD_PATH else ("register", path))
        self.reply = reply_handler

    def UnregisterAdvertisement(self, path):
        self.calls.append("unregister" if path == AD_PATH else ("unregister", path))

    def UnregisterApplication(self, path):
        assert path == ROOT
        self.calls.append("remove-service")

    def RegisterApplication(self, path, options, reply_handler, error_handler):
        assert (path, options) == (ROOT, {})
        self.calls.append("restore-service")
        self.reply = reply_handler

    def Get(self, interface, name):
        assert (interface, name) == (DEVICE, "Connected")
        return self.connected

    def Disconnect(self):
        self.calls.append("disconnect")
        self.connected = False


def test_bluez_fixture_withdraws_advertisement_disconnects_and_registers_once():
    backend = FakeBlueZ()
    scheduled = []
    messages = []
    errors = []
    fixture = PeripheralCycle(
        backend, lambda: (backend, backend), scheduled.append, messages.append, FakeBusError, errors.append
    )

    fixture.advertise()
    backend.reply()
    fixture.cycle()
    fixture.cycle()
    assert backend.calls == ["register", "unregister", "disconnect"]
    assert messages == ["ADVERTISING", "OFFLINE", "IGNORED: advertisement is not active"]
    assert len(scheduled) == 1

    assert scheduled.pop()() is False
    backend.reply()
    assert backend.calls == ["register", "unregister", "disconnect", "register"]
    assert messages[-1] == "ADVERTISING"
    assert not errors


def test_bluez_fixture_does_not_interrupt_an_unconnected_peer():
    backend = FakeBlueZ()
    backend.connected = False
    scheduled = []
    messages = []
    fixture = PeripheralCycle(
        backend, lambda: (backend, backend), scheduled.append, messages.append, FakeBusError, messages.append
    )
    fixture.advertise()
    backend.reply()
    fixture.cycle()
    assert backend.calls == ["register"]
    assert not scheduled
    assert messages[-1] == "REFUSED: specified test peer is not connected"


def test_decoy_keeps_the_name_and_restores_the_uuid_after_its_full_window():
    normal = advertisement_properties(include_service=True)
    assert normal.pop("ServiceUUIDs") == [UUID]
    assert advertisement_properties(include_service=False) == normal

    backend = FakeBlueZ()
    scheduled = []
    messages = []
    errors = []
    fixture = PeripheralCycle(
        backend, lambda: (backend, backend), scheduled.append, messages.append, FakeBusError, errors.append,
        decoy=True,
    )
    fixture.advertise()
    backend.reply()
    fixture.cycle()
    assert backend.calls == ["register", "unregister", "disconnect", ("register", DECOY_PATH)]
    assert not scheduled

    backend.reply()
    assert messages[-1] == "DECOY_ADVERTISING"
    fixture.cycle()
    assert len(scheduled) == 1
    assert scheduled.pop()() is False
    backend.reply()
    assert backend.calls[-2:] == [("unregister", DECOY_PATH), "register"]
    assert messages[-1] == "ADVERTISING"
    assert not errors


def test_missing_service_mode_restores_gatt_without_restarting_the_uuid_advertisement():
    backend = FakeBlueZ()
    scheduled = []
    messages = []
    errors = []
    fixture = PeripheralCycle(
        backend, lambda: (backend, backend), scheduled.append, messages.append, FakeBusError,
        errors.append, missing_gatt=backend,
    )
    fixture.advertise()
    backend.reply()
    fixture.cycle()
    assert backend.calls == ["register", "unregister", "disconnect", "remove-service", "register"]
    assert advertisement_properties(include_service=True)["ServiceUUIDs"] == [UUID]
    assert not scheduled

    backend.reply()
    assert messages[-1] == "ADVERTISING_WITHOUT_GATT"
    fixture.cycle()
    assert len(scheduled) == 1
    assert scheduled.pop()() is False
    backend.reply()
    assert backend.calls[-2:] == ["register", "restore-service"]
    assert messages[-1] == "GATT_RESTORED"
    assert not errors
