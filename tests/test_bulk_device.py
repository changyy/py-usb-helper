"""
Unit tests for BulkDevice — all USB hardware mocked.

Tests cover:
  - open/close lifecycle
  - device not found error
  - bulk write (single frame + multi-frame splitting)
  - bulk write timeout handling
  - bulk read success + timeout
  - bulk_write_read convenience method
  - context manager usage
  - write when device not open
"""

from __future__ import annotations

import struct
from unittest.mock import patch, MagicMock, call

import pytest
import usb.core
import usb.util

from usb_helper.bulk_device import BulkDevice
from usb_helper.types import (
    DeviceIdentity,
    USBDeviceNotFoundError,
    USBPermissionError,
    USBTransferError,
    USBError,
)


@pytest.fixture
def identity():
    return DeviceIdentity(vid=0x1234, pid=0xABCD, bus=1, address=3)


@pytest.fixture
def mock_usb_env(identity):
    """
    Patch usb.core.find and usb.util to return a mock device
    with proper endpoint discovery.
    """
    mock_dev = MagicMock()
    mock_dev.idVendor = identity.vid
    mock_dev.idProduct = identity.pid
    mock_dev.bus = identity.bus
    mock_dev.address = identity.address
    mock_dev.is_kernel_driver_active.return_value = False

    # Mock endpoints
    ep_out = MagicMock()
    ep_out.bEndpointAddress = 0x01

    ep_in = MagicMock()
    ep_in.bEndpointAddress = 0x81

    # Interface and config
    intf = MagicMock()
    cfg = MagicMock()
    cfg.__getitem__ = MagicMock(return_value=intf)
    mock_dev.get_active_configuration.return_value = cfg

    # Patch usb.core.find to return our mock
    with patch("usb_helper.bulk_device.usb.core.find", return_value=mock_dev) as mock_find, \
         patch("usb_helper.bulk_device.usb.util.claim_interface"), \
         patch("usb_helper.bulk_device.usb.util.release_interface"), \
         patch("usb_helper.bulk_device.usb.util.dispose_resources"), \
         patch("usb_helper.bulk_device.usb.util.find_descriptor") as mock_find_desc, \
         patch("usb_helper.bulk_device.usb.util.endpoint_direction") as mock_ep_dir:

        # Make find_descriptor return ep_out first, then ep_in
        mock_find_desc.side_effect = [ep_out, ep_in]

        yield {
            "device": mock_dev,
            "ep_out": ep_out,
            "ep_in": ep_in,
            "find": mock_find,
            "find_descriptor": mock_find_desc,
        }


class TestBulkDeviceOpen:
    def test_open_success(self, identity, mock_usb_env):
        dev = BulkDevice(identity)
        dev.open()
        assert dev.is_open
        mock_usb_env["find"].assert_called_once()

    def test_open_idempotent(self, identity, mock_usb_env):
        dev = BulkDevice(identity)
        dev.open()
        dev.open()  # Should not raise
        assert dev.is_open
        # find should only be called once
        mock_usb_env["find"].assert_called_once()

    def test_open_device_not_found(self, identity):
        with patch("usb_helper.bulk_device.usb.core.find", return_value=None):
            dev = BulkDevice(identity)
            with pytest.raises(USBDeviceNotFoundError):
                dev.open()

    def test_open_permission_error(self, identity):
        mock_dev = MagicMock()
        mock_dev.is_kernel_driver_active.return_value = False

        with patch("usb_helper.bulk_device.usb.core.find", return_value=mock_dev), \
             patch("usb_helper.bulk_device.usb.util.claim_interface",
                   side_effect=usb.core.USBError("Access denied")):
            dev = BulkDevice(identity)
            with pytest.raises(USBPermissionError, match="Cannot claim"):
                dev.open()

    def test_open_busy_error(self, identity):
        mock_dev = MagicMock()
        mock_dev.is_kernel_driver_active.return_value = False

        with patch("usb_helper.bulk_device.usb.core.find", return_value=mock_dev), \
             patch("usb_helper.bulk_device.usb.util.claim_interface",
                   side_effect=usb.core.USBError("Resource busy")):
            dev = BulkDevice(identity)
            with pytest.raises(USBTransferError, match="busy"):
                dev.open()


class TestBulkDeviceClose:
    def test_close(self, identity, mock_usb_env):
        dev = BulkDevice(identity)
        dev.open()
        dev.close()
        assert not dev.is_open

    def test_close_idempotent(self, identity, mock_usb_env):
        dev = BulkDevice(identity)
        dev.open()
        dev.close()
        dev.close()  # Should not raise
        assert not dev.is_open

    def test_close_without_open(self, identity):
        dev = BulkDevice(identity)
        dev.close()  # Should not raise


