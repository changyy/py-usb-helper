"""
Tests for Darwin (macOS) SCSI ioctl backend.

All tests are mock-based and can run on any platform.
"""

from __future__ import annotations

import struct
from unittest.mock import patch, MagicMock, mock_open

import pytest

from usb_helper.types import DeviceIdentity, TransferResult


# ── DarwinSCSITransport tests ───────────────────────────────────


class TestDarwinSCSITransport:
    """Test DarwinSCSITransport with mocked os/fcntl calls."""

    def _make_transport(self):
        from usb_helper._darwin_scsi import DarwinSCSITransport
        return DarwinSCSITransport()

    @patch("usb_helper._darwin_scsi.os.open", return_value=42)
    def test_open_success(self, mock_os_open):
        transport = self._make_transport()
        transport.open("/dev/rdisk4")
        assert transport.is_open
        mock_os_open.assert_called_once()

    @patch("usb_helper._darwin_scsi.os.open", side_effect=OSError("Permission denied"))
    def test_open_permission_error(self, mock_os_open):
        transport = self._make_transport()
        with pytest.raises(OSError, match="Permission"):
            transport.open("/dev/rdisk4")
        assert not transport.is_open

    @patch("usb_helper._darwin_scsi.os.close")
    @patch("usb_helper._darwin_scsi.os.open", return_value=42)
    def test_close(self, mock_os_open, mock_os_close):
        transport = self._make_transport()
        transport.open("/dev/rdisk4")
        transport.close()
        assert not transport.is_open
        mock_os_close.assert_called_once_with(42)

    def test_send_command_not_open(self):
        transport = self._make_transport()
        result = transport.send_command(cdb=b"\xcb\x00", data_in_length=2)
        assert not result.ok
        assert result.error_code == 1

    @patch("usb_helper._darwin_scsi.fcntl.ioctl")
    @patch("usb_helper._darwin_scsi.os.open", return_value=42)
    def test_send_read_command(self, mock_os_open, mock_ioctl):
        transport = self._make_transport()
        transport.open("/dev/rdisk4")

        # ioctl succeeds (returns None / no exception), scsiStatus stays 0
        mock_ioctl.return_value = None

        result = transport.send_command(
            cdb=b"\xcb\x00\x00\x00\x00\x00",
            data_in_length=2,
        )

        assert result.ok
        assert len(result.data) == 2
        mock_ioctl.assert_called_once()

    @patch("usb_helper._darwin_scsi.fcntl.ioctl")
    @patch("usb_helper._darwin_scsi.os.open", return_value=42)
    def test_send_write_command(self, mock_os_open, mock_ioctl):
        transport = self._make_transport()
        transport.open("/dev/rdisk4")

        mock_ioctl.return_value = None

        result = transport.send_command(
            cdb=b"\xb0\x03\x00\x00",
            data_out=b"\xAB" * 512,
        )

        assert result.ok
        assert result.bytes_transferred == 512

    @patch("usb_helper._darwin_scsi.fcntl.ioctl", side_effect=OSError("ioctl error"))
    @patch("usb_helper._darwin_scsi.os.open", return_value=42)
    def test_send_command_ioctl_error(self, mock_os_open, mock_ioctl):
        transport = self._make_transport()
        transport.open("/dev/rdisk4")

        result = transport.send_command(
            cdb=b"\xcb\x00",
            data_in_length=2,
        )

        assert not result.ok
        assert result.error_code == 15
        assert "ioctl failed" in result.error_message

    def test_both_data_out_and_data_in_raises(self):
        from usb_helper._darwin_scsi import DarwinSCSITransport
        transport = DarwinSCSITransport()
        transport._fd = 42  # pretend it's open
        with pytest.raises(ValueError, match="Cannot specify both"):
            transport.send_command(
                cdb=b"\x00",
                data_out=b"\x00",
                data_in_length=2,
            )

    @patch("usb_helper._darwin_scsi.fcntl.ioctl")
    @patch("usb_helper._darwin_scsi.os.open", return_value=42)
    def test_send_command_no_data_phase(self, mock_os_open, mock_ioctl):
        transport = self._make_transport()
        transport.open("/dev/rdisk4")

        mock_ioctl.return_value = None

        result = transport.send_command(cdb=b"\xb0\xff\x00\x00")

        assert result.ok
        assert result.bytes_transferred == 0


