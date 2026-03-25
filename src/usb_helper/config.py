"""
TOML-based profile configuration for usb-helper.

Profile search order:
  1. --config <path>        (explicit file)
  2. --profile <name>       (search in config dirs)

Config directory search order:
  1. ./usb-helper.d/        (current working directory)
  2. ~/.config/usb-helper/  (user home)

Profile file format (TOML):

    # sample.toml
    description = "Sample USB devices"

    [[rules]]
    vid = "1234"
    pid = "abcd"
    label = "Bulk mode"
    name = "Test*"
    serial = "*"
    [rules.metadata]
    mode = "bulk"

    [[rules]]
    vid = "1234"
    pid = "0002"
    label = "Storage mode"
    [rules.metadata]
    mode = "storage"

All fields in [[rules]] are optional:
  - vid:      hex string, e.g. "1234"
  - pid:      hex string, e.g. "abcd"
  - label:    human-readable name for this rule
  - name:     glob pattern matching USB product name
  - serial:   glob pattern matching USB serial number
  - metadata: arbitrary key-value pairs passed through to DeviceMatchRule
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .types import DeviceMatchRule


# ──────────────────────────────────────────────
# TOML loader (Python 3.11+ built-in, fallback to tomli)
# ──────────────────────────────────────────────

def _load_toml(path: Path) -> dict:
    """Load a TOML file, using tomllib (3.11+) or tomli as fallback."""
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            raise ImportError(
                "TOML support requires Python 3.11+ or the 'tomli' package. "
                "Install with: pip install tomli"
            )

    with open(path, "rb") as f:
        return tomllib.load(f)


# ──────────────────────────────────────────────
# Profile dataclass
# ──────────────────────────────────────────────

@dataclass
class Profile:
    """A loaded device profile with match rules."""
    name: str
    description: str
    rules: list[DeviceMatchRule]
    source_path: str  # where this profile was loaded from

    @property
    def rule_count(self) -> int:
        return len(self.rules)

    def __str__(self) -> str:
        return f"{self.name}: {self.description} ({self.rule_count} rules) [{self.source_path}]"


# ──────────────────────────────────────────────
# Config directory resolution
# ──────────────────────────────────────────────

def _get_config_dirs() -> list[Path]:
    """
    Return config directories in search priority order.

    1. ./usb-helper.d/         (current working directory)
    2. ~/.config/usb-helper/   (user home, XDG-style)
    """
    dirs: list[Path] = []

    # CWD
    cwd_dir = Path.cwd() / "usb-helper.d"
    if cwd_dir.is_dir():
        dirs.append(cwd_dir)

    # User config (XDG_CONFIG_HOME or ~/.config)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        user_dir = Path(xdg) / "usb-helper"
    else:
        user_dir = Path.home() / ".config" / "usb-helper"
    if user_dir.is_dir():
        dirs.append(user_dir)

    return dirs


def _find_all_profiles() -> list[Path]:
    """Find all .toml profile files across all config directories."""
    profiles: list[Path] = []
    seen_names: set[str] = set()

    for config_dir in _get_config_dirs():
        for toml_file in sorted(config_dir.glob("*.toml")):
            # First match wins (CWD overrides user config)
            if toml_file.stem not in seen_names:
                profiles.append(toml_file)
                seen_names.add(toml_file.stem)

    return profiles


def _find_profile_file(name: str) -> Optional[Path]:
    """Find a profile file by name (without .toml extension)."""
    for config_dir in _get_config_dirs():
        candidate = config_dir / f"{name}.toml"
        if candidate.is_file():
            return candidate
    return None


# ──────────────────────────────────────────────
# Profile parsing
# ──────────────────────────────────────────────

def _parse_hex_optional(value: Any) -> Optional[int]:
    """Parse a hex string to int, or return None."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    s = str(value).strip().lower()
    if s.startswith("0x"):
        return int(s, 16)
    return int(s, 16)


def _rule_from_dict(d: dict) -> DeviceMatchRule:
    """Parse a single [[rules]] entry from TOML."""
    return DeviceMatchRule(
        vid=_parse_hex_optional(d.get("vid")),
        pid=_parse_hex_optional(d.get("pid")),
        name_pattern=d.get("name"),
        serial_pattern=d.get("serial"),
        label=d.get("label", ""),
        metadata=dict(d.get("metadata", {})),
    )


def load_profile(path: str | Path) -> Profile:
    """
    Load a profile from a TOML file.

    Args:
        path: Path to the .toml file

    Returns:
        Parsed Profile

    Raises:
        FileNotFoundError: File doesn't exist
        ValueError: Invalid TOML or missing required fields
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Profile not found: {path}")

    try:
        data = _load_toml(path)
    except Exception as e:
        raise ValueError(f"Failed to parse {path}: {e}")

    description = data.get("description", "")
    raw_rules = data.get("rules", [])

    if not isinstance(raw_rules, list):
        raise ValueError(f"Invalid 'rules' in {path}: expected a list of [[rules]]")

    rules = [_rule_from_dict(r) for r in raw_rules]

    return Profile(
        name=path.stem,
        description=description,
        rules=rules,
        source_path=str(path),
    )


def load_profile_by_name(name: str) -> Profile:
    """
    Find and load a profile by name.

    Searches config directories in priority order.

    Raises:
        FileNotFoundError: No profile with that name
    """
    path = _find_profile_file(name)
    if path is None:
        dirs = _get_config_dirs()
        search_desc = ", ".join(str(d) for d in dirs) if dirs else "(no config dirs found)"
        raise FileNotFoundError(
            f"Profile '{name}' not found. Searched: {search_desc}"
        )
    return load_profile(path)


def list_profiles() -> list[Profile]:
    """
    List all available profiles from all config directories.

    Returns:
        List of loaded Profile objects (CWD profiles override same-named user ones)
    """
    profiles: list[Profile] = []
    for path in _find_all_profiles():
        try:
            profiles.append(load_profile(path))
        except (ValueError, Exception):
            # Skip broken profiles in listing mode
            profiles.append(Profile(
                name=path.stem,
                description=f"(error loading: {path})",
                rules=[],
                source_path=str(path),
            ))
    return profiles


def get_config_dirs() -> list[Path]:
    """Public accessor for config directory list."""
    return _get_config_dirs()


def ensure_user_config_dir() -> Path:
    """Create and return the user config directory."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        user_dir = Path(xdg) / "usb-helper"
    else:
        user_dir = Path.home() / ".config" / "usb-helper"
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir
