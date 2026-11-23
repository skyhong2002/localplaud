"""Serve the Android Debug BLE lab's synthetic GATT service on Linux."""

import argparse
import re
import signal
import sys

UUID = "85ffec0f-a47f-407b-b20c-b70260aea5d5"
BLUEZ = "org.bluez"
PROPERTIES = "org.freedesktop.DBus.Properties"
OBJECTS = "org.freedesktop.DBus.ObjectManager"
SERVICE = f"{BLUEZ}.GattService1"
ADVERTISEMENT = f"{BLUEZ}.LEAdvertisement1"
DEVICE = f"{BLUEZ}.Device1"
ROOT = "/org/localplaud/blelab"
SERVICE_PATH = f"{ROOT}/service"
AD_PATH = f"{ROOT}/advertisement"
DECOY_PATH = f"{ROOT}/decoy"
LAB_NAME = "LPLab"


def advertisement_properties(include_service):
    properties = {"Type": "peripheral", "LocalName": LAB_NAME}
    if include_service:
        properties["ServiceUUIDs"] = [UUID]
    return properties


class PeripheralCycle:
    def __init__(self, advertising, peer, schedule, report, bus_error, fatal, *, decoy=False, missing_gatt=None):
        self.advertising = advertising
        self.peer = peer
        self.schedule = schedule
        self.report = report
        self.bus_error = bus_error
        self.fatal = fatal
        self.decoy = decoy
        self.missing_gatt = missing_gatt
        self.online = False

    def advertise(self):
        self.advertising.RegisterAdvertisement(
            AD_PATH, {}, reply_handler=self.advertised, error_handler=self.fatal
        )
        return False

    def advertised(self):
        self.online = True
        self.report("ADVERTISING")

    def decoy_advertised(self):
        self.report("DECOY_ADVERTISING")
        self.schedule(self.restore_after_decoy)

    def restore_after_decoy(self):
        try:
            self.advertising.UnregisterAdvertisement(DECOY_PATH)
        except self.bus_error as error:
            self.fatal(error)
            return False
        return self.advertise()

    def advertise_without_service(self):
        try:
            self.missing_gatt.UnregisterApplication(ROOT)
        except self.bus_error as error:
            self.fatal(error)
            return
        self.advertising.RegisterAdvertisement(
            AD_PATH, {}, reply_handler=self.advertised_without_service, error_handler=self.fatal
        )

    def advertised_without_service(self):
        self.report("ADVERTISING_WITHOUT_GATT")
        self.schedule(self.restore_service)

    def restore_service(self):
        self.missing_gatt.RegisterApplication(
            ROOT, {}, reply_handler=self.service_restored, error_handler=self.fatal
        )
        return False

    def service_restored(self):
        self.online = True
        self.report("GATT_RESTORED")

    def cycle(self):
        if not self.online:
            self.report("IGNORED: advertisement is not active")
            return True
        try:
            peer, properties = self.peer()
            if not properties.Get(DEVICE, "Connected"):
                self.report("REFUSED: specified test peer is not connected")
                return True
            self.advertising.UnregisterAdvertisement(AD_PATH)
        except self.bus_error as error:
            self.report(f"REFUSED: could not prepare cycle: {error}")
            return True

        self.online = False
        try:
            peer.Disconnect()
            self.report("OFFLINE")
        except self.bus_error as error:
            self.report(f"ERROR: could not disconnect specified test peer: {error}")
        if self.decoy:
            self.advertising.RegisterAdvertisement(
                DECOY_PATH, {}, reply_handler=self.decoy_advertised, error_handler=self.fatal
            )
        elif self.missing_gatt is not None:
            self.advertise_without_service()
        else:
            self.schedule(self.advertise)
        return True


