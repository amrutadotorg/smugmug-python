"""Unit tests for pure client helpers (no API access)."""

import pytest

from smugmug.client import _sanitize_path_segment, _split_album_path


@pytest.mark.parametrize(
    "full_path,expected_parent,expected_album",
    [
        (
            "2007/2007-05-04 Evening Program, Cabella Ligure (Italy) #80376",
            "2007",
            "2007-05-04 Evening Program, Cabella Ligure (Italy) #80376",
        ),
        ("Italy/2007", "Italy", "2007"),
        ("Ian Paradine/2007", "Ian Paradine", "2007"),
        ("Evening Program/2007", "Evening Program", "2007"),
        ("Krishna Puja/1981", "Krishna Puja", "1981"),
        ("SingleName", "SingleName", "SingleName"),
        ("  Parent / Album  ", "Parent", "Album"),
    ],
)
def test_split_album_path(full_path, expected_parent, expected_album):
    parent, album = _split_album_path(full_path)
    assert parent == expected_parent
    assert album == expected_album


def test_split_album_path_keeps_hierarchy():
    """Regression: sanitizing the whole path first collapsed "parent/album" into one name."""
    full_path = "2007/2007-05-04 Evening Program, Cabella Ligure (Italy) #80376"
    parent, album = _split_album_path(full_path)
    assert parent != album
    assert "/" not in album


@pytest.mark.parametrize(
    "segment,expected",
    [
        ("2007", "2007"),
        ("Evening Program", "Evening Program"),
        ("Cabella Ligure (Italy)", "Cabella Ligure (Italy)"),
        ('A/B*C?"D<E>F:G\\H|I', "A-B-C--D-E-F-G-H-I"),
    ],
)
def test_sanitize_path_segment(segment, expected):
    assert _sanitize_path_segment(segment) == expected


def test_sanitize_per_segment_not_whole_path():
    """The "/" separator must survive sanitization (it is the hierarchy separator)."""
    parent, album = _split_album_path("Italy/2007")
    assert parent == "Italy"
    assert album == "2007"
