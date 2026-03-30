"""
Bulk transfer USB device implementation.

Wraps pyusb to provide bulk IN/OUT transfers with automatic
endpoint discovery and frame-based chunked writes.
"""

from __future__ import annotations

import errno
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


def _is_permission_usb_error(exc: usb.core.USBError) -> bool:
    msg = str(exc).lower()
    errno_value = getattr(exc, "errno", None)
    if errno_value in (errno.EACCES, errno.EPERM):
        return True
    return ("access denied" in msg) or ("permission denied" in msg) or ("not permitted" in msg)


def _is_busy_usb_error(exc: usb.core.USBError) -> bool:
    msg = str(exc).lower()
    errno_value = getattr(exc, "errno", None)
    if errno_value in (errno.EBUSY, errno.EAGAIN):
        return True
    return ("resource busy" in msg) or ("device busy" in msg) or ("in use" in msg)


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
        reset_on_open: bool = False,
    ):
        super().__init__(identity)
        self._frame_size = frame_size
        self._interface_number = interface_number
        self._reset_on_open = reset_on_open
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

        # Find device by bus + address (most precise).
        # IMPORTANT: only include bus/address in kwargs when they have
        # real values.  pyusb treats `bus=None` as a filter that requires
        # `device.bus == None`, which never matches — so passing None
        # causes find() to return nothing even though the device exists.
        find_kwargs: dict = dict(idVendor=ident.vid, idProduct=ident.pid)
        if ident.bus:
            find_kwargs["bus"] = ident.bus
        if ident.address:
            find_kwargs["address"] = ident.address
        dev = usb.core.find(**find_kwargs)

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

        # USB port reset — clears any stuck device state from a previous
        # session.  Especially important on macOS with vendor-class (0xFF)
        # devices that don't get properly reset by the OS between sessions.
        # After reset, the device re-enumerates so we must re-find it.
        if self._reset_on_open:
            dev = self._try_usb_reset(dev)
            self._usb_dev = dev

        # Set default configuration
        try:
            dev.set_configuration()
        except usb.core.USBError:
            pass  # May already be configured

        # Claim interface
        try:
            usb.util.claim_interface(dev, self._interface_number)
        except usb.core.USBError as e:
            if _is_permission_usb_error(e):
                raise USBPermissionError(
                    f"Cannot claim interface {self._interface_number}: {e}"
                ) from e
            if _is_busy_usb_error(e):
                raise USBTransferError(
                    f"Interface {self._interface_number} is busy (likely claimed by OS/other process): {e}"
                ) from e
            raise USBError(
                f"Cannot claim interface {self._interface_number}: {e}"
            ) from e

        # Reset endpoint data toggles by sending SET_INTERFACE.
        # IOKit's USBInterfaceOpen() does this automatically, but
        # libusb's claim_interface does NOT.  Without this, vendor-class
        # devices (class 0xFF) may have out-of-sync data toggles from a
        # previous session, causing the device to silently drop packets.
        try:
            dev.set_interface_altsetting(
                interface=self._interface_number,
                alternate_setting=0,
            )
            logger.debug(
                "SET_INTERFACE(%d, alt=0) OK — endpoint toggles reset",
                self._interface_number,
            )
        except usb.core.USBError as e:
            # Not fatal: some devices don't support SET_INTERFACE
            logger.debug("SET_INTERFACE failed (non-fatal): %s", e)

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

    def _try_usb_reset(self, dev: usb.core.Device) -> usb.core.Device:
        """Send USB port reset and re-find the device.

        After a port reset the device re-enumerates and may get a new
        bus address, so we re-find it by VID/PID (and optionally bus).
        Returns the (possibly new) device object.
        """
        import time

        ident = self._identity
        try:
            dev.reset()
            logger.info("USB device reset OK, waiting for re-enumeration…")
            time.sleep(2)
        except usb.core.USBError as e:
            logger.debug("USB device reset failed (non-fatal): %s", e)
            return dev

        # Re-find device after reset (address may have changed)
        new_dev = usb.core.find(
            idVendor=ident.vid,
            idProduct=ident.pid,
        )
        if new_dev is None:
            logger.warning(
                "Device not found after USB reset, using original handle"
            )
            return dev

        # Detach kernel driver again after reset
        try:
            if new_dev.is_kernel_driver_active(self._interface_number):
                new_dev.detach_kernel_driver(self._interface_number)
        except (usb.core.USBError, NotImplementedError):
            pass

        logger.info(
            "Re-found device after reset at bus=%s addr=%s",
            new_dev.bus, new_dev.address,
        )
        return new_dev

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

        On macOS (IOKit backend), libusb raises EOVERFLOW when the device
        sends a full ``wMaxPacketSize`` packet but the requested buffer is
        smaller.  To prevent this we always allocate at least
        ``wMaxPacketSize`` bytes and trim the result.

        Args:
            size: Maximum bytes to read
            timeout_ms: Timeout in milliseconds

        Returns:
            TransferResult with data
        """
        if not self._is_open or self._ep_in is None:
            return TransferResult(ok=False, error_code=1, error_message="Device not open")

        try:
            # Allocate at least one max-packet to avoid EOVERFLOW on macOS
            # when the device returns a full packet for a small request.
            max_pkt = getattr(self._ep_in, "wMaxPacketSize", 512) or 512
            read_size = max(size, max_pkt)
            raw = self._ep_in.read(read_size, timeout=timeout_ms)
            # Return ALL received bytes — callers (e.g. send_command) may
            # need the extra data to detect an embedded CSW.
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

    def clear_halt(self) -> None:
        """Clear HALT / STALL on both bulk endpoints.

        Call this after a USB protocol error (e.g. PHASE_ERROR) to
        reset the endpoint toggle state before retrying.  Best-effort:
        errors are logged but do not raise.
        """
        if self._usb_dev is None:
            return
        for ep in (self._ep_out, self._ep_in):
            if ep is not None:
                try:
                    self._usb_dev.clear_halt(ep)
                except usb.core.USBError as e:
                    logger.debug("clear_halt(0x%02x): %s", ep.bEndpointAddress, e)

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
