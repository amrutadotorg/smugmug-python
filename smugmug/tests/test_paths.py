"""Unit tests for pure client helpers (no API access)."""

import pytest

from smugmug.client import (
    SmugMugError,
    _list_upload_files,
    _sanitize_path_segment,
    _split_album_path,
)


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
    "full_path",
    [
        "2024/Summer/Wedding",
        "2024/Summer/Wedding/Evening",
        "/api/v2/node/x",
    ],
)
def test_split_album_path_rejects_more_than_two_segments(full_path):
    """Extra segments would be silently dropped — reject instead of guessing."""
    with pytest.raises(SmugMugError):
        _split_album_path(full_path)


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


def test_list_upload_files_sorts_and_skips_hidden(tmp_path):
    (tmp_path / "b.jpg").write_bytes(b"")
    (tmp_path / "a.png").write_bytes(b"")
    (tmp_path / ".hidden.jpg").write_bytes(b"")
    (tmp_path / "subdir").mkdir()

    files = _list_upload_files(tmp_path)
    assert [f.name for f in files] == ["a.png", "b.jpg"]


def test_list_upload_files_filters_by_extension(tmp_path):
    (tmp_path / "a.JPG").write_bytes(b"")
    (tmp_path / "b.txt").write_bytes(b"")
    (tmp_path / "c.png").write_bytes(b"")

    files = _list_upload_files(tmp_path, extensions={".jpg", ".png"})
    assert [f.name for f in files] == ["a.JPG", "c.png"]
