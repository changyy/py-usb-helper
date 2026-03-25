"""Tests for usb_helper.types — no USB hardware required."""

from usb_helper.types import DeviceIdentity, DeviceMatchRule, DeviceEvent, DeviceEventType


def test_device_identity_str():
    dev = DeviceIdentity(vid=0x1234, pid=0xABCD, bus=1, address=3, name="TestDev")
    assert dev.vid_pid_str == "1234:abcd"
    assert dev.device_id == "usb:1-3"
    assert "TestDev" in str(dev)


def test_device_match_rule_vid_pid():
    rule = DeviceMatchRule(vid=0x1234, pid=0xABCD)
    dev_match = DeviceIdentity(vid=0x1234, pid=0xABCD, bus=1, address=1)
    dev_no = DeviceIdentity(vid=0x1234, pid=0x0002, bus=1, address=2)
    assert rule.matches(dev_match)
    assert not rule.matches(dev_no)


def test_device_match_rule_name_pattern():
    rule = DeviceMatchRule(name_pattern="Test*")
    dev_match = DeviceIdentity(vid=0x1234, pid=0xABCD, name="TestDev", bus=1, address=1)
    dev_no = DeviceIdentity(vid=0x1234, pid=0xABCD, name="OtherDevice", bus=1, address=2)
    assert rule.matches(dev_match)
    assert not rule.matches(dev_no)


def test_device_match_rule_wildcard():
    rule = DeviceMatchRule()  # All None = match everything
    dev = DeviceIdentity(vid=0xFFFF, pid=0xFFFF, bus=1, address=1)
    assert rule.matches(dev)


def test_device_match_rule_metadata():
    rule = DeviceMatchRule(vid=0x1234, pid=0xABCD, metadata={"mode": "bulk"})
    assert rule.metadata["mode"] == "bulk"


def test_device_event():
    dev = DeviceIdentity(vid=0x1234, pid=0xABCD, bus=1, address=3)
    rule = DeviceMatchRule(vid=0x1234, label="Bulk")
    event = DeviceEvent(
        event_type=DeviceEventType.ATTACHED,
        device=dev,
        matched_rule=rule,
    )
    assert "attached" in str(event)
    assert "Bulk" in str(event)
