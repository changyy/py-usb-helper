"""Tests for TOML config loader and profile management."""

import os
import pytest

from usb_helper.config import (
    load_profile,
    load_profile_by_name,
    list_profiles,
    _get_config_dirs,
    _parse_hex_optional,
    _rule_from_dict,
    Profile,
)
from usb_helper.types import DeviceMatchRule, DeviceIdentity


# ── _parse_hex_optional ──

class TestParseHex:
    def test_hex_string(self):
        assert _parse_hex_optional("1234") == 0x1234

    def test_hex_with_prefix(self):
        assert _parse_hex_optional("0xABCD") == 0xABCD

    def test_int_passthrough(self):
        assert _parse_hex_optional(0x1234) == 0x1234

    def test_none(self):
        assert _parse_hex_optional(None) is None


# ── _rule_from_dict ──

class TestRuleFromDict:
    def test_full_rule(self):
        d = {
            "vid": "1234",
            "pid": "abcd",
            "label": "Bulk mode",
            "name": "Test*",
            "serial": "SN*",
            "metadata": {"mode": "bulk", "priority": "high"},
        }
        rule = _rule_from_dict(d)
        assert rule.vid == 0x1234
        assert rule.pid == 0xABCD
        assert rule.label == "Bulk mode"
        assert rule.name_pattern == "Test*"
        assert rule.serial_pattern == "SN*"
        assert rule.metadata == {"mode": "bulk", "priority": "high"}

    def test_minimal_rule(self):
        rule = _rule_from_dict({})
        assert rule.vid is None
        assert rule.pid is None
        assert rule.name_pattern is None
        assert rule.metadata == {}

    def test_vid_only(self):
        rule = _rule_from_dict({"vid": "5678"})
        assert rule.vid == 0x5678
        assert rule.pid is None


# ── load_profile ──

class TestLoadProfile:
    def test_load_valid_profile(self, tmp_path):
        toml_content = '''
description = "Test devices"

[[rules]]
vid = "1234"
pid = "abcd"
label = "Device A"
[rules.metadata]
mode = "bulk"

[[rules]]
vid = "1234"
pid = "0002"
label = "Device B"
[rules.metadata]
mode = "storage"
'''
        profile_file = tmp_path / "test-profile.toml"
        profile_file.write_text(toml_content)

        profile = load_profile(profile_file)
        assert profile.name == "test-profile"
        assert profile.description == "Test devices"
        assert profile.rule_count == 2
        assert profile.rules[0].vid == 0x1234
        assert profile.rules[0].pid == 0xABCD
        assert profile.rules[0].metadata == {"mode": "bulk"}
        assert profile.rules[1].pid == 0x0002

    def test_load_empty_rules(self, tmp_path):
        toml_content = 'description = "Empty"\n'
        profile_file = tmp_path / "empty.toml"
        profile_file.write_text(toml_content)

        profile = load_profile(profile_file)
        assert profile.rule_count == 0

    def test_load_nonexistent(self):
        with pytest.raises(FileNotFoundError):
            load_profile("/nonexistent/profile.toml")

    def test_load_name_pattern(self, tmp_path):
        toml_content = '''
description = "Name match"

[[rules]]
vid = "1234"
name = "Test*"
label = "TestDev by name"
'''
        f = tmp_path / "name-match.toml"
        f.write_text(toml_content)

        profile = load_profile(f)
        assert profile.rules[0].name_pattern == "Test*"
        assert profile.rules[0].pid is None  # Not specified

    def test_rules_match_real_device(self, tmp_path):
        toml_content = '''
description = "Match test"

[[rules]]
vid = "1234"
pid = "abcd"
'''
        f = tmp_path / "match.toml"
        f.write_text(toml_content)

        profile = load_profile(f)
        rule = profile.rules[0]

        # Should match
        dev_yes = DeviceIdentity(vid=0x1234, pid=0xABCD, name="TestDev")
        assert rule.matches(dev_yes)

        # Should not match
        dev_no = DeviceIdentity(vid=0x1234, pid=0x0002, name="TestDev")
        assert not rule.matches(dev_no)