class TestBulkDeviceWrite:
    def test_write_not_open(self, identity):
        dev = BulkDevice(identity)
        result = dev.bulk_write(b"\x00" * 64)
        assert not result.ok
        assert result.error_message == "Device not open"

    def test_write_single_frame(self, identity, mock_usb_env):
        ep_out = mock_usb_env["ep_out"]
        ep_out.write.return_value = 64

        dev = BulkDevice(identity, frame_size=1024)
        dev.open()

        result = dev.bulk_write(b"\xAB" * 64)
        assert result.ok
        assert result.bytes_transferred == 64
        ep_out.write.assert_called_once()

    def test_write_multi_frame_split(self, identity, mock_usb_env):
        """Data larger than frame_size should be split into chunks."""
        ep_out = mock_usb_env["ep_out"]
        ep_out.write.side_effect = lambda data, timeout=None: len(data)

        dev = BulkDevice(identity, frame_size=100)
        dev.open()

        data = b"\xCD" * 250  # Will be split: 100 + 100 + 50
        result = dev.bulk_write(data)
        assert result.ok
        assert result.bytes_transferred == 250
        assert ep_out.write.call_count == 3

        # Verify chunk sizes
        calls = ep_out.write.call_args_list
        assert len(calls[0][0][0]) == 100
        assert len(calls[1][0][0]) == 100
        assert len(calls[2][0][0]) == 50

    def test_write_timeout(self, identity, mock_usb_env):
        ep_out = mock_usb_env["ep_out"]
        ep_out.write.side_effect = usb.core.USBTimeoutError("timeout")

        dev = BulkDevice(identity)
        dev.open()

        result = dev.bulk_write(b"\x00" * 64)
        assert not result.ok
        assert result.error_code == 2
        assert "timeout" in result.error_message.lower()

    def test_write_usb_error(self, identity, mock_usb_env):
        ep_out = mock_usb_env["ep_out"]
        ep_out.write.side_effect = usb.core.USBError("pipe error")

        dev = BulkDevice(identity)
        dev.open()

        result = dev.bulk_write(b"\x00" * 64)
        assert not result.ok
        assert result.error_code == 3


class TestBulkDeviceRead:
    def test_read_not_open(self, identity):
        dev = BulkDevice(identity)
        result = dev.bulk_read(64)
        assert not result.ok
        assert result.error_message == "Device not open"

    def test_read_success(self, identity, mock_usb_env):
        ep_in = mock_usb_env["ep_in"]
        ep_in.read.return_value = bytearray(b"\xEF" * 64)

        dev = BulkDevice(identity)
        dev.open()

        result = dev.bulk_read(64)
        assert result.ok
        assert result.data == b"\xEF" * 64
        assert result.bytes_transferred == 64

    def test_read_timeout(self, identity, mock_usb_env):
        ep_in = mock_usb_env["ep_in"]
        ep_in.read.side_effect = usb.core.USBTimeoutError("timeout")

        dev = BulkDevice(identity)
        dev.open()

        result = dev.bulk_read(64)
        assert not result.ok
        assert result.error_code == 2

    def test_read_usb_error(self, identity, mock_usb_env):
        ep_in = mock_usb_env["ep_in"]
        ep_in.read.side_effect = usb.core.USBError("pipe error")

        dev = BulkDevice(identity)
        dev.open()

        result = dev.bulk_read(64)
        assert not result.ok
        assert result.error_code == 3


class TestBulkDeviceWriteRead:
    def test_write_read_success(self, identity, mock_usb_env):
        ep_out = mock_usb_env["ep_out"]
        ep_in = mock_usb_env["ep_in"]
        ep_out.write.return_value = 31
        ep_in.read.return_value = bytearray(b"\xAA" * 13)

        dev = BulkDevice(identity)
        dev.open()

        result = dev.bulk_write_read(b"\x00" * 31, read_size=13)
        assert result.ok
        assert result.data == b"\xAA" * 13

    def test_write_read_write_fails(self, identity, mock_usb_env):
        ep_out = mock_usb_env["ep_out"]
        ep_out.write.side_effect = usb.core.USBTimeoutError("timeout")

        dev = BulkDevice(identity)
        dev.open()

        result = dev.bulk_write_read(b"\x00" * 31, read_size=13)
        assert not result.ok
        # Should not attempt read if write failed


class TestBulkDeviceContextManager:
    def test_context_manager(self, identity, mock_usb_env):
        with BulkDevice(identity) as dev:
            assert dev.is_open
        assert not dev.is_open

    def test_context_manager_exception(self, identity, mock_usb_env):
        """Device should be closed even if exception occurs inside with block."""
        try:
            with BulkDevice(identity) as dev:
                assert dev.is_open
                raise ValueError("test error")
        except ValueError:
            pass
        assert not dev.is_open


class TestBulkDeviceRepr:
    def test_repr_closed(self, identity):
        dev = BulkDevice(identity)
        r = repr(dev)
        assert "BulkDevice" in r
        assert "closed" in r

    def test_repr_open(self, identity, mock_usb_env):
        dev = BulkDevice(identity)
        dev.open()
        r = repr(dev)
        assert "open" in r
