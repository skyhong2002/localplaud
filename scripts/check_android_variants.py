"""Check packaged Android launchers and keep BLE lab code out of Release."""

import argparse
import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

ANDROID = "{http://schemas.android.com/apk/res/android}"
NAMESPACE = "org.localplaud.android"


def check_variant(analyzer, apk, variant):
    manifest = ET.fromstring(subprocess.check_output([analyzer, "manifest", "print", str(apk)]))
    application = manifest.find("application")
    is_lab = variant == "bleLab"
    expected_package = NAMESPACE + (".blelab" if is_lab else "")
    if manifest.get("package") != expected_package or application is None:
        raise ValueError(f"{variant}: unexpected application package")
    if (application.get(ANDROID + "debuggable") == "true") != (variant != "release"):
        raise ValueError(f"{variant}: unexpected debuggability")
    launchers = [
        entry
        for entry in application
        if any(
            {item.get(ANDROID + "name") for item in intent}
            >= {"android.intent.action.MAIN", "android.intent.category.LAUNCHER"}
            for intent in entry.findall("intent-filter")
        )
    ]
    expected_launcher = NAMESPACE + (".BleLabLauncher" if is_lab else ".WorkspaceActivity")
    if len(launchers) != 1 or launchers[0].get(ANDROID + "name") != expected_launcher:
        raise ValueError(f"{variant}: unexpected launcher")
    if launchers[0].get(ANDROID + "exported") != "true":
        raise ValueError(f"{variant}: launcher is not exported")
    if is_lab and launchers[0].get(ANDROID + "targetActivity") != NAMESPACE + ".PlaudSyncActivity":
        raise ValueError("bleLab: launcher does not target Sync Plaud")
    if not is_lab and application.find("activity-alias") is not None:
        raise ValueError(f"{variant}: unexpected test alias")
    if variant == "release":
        permissions = {entry.get(ANDROID + "name") for entry in manifest.findall("uses-permission")}
        if "android.permission.BLUETOOTH_CONNECT" in permissions:
            raise ValueError("release: unexpected Bluetooth connection permission")
        if application.get(ANDROID + "usesCleartextTraffic") == "true":
            raise ValueError("release: cleartext traffic is enabled")
        classes = subprocess.check_output(
            [analyzer, "dex", "packages", "--defined-only", str(apk)], text=True
        )
        for name in ("AndroidDebugBleTransport", "DebugBleLab", "DebugBleProbe", "DebugBleReconnectIntent"):
            if name in classes:
                raise ValueError(f"release: contains {name}")
    print(f"{variant}: packaged launcher and isolation checks passed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apk-root", type=Path, default=Path("android/build/outputs/apk"))
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT", "")
    parser.add_argument("--apkanalyzer", default=str(Path(sdk) / "cmdline-tools/latest/bin/apkanalyzer"))
    args = parser.parse_args()
    for variant in ("debug", "bleLab", "release"):
        candidates = list((args.apk_root / variant).glob("*.apk"))
        if len(candidates) != 1:
            parser.error(f"Expected exactly one {variant} APK; run assembleDebug assembleBleLab assembleRelease")
        check_variant(args.apkanalyzer, candidates[0], variant)


if __name__ == "__main__":
    main()
