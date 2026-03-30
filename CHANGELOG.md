# Changelog

All notable changes to this project will be documented in this file.

## [1.0.4] - 2026-03-27

### Added

- **USB port reset on open**: New `reset_on_open=True` parameter for `BulkDevice` / `SCSIDevice`. Sends a USB port reset before claiming the interface, clearing any stuck device state from previous sessions. After reset the device re-enumerates and is re-found automatically. Off by default.

### Fixed

- **CSW tag=0 quirk handling**: Some vendor devices always return CSW tag=0 regardless of the CBW tag sent. Tag mismatch for tag=0 is now logged at DEBUG (not WARNING) since it's a known device quirk, not a protocol error.
- **Diagnostic logging cleaned up**: All per-CBW/CSW exchange logging moved from INFO to DEBUG to reduce noise during extended retry loops. Key lifecycle events (device open, ready, reset) remain at INFO.

## [1.0.3] - 2026-03-27

### Fixed

- **CSW-instead-of-data detection**: When a vendor-class USB device (interface class 0xFF) skips the data phase and returns a CSW directly (starting with "USBS" signature), `send_command()` now correctly identifies this as a CSW rather than treating the first bytes as data. This addresses a timeout case where the code consumed the CSW during the data read, then waited for a separate CSW that never came.
- **Full-buffer CSW scan**: Embedded CSW detection now uses `bytes.find()` to search the entire response buffer for the "USBS" signature, not just the bytes immediately after `data_in_length`. Handles cases where CSW is at a non-obvious offset within a USB packet.

### Changed

- **Darwin ioctl is now opt-in**: `SCSIDevice(..., darwin_ioctl=True)` must be explicitly set to enable BSD ioctl SCSI pass-through via `/dev/rdiskN`. Default is `False`. Devices without a BSD disk node will fall through to libusb. Existing callers that don't pass `darwin_ioctl` are unaffected (they already used libusb).
- **Diagnostic logging**: `send_command()` now logs CBW details, data-phase hex dumps (first 64 bytes), and CSW resolution (embedded vs separate) at INFO level to aid USB protocol debugging.

### Tests

- Added `TestSCSIDeviceCSWInsteadOfData` with 3 test cases:
  - CSW with PHASE_ERROR instead of data → triggers retry, second attempt succeeds
  - CSW with FAILED status instead of data → returns error immediately
  - CSW with PASSED status but no data → returns empty data
- Added `test_open_skips_darwin_by_default` verifying darwin_ioctl=False is the default
- Updated all Darwin integration tests to explicitly pass `darwin_ioctl=True`
- Total: 136 tests passing

## [1.0.2] - 2026-03-26

### Added

- **macOS auto-unmount option for raw SCSI workflows**: `SCSIDevice(..., darwin_auto_unmount=True)` now performs a best-effort `diskutil unmountDisk` before opening `/dev/rdiskN`, reducing OS contention during raw device operations.

### Improved

- **Darwin busy handling**: added exponential backoff retries for `open()` and `ioctl()` on resource-busy errors in BSD SCSI transport.
- **Error classification**: Darwin ioctl failures now distinguish permission denied vs resource busy vs generic ioctl failure using dedicated `TransferResult.error_code` values.
- **Bulk claim diagnostics**: `BulkDevice.open()` now classifies claim-interface errors more clearly (permission vs busy vs generic), improving upstream recovery logic.

### Tests

- Added coverage for:
  - Darwin busy-retry success path and error-code mapping.
  - Darwin disk unmount helper behavior.
  - `SCSIDevice` auto-unmount integration path.
  - `BulkDevice` busy claim-interface path.

## [1.0.1] - 2026-03-26

### Fixed

- **macOS BSD node matching**: `find_bsd_node(..., serial=...)` now actually applies serial filtering across all Darwin lookup strategies (`diskutil`, `ioreg` line parser, `ioreg` plist parser), avoiding wrong `/dev/rdiskN` selection when multiple devices share the same VID/PID.
- **Darwin SCSI reliability**: improved matching behavior for multi-device setups by normalizing and validating serial values before binding to BSD raw disk nodes.

### Tests

- Added Darwin serial-filter test coverage for:
  - `ioreg` line-based lookup with two same-VID/PID devices.
  - `diskutil` plist lookup with serial disambiguation.
  - `ioreg` plist lookup with serial disambiguation.

## [1.0.0] - 2025-03-25

First release.

### Features

- **Core types**: `DeviceIdentity`, `DeviceMatchRule`, `DeviceEvent`, `TransferResult` with glob-based matching for name and serial patterns.
- **BulkDevice**: pyusb wrapper with automatic endpoint detection, frame-based bulk writes, and configurable frame sizes.
- **SCSIDevice**: SCSI-over-Bulk communication with CBW/CSW protocol (Command Block Wrapper → data transfer → Command Status Wrapper).
- **USBMonitor**: Polling-based USB device attach/detach detection with configurable interval and match rules.
- **TOML profile system**: Named device profiles with multi-directory search (`./usb-helper.d/` → `~/.config/usb-helper/`).
- **CLI tool** (`usb-helper`):
  - Default mode: list connected USB devices (human-readable and `--json`).
  - `--listen`: continuous JSONL monitoring for plug/unplug events.
  - `--vid`, `--pid`, `--name`: device filtering with cross-product rule building.
  - `--profile`: load named TOML profile.
  - `--config`: load TOML profile by file path.
  - `--check`: verify libusb/pyusb environment.
  - `--version`: display version and runtime environment meta.
  - `--interval`: custom poll interval for listen mode.
  - `profiles` subcommand: list available profiles from all config directories.
  - Structured JSONL error output when libusb or pyusb is missing.
  - Every JSONL message includes `meta` field (usb-helper version, Python version, platform, OS, arch, pyusb version, libusb backend).
- **Runtime meta API**: `get_meta()` returns environment metadata dict.
- **Example profile**: `examples/sample.toml`.
- **Test suite**: 102 mock-based tests — no USB hardware required.
