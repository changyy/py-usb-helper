"""
Unit tests for USBMonitor — mock usb.core.find for device scanning.

Tests cover:
  - scan_once with matching devices
  - scan_once with no matches
  - scan_once with no backend (libusb not installed)
  - poll_cycle attach event
  - poll_cycle detach event
  - poll_cycle attach + detach in sequence
  - match_rules filtering
  - callback exception handling
  - run_forever with stop()
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch, MagicMock, call

import pytest

from usb_helper.monitor import USBMonitor, _usb_device_to_identity
from usb_helper.types import DeviceIdentity, DeviceMatchRule, DeviceEvent, DeviceEventType


def _make_mock_pyusb_device(vid, pid, bus, address, product="TestDev", serial="SN001"):
    """Create a minimal mock pyusb device for monitor scanning."""
    dev = MagicMock()
    dev.idVendor = vid
    dev.idProduct = pid
    dev.bus = bus
    dev.address = address
    dev.iProduct = 1
    dev.iSerialNumber = 2
    return dev


class TestUSBDeviceToIdentity:
    def test_basic_conversion(self):
        mock_dev = _make_mock_pyusb_device(0x1234, 0xABCD, 1, 3)

        with patch("usb_helper.monitor.usb.util.get_string") as mock_get_str:
            mock_get_str.side_effect = lambda dev, idx: {1: "TestDev", 2: "SN001"}.get(idx, "")
            identity = _usb_device_to_identity(mock_dev)

        assert identity.vid == 0x1234
        assert identity.pid == 0xABCD
        assert identity.bus == 1
        assert identity.address == 3
        assert identity.name == "TestDev"
        assert identity.serial == "SN001"
        assert identity.device_id == "usb:1-3"

    def test_string_descriptor_error(self):
        """Should handle USB errors when reading string descriptors."""
        import usb.core
        mock_dev = _make_mock_pyusb_device(0x1234, 0xABCD, 1, 3)

        with patch("usb_helper.monitor.usb.util.get_string", side_effect=usb.core.USBError("err")):
            identity = _usb_device_to_identity(mock_dev)

        assert identity.name == ""
        assert identity.serial == ""


class TestUSBMonitorScanOnce:
    def test_scan_finds_matching_devices(self):
        rule = DeviceMatchRule(vid=0x1234, pid=0xABCD, label="Bulk")
        monitor = USBMonitor(match_rules=[rule])

        mock_dev = _make_mock_pyusb_device(0x1234, 0xABCD, 1, 3)

        with patch("usb_helper.monitor.usb.core.find", return_value=[mock_dev]), \
             patch("usb_helper.monitor.usb.util.get_string", return_value="TestDev"):
            results = monitor.scan_once()

        assert len(results) == 1
        identity, matched = results[0]
        assert identity.vid == 0x1234
        assert matched is rule

    def test_scan_filters_non_matching(self):
        rule = DeviceMatchRule(vid=0x1234, pid=0xABCD)
        monitor = USBMonitor(match_rules=[rule])

        # Device has different PID
        mock_dev = _make_mock_pyusb_device(0x1234, 0x0002, 1, 5)

        with patch("usb_helper.monitor.usb.core.find", return_value=[mock_dev]), \
             patch("usb_helper.monitor.usb.util.get_string", return_value=""):
            results = monitor.scan_once()

        assert len(results) == 0

    def test_scan_no_rules_matches_all(self):
        monitor = USBMonitor(match_rules=[])  # empty = match all

        mock_dev = _make_mock_pyusb_device(0xFFFF, 0xFFFF, 2, 7)

        with patch("usb_helper.monitor.usb.core.find", return_value=[mock_dev]), \
             patch("usb_helper.monitor.usb.util.get_string", return_value=""):
            results = monitor.scan_once()

        assert len(results) == 1

    def test_scan_no_backend(self):
        """Should handle missing libusb gracefully."""
        import usb.core
        monitor = USBMonitor()

        with patch("usb_helper.monitor.usb.core.find", side_effect=usb.core.NoBackendError("no backend")):
            results = monitor.scan_once()

        assert results == []


class TestUSBMonitorPollCycle:
    def test_attach_event(self):
        rule = DeviceMatchRule(vid=0x1234, label="test")
        monitor = USBMonitor(match_rules=[rule])
        events: list[DeviceEvent] = []
        monitor.on_device_event = lambda e: events.append(e)

        mock_dev = _make_mock_pyusb_device(0x1234, 0xABCD, 1, 3)

        with patch("usb_helper.monitor.usb.core.find", return_value=[mock_dev]), \
             patch("usb_helper.monitor.usb.util.get_string", return_value=""):
            monitor._poll_cycle()

        assert len(events) == 1
        assert events[0].event_type == DeviceEventType.ATTACHED

    def test_detach_event(self):
        rule = DeviceMatchRule(vid=0x1234, label="test")
        monitor = USBMonitor(match_rules=[rule])
        events: list[DeviceEvent] = []
        monitor.on_device_event = lambda e: events.append(e)

        mock_dev = _make_mock_pyusb_device(0x1234, 0xABCD, 1, 3)

        with patch("usb_helper.monitor.usb.core.find") as mock_find, \
             patch("usb_helper.monitor.usb.util.get_string", return_value=""):
            # Cycle 1: device appears
            mock_find.return_value = [mock_dev]
            monitor._poll_cycle()

            # Cycle 2: device gone
            mock_find.return_value = []
            monitor._poll_cycle()

        assert len(events) == 2
        assert events[0].event_type == DeviceEventType.ATTACHED
        assert events[1].event_type == DeviceEventType.DETACHED

    def test_no_event_if_unchanged(self):
        rule = DeviceMatchRule(vid=0x1234, label="test")
        monitor = USBMonitor(match_rules=[rule])
        events: list[DeviceEvent] = []
        monitor.on_device_event = lambda e: events.append(e)

        mock_dev = _make_mock_pyusb_device(0x1234, 0xABCD, 1, 3)

        with patch("usb_helper.monitor.usb.core.find", return_value=[mock_dev]), \
             patch("usb_helper.monitor.usb.util.get_string", return_value=""):
            monitor._poll_cycle()
            monitor._poll_cycle()  # Same device, no change

        assert len(events) == 1  # Only the initial attach

    def test_callback_exception_handled(self):
        """Monitor should not crash if callback raises."""
        rule = DeviceMatchRule(vid=0x1234)
        monitor = USBMonitor(match_rules=[rule])
        monitor.on_device_event = lambda e: 1 / 0  # ZeroDivisionError

        mock_dev = _make_mock_pyusb_device(0x1234, 0xABCD, 1, 3)

        with patch("usb_helper.monitor.usb.core.find", return_value=[mock_dev]), \
             patch("usb_helper.monitor.usb.util.get_string", return_value=""):
            monitor._poll_cycle()  # Should not raise


class TestUSBMonitorRunForever:
    def test_stop(self):
        """run_forever should exit when stop() is called from another thread."""
        monitor = USBMonitor(match_rules=[], poll_interval_ms=50)

        with patch("usb_helper.monitor.usb.core.find", return_value=[]):
            def stop_after_delay():
                time.sleep(0.15)
                monitor.stop()

            t = threading.Thread(target=stop_after_delay, daemon=True)
            t.start()
            monitor.run_forever()
            t.join(timeout=1)

        assert not monitor.is_running


class TestUSBMonitorKnownDevices:
    def test_known_devices_property(self):
        rule = DeviceMatchRule(vid=0x1234)
        monitor = USBMonitor(match_rules=[rule])

        mock_dev = _make_mock_pyusb_device(0x1234, 0xABCD, 1, 3)

        with patch("usb_helper.monitor.usb.core.find", return_value=[mock_dev]), \
             patch("usb_helper.monitor.usb.util.get_string", return_value=""):
            monitor._poll_cycle()

        known = monitor.known_devices
        assert len(known) == 1
        assert "usb:1-3" in known
