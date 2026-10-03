"""Checksums of an image, with progress. MD5, SHA-1, SHA-256, SHA-512 like Rufus."""

import hashlib
import os

ALGORITHMS = ("md5", "sha1", "sha256", "sha512")


def hash_file(path, algorithms=ALGORITHMS, emitter=None, cancel=None, chunk=4 * 1024 * 1024):
    hashers = {a: hashlib.new(a) for a in algorithms}
    total = os.path.getsize(path)
    done = 0
    with open(path, "rb") as f:
        while True:
            if cancel:
                cancel.check()
            buf = f.read(chunk)
            if not buf:
                break
            for h in hashers.values():
                h.update(buf)
            done += len(buf)
            if emitter:
                emitter.progress("hash", done / total if total else 1.0)
    return {a: h.hexdigest() for a, h in hashers.items()}
