"""
USB device monitor with hot-plug detection.

Polls for USB devices matching configured rules and emits
events when devices are attached or detached.
"""

from __future__ import annotations

import logging
import time
import threading
from typing import Callable, Optional

import usb.core

from .types import (
    DeviceIdentity,
    DeviceMatchRule,
    DeviceEvent,
    DeviceEventType,
    USBError,
)

logger = logging.getLogger(__name__)

# Type alias for event callback
DeviceEventCallback = Callable[[DeviceEvent], None]


def _usb_device_to_identity(dev: usb.core.Device) -> DeviceIdentity:
    """Convert a pyusb Device to our DeviceIdentity."""
    # Read string descriptors safely
    name = ""
    serial = ""
    try:
        if dev.iProduct:
            name = usb.util.get_string(dev, dev.iProduct) or ""
    except (usb.core.USBError, ValueError):
        pass
    try:
        if dev.iSerialNumber:
            serial = usb.util.get_string(dev, dev.iSerialNumber) or ""
    except (usb.core.USBError, ValueError):
        pass

    return DeviceIdentity(
        vid=dev.idVendor,
        pid=dev.idProduct,
        serial=serial,
        name=name,
        location_id=f"{dev.bus}-{dev.address}",
        bus=dev.bus,
        address=dev.address,
    )


class USBMonitor:
    """
    Monitor for USB device attach/detach events.

    Uses polling (not hotplug API) for maximum cross-platform compatibility.
    The monitor maintains a set of "known" devices and emits events when
    the set changes between poll cycles.

    Args:
        match_rules: List of rules to filter devices. Only matching devices
            trigger events. Empty list = match all USB devices.
        poll_interval_ms: Milliseconds between scans (default 500)

    Example:
        monitor = USBMonitor(
            match_rules=[DeviceMatchRule(vid=0x1234)],
            poll_interval_ms=500,
        )
        monitor.on_device_event = lambda evt: print(evt)
        monitor.run_forever()
    """

    def __init__(
        self,
        match_rules: Optional[list[DeviceMatchRule]] = None,
        poll_interval_ms: int = 500,
    ):
        self._match_rules = match_rules or []
        self._poll_interval = poll_interval_ms / 1000.0  # Convert to seconds
        self._known_devices: dict[str, tuple[DeviceIdentity, Optional[DeviceMatchRule]]] = {}
        self._running = False
        self._stop_event = threading.Event()

        # User-assignable callback
        self.on_device_event: Optional[DeviceEventCallback] = None

    @property
    def match_rules(self) -> list[DeviceMatchRule]:
        return self._match_rules

    @match_rules.setter
    def match_rules(self, rules: list[DeviceMatchRule]) -> None:
        self._match_rules = rules

    @property
    def known_devices(self) -> dict[str, DeviceIdentity]:
        """Currently known (connected) devices, keyed by device_id."""
        return {k: v[0] for k, v in self._known_devices.items()}

    def scan_once(self) -> list[tuple[DeviceIdentity, Optional[DeviceMatchRule]]]:
        """
        Scan for currently connected devices matching the rules.

        Returns:
            List of (DeviceIdentity, matched_rule) tuples.
            matched_rule is None if match_rules is empty (match-all mode).
        """
        results: list[tuple[DeviceIdentity, Optional[DeviceMatchRule]]] = []

        try:
            all_devices = usb.core.find(find_all=True)
        except usb.core.NoBackendError:
            logger.error(
                "No libusb backend found. Install libusb: "
                "brew install libusb (mac), apt install libusb-1.0-0 (linux), "
                "or use Zadig (windows)"
            )
            return results

        for dev in all_devices:
            # On Windows, reading string descriptors from devices without
            # a WinUSB driver throws USBError.  Do a quick VID/PID pre-filter
            # before attempting descriptor reads to avoid crashing on
            # unrelated system devices (keyboards, webcams, etc.).
            if self._match_rules and not self._pre_match_vid_pid(dev):
                continue

            try:
                identity = _usb_device_to_identity(dev)
            except Exception as e:
                logger.debug(
                    "Skipping device %04x:%04x — cannot read descriptor: %s",
                    dev.idVendor, dev.idProduct, e,
                )
                continue

            matched_rule = self._match_device(identity)

            if matched_rule is not None or not self._match_rules:
                results.append((identity, matched_rule))

        return results

    def _pre_match_vid_pid(self, dev: usb.core.Device) -> bool:
        """Quick VID/PID check before reading string descriptors."""
        for rule in self._match_rules:
            vid_ok = rule.vid is None or rule.vid == dev.idVendor
            pid_ok = rule.pid is None or rule.pid == dev.idProduct
            if vid_ok and pid_ok:
                return True
        return False

    def _match_device(self, device: DeviceIdentity) -> Optional[DeviceMatchRule]:
        """Find the first matching rule for a device, or None."""
        for rule in self._match_rules:
            if rule.matches(device):
                return rule
        return None

    def _poll_cycle(self) -> None:
        """Run one poll cycle: scan devices, emit attach/detach events."""
        current_scan = self.scan_once()
        current_ids: dict[str, tuple[DeviceIdentity, Optional[DeviceMatchRule]]] = {}

        for identity, rule in current_scan:
            dev_id = identity.device_id
            current_ids[dev_id] = (identity, rule)

            # New device?
            if dev_id not in self._known_devices:
                event = DeviceEvent(
                    event_type=DeviceEventType.ATTACHED,
                    device=identity,
                    matched_rule=rule,
                )
                logger.info("Device attached: %s", event)
                self._emit(event)

        # Detect detached devices
        for dev_id, (identity, rule) in self._known_devices.items():
            if dev_id not in current_ids:
                event = DeviceEvent(
                    event_type=DeviceEventType.DETACHED,
                    device=identity,
                    matched_rule=rule,
                )
                logger.info("Device detached: %s", event)
                self._emit(event)

        self._known_devices = current_ids

    def _emit(self, event: DeviceEvent) -> None:
        """Emit an event to the registered callback."""
        if self.on_device_event is not None:
            try:
                self.on_device_event(event)
            except Exception:
                logger.exception("Error in device event callback")

    def run_forever(self) -> None:
        """
        Run the monitor loop, blocking until stop() is called or Ctrl+C.

        Polls for devices at the configured interval and emits events
        for attach/detach changes.
        """
        self._running = True
        self._stop_event.clear()
        logger.info(
            "USB monitor started (poll every %dms, %d rules)",
            int(self._poll_interval * 1000),
            len(self._match_rules),
        )

        # Initial scan
        self._poll_cycle()

        try:
            while not self._stop_event.is_set():
                self._stop_event.wait(timeout=self._poll_interval)
                if not self._stop_event.is_set():
                    self._poll_cycle()
        except KeyboardInterrupt:
            logger.info("USB monitor stopped by user")
        finally:
            self._running = False

    def stop(self) -> None:
        """Signal the monitor loop to stop."""
        self._stop_event.set()

    @property
    def is_running(self) -> bool:
        return self._running
