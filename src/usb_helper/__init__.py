"""
usb-helper: A Python USB device monitoring and communication framework.

Built on pyusb/libusb for cross-platform USB device enumeration,
hot-plug monitoring, bulk transfers, and SCSI-over-Bulk communication.
"""

from .types import (
    DeviceIdentity,
    DeviceMatchRule,
    DeviceEvent,
    DeviceEventType,
    TransferResult,
    USBDirection,
    USBError,
)
from .monitor import USBMonitor
from .device import USBDevice
from .bulk_device import BulkDevice
from .scsi_device import SCSIDevice
from .config import Profile, load_profile, load_profile_by_name, list_profiles

__version__ = "1.0.2"

def get_meta() -> dict:
    """Return runtime environment metadata (version, platform, pyusb, libusb)."""
    from .cli import _build_meta
    return _build_meta()


__all__ = [
    # Types
    "DeviceIdentity",
    "DeviceMatchRule",
    "DeviceEvent",
    "DeviceEventType",
    "TransferResult",
    "USBDirection",
    "USBError",
    # Core classes
    "USBMonitor",
    "USBDevice",
    "BulkDevice",
    "SCSIDevice",
    # Config
    "Profile",
    "load_profile",
    "load_profile_by_name",
    "list_profiles",
    # Meta
    "get_meta",
]
