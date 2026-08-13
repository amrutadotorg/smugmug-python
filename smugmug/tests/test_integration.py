"""Integration tests — require a live SmugMug account.

Credentials come from env vars: SMUGMUG_API_KEY, SMUGMUG_API_SECRET,
SMUGMUG_ACCESS_TOKEN, SMUGMUG_TOKEN_SECRET.
"""

import hashlib
import os
from io import BytesIO

import pytest

from smugmug import SmugMugClient, SmugMugError

pytestmark = pytest.mark.integration

ROOT_FOLDER_URI = "/api/v2/node/BHtXzC"


@pytest.fixture(scope="session")
def sm_client():
    return SmugMugClient.from_config(
        api_key=os.environ["SMUGMUG_API_KEY"],
        api_secret=os.environ["SMUGMUG_API_SECRET"],
        access_token=os.environ["SMUGMUG_ACCESS_TOKEN"],
        token_secret=os.environ["SMUGMUG_TOKEN_SECRET"],
    )


@pytest.fixture(scope="session")
def test_album_uri(sm_client):
    """Create a temporary album in Inbox with one test image, clean up at session end."""
    import os as _os

    INBOX_FOLDER_URI = "/api/v2/node/hhZgsH"

    node = sm_client.get_or_create_node(
        INBOX_FOLDER_URI, "Album", f"_test_{_os.getpid()}"
    )
    album_uri = sm_client.get_album_uri(node)

    jpg_data = BytesIO()
    jpg_data.write(
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    )
    jpg_data.write(b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t")
    jpg_data.write(b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00")
    jpg_data.write(b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00")
    jpg_data.write(
        b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xd2\xcf%UY\x16\x05\x03\xc0\x00\xff\xd9"
    )
    image_bytes = jpg_data.getvalue()

    md5_hash = hashlib.md5(image_bytes).hexdigest()
    result = sm_client._upload_bytes(album_uri, "_test.jpg", image_bytes)
    assert result is not None, "Test image upload failed"
    assert result["md5"] == md5_hash

    yield album_uri

    sm_client.delete_album(album_uri)


class TestGetAlbumImageHashes:
    def test_returns_list_of_dicts(self, sm_client, test_album_uri):
        hashes = sm_client.get_album_image_hashes(test_album_uri)
        assert isinstance(hashes, list)
        assert len(hashes) > 0
        first = hashes[0]
        assert "FileName" in first
        assert "ArchivedMD5" in first
        assert "Uri" in first
        assert "ImageKey" in first
        assert len(first["ArchivedMD5"]) == 32
        assert first["ImageKey"] != ""

    def test_raises_for_invalid_uri(self, sm_client):
        with pytest.raises(SmugMugError):
            sm_client.get_album_image_hashes("/api/v2/album/nonexistent99")


class TestNodeOperations:
    def test_get_node_children_root(self, sm_client):
        children = sm_client.get_node_children(sm_client.root_node_uri, count=10)
        assert isinstance(children, list)
        assert len(children) > 0

    def test_get_node(self, sm_client):
        node = sm_client.get_node(ROOT_FOLDER_URI)
        assert node is not None
        assert node["Type"] == "Folder"

    def test_image_count(self, sm_client, test_album_uri):
        assert sm_client.image_count(test_album_uri) >= 1

    def test_get_album_images(self, sm_client, test_album_uri):
        uris = sm_client.get_album_images(test_album_uri)
        assert len(uris) >= 1
        assert all(u.startswith("/api/v2/") for u in uris)

    def test_get_weburi(self, sm_client, test_album_uri):
        weburi = sm_client.get_weburi(test_album_uri)
        assert weburi is None or "smugmug.com" in weburi


class TestAlbumHierarchy:
    """get_album_else_create must produce folder/album with the intended names."""

    def test_creates_folder_and_album(self, sm_client):
        import os as _os

        parent_name = f"_test_parent_{_os.getpid()}"
        album_name = "_test_album"
        album_uri = sm_client.get_album_else_create(
            sm_client.root_node_uri, f"{parent_name}/{album_name}"
        )
        assert album_uri.startswith("/api/v2/album/")

        children = sm_client.get_node_children(sm_client.root_node_uri)
        folder = next(c for c in children if c.get("Name") == parent_name)
        assert folder["Type"] == "Folder"
        album_children = sm_client.get_node_children(folder["Uri"])
        assert [c["Name"] for c in album_children] == [album_name]

        sm_client.delete_album(album_uri)
        sm_client._delete(folder["Uri"])
