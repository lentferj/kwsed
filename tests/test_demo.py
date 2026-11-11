from kws import records as r
from kwsed.demo import DemoDevice


def test_write_performance_roundtrip(tmp_path):
    dev = DemoDevice()
    data = bytes([7] * r.PERFORMANCE_SIZE)
    result = dev.write_performance("CARD", 3, data, backup_dir=str(tmp_path))
    assert result["verified"] is True
    assert (tmp_path).exists()
    import os

    assert any(f.endswith(".json") for f in os.listdir(tmp_path))
    assert dev.read_performance("CARD", 3) == data


def test_write_patch_splice(tmp_path):
    dev = DemoDevice()
    data = bytes([9] * r.PATCH_SIZE)
    dev.write_patch("CARD", 2, data, backup_dir=str(tmp_path))
    assert dev.read_patch("CARD", 2) == data


def test_send_performance_to_buffer(tmp_path):
    dev = DemoDevice()
    old = dev.read_performance("CARD", 3)
    dev.send_performance_to_buffer("CARD", 3, b"abc" + bytes(r.PERFORMANCE_SIZE - 3))
    assert dev.read_performance("CARD", 3) == old
    assert dev.edit_buffer is not None
    assert dev.buffer_log == [("Card", 3)]


def test_rom_write_raises(tmp_path):
    dev = DemoDevice()
    import pytest

    with pytest.raises(RuntimeError, match="ROM"):
        dev.write_performance(
            "ROM4", 3, bytes(r.PERFORMANCE_SIZE), backup_dir=str(tmp_path)
        )
