"""
Bulk transfer USB device implementation.

Wraps pyusb to provide bulk IN/OUT transfers with automatic
endpoint discovery and frame-based chunked writes.
"""

from __future__ import annotations

import logging
from typing import Optional

import usb.core
import usb.util

from .device import USBDevice
from .types import (
    DeviceIdentity,
    TransferResult,
    USBDeviceNotFoundError,
    USBError,
    USBPermissionError,
    USBTimeoutError,
    USBTransferError,
)

logger = logging.getLogger(__name__)


class BulkDevice(USBDevice):
    """
    USB device communicating via bulk transfers.

    Automatically discovers bulk IN and OUT endpoints on the first
    interface. Supports chunked writes for large data transfers.

    Args:
        identity: Device identity (vid, pid, bus, address)
        frame_size: Maximum bytes per single bulk write (default 65536)
        interface_number: USB interface to claim (default 0)
    """

    def __init__(
        self,
        identity: DeviceIdentity,
        frame_size: int = 65536,
        interface_number: int = 0,
    ):
        super().__init__(identity)
        self._frame_size = frame_size
        self._interface_number = interface_number
        self._usb_dev: Optional[usb.core.Device] = None
        self._ep_out: Optional[usb.core.Endpoint] = None
        self._ep_in: Optional[usb.core.Endpoint] = None

    @property
    def frame_size(self) -> int:
        """Maximum bytes per single bulk transfer."""
        return self._frame_size

    def open(self) -> None:
        """Open device: find by bus/address, claim interface, discover endpoints."""
        if self._is_open:
            return

        ident = self._identity

        # Find device by bus + address (most precise)
        dev = usb.core.find(
            idVendor=ident.vid,
            idProduct=ident.pid,
            bus=ident.bus or None,
            address=ident.address or None,
        )

        if dev is None:
            raise USBDeviceNotFoundError(
                f"Device not found: {ident.vid_pid_str} "
                f"(bus={ident.bus}, addr={ident.address})"
            )

        self._usb_dev = dev

        # Detach kernel driver if active (Linux)
        try:
            if dev.is_kernel_driver_active(self._interface_number):
                dev.detach_kernel_driver(self._interface_number)
                logger.debug("Detached kernel driver for interface %d", self._interface_number)
        except (usb.core.USBError, NotImplementedError):
            pass  # Not supported on all platforms

        # Set default configuration
        try:
            dev.set_configuration()
        except usb.core.USBError:
            pass  # May already be configured

        # Claim interface
        try:
            usb.util.claim_interface(dev, self._interface_number)
        except usb.core.USBError as e:
            raise USBPermissionError(
                f"Cannot claim interface {self._interface_number}: {e}"
            ) from e

        # Discover endpoints
        cfg = dev.get_active_configuration()
        intf = cfg[(self._interface_number, 0)]

        self._ep_out = usb.util.find_descriptor(
            intf,
            custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
            == usb.util.ENDPOINT_OUT,
        )
        self._ep_in = usb.util.find_descriptor(
            intf,
            custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
            == usb.util.ENDPOINT_IN,
        )

        if self._ep_out is None:
            raise USBError("No bulk OUT endpoint found")
        if self._ep_in is None:
            raise USBError("No bulk IN endpoint found")

        self._is_open = True
        logger.info(
            "Opened %s (EP OUT=0x%02x, EP IN=0x%02x)",
            ident.device_id,
            self._ep_out.bEndpointAddress,
            self._ep_in.bEndpointAddress,
        )

    def close(self) -> None:
        """Release interface and free device."""
        if not self._is_open:
            return

        try:
            if self._usb_dev is not None:
                usb.util.release_interface(self._usb_dev, self._interface_number)
                usb.util.dispose_resources(self._usb_dev)
        except usb.core.USBError as e:
            logger.warning("Error closing device: %s", e)
        finally:
            self._usb_dev = None
            self._ep_out = None
            self._ep_in = None
            self._is_open = False
            logger.debug("Closed %s", self._identity.device_id)

    def bulk_write(
        self,
        data: bytes,
        timeout_ms: int = 5000,
    ) -> TransferResult:
        """
        Write data to bulk OUT endpoint, splitting into frames if needed.

        Args:
            data: Data to write
            timeout_ms: Timeout per frame in milliseconds

        Returns:
            TransferResult with total bytes_transferred
        """
        if not self._is_open or self._ep_out is None:
            return TransferResult(ok=False, error_code=1, error_message="Device not open")

        total_written = 0
        offset = 0

        try:
            while offset < len(data):
                chunk = data[offset : offset + self._frame_size]
                written = self._ep_out.write(chunk, timeout=timeout_ms)
                total_written += written
                offset += len(chunk)

            return TransferResult(ok=True, bytes_transferred=total_written)

        except usb.core.USBTimeoutError as e:
            return TransferResult(
                ok=False,
                bytes_transferred=total_written,
                error_code=2,
                error_message=f"Write timeout at offset {offset}: {e}",
            )
        except usb.core.USBError as e:
            return TransferResult(
                ok=False,
                bytes_transferred=total_written,
                error_code=3,
                error_message=f"Write error at offset {offset}: {e}",
            )

    def bulk_read(
        self,
        size: int,
        timeout_ms: int = 5000,
    ) -> TransferResult:
        """
        Read data from bulk IN endpoint.

        Args:
            size: Maximum bytes to read
            timeout_ms: Timeout in milliseconds

        Returns:
            TransferResult with data
        """
        if not self._is_open or self._ep_in is None:
            return TransferResult(ok=False, error_code=1, error_message="Device not open")

        try:
            raw = self._ep_in.read(size, timeout=timeout_ms)
            data = bytes(raw)
            return TransferResult(ok=True, data=data, bytes_transferred=len(data))

        except usb.core.USBTimeoutError as e:
            return TransferResult(
                ok=False, error_code=2, error_message=f"Read timeout: {e}"
            )
        except usb.core.USBError as e:
            return TransferResult(
                ok=False, error_code=3, error_message=f"Read error: {e}"
            )

    def bulk_write_read(
        self,
        data_out: bytes,
        read_size: int,
        write_timeout_ms: int = 5000,
        read_timeout_ms: int = 5000,
    ) -> TransferResult:
        """
        Convenience: write data then read response.

        Args:
            data_out: Data to send
            read_size: Expected response size
            write_timeout_ms: Write timeout
            read_timeout_ms: Read timeout

        Returns:
            TransferResult from the read operation (includes response data)
        """
        wr = self.bulk_write(data_out, timeout_ms=write_timeout_ms)
        if not wr.ok:
            return wr
        return self.bulk_read(read_size, timeout_ms=read_timeout_ms)
