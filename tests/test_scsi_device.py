"""
Unit tests for SCSIDevice — mock the underlying BulkDevice layer.

Tests cover:
  - send_command read (data IN)
  - send_command write (data OUT)
  - CSW failure handling
  - CSW phase error → automatic retry
  - max retries exhausted
  - invalid CSW response
  - both data_out and data_in_length → ValueError
"""

from __future__ import annotations

import struct
from unittest.mock import patch, MagicMock

import pytest

from usb_helper.scsi_device import SCSIDevice
from usb_helper.types import DeviceIdentity, TransferResult
from usb_helper._cbw import CSW_SIZE, CSW_STATUS_PASSED, CSW_STATUS_FAILED, CSW_STATUS_PHASE_ERROR

from tests.conftest import build_csw_bytes


@pytest.fixture
def identity():
    return DeviceIdentity(vid=0x1234, pid=0x0002, bus=1, address=5)


@pytest.fixture
def mock_scsi(identity):
    """
    Create a SCSIDevice with its internal BulkDevice fully mocked.
    Returns (scsi_device, mock_bulk) for direct control of bulk I/O.
    """
    scsi = SCSIDevice(identity, frame_size=16384, max_retries=2)

    mock_bulk = MagicMock()
    mock_bulk.is_open = True
    scsi._bulk = mock_bulk
    scsi._is_open = True

    return scsi, mock_bulk


class TestSCSIDeviceReadCommand:
    def test_read_command_success(self, mock_scsi):
        scsi, mock_bulk = mock_scsi
        cdb = bytes([0xAF, 0x00, 0x00, 0x00, 0x00, 0x00])
        response_data = b"\xDE\xAD" * 32

        # Sequence: write CBW → read data → read CSW
        csw = build_csw_bytes(tag=1, residue=0, status=CSW_STATUS_PASSED)
        mock_bulk.bulk_write.return_value = TransferResult(ok=True, bytes_transferred=31)
        mock_bulk.bulk_read.side_effect = [
            TransferResult(ok=True, data=response_data, bytes_transferred=64),   # data phase
            TransferResult(ok=True, data=csw, bytes_transferred=CSW_SIZE),        # CSW
        ]

        result = scsi.send_command(cdb=cdb, data_in_length=64)
        assert result.ok
        assert result.data == response_data
        assert mock_bulk.bulk_write.call_count == 1   # CBW only
        assert mock_bulk.bulk_read.call_count == 2     # data + CSW


class TestSCSIDeviceWriteCommand:
    def test_write_command_success(self, mock_scsi):
        scsi, mock_bulk = mock_scsi
        cdb = bytes([0xB0, 0x03, 0x00, 0x00])
        data_out = b"\xAB" * 512

        csw = build_csw_bytes(tag=1, residue=0, status=CSW_STATUS_PASSED)
        mock_bulk.bulk_write.return_value = TransferResult(ok=True, bytes_transferred=31)
        mock_bulk.bulk_read.return_value = TransferResult(ok=True, data=csw, bytes_transferred=CSW_SIZE)

        result = scsi.send_command(cdb=cdb, data_out=data_out)
        assert result.ok
        assert mock_bulk.bulk_write.call_count == 2  # CBW + data


class TestSCSIDeviceErrors:
    def test_csw_command_failed(self, mock_scsi):
        scsi, mock_bulk = mock_scsi
        cdb = bytes(6)

        csw = build_csw_bytes(tag=1, residue=0, status=CSW_STATUS_FAILED)
        mock_bulk.bulk_write.return_value = TransferResult(ok=True, bytes_transferred=31)
        mock_bulk.bulk_read.return_value = TransferResult(ok=True, data=csw, bytes_transferred=CSW_SIZE)

        result = scsi.send_command(cdb=cdb, data_in_length=64)
        assert not result.ok
        assert result.error_code == 12
        assert "FAILED" in result.error_message

    def test_csw_phase_error_retries(self, mock_scsi):
        """Phase error should trigger retry up to max_retries."""
        scsi, mock_bulk = mock_scsi
        cdb = bytes(6)

        csw_phase = build_csw_bytes(tag=1, residue=0, status=CSW_STATUS_PHASE_ERROR)
        csw_pass = build_csw_bytes(tag=3, residue=0, status=CSW_STATUS_PASSED)

        mock_bulk.bulk_write.return_value = TransferResult(ok=True, bytes_transferred=31)
        # Attempt 1: phase error → retry
        # Attempt 2: phase error → retry
        # Attempt 3: success
        mock_bulk.bulk_read.side_effect = [
            TransferResult(ok=True, data=b"\x00" * 64, bytes_transferred=64),  # data attempt 1
            TransferResult(ok=True, data=csw_phase, bytes_transferred=CSW_SIZE),  # CSW attempt 1
            TransferResult(ok=True, data=b"\x00" * 64, bytes_transferred=64),  # data attempt 2
            TransferResult(ok=True, data=csw_phase, bytes_transferred=CSW_SIZE),  # CSW attempt 2
            TransferResult(ok=True, data=b"\x00" * 64, bytes_transferred=64),  # data attempt 3
            TransferResult(ok=True, data=csw_pass, bytes_transferred=CSW_SIZE),   # CSW attempt 3
        ]

        result = scsi.send_command(cdb=cdb, data_in_length=64)
        assert result.ok
        # 3 attempts × (1 write + 2 reads) = should have multiple calls
        assert mock_bulk.bulk_write.call_count == 3  # 3 CBWs

    def test_cbw_write_fails(self, mock_scsi):
        scsi, mock_bulk = mock_scsi
        mock_bulk.bulk_write.return_value = TransferResult(
            ok=False, error_code=2, error_message="timeout"
        )
        result = scsi.send_command(cdb=bytes(6), data_in_length=64)
        assert not result.ok

    def test_csw_read_fails(self, mock_scsi):
        scsi, mock_bulk = mock_scsi
        mock_bulk.bulk_write.return_value = TransferResult(ok=True, bytes_transferred=31)
        mock_bulk.bulk_read.side_effect = [
            TransferResult(ok=True, data=b"\x00" * 64, bytes_transferred=64),  # data
            TransferResult(ok=False, error_code=2, error_message="CSW timeout"),  # CSW failed
        ]
        result = scsi.send_command(cdb=bytes(6), data_in_length=64)
        assert not result.ok
        assert result.error_code == 10

    def test_invalid_csw_data(self, mock_scsi):
        scsi, mock_bulk = mock_scsi
        mock_bulk.bulk_write.return_value = TransferResult(ok=True, bytes_transferred=31)
        mock_bulk.bulk_read.side_effect = [
            TransferResult(ok=True, data=b"\x00" * 64, bytes_transferred=64),  # data
            TransferResult(ok=True, data=b"XXXX" + b"\x00" * 9, bytes_transferred=13),  # bad CSW
        ]
        result = scsi.send_command(cdb=bytes(6), data_in_length=64)
        assert not result.ok
        assert result.error_code == 11

    def test_both_data_out_and_data_in_raises(self, mock_scsi):
        scsi, mock_bulk = mock_scsi
        with pytest.raises(ValueError, match="Cannot specify both"):
            scsi.send_command(cdb=bytes(6), data_out=b"\x00", data_in_length=64)


