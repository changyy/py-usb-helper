"""
USB Mass Storage Command Block Wrapper (CBW) / Command Status Wrapper (CSW) protocol.

Implements the low-level binary protocol for SCSI-over-Bulk USB communication.
Reference: USB Mass Storage Class - Bulk-Only Transport (BBB) specification.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

# Protocol constants
CBW_SIGNATURE = b"USBC"  # 0x55534243
CSW_SIGNATURE = b"USBS"  # 0x55534253
CBW_SIZE = 31
CSW_SIZE = 13
MAX_CDB_LENGTH = 16

# CSW status codes
CSW_STATUS_PASSED = 0x00
CSW_STATUS_FAILED = 0x01
CSW_STATUS_PHASE_ERROR = 0x02


@dataclass
class CSWResult:
    """Parsed Command Status Wrapper."""
    tag: int
    data_residue: int
    status: int

    @property
    def ok(self) -> bool:
        return self.status == CSW_STATUS_PASSED

    @property
    def status_str(self) -> str:
        return {
            CSW_STATUS_PASSED: "PASSED",
            CSW_STATUS_FAILED: "FAILED",
            CSW_STATUS_PHASE_ERROR: "PHASE_ERROR",
        }.get(self.status, f"UNKNOWN({self.status})")


def build_cbw(
    tag: int,
    transfer_length: int,
    direction_in: bool,
    lun: int,
    cdb: bytes,
) -> bytes:
    """
    Build a 31-byte Command Block Wrapper.

    Args:
        tag: Transaction tag (should match CSW response)
        transfer_length: Expected data transfer length in bytes
        direction_in: True = device-to-host (IN), False = host-to-device (OUT)
        lun: Logical Unit Number (usually 0)
        cdb: SCSI Command Descriptor Block (up to 16 bytes)

    Returns:
        31-byte CBW as bytes
    """
    if len(cdb) > MAX_CDB_LENGTH:
        raise ValueError(f"CDB length {len(cdb)} exceeds maximum {MAX_CDB_LENGTH}")

    flags = 0x80 if direction_in else 0x00
    cdb_length = len(cdb)

    # Pad CDB to 16 bytes
    cdb_padded = cdb.ljust(MAX_CDB_LENGTH, b"\x00")

    # CBW format: signature(4) + tag(4) + transfer_length(4) + flags(1) + lun(1) + cdb_length(1) + cdb(16)
    cbw = struct.pack(
        "<4sIIBBB",
        CBW_SIGNATURE,
        tag,
        transfer_length,
        flags,
        lun,
        cdb_length,
    ) + cdb_padded

    assert len(cbw) == CBW_SIZE, f"CBW size mismatch: {len(cbw)} != {CBW_SIZE}"
    return cbw


def parse_csw(data: bytes) -> CSWResult:
    """
    Parse a 13-byte Command Status Wrapper.

    Args:
        data: Raw 13-byte CSW data

    Returns:
        Parsed CSWResult

    Raises:
        ValueError: If data is invalid or signature doesn't match
    """
    if len(data) < CSW_SIZE:
        raise ValueError(f"CSW too short: {len(data)} bytes (expected {CSW_SIZE})")

    signature, tag, data_residue, status = struct.unpack("<4sIIB", data[:CSW_SIZE])

    if signature != CSW_SIGNATURE:
        raise ValueError(
            f"Invalid CSW signature: {signature.hex()} (expected {CSW_SIGNATURE.hex()})"
        )

    return CSWResult(tag=tag, data_residue=data_residue, status=status)


def next_tag(current: int = 0) -> int:
    """Generate next CBW/CSW tag, wrapping at 32-bit boundary."""
    return (current + 1) & 0xFFFFFFFF
