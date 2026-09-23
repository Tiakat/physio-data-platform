"""physio.sync against in-memory fakes of Dropbox and Azure (no network)."""
import hashlib

import pytest

from physio import sync
from physio.common import DropboxHasher


def dbx_hash(b):
    h = DropboxHasher(); h.update(b); return h.hexdigest()


class Resp:
    def __init__(self, data): self.data = data
    def iter_content(self, n):
        for i in range(0, len(self.data), n): yield self.data[i:i + n]
    def close(self): pass


class FakeDbx:
    def __init__(self, files): self.files = files
    def files_download(self, path): return None, Resp(self.files[path])


class Props:
    def __init__(self, size, metadata): self.size, self.metadata = size, metadata


class FakeBlob:
    def __init__(self, store, name): self.store, self.name = store, name
    def upload_blob(self, f, overwrite, metadata, **kw):
        from azure.core.exceptions import ResourceExistsError
        assert overwrite is False
        if self.name in self.store: raise ResourceExistsError("exists")
        self.store[self.name] = (f.read(), metadata)
    def get_blob_properties(self):
        d, m = self.store[self.name]; return Props(len(d), m)


class FakeContainer:
    def __init__(self): self.store = {}
    def get_blob_client(self, name): return FakeBlob(self.store, name)


def row(path, data, target):
    return {"project": "P", "dropbox_path": path, "content_hash": dbx_hash(data),
            "size_bytes": str(len(data)), "subject": "1", "source": "NOL", "stage": "RAW",
            "target_path": target}


def test_upload_verified_and_metadata_ascii(tmp_path):
    data = b"abc" * 1000
    dbx, cc = FakeDbx({"/Données/x.csv": data}), FakeContainer()
    r = sync.upload_one(dbx, cc, row("/Données/x.csv", data, "P/Database/RawData/1/NOL/x.csv"), str(tmp_path))
    assert r["status"] == "VERIFIED" and r["sha256"] == hashlib.sha256(data).hexdigest()
    md = cc.store["P/Database/RawData/1/NOL/x.csv"][1]
    assert all(v.isascii() for v in md.values())


def test_never_overwrites_different_content(tmp_path):
    cc = FakeContainer()
    cc.store["T"] = (b"old", {"dropbox_content_hash": "other"})
    r = sync.upload_one(FakeDbx({"/a": b"new"}), cc, row("/a", b"new", "T"), str(tmp_path))
    assert r["status"] == "CONFLICT" and cc.store["T"][0] == b"old"


def test_corrupted_download_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    bad = row("/a", b"expected", "T")
    r = sync.upload_one(FakeDbx({"/a": b"corrupt!"}), FakeContainer(), bad, str(tmp_path), attempts=2)
    assert r["status"] == "FAILED" and "corrupted" in r["error"]
