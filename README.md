# usb-helper

[![PyPI](https://img.shields.io/pypi/v/usb-helper.svg)](https://pypi.org/project/usb-helper/)
[![PyPI Downloads](https://static.pepy.tech/badge/usb-helper)](https://pepy.tech/projects/usb-helper)

A cross-platform Python framework for USB device monitoring, bulk transfer, and SCSI-over-Bulk communication. Built on pyusb/libusb.

Includes a CLI tool for quick device diagnostics, plug/unplug monitoring (JSONL output), and TOML-based device profile management.

## Installation

```
% pip install usb-helper
```

### Platform prerequisites

| Platform | Requirement |
|----------|-------------|
| macOS | `brew install libusb` |
| Linux | `sudo apt install libusb-1.0-0-dev` |
| Windows | Install WinUSB driver via [Zadig](https://zadig.akeo.ie/) |

Verify your setup:

```
% usb-helper --check
```

---

## CLI Usage

### List connected USB devices

```
% usb-helper

Found 3 device(s):

  usb:1-3  1234:abcd  "TestDev"  [Bulk mode]  (mode=bulk)
  usb:1-5  05ac:8106
  usb:2-1  1d6b:0002
```

```
% usb-helper --json
{"status": true, "action": "scan", "meta": {...}, "data": [{"device_id": "usb:1-3", "vid": "1234", "pid": "abcd", ...}]}
```

### Filter devices

```
% usb-helper --vid 1234                     # single vendor ID
% usb-helper --vid 1234 --vid 5678          # multiple vendor IDs
% usb-helper --vid 1234 --pid abcd          # vendor + product ID
% usb-helper --name "Test*"                 # product name glob pattern
```

Multiple `--vid`, `--pid`, and `--name` values are cross-producted into match rules. For example, `--vid 1234 --vid 5678 --pid abcd` creates two rules: `1234:abcd` and `5678:abcd`.

### Monitor plug/unplug events

```
% usb-helper --listen
```

Outputs JSONL (one JSON object per line) — designed for piping into AI agents or automation scripts:

```json
{"status": true, "action": "init", "meta": {...}, "data": [{"device_id": "usb:1-3", "vid": "1234", "pid": "abcd", "name": "TestDev", ...}]}
{"status": true, "action": "plug", "meta": {...}, "data": [{"device_id": "usb:2-5", "vid": "1234", "pid": "abcd", ...}]}
{"status": true, "action": "unplug", "meta": {...}, "data": [{"device_id": "usb:2-5", "vid": "1234", "pid": "abcd", ...}]}
{"status": true, "action": "stop", "meta": {...}, "data": []}
```

Events: `init` (startup device list), `plug`, `unplug`, `error`, `stop` (Ctrl+C).

Combine with filters:

```
% usb-helper --listen --vid 1234 --interval 1000
% usb-helper --listen --profile sample
```

### Error output

When libusb or pyusb is missing, all modes (including `--listen`) emit a structured error:

```json
{"status": false, "action": "error", "meta": {...}, "error": -1, "errorMessage": "No libusb backend found. Install libusb: macOS: brew install libusb | Linux: ..."}
```

---

## Device Profiles

Profiles are TOML files that define named sets of device match rules. Instead of typing `--vid` and `--pid` every time, save your device definitions once and reference them by name.

### Profile search directories (first match wins)

1. `./usb-helper.d/` — current working directory (project-level)
2. `~/.config/usb-helper/` — user config (shared across projects)

### Create a profile

```
% mkdir -p ~/.config/usb-helper
% cat > ~/.config/usb-helper/sample.toml << 'EOF'
description = "My USB devices"

[[rules]]
vid = "1234"
pid = "abcd"
label = "Bulk mode"
[rules.metadata]
mode = "bulk"

[[rules]]
vid = "1234"
pid = "0002"
label = "Storage mode"
[rules.metadata]
mode = "storage"
EOF
```

Each `[[rules]]` entry supports these optional fields:

| Field | Description | Example |
|-------|-------------|---------|
| `vid` | Vendor ID (hex string) | `"1234"` |
| `pid` | Product ID (hex string) | `"abcd"` |
| `label` | Human-readable rule name | `"Bulk mode"` |
| `name` | Product name glob pattern | `"Test*"` |
| `serial` | Serial number glob pattern | `"SN-*"` |
| `metadata` | Arbitrary key-value pairs | `{mode = "bulk"}` |

### Use a profile

```
% usb-helper --profile sample
% usb-helper --profile sample --listen
% usb-helper --profile sample --json
```

Or specify a TOML file directly:

```
% usb-helper --config ./my-devices.toml
```

Profile rules and CLI flags (`--vid`, `--pid`, `--name`) are merged — you can add extra filters on top of a profile.

### List available profiles

```
% usb-helper profiles

Config directories (search order):
  ./usb-helper.d
  /Users/you/.config/usb-helper

Available profiles (1):

  sample
    My USB devices
    6 rule(s) — /Users/you/.config/usb-helper/sample.toml
```

```
% usb-helper profiles --json
{"status": true, "action": "profiles", "meta": {...}, "data": {"config_dirs": [...], "profiles": [{"name": "sample", ...}]}}
```

### Project-level profiles

Place TOML files in `usb-helper.d/` in your project root. These take priority over user-level profiles with the same name:

```
my-project/
  usb-helper.d/
    my-devices.toml      ← project-specific config
  src/
    main.py
```

---

## Python API

### Device monitoring

```python
from usb_helper import USBMonitor, DeviceMatchRule

rules = [
    DeviceMatchRule(vid=0x1234, pid=0xABCD, label="Bulk mode"),
    DeviceMatchRule(vid=0x1234, pid=0x0002, label="Storage mode"),
]

monitor = USBMonitor(match_rules=rules, poll_interval_ms=500)

# One-time scan
for identity, rule in monitor.scan_once():
    print(f"Found: {identity} (rule: {rule.label})")

# Continuous monitoring
monitor.on_device_event = lambda event: print(f"[{event.event_type.value}] {event.device}")
monitor.run_forever()  # Ctrl+C to stop
```

### Bulk transfer

```python
from usb_helper import BulkDevice, DeviceIdentity

identity = DeviceIdentity(vid=0x1234, pid=0xABCD, bus=1, address=3)

with BulkDevice(identity, frame_size=65536) as device:
    # Write (auto-splits into frames)
    result = device.bulk_write(data)

    # Read
    result = device.bulk_read(512, timeout_ms=5000)
    if result.ok:
        print(f"Received {len(result.data)} bytes")
```

### SCSI-over-Bulk

```python
from usb_helper import SCSIDevice, DeviceIdentity

identity = DeviceIdentity(vid=0x1234, pid=0x0002, bus=1, address=5)

with SCSIDevice(identity) as device:
    # Send SCSI command (CBW → data → CSW)
    cdb = bytes([0xCB, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
    result = device.send_command(cdb=cdb, data_in_length=2, timeout_ms=5000)
    if result.ok:
        print(f"Response: {result.data.hex()}")
```

### Load profiles programmatically

```python
from usb_helper import load_profile, load_profile_by_name, list_profiles

# By name (searches config dirs)
profile = load_profile_by_name("sample")

# By path
profile = load_profile("./my-devices.toml")

# List all
for p in list_profiles():
    print(f"{p.name}: {p.description} ({p.rule_count} rules)")

# Use profile rules with USBMonitor
monitor = USBMonitor(match_rules=profile.rules)
```

### Runtime metadata

```python
from usb_helper import get_meta

meta = get_meta()
# {"usb_helper": "0.1.0", "python": "3.12.3", "platform": "...", "os": "Darwin", "arch": "arm64", "pyusb": "1.2.1", "libusb": "usb.backend.libusb1"}
```

---

## Running Tests

Tests are fully mock-based — no real USB hardware needed.

```
% git clone https://github.com/changyy/py-usb-helper.git
% cd py-usb-helper
% pip install -e ".[dev]"
% python -m pytest tests/ -v
```

Current test suite: **102 tests** covering types, CBW/CSW protocol, BulkDevice, SCSIDevice, USBMonitor, config loader, CLI, and meta.

To run with coverage:

```
% python -m pytest tests/ --cov=usb_helper --cov-report=term-missing
```

---

## Project Structure

```
py-usb-helper/
  src/usb_helper/
    __init__.py         Package exports
    types.py            DeviceIdentity, DeviceMatchRule, DeviceEvent, TransferResult
    _cbw.py             CBW/CSW binary protocol (SCSI command blocks)
    device.py           USBDevice abstract base class
    bulk_device.py      BulkDevice — pyusb wrapper with frame-based writes
    scsi_device.py      SCSIDevice — SCSI-over-Bulk with CBW→data→CSW
    monitor.py          USBMonitor — polling-based attach/detach detection
    config.py           TOML profile loader
    cli.py              CLI entry point (usb-helper command)
  tests/
    test_types.py       DeviceIdentity, DeviceMatchRule matching
    test_cbw.py         CBW build/CSW parse, tag wrapping
    test_bulk_device.py BulkDevice open/close/read/write (22 tests)
    test_scsi_device.py SCSIDevice command/error/retry (8 tests)
    test_monitor.py     USBMonitor scan/attach/detach (11 tests)
    test_config.py      TOML loader, profile search, sample validation (18 tests)
    test_cli.py         CLI argument parsing, rule building, meta (23 tests)
  examples/
    sample.toml         Sample device profile example
  pyproject.toml
  README.md
```

---

## Requirements

- Python 3.10+
- libusb (platform-specific, see installation)
- pyusb >= 1.2.1

---

## License

MIT © [Yuan-Yi Chang](https://github.com/changyy)
