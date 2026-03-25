"""
SCSI-over-Bulk USB device implementation.

Wraps BulkDevice to provide SCSI command execution using
the USB Mass Storage Bulk-Only Transport (CBW/CSW) protocol.
"""

from __future__ import annotations

import logging
from typing import Optional

from .bulk_device import BulkDevice
from .device import USBDevice
from ._cbw import (
    build_cbw,
    parse_csw,
    next_tag,
    CSWResult,
    CSW_SIZE,
    CSW_STATUS_PASSED,
    CSW_STATUS_PHASE_ERROR,
)
from .types import (
    DeviceIdentity,
    TransferResult,
    USBError,
    USBTransferError,
)

logger = logging.getLogger(__name__)


class SCSIDevice(USBDevice):
    """
    USB device communicating via SCSI commands over bulk transport.

    Internally uses BulkDevice for raw bulk transfers and wraps
    commands in CBW/CSW protocol.

    Args:
        identity: Device identity
        frame_size: Max data payload per bulk transfer (default 16384)
        interface_number: USB interface to claim (default 0)
        max_retries: Max retries on phase error (default 2)
    """

    def __init__(
        self,
        identity: DeviceIdentity,
        frame_size: int = 16384,
        interface_number: int = 0,
        max_retries: int = 2,
    ):
        super().__init__(identity)
        self._bulk = BulkDevice(
            identity,
            frame_size=frame_size,
            interface_number=interface_number,
        )
        self._frame_size = frame_size
        self._max_retries = max_retries
        self._tag: int = 0

    @property
    def frame_size(self) -> int:
        return self._frame_size

    @property
    def bulk_device(self) -> BulkDevice:
        """Access the underlying BulkDevice for raw transfers if needed."""
        return self._bulk

    def open(self) -> None:
        if self._is_open:
            return
        self._bulk.open()
        self._is_open = True
        self._tag = 0

    def close(self) -> None:
        if not self._is_open:
            return
        self._bulk.close()
        self._is_open = False

    def bulk_write(self, data: bytes, timeout_ms: int = 5000) -> TransferResult:
        """Direct bulk write (delegates to underlying BulkDevice)."""
        return self._bulk.bulk_write(data, timeout_ms=timeout_ms)

    def bulk_read(self, size: int, timeout_ms: int = 5000) -> TransferResult:
        """Direct bulk read (delegates to underlying BulkDevice)."""
        return self._bulk.bulk_read(size, timeout_ms=timeout_ms)

    def send_command(
        self,
        cdb: bytes,
        data_out: Optional[bytes] = None,
        data_in_length: int = 0,
        timeout_ms: int = 5000,
    ) -> TransferResult:
        """
        Execute a SCSI command via CBW/CSW protocol.

        Args:
            cdb: SCSI Command Descriptor Block (up to 16 bytes)
            data_out: Data to send after CBW (for write commands)
            data_in_length: Expected response data length (for read commands)
                Only one of data_out or data_in_length should be set.
            timeout_ms: Timeout in milliseconds

        Returns:
            TransferResult:
                - For read commands: .data contains the response
                - For write commands: .bytes_transferred shows bytes sent
                - .ok is False if CSW indicates failure

        Raises:
            USBTransferError: If CBW/CSW protocol fails after retries
        """
        if data_out and data_in_length:
            raise ValueError("Cannot specify both data_out and data_in_length")

        direction_in = data_in_length > 0
        transfer_length = data_in_length if direction_in else len(data_out or b"")

        for attempt in range(self._max_retries + 1):
            self._tag = next_tag(self._tag)

            # 1. Send CBW
            cbw = build_cbw(
                tag=self._tag,
                transfer_length=transfer_length,
                direction_in=direction_in,
                lun=0,
                cdb=cdb,
            )
            wr = self._bulk.bulk_write(cbw, timeout_ms=timeout_ms)
            if not wr.ok:
                return wr

            # 2. Data phase
            data_result = b""
            if data_out:
                wr = self._bulk.bulk_write(data_out, timeout_ms=timeout_ms)
                if not wr.ok:
                    return wr
            elif data_in_length > 0:
                rd = self._bulk.bulk_read(data_in_length, timeout_ms=timeout_ms)
                if not rd.ok:
                    return rd
                data_result = rd.data

            # 3. Read CSW
            csw_rd = self._bulk.bulk_read(CSW_SIZE, timeout_ms=timeout_ms)
            if not csw_rd.ok:
                return TransferResult(
                    ok=False,
                    error_code=10,
                    error_message=f"Failed to read CSW: {csw_rd.error_message}",
                )

            try:
                csw = parse_csw(csw_rd.data)
            except ValueError as e:
                return TransferResult(
                    ok=False,
                    error_code=11,
                    error_message=f"Invalid CSW: {e}",
                )

            # Verify tag
            if csw.tag != self._tag:
                logger.warning(
                    "CSW tag mismatch: expected %d, got %d", self._tag, csw.tag
                )

            if csw.ok:
                return TransferResult(
                    ok=True,
                    data=data_result,
                    bytes_transferred=transfer_length - csw.data_residue,
                )

            if csw.status == CSW_STATUS_PHASE_ERROR and attempt < self._max_retries:
                logger.warning(
                    "SCSI phase error (attempt %d/%d), retrying...",
                    attempt + 1,
                    self._max_retries + 1,
                )
                continue

            # Command failed
            return TransferResult(
                ok=False,
                data=data_result,
                error_code=12,
                error_message=f"SCSI command failed: CSW status={csw.status_str}",
            )

        return TransferResult(
            ok=False,
            error_code=13,
            error_message=f"SCSI command failed after {self._max_retries + 1} attempts",
        )
