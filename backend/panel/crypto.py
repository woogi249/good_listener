"""Application-level encryption and local master-key protection."""
from __future__ import annotations

import base64
import ctypes
import os
import secrets
import stat
from ctypes import wintypes
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_TEXT_PREFIX = "enc:v1:"
_BINARY_MAGIC = b"GLENC1\x00"
_AAD_PREFIX = b"good-listener:v1:"


class EncryptionManager:
    """AES-256-GCM envelope encryption backed by one local master key.

    On Windows the persisted key blob is protected with DPAPI for the current
    user.  Tests and non-Windows deployments use a mode-0600 key file.  A
    base64 key may be injected explicitly for managed deployments.
    """

    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("master key must be exactly 32 bytes")
        self._aead = AESGCM(key)

    @classmethod
    def load(cls, key_path: str | Path, *, environment_key: str | None = None) -> "EncryptionManager":
        encoded = environment_key or os.getenv("GOOD_LISTENER_MASTER_KEY", "").strip()
        if encoded:
            try:
                key = base64.urlsafe_b64decode(encoded.encode("ascii"))
            except Exception as exc:
                raise ValueError("GOOD_LISTENER_MASTER_KEY must be URL-safe base64") from exc
            return cls(key)

        path = Path(key_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            protected = path.read_bytes()
            key = _dpapi_unprotect(protected) if os.name == "nt" else protected
        else:
            key = secrets.token_bytes(32)
            protected = _dpapi_protect(key) if os.name == "nt" else key
            _write_private_file(path, protected)
        if len(key) != 32:
            raise ValueError(f"invalid master key at {path}")
        return cls(key)

    def encrypt_text(self, value: str, *, context: str) -> str:
        encrypted = self.encrypt_bytes(value.encode("utf-8"), context=context)
        return _TEXT_PREFIX + base64.urlsafe_b64encode(encrypted).decode("ascii")

    def decrypt_text(self, value: str, *, context: str) -> str:
        if not self.is_encrypted_text(value):
            # Legacy plaintext remains readable so startup migration can seal it.
            return value
        raw = base64.urlsafe_b64decode(value[len(_TEXT_PREFIX) :].encode("ascii"))
        return self.decrypt_bytes(raw, context=context).decode("utf-8")

    def encrypt_bytes(self, value: bytes, *, context: str) -> bytes:
        nonce = secrets.token_bytes(12)
        ciphertext = self._aead.encrypt(nonce, value, _AAD_PREFIX + context.encode("utf-8"))
        return _BINARY_MAGIC + nonce + ciphertext

    def decrypt_bytes(self, value: bytes, *, context: str) -> bytes:
        if not value.startswith(_BINARY_MAGIC):
            raise ValueError("unencrypted binary payload")
        offset = len(_BINARY_MAGIC)
        nonce = value[offset : offset + 12]
        ciphertext = value[offset + 12 :]
        return self._aead.decrypt(
            nonce,
            ciphertext,
            _AAD_PREFIX + context.encode("utf-8"),
        )

    @staticmethod
    def is_encrypted_text(value: str) -> bool:
        if not value.startswith(_TEXT_PREFIX):
            return False
        try:
            raw = base64.urlsafe_b64decode(value[len(_TEXT_PREFIX) :].encode("ascii"))
        except Exception:
            return False
        return raw.startswith(_BINARY_MAGIC) and len(raw) >= len(_BINARY_MAGIC) + 12 + 16


def _write_private_file(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(str(path), flags, 0o600)
    try:
        view = memoryview(data)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError("failed to persist the complete master key")
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if os.name != "nt":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _make_blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    return blob, buffer


def _dpapi_protect(data: bytes) -> bytes:
    if os.name != "nt":
        return data
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    source, source_buffer = _make_blob(data)
    entropy, entropy_buffer = _make_blob(b"good-listener-master-key-v1")
    output = _DataBlob()
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        "Good Listener master key",
        ctypes.byref(entropy),
        None,
        None,
        0x01,
        ctypes.byref(output),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)
        _ = source_buffer, entropy_buffer


def _dpapi_unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        return data
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    source, source_buffer = _make_blob(data)
    entropy, entropy_buffer = _make_blob(b"good-listener-master-key-v1")
    output = _DataBlob()
    description = wintypes.LPWSTR()
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source),
        ctypes.byref(description),
        ctypes.byref(entropy),
        None,
        None,
        0x01,
        ctypes.byref(output),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)
        if description:
            kernel32.LocalFree(description)
        _ = source_buffer, entropy_buffer
