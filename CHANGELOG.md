# Changelog

All notable changes to this project will be documented in this file.

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
