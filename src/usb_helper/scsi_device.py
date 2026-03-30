"""
SCSI-over-Bulk USB device implementation.

Wraps BulkDevice to provide SCSI command execution using
the USB Mass Storage Bulk-Only Transport (CBW/CSW) protocol.

On macOS, can optionally use BSD ioctl SCSI pass-through when
the device appears as a mass-storage disk (``/dev/rdiskN``).
This is useful when the kernel's IOUSBMassStorageClass driver has
claimed the USB interface.  Disabled by default; enable via
``darwin_ioctl=True``.
"""

from __future__ import annotations

import logging
import sys
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
from ._cbw import CSW_SIGNATURE
from .types import (
    DeviceIdentity,
    TransferResult,
    USBError,
    USBTransferError,
)

logger = logging.getLogger(__name__)

_IS_DARWIN = sys.platform == "darwin"


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
        darwin_ioctl: On macOS, try BSD ioctl SCSI pass-through via
            ``/dev/rdiskN`` before falling back to libusb.
            Default ``False`` (opt-in).
        darwin_auto_unmount: On macOS, run ``diskutil unmountDisk`` before
            opening ``/dev/rdiskN`` for ioctl (best-effort, default False).
            Only relevant when ``darwin_ioctl=True``.
        reset_on_open: Send a USB port reset before claiming the interface.
            Clears stuck device state from previous sessions.  The device
            re-enumerates after reset, so address may change.  Default ``False``.
    """

    def __init__(
        self,
        identity: DeviceIdentity,
        frame_size: int = 16384,
        interface_number: int = 0,
        max_retries: int = 2,
        darwin_ioctl: bool = False,
        darwin_auto_unmount: bool = False,
        reset_on_open: bool = False,
    ):
        super().__init__(identity)
        self._bulk = BulkDevice(
            identity,
            frame_size=frame_size,
            interface_number=interface_number,
            reset_on_open=reset_on_open,
        )
        self._frame_size = frame_size
        self._max_retries = max_retries
        self._tag: int = 0
        # macOS Darwin SCSI ioctl transport (None when not on macOS or no BSD node)
        self._darwin_transport: Optional[object] = None
        self._darwin_ioctl = bool(darwin_ioctl)
        self._darwin_auto_unmount = bool(darwin_auto_unmount)

    @property
    def frame_size(self) -> int:
        return self._frame_size

    @property
    def bulk_device(self) -> BulkDevice:
        """Access the underlying BulkDevice for raw transfers if needed."""
        return self._bulk

    @property
    def using_darwin_ioctl(self) -> bool:
        """Whether this device is using macOS BSD ioctl instead of libusb."""
        return self._darwin_transport is not None

    def open(self) -> None:
        if self._is_open:
            return

        # On macOS, optionally try BSD ioctl SCSI pass-through.
        # Only attempted when darwin_ioctl=True.  This is useful when
        # the kernel's IOUSBMassStorageClass has claimed the USB
        # interface. For devices that do NOT have a BSD disk node,
        # this will harmlessly fall through to libusb.
        if _IS_DARWIN and self._darwin_ioctl:
            try:
                from ._darwin_scsi import (
                    find_bsd_node,
                    DarwinSCSITransport,
                    unmount_disk_for_bsd_node,
                )

                logger.info(
                    "Darwin: looking up BSD node for %s (VID=%04x PID=%04x serial=%r)",
                    self._identity.device_id,
                    self._identity.vid,
                    self._identity.pid,
                    self._identity.serial,
                )
                bsd_path = find_bsd_node(
                    self._identity.vid,
                    self._identity.pid,
                    serial=self._identity.serial,
                )
                if bsd_path:
                    if self._darwin_auto_unmount:
                        ok = unmount_disk_for_bsd_node(bsd_path)
                        if not ok:
                            logger.info(
                                "Darwin auto-unmount did not succeed for %s; continuing",
                                bsd_path,
                            )

                    transport = DarwinSCSITransport()
                    transport.open(bsd_path)
                    self._darwin_transport = transport
                    self._is_open = True
                    logger.info(
                        "Using Darwin SCSI ioctl for %s via %s",
                        self._identity.device_id,
                        bsd_path,
                    )
                    return
            except Exception as e:
                logger.info(
                    "Darwin SCSI ioctl not available (%s), falling back to libusb",
                    e,
                )

        # Fallback: standard libusb CBW/CSW path
        self._bulk.open()
        # Drain any stale data left in the IN endpoint from a previous
        # session. Vendor-class devices (class 0xFF) may
        # have a leftover CSW or other response queued.  We consume it
        # with a short-timeout read so it doesn't confuse the first
        # real CBW/CSW exchange.
        self._drain_stale_data()
        self._is_open = True
        self._tag = 0

    def _drain_stale_data(self, timeout_ms: int = 100) -> None:
        """Best-effort read to consume any leftover data in the IN endpoint.

        Uses a very short timeout so it returns quickly when the pipe is
        clean.  Any data read (stale CSW, partial response, etc.) is
        logged and discarded.
        """
        for _ in range(3):  # at most 3 stale packets
            rd = self._bulk.bulk_read(512, timeout_ms=timeout_ms)
            if not rd.ok:
                break  # timeout = pipe is clean
            logger.debug(
                "Drained %d stale bytes from IN endpoint: %s",
                len(rd.data),
                rd.data[:32].hex(" "),
            )

    def close(self) -> None:
        if not self._is_open:
            return

        if self._darwin_transport is not None:
            self._darwin_transport.close()
            self._darwin_transport = None
        else:
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
        Execute a SCSI command.

        On macOS with a BSD device node, uses ioctl pass-through.
        Otherwise uses CBW/CSW protocol over USB bulk transfers.

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

        # macOS ioctl path — no CBW/CSW needed, kernel handles framing
        if self._darwin_transport is not None:
            return self._darwin_transport.send_command(
                cdb=cdb,
                data_out=data_out,
                data_in_length=data_in_length,
                timeout_ms=timeout_ms,
            )

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
            logger.debug(
                "CBW[tag=%d]: cdb=%s dir=%s xfer_len=%d (attempt %d/%d)",
                self._tag,
                cdb[:10].hex(" "),
                "IN" if direction_in else "OUT",
                transfer_length,
                attempt + 1,
                self._max_retries + 1,
            )
            wr = self._bulk.bulk_write(cbw, timeout_ms=timeout_ms)
            if not wr.ok:
                logger.debug("CBW write failed: %s", wr.error_message)
                return wr

            # 2. Data phase
            data_result = b""
            embedded_csw: bytes | None = None
            if data_out:
                wr = self._bulk.bulk_write(data_out, timeout_ms=timeout_ms)
                if not wr.ok:
                    return wr
            elif data_in_length > 0:
                rd = self._bulk.bulk_read(data_in_length, timeout_ms=timeout_ms)
                if not rd.ok:
                    return rd

                raw = rd.data

                # Diagnostic hex dump
                logger.debug(
                    "DATA-IN read: requested=%d, received=%d bytes, "
                    "hex(first 64)=%s",
                    data_in_length,
                    len(raw),
                    raw[:64].hex(" "),
                )

                csw_sig = CSW_SIGNATURE  # b"USBS"

                # Case A: Device skipped data phase and returned CSW
                # directly.  This happens on vendor-class devices
                # (0xFF) when the command fails or the device is in
                # an error state — the response starts with "USBS".
                if len(raw) >= CSW_SIZE and raw[:4] == csw_sig:
                    embedded_csw = raw[:CSW_SIZE]
                    data_result = b""
                    logger.debug(
                        "Device returned CSW instead of data "
                        "(skipped data phase): %s",
                        embedded_csw.hex(" "),
                    )
                else:
                    data_result = raw[:data_in_length]

                    # Case B: Data + CSW packed into a single USB
                    # transfer.  Scan for "USBS" anywhere after the
                    # data bytes.
                    search_start = data_in_length
                    tail = raw[search_start:]
                    csw_offset = tail.find(csw_sig)
                    if csw_offset >= 0 and (len(tail) - csw_offset) >= CSW_SIZE:
                        embedded_csw = tail[csw_offset : csw_offset + CSW_SIZE]
                        logger.debug(
                            "Embedded CSW found at offset %d in "
                            "%d-byte response (data_in=%d): %s",
                            search_start + csw_offset,
                            len(raw),
                            data_in_length,
                            embedded_csw.hex(" "),
                        )
                    elif len(raw) > data_in_length:
                        logger.debug(
                            "No embedded CSW found in %d extra "
                            "bytes after %d data bytes: "
                            "tail_hex=%s",
                            len(raw) - data_in_length,
                            data_in_length,
                            tail[:32].hex(" "),
                        )

            # 3. Read CSW (skip if already captured from the data transfer)
            if embedded_csw is not None:
                csw_data = embedded_csw
                logger.debug("Using embedded CSW (skipping separate CSW read)")
            else:
                logger.debug(
                    "Reading separate CSW (%d bytes)…", CSW_SIZE,
                )
                csw_rd = self._bulk.bulk_read(CSW_SIZE, timeout_ms=timeout_ms)
                if not csw_rd.ok:
                    logger.debug(
                        "CSW read failed: %s (data phase returned %d bytes)",
                        csw_rd.error_message,
                        len(raw) if direction_in else 0,
                    )
                    return TransferResult(
                        ok=False,
                        error_code=10,
                        error_message=f"Failed to read CSW: {csw_rd.error_message}",
                    )
                csw_data = csw_rd.data[:CSW_SIZE]
                logger.debug(
                    "CSW read OK: %d bytes, hex=%s",
                    len(csw_rd.data),
                    csw_rd.data[:32].hex(" "),
                )

            try:
                csw = parse_csw(csw_data)
            except ValueError as e:
                return TransferResult(
                    ok=False,
                    error_code=11,
                    error_message=f"Invalid CSW: {e}",
                )

            # Verify tag — some vendor devices always return tag=0
            # regardless of the CBW tag sent.  Log at DEBUG for known
            # quirk (tag=0), WARNING only for unexpected mismatches.
            if csw.tag != self._tag:
                if csw.tag == 0:
                    logger.debug(
                        "CSW tag=0 (device quirk, expected %d)",
                        self._tag,
                    )
                else:
                    logger.warning(
                        "CSW tag mismatch: expected %d, got %d",
                        self._tag, csw.tag,
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
                # Note: do NOT call clear_halt() here. Testing on some
                # vendor-class (0xFF) devices showed that clear_halt
                # after PHASE_ERROR causes the device to stop
                # responding entirely.  A simple immediate retry works
                # better — the stale CSW has already been consumed.
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
