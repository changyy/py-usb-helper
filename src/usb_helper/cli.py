"""
usb-helper CLI — USB device diagnostic & monitoring tool.

Usage:
  usb-helper                                — List connected USB devices
  usb-helper --listen                       — Monitor plug/unplug (JSONL output)
  usb-helper --vid 1234                     — Filter by vendor ID
  usb-helper --vid 1234 --vid 5678          — Multiple vendor IDs
  usb-helper --profile sample               — Use a named device profile
  usb-helper --profile sample --listen      — Monitor with profile
  usb-helper --config ./my-devices.toml     — Use a specific config file
  usb-helper profiles                       — List available profiles
  usb-helper --check                        — Verify libusb backend
  usb-helper --json                         — JSON output for list mode
  usb-helper --version                      — Show version info

Profile search directories (first match wins):
  1. ./usb-helper.d/          (current working directory)
  2. ~/.config/usb-helper/    (user config)
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import __version__
from .types import DeviceIdentity, DeviceMatchRule, DeviceEvent, DeviceEventType


# ──────────────────────────────────────────────
# Meta / version helpers
# ──────────────────────────────────────────────

def _build_meta() -> dict:
    """
    Build a meta dict describing the runtime environment.

    Returned keys:
      usb_helper  — usb-helper package version
      python      — Python version string
      platform    — e.g. "macOS-15.3-arm64" / "Linux-6.5.0-x86_64"
      os          — e.g. "Darwin", "Linux", "Windows"
      arch        — e.g. "arm64", "x86_64"
      pyusb       — pyusb version or null
      libusb      — libusb backend module name or null
    """
    meta: dict = {
        "usb_helper": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "os": platform.system(),
        "arch": platform.machine(),
        "pyusb": None,
        "libusb": None,
    }

    # pyusb version
    try:
        import usb
        meta["pyusb"] = getattr(usb, "__version__", "unknown")
    except ImportError:
        pass

    # libusb backend info
    try:
        import usb.backend.libusb1
        backend = usb.backend.libusb1.get_backend()
        if backend is None:
            import usb.backend.libusb0
            backend = usb.backend.libusb0.get_backend()
        if backend is not None:
            meta["libusb"] = type(backend).__module__
    except Exception:
        pass

    return meta


# ──────────────────────────────────────────────
# JSONL output helpers
# ──────────────────────────────────────────────

def _emit_json(
    status: bool,
    action: str,
    data: list[dict] | dict | None = None,
    error: int = 0,
    error_message: str = "",
):
    """Emit a single JSONL record to stdout, always including meta."""
    record: dict = {
        "status": status,
        "action": action,
        "meta": _build_meta(),
    }
    if error != 0:
        record["error"] = error
    if error_message:
        record["errorMessage"] = error_message
    if data is not None:
        record["data"] = data
    print(json.dumps(record, ensure_ascii=False), flush=True)


def _identity_to_dict(identity: DeviceIdentity, rule: Optional[DeviceMatchRule] = None) -> dict:
    """Convert DeviceIdentity to a serializable dict."""
    d = {
        "device_id": identity.device_id,
        "vid": f"{identity.vid:04x}",
        "pid": f"{identity.pid:04x}",
        "name": identity.name,
        "serial": identity.serial,
        "bus": identity.bus,
        "address": identity.address,
    }
    if rule and rule.label:
        d["label"] = rule.label
    if rule and rule.metadata:
        d["metadata"] = rule.metadata
    return d


def _parse_hex(value: str) -> int:
    """Parse a hex string (with or without 0x prefix)."""
    value = value.strip().lower()
    if value.startswith("0x"):
        return int(value, 16)
    return int(value, 16)


# ──────────────────────────────────────────────
# Environment check — libusb availability
# ──────────────────────────────────────────────

def _check_environment(json_mode: bool = False) -> bool:
    """
    Verify pyusb and libusb backend are available.

    Returns True if OK. On failure:
      - json_mode=True: emits JSONL error and returns False
      - json_mode=False: prints error to stderr and returns False
    """
    # 1. pyusb
    try:
        import usb.core  # noqa: F401
    except ImportError:
        msg = (
            "pyusb is not installed. "
            "Install with: pip install pyusb"
        )
        if json_mode:
            _emit_json(False, "error", error=-1, error_message=msg)
        else:
            print(f"Error: {msg}", file=sys.stderr)
        return False

    # 2. libusb backend
    try:
        import usb.backend.libusb1
        backend = usb.backend.libusb1.get_backend()
        if backend is None:
            import usb.backend.libusb0
            backend = usb.backend.libusb0.get_backend()

        if backend is None:
            msg = (
                "No libusb backend found. Install libusb: "
                "macOS: brew install libusb | "
                "Linux: sudo apt install libusb-1.0-0-dev | "
                "Windows: use Zadig (https://zadig.akeo.ie/)"
            )
            if json_mode:
                _emit_json(False, "error", error=-1, error_message=msg)
            else:
                print(f"Error: {msg}", file=sys.stderr)
            return False
    except Exception as e:
        msg = f"libusb backend error: {e}"
        if json_mode:
            _emit_json(False, "error", error=-1, error_message=msg)
        else:
            print(f"Error: {msg}", file=sys.stderr)
        return False

    return True


# ──────────────────────────────────────────────
# Rule building — merge CLI args + profile
# ──────────────────────────────────────────────

def _build_rules_from_args(
    vids: list[str] | None,
    pids: list[str] | None,
    names: list[str] | None,
) -> list[DeviceMatchRule]:
    """
    Build match rules from CLI --vid/--pid/--name arguments.

    Supports multiple values:
      --vid 1234 --vid 5678           → 2 rules (one per VID)
      --vid 1234 --pid abcd           → 1 rule (VID+PID)
      --vid 1234 --vid 5678 --pid abcd  → 2 rules (each VID + same PID)
    """
    vids = vids or []
    pids = pids or []
    names = names or []

    # No filters at all → empty (match all)
    if not vids and not pids and not names:
        return []

    parsed_vids = [_parse_hex(v) for v in vids] if vids else [None]
    parsed_pids = [_parse_hex(p) for p in pids] if pids else [None]
    name_pats = names if names else [None]

    # Cross-product: each VID × each PID × each name
    rules: list[DeviceMatchRule] = []
    for vid in parsed_vids:
        for pid in parsed_pids:
            for name in name_pats:
                rules.append(DeviceMatchRule(vid=vid, pid=pid, name_pattern=name))

    return rules


def _resolve_rules(args: argparse.Namespace) -> list[DeviceMatchRule]:
    """
    Resolve final match rules from all sources (priority order):
      1. --config <file>   (explicit TOML file)
      2. --profile <name>  (named profile from config dirs)
      3. --vid/--pid/--name (CLI flags)

    --config and --profile provide base rules.
    --vid/--pid/--name, if given alongside, ADD to the profile rules.
    """
    from .config import load_profile, load_profile_by_name

    profile_rules: list[DeviceMatchRule] = []
    cli_rules = _build_rules_from_args(args.vid, args.pid, args.name)

    # Load profile if specified
    if args.config:
        try:
            profile = load_profile(args.config)
            profile_rules = profile.rules
        except FileNotFoundError as e:
            _emit_json(False, "error", error=-2, error_message=str(e))
            sys.exit(1)
        except ValueError as e:
            _emit_json(False, "error", error=-2, error_message=str(e))
            sys.exit(1)
    elif args.profile:
        try:
            profile = load_profile_by_name(args.profile)
            profile_rules = profile.rules
        except FileNotFoundError as e:
            _emit_json(False, "error", error=-2, error_message=str(e))
            sys.exit(1)

    # Merge: profile rules + CLI rules
    all_rules = profile_rules + cli_rules
    return all_rules


# ──────────────────────────────────────────────
# profiles — list available profiles
# ──────────────────────────────────────────────

def do_profiles(args: argparse.Namespace) -> int:
    """List all available profiles."""
    from .config import list_profiles, get_config_dirs

    config_dirs = get_config_dirs()

    if args.json:
        profiles = list_profiles()
        data = {
            "config_dirs": [str(d) for d in config_dirs],
            "profiles": [
                {
                    "name": p.name,
                    "description": p.description,
                    "rules": p.rule_count,
                    "source": p.source_path,
                }
                for p in profiles
            ],
        }
        _emit_json(True, "profiles", data)
    else:
        print("Config directories (search order):")
        if config_dirs:
            for d in config_dirs:
                print(f"  {d}")
        else:
            print("  (none found)")
            print()
            print("Create a config directory:")
            print("  mkdir -p ~/.config/usb-helper")
            print("  # or in current directory:")
            print("  mkdir usb-helper.d")

        profiles = list_profiles()
        print()
        if profiles:
            print(f"Available profiles ({len(profiles)}):\n")
            for p in profiles:
                print(f"  {p.name}")
                if p.description:
                    print(f"    {p.description}")
                print(f"    {p.rule_count} rule(s) — {p.source_path}")
                print()
        else:
            print("No profiles found.")
            print()
            print("Create a profile, e.g. ~/.config/usb-helper/my-devices.toml:")
            print()
            print('  description = "My USB devices"')
            print()
            print("  [[rules]]")
            print('  vid = "1234"')
            print('  pid = "abcd"')
            print('  label = "My device"')

    return 0


# ──────────────────────────────────────────────
# --check: verify libusb backend (detailed)
# ──────────────────────────────────────────────

def do_check(args: argparse.Namespace) -> int:
    """Verify that libusb backend is available (detailed human output)."""
    meta = _build_meta()
    print("usb-helper check")
    print(f"  version:  {meta['usb_helper']}")
    print(f"  python:   {meta['python']}")
    print(f"  platform: {meta['platform']}")
    print()

    # 1. pyusb
    print("[1/4] Checking pyusb import...", end=" ")
    try:
        import usb.core
        import usb.util
        print(f"OK (pyusb {getattr(usb, '__version__', 'unknown')})")
    except ImportError as e:
        print("FAIL")
        print(f"  Error: {e}")
        print("  Fix: pip install pyusb")
        return 1

    # 2. libusb backend
    print("[2/4] Checking libusb backend...", end=" ")
    try:
        import usb.backend.libusb1
        backend = usb.backend.libusb1.get_backend()
        if backend is None:
            import usb.backend.libusb0
            backend = usb.backend.libusb0.get_backend()

        if backend is not None:
            print(f"OK ({type(backend).__module__})")
        else:
            print("FAIL")
            print("  No libusb backend found.")
            print("  Fix:")
            print("    macOS:   brew install libusb")
            print("    Linux:   sudo apt install libusb-1.0-0-dev")
            print("    Windows: Install WinUSB driver via Zadig (https://zadig.akeo.ie/)")
            return 1
    except Exception as e:
        print(f"FAIL ({e})")
        return 1

    # 3. Bus scan
    print("[3/4] Scanning USB bus...", end=" ")
    try:
        devices = list(usb.core.find(find_all=True))
        print(f"OK ({len(devices)} device(s) on bus)")
    except usb.core.NoBackendError:
        print("FAIL (no backend)")
        return 1
    except Exception as e:
        print(f"WARN ({e})")

    # 4. Config directories
    print("[4/4] Checking config directories...", end=" ")
    from .config import get_config_dirs, list_profiles
    dirs = get_config_dirs()
    profiles = list_profiles()
    if dirs:
        print(f"OK ({len(dirs)} dir(s), {len(profiles)} profile(s))")
    else:
        print("NONE (optional — create ~/.config/usb-helper/)")

    print()
    print("All checks passed. usb-helper is ready to use.")
    return 0


# ──────────────────────────────────────────────
# Default: list devices
# ──────────────────────────────────────────────

def do_list(rules: list[DeviceMatchRule], as_json: bool) -> int:
    """Scan and list connected USB devices."""
    if not _check_environment(json_mode=as_json):
        return 1

    from .monitor import USBMonitor

    monitor = USBMonitor(match_rules=rules)
    try:
        found = monitor.scan_once()
    except Exception as e:
        if as_json:
            _emit_json(False, "scan", error=-1, error_message=str(e))
        else:
            print(f"Error: {e}", file=sys.stderr)
        return 1

    if as_json:
        devices = [_identity_to_dict(identity, rule) for identity, rule in found]
        _emit_json(True, "scan", devices)
    else:
        if not found:
            print("No USB devices found.")
            return 0

        print(f"Found {len(found)} device(s):\n")
        for identity, rule in found:
            line = f"  {identity.device_id}  {identity.vid:04x}:{identity.pid:04x}"
            if identity.name:
                line += f'  "{identity.name}"'
            if identity.serial:
                line += f"  serial={identity.serial}"
            if rule and rule.label:
                line += f"  [{rule.label}]"
            if rule and rule.metadata:
                meta_str = " ".join(f"{k}={v}" for k, v in rule.metadata.items())
                line += f"  ({meta_str})"
            print(line)

    return 0


# ──────────────────────────────────────────────
# --listen: monitor plug/unplug events (JSONL)
# ──────────────────────────────────────────────

def do_listen(rules: list[DeviceMatchRule], interval_ms: int) -> int:
    """Monitor USB device attach/detach events, output JSONL."""
    if not _check_environment(json_mode=True):
        return 1

    from .monitor import USBMonitor

    monitor = USBMonitor(match_rules=rules, poll_interval_ms=interval_ms)

    # Emit initial device list
    try:
        found = monitor.scan_once()
        devices = [_identity_to_dict(identity, rule) for identity, rule in found]
        _emit_json(True, "init", devices)
    except Exception as e:
        _emit_json(False, "init", error=-1, error_message=str(e))
        return 1

    def on_event(event: DeviceEvent):
        device_dict = _identity_to_dict(event.device, event.matched_rule)
        if event.event_type == DeviceEventType.ATTACHED:
            _emit_json(True, "plug", [device_dict])
        elif event.event_type == DeviceEventType.DETACHED:
            _emit_json(True, "unplug", [device_dict])
        elif event.event_type == DeviceEventType.ERROR:
            _emit_json(False, "error", [device_dict], error_message=event.error_message)

    monitor.on_device_event = on_event

    try:
        monitor.run_forever()
    except KeyboardInterrupt:
        _emit_json(True, "stop", [])

    return 0


# ──────────────────────────────────────────────
# Argument parser
# ──────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="usb-helper",
        description="USB diagnostic tool — list devices, monitor plug/unplug, manage profiles.",
        epilog=(
            "Examples:\n"
            "  usb-helper                              List all USB devices\n"
            "  usb-helper --json                       List as JSON\n"
            "  usb-helper --vid 1234                   Filter by vendor ID\n"
            "  usb-helper --vid 1234 --vid 5678        Multiple vendor IDs\n"
            "  usb-helper --profile sample             Use device profile\n"
            "  usb-helper --listen                     Monitor plug/unplug (JSONL)\n"
            "  usb-helper --listen --profile sample    Monitor with profile\n"
            "  usb-helper --config ./my.toml           Use specific config file\n"
            "  usb-helper profiles                     List available profiles\n"
            "  usb-helper --check                      Verify libusb setup\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Subcommands
    subparsers = parser.add_subparsers(dest="subcommand")
    sub_profiles = subparsers.add_parser(
        "profiles", help="List available device profiles",
    )
    sub_profiles.add_argument("--json", action="store_true", help="Output as JSON")

    # Main flags
    parser.add_argument("--listen", action="store_true",
                        help="Monitor USB plug/unplug events (JSONL output)")
    parser.add_argument("--profile", "-p", type=str, default=None,
                        help="Use a named device profile (e.g. 'sample')")
    parser.add_argument("--config", "-c", type=str, default=None,
                        help="Path to a TOML config file")
    parser.add_argument("--vid", action="append", default=None,
                        help="Filter by vendor ID (hex). Can be specified multiple times")
    parser.add_argument("--pid", action="append", default=None,
                        help="Filter by product ID (hex). Can be specified multiple times")
    parser.add_argument("--name", action="append", default=None,
                        help="Filter by product name (glob). Can be specified multiple times")
    parser.add_argument("--interval", type=int, default=500,
                        help="Poll interval in ms for --listen mode (default: 500)")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")
    parser.add_argument("--check", action="store_true",
                        help="Verify libusb backend is working")
    parser.add_argument("--version", action="store_true",
                        help="Show version info")

    return parser


def main():
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args()

    # Subcommand: profiles
    if args.subcommand == "profiles":
        sys.exit(do_profiles(args))

    # --version
    if args.version:
        meta = _build_meta()
        if getattr(args, "json", False):
            _emit_json(True, "version", meta)
        else:
            print(f"usb-helper {meta['usb_helper']}")
            print(f"  python:   {meta['python']}")
            print(f"  platform: {meta['platform']}")
            print(f"  os:       {meta['os']}")
            print(f"  arch:     {meta['arch']}")
            print(f"  pyusb:    {meta['pyusb'] or 'not installed'}")
            print(f"  libusb:   {meta['libusb'] or 'not found'}")
        sys.exit(0)

    # --check
    if args.check:
        sys.exit(do_check(args))

    # Resolve match rules (profile + CLI flags)
    rules = _resolve_rules(args)

    # --listen mode
    if args.listen:
        sys.exit(do_listen(rules, args.interval))

    # Default: list devices
    sys.exit(do_list(rules, as_json=args.json))


if __name__ == "__main__":
    main()
