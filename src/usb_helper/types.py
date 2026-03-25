"""
Core type definitions for usb-helper.

All types are plain dataclasses with no USB library dependency,
making them safe to import and use without libusb installed.
"""

from __future__ import annotations

import enum
import fnmatch
from dataclasses import dataclass, field
from typing import Any, Optional


class DeviceEventType(enum.Enum):
    """Types of USB device events."""
    ATTACHED = "attached"
    DETACHED = "detached"
    ERROR = "error"


class USBDirection(enum.Enum):
    """USB transfer direction."""
    IN = "in"    # Device to host
    OUT = "out"  # Host to device


class USBError(Exception):
    """Base exception for USB operations."""

    def __init__(self, message: str, error_code: int = 0, detail: Any = None):
        super().__init__(message)
        self.error_code = error_code
        self.detail = detail


class USBTimeoutError(USBError):
    """USB transfer timed out."""
    pass


class USBDeviceNotFoundError(USBError):
    """Requested USB device not found."""
    pass


class USBTransferError(USBError):
    """USB bulk/interrupt transfer failed."""
    pass


class USBPermissionError(USBError):
    """Insufficient permissions to access USB device."""
    pass


@dataclass(frozen=True)
class DeviceIdentity:
    """
    Identifies a connected USB device.

    Attributes:
        vid: Vendor ID (e.g., 0x1234)
        pid: Product ID (e.g., 0xFF88)
        serial: USB serial number string (may be empty)
        name: USB product name string (may be empty)
        location_id: OS-specific location identifier (unique per physical port)
        bus: USB bus number
        address: USB device address on the bus
    """
    vid: int
    pid: int
    serial: str = ""
    name: str = ""
    location_id: str = ""
    bus: int = 0
    address: int = 0

    @property
    def vid_pid_str(self) -> str:
        """Format as 'VVVV:PPPP' hex string."""
        return f"{self.vid:04x}:{self.pid:04x}"

    @property
    def device_id(self) -> str:
        """Unique identifier string like 'usb:1-3' (bus-address)."""
        return f"usb:{self.bus}-{self.address}"

    def __str__(self) -> str:
        parts = [self.device_id, self.vid_pid_str]
        if self.name:
            parts.append(f'"{self.name}"')
        if self.serial:
            parts.append(f"serial={self.serial}")
        return " ".join(parts)


@dataclass
class DeviceMatchRule:
    """
    Rule for matching USB devices.

    All specified fields must match. Fields left as None are wildcards.

    Attributes:
        vid: Vendor ID to match (None = any)
        pid: Product ID to match (None = any)
        name_pattern: Glob pattern for USB product name (None = any)
            Examples: "Test*", "My Device*", "*Gadget*"
        serial_pattern: Glob pattern for serial number (None = any)
        label: Human-readable label for this rule (for logging)
        metadata: Arbitrary data attached to matched devices (e.g., {"mode": "bulk"})
    """
    vid: Optional[int] = None
    pid: Optional[int] = None
    name_pattern: Optional[str] = None
    serial_pattern: Optional[str] = None
    label: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def matches(self, device: DeviceIdentity) -> bool:
        """Check if a device matches this rule."""
        if self.vid is not None and device.vid != self.vid:
            return False
        if self.pid is not None and device.pid != self.pid:
            return False
        if self.name_pattern is not None:
            if not fnmatch.fnmatch(device.name, self.name_pattern):
                return False
        if self.serial_pattern is not None:
            if not fnmatch.fnmatch(device.serial, self.serial_pattern):
                return False
        return True

    def __str__(self) -> str:
        parts = []
        if self.label:
            parts.append(f"[{self.label}]")
        if self.vid is not None:
            parts.append(f"vid={self.vid:04x}")
        if self.pid is not None:
            parts.append(f"pid={self.pid:04x}")
        if self.name_pattern:
            parts.append(f"name={self.name_pattern}")
        return " ".join(parts) or "(match-all)"


@dataclass
class DeviceEvent:
    """
    An event from the USB monitor.

    Attributes:
        event_type: Type of event (attached, detached, error)
        device: The device involved
        matched_rule: Which rule matched this device (if any)
        error_message: Error description (for error events)
    """
    event_type: DeviceEventType
    device: DeviceIdentity
    matched_rule: Optional[DeviceMatchRule] = None
    error_message: str = ""

    def __str__(self) -> str:
        s = f"[{self.event_type.value}] {self.device}"
        if self.matched_rule and self.matched_rule.label:
            s += f" (rule: {self.matched_rule.label})"
        if self.error_message:
            s += f" error: {self.error_message}"
        return s


@dataclass
class TransferResult:
    """
    Result of a USB transfer operation.

    Attributes:
        ok: Whether the transfer succeeded
        data: Data received (for IN transfers)
        bytes_transferred: Number of bytes actually transferred
        error_code: Error code (0 = success)
        error_message: Human-readable error description
    """
    ok: bool
    data: bytes = b""
    bytes_transferred: int = 0
    error_code: int = 0
    error_message: str = ""