class TestSCSIDeviceCSWInsteadOfData:
    """Test handling of devices that skip data phase and return CSW directly.

    Some vendor-class USB devices (interface class 0xFF) respond to
    data-IN commands by returning a CSW immediately instead of the
    requested data bytes.  For example, the vendor-class test device
    returns 13 bytes starting with "USBS" (CSW signature) when the
    command is sent while the device is not ready.
    """

    def test_csw_instead_of_data_phase_error(self, mock_scsi):
        """Device returns CSW with PHASE_ERROR instead of data → retry."""
        scsi, mock_bulk = mock_scsi
        cdb = bytes([0xCB, 0x00, 0x00, 0x00, 0x00, 0x00])

        # CSW returned directly instead of 2-byte data (phase error)
        csw_phase = build_csw_bytes(tag=0, residue=0, status=CSW_STATUS_PHASE_ERROR)
        # After retry, device returns proper data + successful CSW
        good_data = b"\xFF\x00"
        csw_pass = build_csw_bytes(tag=2, residue=0, status=CSW_STATUS_PASSED)

        mock_bulk.bulk_write.return_value = TransferResult(ok=True, bytes_transferred=31)
        mock_bulk.bulk_read.side_effect = [
            # Attempt 1: CSW instead of data (13 bytes starting with USBS)
            TransferResult(ok=True, data=csw_phase, bytes_transferred=len(csw_phase)),
            # Attempt 2: proper data
            TransferResult(ok=True, data=good_data, bytes_transferred=2),
            # Attempt 2: separate CSW
            TransferResult(ok=True, data=csw_pass, bytes_transferred=CSW_SIZE),
        ]

        result = scsi.send_command(cdb=cdb, data_in_length=2)
        assert result.ok
        assert result.data == b"\xFF\x00"
        # 2 attempts = 2 CBW writes
        assert mock_bulk.bulk_write.call_count == 2
        # Attempt 1: 1 read (got CSW). Attempt 2: 2 reads (data + CSW)
        assert mock_bulk.bulk_read.call_count == 3

    def test_csw_instead_of_data_command_failed(self, mock_scsi):
        """Device returns CSW with FAILED status instead of data."""
        scsi, mock_bulk = mock_scsi
        cdb = bytes([0xCB, 0x00, 0x00, 0x00, 0x00, 0x00])

        csw_fail = build_csw_bytes(tag=1, residue=0, status=CSW_STATUS_FAILED)

        mock_bulk.bulk_write.return_value = TransferResult(ok=True, bytes_transferred=31)
        mock_bulk.bulk_read.return_value = TransferResult(
            ok=True, data=csw_fail, bytes_transferred=len(csw_fail),
        )

        result = scsi.send_command(cdb=cdb, data_in_length=2)
        assert not result.ok
        assert result.error_code == 12
        assert "FAILED" in result.error_message
        # Only 1 read needed (CSW came instead of data)
        assert mock_bulk.bulk_read.call_count == 1

    def test_csw_instead_of_data_success(self, mock_scsi):
        """Device returns CSW with PASSED status but no data."""
        scsi, mock_bulk = mock_scsi
        cdb = bytes([0xCB, 0x00, 0x00, 0x00, 0x00, 0x00])

        csw_pass = build_csw_bytes(tag=1, residue=2, status=CSW_STATUS_PASSED)

        mock_bulk.bulk_write.return_value = TransferResult(ok=True, bytes_transferred=31)
        mock_bulk.bulk_read.return_value = TransferResult(
            ok=True, data=csw_pass, bytes_transferred=len(csw_pass),
        )

        result = scsi.send_command(cdb=cdb, data_in_length=2)
        assert result.ok
        assert result.data == b""  # No data, just CSW
        assert mock_bulk.bulk_read.call_count == 1
