"""End-to-end photo measurement tests against real ImageMagick (skipped if absent)."""

import json
import shutil

import pytest

from tools.ghost_photo import main, reference_error


@pytest.mark.skipif(not shutil.which("convert") and not shutil.which("magick"),
                    reason="ImageMagick not installed")
def test_real_reference_rmse_and_noise_floor(tmp_path, capsys):
    reference = tmp_path/"clean.pgm"
    same = tmp_path/"repeat.pgm"
    ghost = tmp_path/"ghost.pgm"
    reference.write_bytes(b"P5\n8 8\n255\n" + bytes([255])*64)
    same.write_bytes(reference.read_bytes())
    ghost.write_bytes(b"P5\n8 8\n255\n" + bytes([0])*32 + bytes([255])*32)
    assert reference_error(str(reference), str(same), (0,0,8,8)) == 0
    assert reference_error(str(ghost), str(reference), (0,0,8,8)) == pytest.approx(2**-.5, abs=1e-5)
    assert main(["--crop", "8x8+0+0", "--reference", str(reference),
                 "--noise-reference", str(same), "--json", str(ghost)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["corrected_rmse"] == data["reference_rmse"]


def test_noise_reference_requires_clean_reference(capsys):
    assert main(["--crop", "8x8+0+0", "--noise-reference", "x", "y"]) == 1
    assert "requires" in capsys.readouterr().err
