# Changelog

All notable changes to this project will be documented in this file.

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