# ── load_profile_by_name ──

class TestLoadProfileByName:
    def test_find_in_cwd(self, tmp_path, monkeypatch):
        # Create usb-helper.d/ in tmp_path
        config_dir = tmp_path / "usb-helper.d"
        config_dir.mkdir()
        toml_content = 'description = "CWD profile"\n\n[[rules]]\nvid = "aaaa"\n'
        (config_dir / "my-devices.toml").write_text(toml_content)

        monkeypatch.chdir(tmp_path)

        profile = load_profile_by_name("my-devices")
        assert profile.name == "my-devices"
        assert profile.description == "CWD profile"

    def test_find_in_user_config(self, tmp_path, monkeypatch):
        # Set XDG_CONFIG_HOME to tmp_path
        config_dir = tmp_path / "usb-helper"
        config_dir.mkdir(parents=True)
        toml_content = 'description = "User profile"\n\n[[rules]]\nvid = "bbbb"\n'
        (config_dir / "user-devices.toml").write_text(toml_content)

        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.chdir("/tmp")  # CWD without usb-helper.d/

        profile = load_profile_by_name("user-devices")
        assert profile.description == "User profile"

    def test_cwd_overrides_user(self, tmp_path, monkeypatch):
        """CWD profile takes priority over user config."""
        # CWD profile
        cwd_dir = tmp_path / "work"
        cwd_dir.mkdir()
        cwd_config = cwd_dir / "usb-helper.d"
        cwd_config.mkdir()
        (cwd_config / "shared.toml").write_text('description = "CWD wins"\n')

        # User profile (same name)
        user_config = tmp_path / "xdg" / "usb-helper"
        user_config.mkdir(parents=True)
        (user_config / "shared.toml").write_text('description = "User loses"\n')

        monkeypatch.chdir(cwd_dir)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

        profile = load_profile_by_name("shared")
        assert profile.description == "CWD wins"

    def test_not_found_raises(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-xdg"))

        with pytest.raises(FileNotFoundError, match="not found"):
            load_profile_by_name("nonexistent-profile")


# ── list_profiles ──

class TestListProfiles:
    def test_list_from_multiple_dirs(self, tmp_path, monkeypatch):
        # CWD profiles
        cwd_config = tmp_path / "work" / "usb-helper.d"
        cwd_config.mkdir(parents=True)
        (cwd_config / "alpha.toml").write_text('description = "Alpha"\n')
        (cwd_config / "beta.toml").write_text('description = "Beta"\n')

        # User profile
        user_config = tmp_path / "xdg" / "usb-helper"
        user_config.mkdir(parents=True)
        (user_config / "gamma.toml").write_text('description = "Gamma"\n')

        monkeypatch.chdir(tmp_path / "work")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

        profiles = list_profiles()
        names = {p.name for p in profiles}
        assert "alpha" in names
        assert "beta" in names
        assert "gamma" in names

    def test_empty_when_no_dirs(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))

        profiles = list_profiles()
        assert profiles == []


# ── Profile ──

class TestProfile:
    def test_str_representation(self):
        p = Profile(
            name="test",
            description="Test profile",
            rules=[DeviceMatchRule(vid=0x1234)],
            source_path="/path/to/test.toml",
        )
        s = str(p)
        assert "test" in s
        assert "Test profile" in s
        assert "1 rules" in s


# ── sample.toml example validation ──

class TestSampleExample:
    def test_load_sample_example(self):
        """Verify the bundled sample.toml example is valid."""
        import pathlib
        example = pathlib.Path(__file__).parent.parent / "examples" / "sample.toml"
        if not example.exists():
            pytest.skip("sample.toml example not found")

        profile = load_profile(example)
        assert profile.name == "sample"
        assert profile.rule_count == 6
        assert profile.description == "Sample USB devices"

        # Verify all rules have metadata.mode
        for rule in profile.rules:
            assert "mode" in rule.metadata
            assert rule.metadata["mode"] in ("bulk", "storage")

        # Check specific VID combos
        vids = {r.vid for r in profile.rules}
        assert 0x1234 in vids
        assert 0x5678 in vids
