"""
Abstract base class for USB device communication.

Concrete implementations: BulkDevice, SCSIDevice.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

from .types import DeviceIdentity, TransferResult, USBError

logger = logging.getLogger(__name__)


class USBDevice(ABC):
    """
    Abstract USB device providing bulk read/write operations.

    Usage as context manager:
        with BulkDevice(device_identity) as dev:
            dev.bulk_write(data)
            response = dev.bulk_read(64)
    """

    def __init__(self, identity: DeviceIdentity):
        self._identity = identity
        self._is_open = False

    @property
    def identity(self) -> DeviceIdentity:
        """The device identity this handle refers to."""
        return self._identity

    @property
    def is_open(self) -> bool:
        """Whether the device handle is currently open."""
        return self._is_open

    @abstractmethod
    def open(self) -> None:
        """
        Open the device for communication.

        Finds the device via pyusb, claims the interface,
        and discovers bulk IN/OUT endpoints.

        Raises:
            USBDeviceNotFoundError: Device no longer connected
            USBPermissionError: Insufficient permissions
            USBError: Other USB errors
        """
        ...

    @abstractmethod
    def close(self) -> None:
        """
        Release the device.

        Releases the USB interface and frees resources.
        Safe to call multiple times.
        """
        ...

    @abstractmethod
    def bulk_write(
        self,
        data: bytes,
        timeout_ms: int = 5000,
    ) -> TransferResult:
        """
        Write data to the device's bulk OUT endpoint.

        Args:
            data: Data to send
            timeout_ms: Timeout in milliseconds

        Returns:
            TransferResult with bytes_transferred
        """
        ...

    @abstractmethod
    def bulk_read(
        self,
        size: int,
        timeout_ms: int = 5000,
    ) -> TransferResult:
        """
        Read data from the device's bulk IN endpoint.

        Args:
            size: Maximum number of bytes to read
            timeout_ms: Timeout in milliseconds

        Returns:
            TransferResult with data
        """
        ...

    def get_info(self) -> DeviceIdentity:
        """Return the device identity."""
        return self._identity

    # Context manager support

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def __repr__(self) -> str:
        status = "open" if self._is_open else "closed"
        return f"<{self.__class__.__name__} {self._identity.device_id} [{status}]>"
