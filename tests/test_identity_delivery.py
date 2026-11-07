"""Delivery of identity.jwt: the directory is opened once and read by descriptor."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from clagentic_loadout.identity import IdentityError, delivery
from tests._support.identity_support import make_delivery

DIR_VAR = "CLAGENTIC_IDENTITY_DIR"


def test_swapping_the_directory_after_the_check_never_reads_the_swapped_in_token(
    tmp_path: Path, monkeypatch
) -> None:
    original = make_delivery(tmp_path, "original-token", name="delivery")
    attacker = make_delivery(tmp_path, "swapped-in-token", name="attacker")
    parked = str(tmp_path / "parked")
    real_fstat = os.fstat
    swapped = []

    def swapping_fstat(fd):
        result = real_fstat(fd)
        if not swapped:
            # Runs between the directory check and the file open.
            swapped.append(True)
            os.rename(original, parked)
            os.rename(attacker, original)
        return result

    monkeypatch.setattr(delivery.os, "fstat", swapping_fstat)
    try:
        token, _ = delivery.read_delivery({DIR_VAR: original})
    except IdentityError:
        return  # refusing is an acceptable outcome
    assert swapped
    assert token == "original-token"


def test_a_symlinked_directory_is_refused(tmp_path: Path) -> None:
    real = make_delivery(tmp_path, "tok", name="real")
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(IdentityError) as caught:
        delivery.read_delivery({DIR_VAR: str(link)})
    assert "symlink" in str(caught.value)
