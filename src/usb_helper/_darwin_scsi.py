"""
macOS (Darwin) SCSI pass-through via BSD ioctl.

When a USB device appears as a mass-storage device on macOS, the kernel's
IOUSBMassStorageClass driver claims the USB interface exclusively.
Userspace libusb can still enumerate the device, but cannot reliably
send vendor-specific SCSI commands because the kernel driver intercepts
them.

The macOS solution is to use BSD ioctl SCSI pass-through:

    open("/dev/rdiskN")  →  ioctl(fd, DKIOCSCSICMD, &dk_scsi_cmd_t)

This module provides:

* ``find_bsd_node(vid, pid)``
    Discover which ``/dev/rdiskN`` corresponds to a given USB VID/PID.

* ``DarwinSCSITransport``
    A thin wrapper that sends SCSI CDBs through the BSD ioctl.
    Drop-in replacement for the libusb CBW/CSW path.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import fcntl
import logging
import os
import plistlib
import subprocess
import sys
import time
from typing import Optional

from .types import DeviceIdentity, TransferResult

logger = logging.getLogger(__name__)

# ── ioctl constant ──────────────────────────────────────────────

# Defined in macOS IOStorageFamily:
#   #define DKIOCSCSICMD  _IOWR('d', 253, dk_scsi_cmd_t)
# The numeric value is stable across macOS versions.
DKIOCSCSICMD = 0xC02864FD

# SCSI ioctl direction constants
_DIR_NONE = 0
_DIR_IN = 1    # device → host  (read)
_DIR_OUT = 2   # host → device  (write)

# TransferResult error codes (Darwin ioctl path)
ERR_IOCTL_FAILED = 15
ERR_PERMISSION_DENIED = 16
ERR_RESOURCE_BUSY = 17


# ── dk_scsi_cmd_t ──────────────────────────────────────────────
# Mirrors the Swift struct definition used by the macOS ioctl path.
# Layout verified against Darwin IOStorageFamily headers.

class DKSCSICmd(ctypes.Structure):
    """
    dk_scsi_cmd_t as used by macOS DKIOCSCSICMD ioctl.

    Field order and padding match the Swift definition in
    USBMassStorageDevice.swift (lines 365-381).
    """

    _fields_ = [
        ("cdbLen", ctypes.c_uint8),
        ("direction", ctypes.c_uint8),
        # 2 bytes implicit padding for uint32 alignment
        ("timeoutValue", ctypes.c_uint32),
        ("dataTransferLength", ctypes.c_uint32),
        # 4 bytes implicit padding for pointer alignment on 64-bit
        ("dataBuffer", ctypes.c_void_p),
        ("senseDataLength", ctypes.c_uint8),
        ("senseData", ctypes.c_uint8 * 32),
        ("cdb", ctypes.c_uint8 * 16),
        ("scsiStatus", ctypes.c_uint8),
    ]


# ── BSD node discovery ─────────────────────────────────────────

def find_bsd_node(
    vid: int,
    pid: int,
    *,
    serial: str = "",
) -> Optional[str]:
    """
    Find the raw BSD device path (``/dev/rdiskN``) for a USB device.

    Tries multiple strategies because macOS device enumeration varies:

    1. ``diskutil list -plist`` — check ALL disks (not just external),
       then match by USB VID/PID via ``diskutil info``.
    2. ``ioreg`` line-based — find IOMedia nodes with BSD Name, then
       trace the ancestor chain for matching USB VID/PID.
    3. ``ioreg`` plist — parse the full IOUSBHostDevice tree recursively.

    Args:
        vid: USB Vendor ID
        pid: USB Product ID
        serial: Optional serial number for disambiguation

    Returns:
        ``"/dev/rdiskN"`` if found, else ``None``.
    """
    if sys.platform != "darwin":
        logger.debug("find_bsd_node: not Darwin, skipping")
        return None

    _log_stderr(
        "find_bsd_node: searching for VID=%04x PID=%04x serial=%r",
        vid, pid, serial,
    )

    # Strategy 1: diskutil (all disks)
    for scope in ("external", "physical"):
        try:
            bsd = _find_bsd_via_diskutil(vid, pid, serial, scope=scope)
            if bsd:
                _log_stderr("find_bsd_node: diskutil(%s) matched → %s", scope, bsd)
                return bsd
        except Exception as e:
            _log_stderr("find_bsd_node: diskutil(%s) failed: %s", scope, e)

    # Strategy 2: ioreg line-based (reverse: from IOMedia up to USB device)
    try:
        bsd = _find_bsd_via_ioreg_line(vid, pid, serial=serial)
        if bsd:
            _log_stderr("find_bsd_node: ioreg-line matched → %s", bsd)
            return bsd
    except Exception as e:
        _log_stderr("find_bsd_node: ioreg-line failed: %s", e)

    # Strategy 3: ioreg plist (forward: from IOUSBHostDevice down to BSD Name)
    try:
        bsd = _find_bsd_via_ioreg_plist(vid, pid, serial=serial)
        if bsd:
            _log_stderr("find_bsd_node: ioreg-plist matched → %s", bsd)
            return bsd
    except Exception as e:
        _log_stderr("find_bsd_node: ioreg-plist failed: %s", e)

    _log_stderr(
        "find_bsd_node: NO BSD node found for VID=%04x PID=%04x — "
        "will fall back to libusb",
        vid, pid,
    )
    return None


def _log_stderr(fmt: str, *args: object) -> None:
    """Log to both Python logger (INFO) and stderr (always visible)."""
    msg = fmt % args if args else fmt
    logger.info(msg)
    import sys as _sys
    print(msg, file=_sys.stderr, flush=True)


def _to_disk_node_path(bsd_path: str) -> str:
    """
    Normalize a BSD path/name to ``/dev/diskN`` form for diskutil commands.

    Examples:
      /dev/rdisk4 -> /dev/disk4
      /dev/disk5  -> /dev/disk5
      rdisk6      -> /dev/disk6
      disk7       -> /dev/disk7
    """
    value = bsd_path.strip()
    if value.startswith("/dev/rdisk"):
        return value.replace("/dev/rdisk", "/dev/disk", 1)
    if value.startswith("/dev/disk"):
        return value
    if value.startswith("rdisk"):
        return f"/dev/disk{value[len('rdisk'):]}"
    if value.startswith("disk"):
        return f"/dev/{value}"
    return value


def unmount_disk_for_bsd_node(
    bsd_path: str,
    *,
    force: bool = False,
    timeout_sec: int = 10,
) -> bool:
    """
    Try to unmount all mounted volumes for a BSD disk before raw SCSI access.

    Returns:
        True when unmount succeeds, False otherwise.
    """
    disk_path = _to_disk_node_path(bsd_path)
    cmd = ["diskutil", "unmountDisk"]
    if force:
        cmd.append("force")
    cmd.append(disk_path)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except Exception as e:
        logger.info("diskutil unmount failed for %s: %s", disk_path, e)
        return False

    if result.returncode == 0:
        logger.info("diskutil unmount succeeded for %s", disk_path)
        return True

    stderr = (result.stderr or "").strip()
    stdout = (result.stdout or "").strip()
    detail = stderr or stdout or f"returncode={result.returncode}"
    logger.info("diskutil unmount failed for %s: %s", disk_path, detail)
    return False


# ── Strategy 1: diskutil ──────────────────────────────────────

def _find_bsd_via_diskutil(
    vid: int, pid: int, serial: str = "", *, scope: str = "external",
) -> Optional[str]:
    """
    Match USB device to BSD node using diskutil.

    Runs ``diskutil list -plist <scope>`` to get disks,
    then checks each with ``diskutil info -plist`` for USB VID/PID.
    """
    result = subprocess.run(
        ["diskutil", "list", "-plist", scope],
        capture_output=True, timeout=10,
    )
    if result.returncode != 0:
        _log_stderr("diskutil list %s: returncode=%d", scope, result.returncode)
        return None

    plist = plistlib.loads(result.stdout)

    # Try both keys that diskutil uses
    disk_names = plist.get("AllDisksAndPartitions", [])
    whole_disks = plist.get("AllDisks", [])
    _log_stderr(
        "diskutil(%s): %d partition entries, AllDisks=%s",
        scope, len(disk_names),
        whole_disks[:10] if whole_disks else [d.get("DeviceIdentifier", "?") for d in disk_names],
    )

    # Collect whole-disk identifiers to check
    disk_ids = set()
    for entry in disk_names:
        did = entry.get("DeviceIdentifier", "")
        if did:
            disk_ids.add(did)
    for did in whole_disks:
        if did and not did.endswith("s") and "s" not in did.split("disk")[-1]:
            disk_ids.add(did)

    for disk_id in sorted(disk_ids):
        info_result = subprocess.run(
            ["diskutil", "info", "-plist", disk_id],
            capture_output=True, timeout=10,
        )
        if info_result.returncode != 0:
            continue

        info = plistlib.loads(info_result.stdout)

        # diskutil stores USB VID/PID under different key names depending on version
        io_vid = info.get("IORegistryEntryUSBVendorID", 0)
        io_pid = info.get("IORegistryEntryUSBProductID", 0)
        protocol = info.get("DeviceProtocol", "")
        _log_stderr(
            "diskutil: %s → VID=%04x PID=%04x protocol=%s (want VID=%04x PID=%04x)",
            disk_id, io_vid, io_pid, protocol, vid, pid,
        )

        if io_vid == vid and io_pid == pid:
            serial_candidates = []
            known_serial_keys = (
                "IORegistryEntrySerialNumber",
                "SerialNumber",
                "USBSerialNumber",
                "USB Serial Number",
                "MediaSerialNumber",
            )
            for key in known_serial_keys:
                value = info.get(key)
                if isinstance(value, str) and value:
                    serial_candidates.append(value)
            for key, value in info.items():
                if "serial" in str(key).lower() and isinstance(value, str) and value:
                    serial_candidates.append(value)

            if serial and not _serial_matches(serial, serial_candidates):
                _log_stderr(
                    "diskutil: %s serial mismatch (want=%r, got=%s)",
                    disk_id, serial, serial_candidates[:4],
                )
                continue

            bsd_name = info.get("DeviceNode", "")
            if bsd_name:
                raw = bsd_name.replace("/dev/disk", "/dev/rdisk")
                return raw

    return None


# ── Strategy 2: ioreg line-based (reverse lookup) ─────────────

def _find_bsd_via_ioreg_line(vid: int, pid: int, *, serial: str = "") -> Optional[str]:
    """
    Find BSD node by parsing ioreg text output.

    Uses ``ioreg -l -r -c IOMedia`` which shows IOMedia nodes and their
    full property sets. For each IOMedia with a ``BSD Name``, we check
    the parent chain (by indentation) for USB VID/PID properties.

    This is more reliable than plist-based search because ``ioreg -a``
    may truncate deep child trees.
    """
    # First get the full tree path from USB to media
    result = subprocess.run(
        ["ioreg", "-l", "-w", "0"],
        capture_output=True, timeout=15,
        text=True,
    )
    if result.returncode != 0:
        return None

    lines = result.stdout.splitlines()
    vid_hex = f"0x{vid:x}"
    pid_hex = f"0x{pid:x}"
    vid_dec = str(vid)
    pid_dec = str(pid)

    # Pass 1: find line indices where our VID/PID appears
    usb_device_ranges: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Look for "idVendor" = VID or "USB Vendor ID" = VID
        if ('"idVendor"' in line or '"USB Vendor ID"' in line):
            if vid_hex in line or vid_dec in line:
                # Check next few lines for matching PID
                for j in range(max(0, i - 3), min(len(lines), i + 10)):
                    pline = lines[j]
                    if ('"idProduct"' in pline or '"USB Product ID"' in pline):
                        if pid_hex in pline or pid_dec in pline:
                            # Find the indentation level of the device entry
                            indent = _ioreg_indent(lines, i)
                            usb_device_ranges.append((i, indent))
                            break
        i += 1

    if not usb_device_ranges:
        _log_stderr("ioreg-line: no USB device found with VID=%04x PID=%04x", vid, pid)
        return None

    _log_stderr("ioreg-line: found %d USB device match(es)", len(usb_device_ranges))

    # Pass 2: for each matched USB device, search forward for serial/BSD Name at deeper indent
    for dev_line_idx, dev_indent in usb_device_ranges:
        if serial and not _ioreg_subtree_has_serial(
            lines,
            start_idx=dev_line_idx,
            root_indent=dev_indent,
            serial=serial,
        ):
            _log_stderr("ioreg-line: serial mismatch near line %d", dev_line_idx)
            continue

        for j in range(dev_line_idx, min(len(lines), dev_line_idx + 500)):
            line = lines[j]
            if '"BSD Name"' in line and "disk" in line:
                # Extract the BSD name value
                bsd = _extract_ioreg_string(line, "BSD Name")
                if bsd and bsd.startswith("disk"):
                    raw = f"/dev/r{bsd}"
                    _log_stderr("ioreg-line: found BSD Name=%s near line %d", bsd, j)
                    return raw
            # Stop if we've gone past this device's subtree
            if j > dev_line_idx + 5:
                cur_indent = len(line) - len(line.lstrip())
                if cur_indent <= dev_indent and line.strip().startswith("+-o"):
                    break

    return None


def _ioreg_indent(lines: list[str], idx: int) -> int:
    """Find the indentation level of the ioreg entry containing line idx."""
    for i in range(idx, -1, -1):
        line = lines[i]
        if "+-o" in line:
            return len(line) - len(line.lstrip())
    return 0


def _extract_ioreg_string(line: str, key: str) -> Optional[str]:
    """Extract string value from an ioreg property line like '  "BSD Name" = "disk4"'."""
    import re
    m = re.search(rf'"{key}"\s*=\s*"([^"]+)"', line)
    return m.group(1) if m else None


def _normalize_serial(value: str) -> str:
    return value.strip().strip("\x00").lower()


def _serial_matches(expected: str, candidates: list[str]) -> bool:
    expected_norm = _normalize_serial(expected)
    if not expected_norm:
        return True
    for candidate in candidates:
        if _normalize_serial(candidate) == expected_norm:
            return True
    return False


def _ioreg_subtree_has_serial(
    lines: list[str],
    *,
    start_idx: int,
    root_indent: int,
    serial: str,
) -> bool:
    expected_norm = _normalize_serial(serial)
    if not expected_norm:
        return True

    for j in range(start_idx, min(len(lines), start_idx + 500)):
        line = lines[j]
        if j > start_idx + 5:
            cur_indent = len(line) - len(line.lstrip())
            if cur_indent <= root_indent and line.strip().startswith("+-o"):
                break

        for key in ("USB Serial Number", "kUSBSerialNumberString", "Serial Number"):
            value = _extract_ioreg_string(line, key)
            if value and _normalize_serial(value) == expected_norm:
                return True

    return False


def _is_permission_error(exc: OSError) -> bool:
    if exc.errno in (errno.EACCES, errno.EPERM):
        return True
    msg = str(exc).lower()
    return ("permission denied" in msg) or ("operation not permitted" in msg)


def _is_resource_busy_error(exc: OSError) -> bool:
    if exc.errno in (errno.EBUSY, errno.EAGAIN):
        return True
    msg = str(exc).lower()
    return ("resource busy" in msg) or ("device busy" in msg) or ("in use" in msg)


# ── Strategy 3: ioreg plist (forward lookup) ──────────────────

def _find_bsd_via_ioreg_plist(vid: int, pid: int, *, serial: str = "") -> Optional[str]:
    """
    Parse ioreg plist output for IOUSBHostDevice nodes.

    Searches IOKit registry for IOUSBHostDevice nodes matching VID/PID,
    then follows child hierarchy to find a BSD Name.
    """
    result = subprocess.run(
        ["ioreg", "-r", "-c", "IOUSBHostDevice", "-a"],
        capture_output=True, timeout=10,
    )
    if result.returncode != 0:
        return None

    try:
        tree = plistlib.loads(result.stdout)
    except Exception:
        return None

    nodes = tree if isinstance(tree, list) else [tree]
    for node in nodes:
        bsd = _search_ioreg_node(node, vid, pid, serial=serial)
        if bsd:
            return bsd

    return None


def _search_ioreg_node(
    node: dict,
    vid: int,
    pid: int,
    *,
    serial: str = "",
) -> Optional[str]:
    """Recursively search ioreg node tree for matching VID/PID + BSD Name."""
    if not isinstance(node, dict):
        return None

    node_vid = node.get("idVendor", node.get("USB Vendor ID", 0))
    node_pid = node.get("idProduct", node.get("USB Product ID", 0))

    if node_vid == vid and node_pid == pid:
        node_serial_candidates = []
        for key in ("USB Serial Number", "kUSBSerialNumberString", "Serial Number"):
            value = node.get(key)
            if isinstance(value, str) and value:
                node_serial_candidates.append(value)

        if serial and not _serial_matches(serial, node_serial_candidates):
            # Same VID/PID but not the expected serial; keep searching siblings.
            pass
        else:
            bsd = _find_bsd_in_children(node)
            if bsd:
                raw = f"/dev/r{bsd}" if not bsd.startswith("/dev/") else bsd.replace("/dev/disk", "/dev/rdisk")
                return raw

    # Recurse into children
    for child in node.get("IORegistryEntryChildren", []):
        bsd = _search_ioreg_node(child, vid, pid, serial=serial)
        if bsd:
            return bsd

    return None


def _find_bsd_in_children(node: dict) -> Optional[str]:
    """Walk children of a matched USB node looking for 'BSD Name'."""
    if not isinstance(node, dict):
        return None

    bsd = node.get("BSD Name")
    if bsd and bsd.startswith("disk"):
        return bsd

    for child in node.get("IORegistryEntryChildren", []):
        result = _find_bsd_in_children(child)
        if result:
            return result

    return None


# ── DarwinSCSITransport ───────────────────────────────────────

class DarwinSCSITransport:
    """
    SCSI command transport using macOS BSD ioctl (DKIOCSCSICMD).

    Equivalent to the Swift version's ``executeSCSIViaIOCTL()`` path in
    USBMassStorageDevice.swift.

    Usage::

        transport = DarwinSCSITransport()
        transport.open("/dev/rdisk4")
        result = transport.send_command(cdb=b"\\xcb\\x00...", data_in_length=2)
        transport.close()
    """

    def __init__(
        self,
        *,
        busy_retries: int = 3,
        busy_backoff_ms: int = 80,
    ) -> None:
        self._fd: int = -1
        self._path: str = ""
        self._busy_retries = max(0, busy_retries)
        self._busy_backoff_ms = max(1, busy_backoff_ms)

    @property
    def is_open(self) -> bool:
        return self._fd >= 0

    def open(self, bsd_path: str) -> None:
        """Open a raw BSD device for SCSI ioctl."""
        if self._fd >= 0:
            return
        last_exc: Optional[OSError] = None
        for attempt in range(self._busy_retries + 1):
            try:
                fd = os.open(bsd_path, os.O_RDWR)
                self._fd = fd
                self._path = bsd_path
                logger.info("Darwin SCSI transport opened: %s (fd=%d)", bsd_path, fd)
                return
            except OSError as e:
                last_exc = e
                if _is_permission_error(e):
                    raise
                if _is_resource_busy_error(e) and attempt < self._busy_retries:
                    delay_s = (self._busy_backoff_ms * (2 ** attempt)) / 1000.0
                    logger.info(
                        "open(%s) busy, retrying in %.3fs (attempt %d/%d)",
                        bsd_path,
                        delay_s,
                        attempt + 1,
                        self._busy_retries,
                    )
                    time.sleep(delay_s)
                    continue
                raise

        if last_exc is not None:
            raise last_exc

    def close(self) -> None:
        """Close the BSD device."""
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError as e:
                logger.warning("Error closing %s: %s", self._path, e)
            finally:
                self._fd = -1
                self._path = ""

    def send_command(
        self,
        cdb: bytes,
        data_out: Optional[bytes] = None,
        data_in_length: int = 0,
        timeout_ms: int = 5000,
    ) -> TransferResult:
        """
        Execute a SCSI command via macOS BSD ioctl.

        Args:
            cdb: SCSI Command Descriptor Block (up to 16 bytes)
            data_out: Data to send to device (write direction)
            data_in_length: Number of bytes to read from device
            timeout_ms: Timeout in milliseconds

        Returns:
            TransferResult with response data or error.
        """
        if self._fd < 0:
            return TransferResult(ok=False, error_code=1, error_message="Not open")

        if data_out and data_in_length:
            raise ValueError("Cannot specify both data_out and data_in_length")

        # Build dk_scsi_cmd_t structure
        cmd = DKSCSICmd()
        cmd.cdbLen = min(len(cdb), 16)
        cmd.timeoutValue = max(1, timeout_ms // 1000)
        cmd.senseDataLength = 32

        # Copy CDB bytes
        for i in range(cmd.cdbLen):
            cmd.cdb[i] = cdb[i]

        try:
            if data_in_length > 0:
                # READ direction: device → host
                cmd.direction = _DIR_IN
                cmd.dataTransferLength = data_in_length
                buf = ctypes.create_string_buffer(data_in_length)
                cmd.dataBuffer = ctypes.cast(buf, ctypes.c_void_p).value

                err = self._ioctl_with_retry(cmd)
                if err is not None:
                    return self._ioctl_error_result(err)

                if cmd.scsiStatus != 0:
                    return TransferResult(
                        ok=False,
                        error_code=14,
                        error_message=f"SCSI status {cmd.scsiStatus} on {self._path}",
                    )

                return TransferResult(
                    ok=True,
                    data=buf.raw[:data_in_length],
                    bytes_transferred=data_in_length,
                )

            elif data_out:
                # WRITE direction: host → device
                cmd.direction = _DIR_OUT
                cmd.dataTransferLength = len(data_out)
                buf = ctypes.create_string_buffer(data_out, len(data_out))
                cmd.dataBuffer = ctypes.cast(buf, ctypes.c_void_p).value

                err = self._ioctl_with_retry(cmd)
                if err is not None:
                    return self._ioctl_error_result(err)

                if cmd.scsiStatus != 0:
                    return TransferResult(
                        ok=False,
                        error_code=14,
                        error_message=f"SCSI status {cmd.scsiStatus} on {self._path}",
                    )

                return TransferResult(
                    ok=True,
                    bytes_transferred=len(data_out),
                )

            else:
                # No data phase
                cmd.direction = _DIR_NONE
                cmd.dataTransferLength = 0
                cmd.dataBuffer = None

                err = self._ioctl_with_retry(cmd)
                if err is not None:
                    return self._ioctl_error_result(err)

                if cmd.scsiStatus != 0:
                    return TransferResult(
                        ok=False,
                        error_code=14,
                        error_message=f"SCSI status {cmd.scsiStatus} on {self._path}",
                    )

                return TransferResult(ok=True, bytes_transferred=0)

        except OSError as e:
            return self._ioctl_error_result(e)

    def _ioctl_with_retry(self, cmd: DKSCSICmd) -> Optional[OSError]:
        """Return None on success, or the final OSError after retries."""
        for attempt in range(self._busy_retries + 1):
            try:
                fcntl.ioctl(self._fd, DKIOCSCSICMD, cmd)
                return None
            except OSError as e:
                if _is_permission_error(e):
                    return e
                if _is_resource_busy_error(e) and attempt < self._busy_retries:
                    delay_s = (self._busy_backoff_ms * (2 ** attempt)) / 1000.0
                    logger.info(
                        "ioctl(%s) busy, retrying in %.3fs (attempt %d/%d)",
                        self._path,
                        delay_s,
                        attempt + 1,
                        self._busy_retries,
                    )
                    time.sleep(delay_s)
                    continue
                return e
        return None

    def _ioctl_error_result(self, err: OSError) -> TransferResult:
        if _is_permission_error(err):
            code = ERR_PERMISSION_DENIED
            kind = "permission denied"
        elif _is_resource_busy_error(err):
            code = ERR_RESOURCE_BUSY
            kind = "resource busy"
        else:
            code = ERR_IOCTL_FAILED
            kind = "ioctl failed"
        return TransferResult(
            ok=False,
            error_code=code,
            error_message=f"{kind} on {self._path}: {err}",
        )
