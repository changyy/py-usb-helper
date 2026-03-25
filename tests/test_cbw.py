"""Tests for CBW/CSW protocol — no USB hardware required."""

from usb_helper._cbw import build_cbw, parse_csw, next_tag, CBW_SIZE, CSW_SIZE


def test_build_cbw_size():
    cdb = bytes([0xcb, 0x00, 0x00, 0x00, 0x00, 0x00])
    cbw = build_cbw(tag=1, transfer_length=512, direction_in=True, lun=0, cdb=cdb)
    assert len(cbw) == CBW_SIZE


def test_build_cbw_signature():
    cdb = bytes(6)
    cbw = build_cbw(tag=1, transfer_length=0, direction_in=False, lun=0, cdb=cdb)
    assert cbw[:4] == b"USBC"


def test_build_cbw_direction_flag():
    cdb = bytes(6)
    cbw_in = build_cbw(tag=1, transfer_length=64, direction_in=True, lun=0, cdb=cdb)
    cbw_out = build_cbw(tag=1, transfer_length=64, direction_in=False, lun=0, cdb=cdb)
    # Flags byte is at offset 12
    assert cbw_in[12] == 0x80  # IN
    assert cbw_out[12] == 0x00  # OUT


def test_parse_csw_passed():
    # Build a valid CSW: signature(4) + tag(4) + residue(4) + status(1)
    import struct
    csw_data = struct.pack("<4sIIB", b"USBS", 42, 0, 0)
    csw = parse_csw(csw_data)
    assert csw.ok
    assert csw.tag == 42
    assert csw.data_residue == 0


def test_parse_csw_failed():
    import struct
    csw_data = struct.pack("<4sIIB", b"USBS", 42, 0, 1)
    csw = parse_csw(csw_data)
    assert not csw.ok
    assert csw.status_str == "FAILED"


def test_parse_csw_invalid_signature():
    import struct, pytest
    bad_data = struct.pack("<4sIIB", b"XXXX", 1, 0, 0)
    with pytest.raises(ValueError, match="Invalid CSW signature"):
        parse_csw(bad_data)


def test_next_tag_wraps():
    assert next_tag(0) == 1
    assert next_tag(0xFFFFFFFF) == 0