def run(adapter_name, peer_address, offline_ms, *, decoy=False, missing_service=False):
    import dbus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib, GLibUnix

    adapter_path = f"/org/bluez/{adapter_name}"
    peer_path = f"{adapter_path}/dev_{peer_address.replace(':', '_')}"
    service_properties = {"UUID": UUID, "Primary": dbus.Boolean(True)}

    class Application(dbus.service.Object):
        @dbus.service.method(OBJECTS, out_signature="a{oa{sa{sv}}}")
        def GetManagedObjects(self):
            return {SERVICE_PATH: {SERVICE: service_properties}}

    class TestService(dbus.service.Object):
        @dbus.service.method(PROPERTIES, in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface):
            return service_properties if interface == SERVICE else {}

    class Advertisement(dbus.service.Object):
        def __init__(self, bus, path, include_service):
            super().__init__(bus, path)
            self.include_service = include_service

        @dbus.service.method(PROPERTIES, in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface):
            if interface != ADVERTISEMENT:
                return {}
            return advertisement_properties(self.include_service)

        @dbus.service.method(ADVERTISEMENT)
        def Release(self):
            if self.include_service:
                fixture.online = False

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    loop = GLib.MainLoop()
    bus = dbus.SystemBus(private=True)
    failed = False

    def report(message):
        print(message, flush=True)

    def fatal(error):
        nonlocal failed
        failed = True
        report(f"ERROR: {error}")
        loop.quit()

    def stop():
        loop.quit()
        return False

    def peer():
        target = bus.get_object(BLUEZ, peer_path, introspect=False)
        return dbus.Interface(target, DEVICE), dbus.Interface(target, PROPERTIES)

    def connection_changed(interface, changes, _invalidated):
        if interface == DEVICE and "Connected" in changes:
            report(f"CONNECTED={int(changes['Connected'])}")

    try:
        adapter = bus.get_object(BLUEZ, adapter_path)
        advertising = dbus.Interface(adapter, f"{BLUEZ}.LEAdvertisingManager1")
        gatt = dbus.Interface(adapter, f"{BLUEZ}.GattManager1")
        properties = dbus.Interface(adapter, PROPERTIES)
        if not properties.Get(f"{BLUEZ}.Adapter1", "Powered"):
            raise RuntimeError("the specified BlueZ adapter is not powered")
        if not properties.Get(f"{BLUEZ}.LEAdvertisingManager1", "SupportedInstances"):
            raise RuntimeError("the specified BlueZ adapter does not support advertising")
        fixture = PeripheralCycle(
            advertising, peer, lambda callback: GLib.timeout_add(offline_ms, callback),
            report, dbus.DBusException, fatal, decoy=decoy, missing_gatt=gatt if missing_service else None,
        )
        exports = [
            Application(bus, ROOT), TestService(bus, SERVICE_PATH), Advertisement(bus, AD_PATH, True),
        ]
        if decoy:
            exports.append(Advertisement(bus, DECOY_PATH, False))
        bus.add_signal_receiver(
            connection_changed, signal_name="PropertiesChanged", dbus_interface=PROPERTIES,
            bus_name=BLUEZ, path=peer_path,
        )
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, fixture.cycle)
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, stop)
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, stop)
        gatt.RegisterApplication(ROOT, {}, reply_handler=fixture.advertise, error_handler=fatal)
        loop.run()
        for export in exports:
            export.remove_from_connection()
    except (dbus.DBusException, RuntimeError) as error:
        fatal(error)
    finally:
        bus.close()
    return int(failed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", default="hci0", help="BlueZ adapter name")
    parser.add_argument("--peer-address", required=True, help="only this test phone may be disconnected")
    parser.add_argument("--offline-ms", type=int, default=4_000)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--decoy", action="store_true", help="advertise the same name without the test UUID during downtime")
    mode.add_argument("--missing-service", action="store_true", help="advertise the test UUID without its GATT service")
    args = parser.parse_args()
    address = args.peer_address.upper()
    if not re.fullmatch(r"hci\d+", args.adapter):
        parser.error("--adapter must be a BlueZ controller name (for example hci0)")
    if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", address):
        parser.error("--peer-address must be a Bluetooth address")
    if not 1 <= args.offline_ms <= 60_000:
        parser.error("--offline-ms must be between 1 and 60000")
    try:
        return run(args.adapter, address, args.offline_ms, decoy=args.decoy, missing_service=args.missing_service)
    except ImportError as error:
        print(f"ERROR: install the system Python D-Bus and PyGObject bindings: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
