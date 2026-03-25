"""
Shared test fixtures for py-usb-helper.

Provides mock USB devices and endpoints so all tests
run without real USB hardware.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from usb_helper.types import DeviceIdentity, DeviceMatchRule
from usb_helper._cbw import CSW_SIGNATURE, CSW_STATUS_PASSED, CSW_STATUS_FAILED, CSW_STATUS_PHASE_ERROR


# ──────────────────────────────────────────────
# Common device identities for tests
# ──────────────────────────────────────────────

@pytest.fixture
def bulk_identity() -> DeviceIdentity:
    """Bulk mode device identity."""
    return DeviceIdentity(
        vid=0x1234, pid=0xABCD,
        serial="TEST001", name="TestDev Bulk",
        location_id="1-3", bus=1, address=3,
    )


@pytest.fixture
def scsi_identity() -> DeviceIdentity:
    """Storage mode device identity."""
    return DeviceIdentity(
        vid=0x1234, pid=0x0002,
        serial="TEST002", name="TestDev Storage",
        location_id="1-5", bus=1, address=5,
    )


@pytest.fixture
def bulk_match_rule() -> DeviceMatchRule:
    return DeviceMatchRule(
        vid=0x1234, pid=0xABCD,
        label="Bulk", metadata={"mode": "bulk"},
    )


@pytest.fixture
def scsi_match_rule() -> DeviceMatchRule:
    return DeviceMatchRule(
        vid=0x1234, pid=0x0002,
        label="Storage", metadata={"mode": "storage"},
    )


# ──────────────────────────────────────────────
# Mock pyusb objects
# ──────────────────────────────────────────────

def make_mock_usb_device(
    vid: int = 0x1234,
    pid: int = 0xABCD,
    bus: int = 1,
    address: int = 3,
    product: str = "TestDev",
    serial: str = "TEST001",
    ep_out_addr: int = 0x01,
    ep_in_addr: int = 0x81,
) -> MagicMock:
    """
    Create a mock usb.core.Device that behaves like a real device
    for open/claim/endpoint discovery.
    """
    dev = MagicMock()
    dev.idVendor = vid
    dev.idProduct = pid
    dev.bus = bus
    dev.address = address
    dev.iProduct = 1
    dev.iSerialNumber = 2

    # Kernel driver
    dev.is_kernel_driver_active.return_value = False

    # Endpoints
    ep_out = MagicMock()
    ep_out.bEndpointAddress = ep_out_addr
    ep_out.write.side_effect = lambda data, timeout=None: len(data)

    ep_in = MagicMock()
    ep_in.bEndpointAddress = ep_in_addr

    # Configuration → interface → endpoints
    intf = MagicMock()
    intf.__iter__ = MagicMock(return_value=iter([ep_out, ep_in]))
    intf.__getitem__ = MagicMock(return_value=intf)

    cfg = MagicMock()
    cfg.__getitem__ = MagicMock(return_value=intf)

    dev.get_active_configuration.return_value = cfg

    # Store refs for test access
    dev._test_ep_out = ep_out
    dev._test_ep_in = ep_in
    dev._test_product = product
    dev._test_serial = serial

    return dev


def build_csw_bytes(tag: int, residue: int = 0, status: int = CSW_STATUS_PASSED) -> bytes:
    """Build a 13-byte CSW response for mock bulk reads."""
    return struct.pack("<4sIIB", CSW_SIGNATURE, tag, residue, status)