# ── find_bsd_node tests ────────────────────────────────────────


class TestFindBsdNode:
    """Test BSD node discovery with mocked subprocess calls."""

    @patch("usb_helper._darwin_scsi.sys")
    def test_not_darwin_returns_none(self, mock_sys):
        from usb_helper._darwin_scsi import find_bsd_node
        mock_sys.platform = "linux"
        assert find_bsd_node(0x1DE1, 0x1205) is None

    @patch("usb_helper._darwin_scsi.sys")
    @patch("usb_helper._darwin_scsi._find_bsd_via_diskutil")
    def test_diskutil_match(self, mock_diskutil, mock_sys):
        from usb_helper._darwin_scsi import find_bsd_node
        mock_sys.platform = "darwin"
        mock_diskutil.return_value = "/dev/rdisk4"

        result = find_bsd_node(0x1DE1, 0x1205)
        assert result == "/dev/rdisk4"

    @patch("usb_helper._darwin_scsi.sys")
    @patch("usb_helper._darwin_scsi._find_bsd_via_ioreg_line")
    @patch("usb_helper._darwin_scsi._find_bsd_via_diskutil", return_value=None)
    def test_fallback_to_ioreg_line(self, mock_diskutil, mock_ioreg_line, mock_sys):
        from usb_helper._darwin_scsi import find_bsd_node
        mock_sys.platform = "darwin"
        mock_ioreg_line.return_value = "/dev/rdisk5"

        result = find_bsd_node(0x1DE1, 0x1205)
        assert result == "/dev/rdisk5"

    @patch("usb_helper._darwin_scsi.sys")
    @patch("usb_helper._darwin_scsi._find_bsd_via_ioreg_plist")
    @patch("usb_helper._darwin_scsi._find_bsd_via_ioreg_line", return_value=None)
    @patch("usb_helper._darwin_scsi._find_bsd_via_diskutil", return_value=None)
    def test_fallback_to_ioreg_plist(self, mock_diskutil, mock_ioreg_line, mock_ioreg_plist, mock_sys):
        from usb_helper._darwin_scsi import find_bsd_node
        mock_sys.platform = "darwin"
        mock_ioreg_plist.return_value = "/dev/rdisk6"

        result = find_bsd_node(0x1DE1, 0x1205)
        assert result == "/dev/rdisk6"

    @patch("usb_helper._darwin_scsi.sys")
    @patch("usb_helper._darwin_scsi._find_bsd_via_ioreg_plist", return_value=None)
    @patch("usb_helper._darwin_scsi._find_bsd_via_ioreg_line", return_value=None)
    @patch("usb_helper._darwin_scsi._find_bsd_via_diskutil", return_value=None)
    def test_no_match_returns_none(self, mock_diskutil, mock_ioreg_line, mock_ioreg_plist, mock_sys):
        from usb_helper._darwin_scsi import find_bsd_node
        mock_sys.platform = "darwin"
        assert find_bsd_node(0x1DE1, 0x1205) is None

    def test_ioreg_line_parser(self):
        """Test the ioreg line-based search with synthetic ioreg output."""
        from usb_helper._darwin_scsi import _find_bsd_via_ioreg_line

        with patch("usb_helper._darwin_scsi.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="""\
+-o Root  <class IORegistryEntry>
  +-o AppleARMPE  <class AppleARMPE>
    +-o USB20Bus@01000000  <class IOUSBHostDevice>
      | {
      |   "idVendor" = 0x1de1
      |   "idProduct" = 0x1205
      |   "USB Product Name" = "Actions Device"
      | }
      +-o IOUSBMassStorageClass  <class IOUSBMassStorageClass>
        +-o IOSCSIPeripheralDeviceType00  <class IOSCSIPeripheralDeviceType00>
          +-o IOMedia  <class IOMedia>
            | {
            |   "BSD Name" = "disk4"
            |   "Content" = "None"
            | }
""",
            )
            result = _find_bsd_via_ioreg_line(0x1DE1, 0x1205)
            assert result == "/dev/rdisk4"

    def test_ioreg_line_parser_with_serial_filter(self):
        from usb_helper._darwin_scsi import _find_bsd_via_ioreg_line

        with patch("usb_helper._darwin_scsi.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="""\
+-o Root  <class IORegistryEntry>
  +-o DevA  <class IOUSBHostDevice>
    | {
    |   "idVendor" = 0x1de1
    |   "idProduct" = 0x1205
    |   "USB Serial Number" = "AAA111"
    | }
    +-o IOMedia  <class IOMedia>
      | {
      |   "BSD Name" = "disk4"
      | }
  +-o DevB  <class IOUSBHostDevice>
    | {
    |   "idVendor" = 0x1de1
    |   "idProduct" = 0x1205
    |   "USB Serial Number" = "BBB222"
    | }
    +-o IOMedia  <class IOMedia>
      | {
      |   "BSD Name" = "disk5"
      | }
""",
            )
            result = _find_bsd_via_ioreg_line(0x1DE1, 0x1205, serial="BBB222")
            assert result == "/dev/rdisk5"

    def test_diskutil_serial_filter(self):
        from usb_helper._darwin_scsi import _find_bsd_via_diskutil

        list_plist = (
            b"<?xml version='1.0' encoding='UTF-8'?>"
            b"<!DOCTYPE plist PUBLIC '-//Apple//DTD PLIST 1.0//EN' "
            b"'http://www.apple.com/DTDs/PropertyList-1.0.dtd'>"
            b"<plist version='1.0'><dict>"
            b"<key>AllDisks</key><array><string>disk4</string><string>disk5</string></array>"
            b"<key>AllDisksAndPartitions</key><array>"
            b"<dict><key>DeviceIdentifier</key><string>disk4</string></dict>"
            b"<dict><key>DeviceIdentifier</key><string>disk5</string></dict>"
            b"</array></dict></plist>"
        )
        info_disk4 = (
            b"<?xml version='1.0' encoding='UTF-8'?>"
            b"<!DOCTYPE plist PUBLIC '-//Apple//DTD PLIST 1.0//EN' "
            b"'http://www.apple.com/DTDs/PropertyList-1.0.dtd'>"
            b"<plist version='1.0'><dict>"
            b"<key>IORegistryEntryUSBVendorID</key><integer>7649</integer>"
            b"<key>IORegistryEntryUSBProductID</key><integer>4613</integer>"
            b"<key>USBSerialNumber</key><string>AAA111</string>"
            b"<key>DeviceNode</key><string>/dev/disk4</string>"
            b"</dict></plist>"
        )
        info_disk5 = (
            b"<?xml version='1.0' encoding='UTF-8'?>"
            b"<!DOCTYPE plist PUBLIC '-//Apple//DTD PLIST 1.0//EN' "
            b"'http://www.apple.com/DTDs/PropertyList-1.0.dtd'>"
            b"<plist version='1.0'><dict>"
            b"<key>IORegistryEntryUSBVendorID</key><integer>7649</integer>"
            b"<key>IORegistryEntryUSBProductID</key><integer>4613</integer>"
            b"<key>USBSerialNumber</key><string>BBB222</string>"
            b"<key>DeviceNode</key><string>/dev/disk5</string>"
            b"</dict></plist>"
        )

        with patch("usb_helper._darwin_scsi.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=list_plist),
                MagicMock(returncode=0, stdout=info_disk4),
                MagicMock(returncode=0, stdout=info_disk5),
            ]
            result = _find_bsd_via_diskutil(0x1DE1, 0x1205, serial="BBB222")
            assert result == "/dev/rdisk5"

    def test_ioreg_plist_serial_filter(self):
        from usb_helper._darwin_scsi import _find_bsd_via_ioreg_plist
        import plistlib

        plist_bytes = plistlib.dumps(
            [
                {
                    "idVendor": 0x1DE1,
                    "idProduct": 0x1205,
                    "USB Serial Number": "AAA111",
                    "IORegistryEntryChildren": [{"BSD Name": "disk4"}],
                },
                {
                    "idVendor": 0x1DE1,
                    "idProduct": 0x1205,
                    "USB Serial Number": "BBB222",
                    "IORegistryEntryChildren": [{"BSD Name": "disk5"}],
                },
            ]
        )

        with patch("usb_helper._darwin_scsi.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=plist_bytes)
            result = _find_bsd_via_ioreg_plist(0x1DE1, 0x1205, serial="BBB222")
            assert result == "/dev/rdisk5"


# ── SCSIDevice Darwin integration tests ────────────────────────


class TestSCSIDeviceDarwinIntegration:
    """Test that SCSIDevice routes to Darwin transport on macOS."""

    @patch("usb_helper.scsi_device._IS_DARWIN", True)
    @patch("usb_helper._darwin_scsi.find_bsd_node", return_value="/dev/rdisk4")
    @patch("usb_helper._darwin_scsi.DarwinSCSITransport")
    def test_open_uses_darwin_on_macos(self, MockTransport, mock_find):
        transport = MagicMock()
        MockTransport.return_value = transport

        identity = DeviceIdentity(vid=0x1DE1, pid=0x1205, bus=1, address=3)
        scsi = SCSIDevice(identity)
        scsi.open()

        assert scsi.is_open
        assert scsi.using_darwin_ioctl
        transport.open.assert_called_once_with("/dev/rdisk4")

    @patch("usb_helper.scsi_device._IS_DARWIN", True)
    @patch("usb_helper._darwin_scsi.find_bsd_node", return_value=None)
    def test_fallback_to_libusb_when_no_bsd_node(self, mock_find):
        identity = DeviceIdentity(vid=0x1DE1, pid=0x1205, bus=1, address=3)
        scsi = SCSIDevice(identity)

        # Mock the bulk device to avoid actual USB
        mock_bulk = MagicMock()
        scsi._bulk = mock_bulk

        scsi.open()

        assert scsi.is_open
        assert not scsi.using_darwin_ioctl
        mock_bulk.open.assert_called_once()

    @patch("usb_helper.scsi_device._IS_DARWIN", True)
    @patch("usb_helper._darwin_scsi.find_bsd_node", return_value="/dev/rdisk4")
    @patch("usb_helper._darwin_scsi.DarwinSCSITransport")
    def test_send_command_routes_to_darwin(self, MockTransport, mock_find):
        transport = MagicMock()
        transport.send_command.return_value = TransferResult(
            ok=True, data=b"\xFF\x00", bytes_transferred=2
        )
        MockTransport.return_value = transport

        identity = DeviceIdentity(vid=0x1DE1, pid=0x1205, bus=1, address=3)
        scsi = SCSIDevice(identity)
        scsi.open()

        result = scsi.send_command(cdb=b"\xcb\x00", data_in_length=2)

        assert result.ok
        assert result.data == b"\xFF\x00"
        transport.send_command.assert_called_once()

    @patch("usb_helper.scsi_device._IS_DARWIN", True)
    @patch("usb_helper._darwin_scsi.find_bsd_node", return_value="/dev/rdisk4")
    @patch("usb_helper._darwin_scsi.DarwinSCSITransport")
    def test_close_closes_darwin_transport(self, MockTransport, mock_find):
        transport = MagicMock()
        MockTransport.return_value = transport

        identity = DeviceIdentity(vid=0x1DE1, pid=0x1205, bus=1, address=3)
        scsi = SCSIDevice(identity)
        scsi.open()
        scsi.close()

        transport.close.assert_called_once()
        assert not scsi.is_open

    @patch("usb_helper.scsi_device._IS_DARWIN", False)
    def test_linux_skips_darwin(self):
        """On non-Darwin platforms, never try Darwin transport."""
        identity = DeviceIdentity(vid=0x1DE1, pid=0x1205, bus=1, address=3)
        scsi = SCSIDevice(identity)

        mock_bulk = MagicMock()
        scsi._bulk = mock_bulk

        scsi.open()

        assert not scsi.using_darwin_ioctl
        mock_bulk.open.assert_called_once()


# Need to import after mocking setup
from usb_helper.scsi_device import SCSIDevice
