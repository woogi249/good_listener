import base64

import pytest

from panel.crypto import EncryptionManager, _write_private_file


def test_aes_gcm_round_trip_and_context_binding():
    cipher = EncryptionManager(b"a" * 32)
    sealed = cipher.encrypt_text("민감한 회의 원문", context="utterance.text")

    assert sealed.startswith("enc:v1:")
    assert "민감한" not in sealed
    assert cipher.decrypt_text(sealed, context="utterance.text") == "민감한 회의 원문"
    with pytest.raises(Exception):
        cipher.decrypt_text(sealed, context="minutes.markdown")


def test_environment_key_does_not_create_key_file(tmp_path):
    encoded = base64.urlsafe_b64encode(b"b" * 32).decode("ascii")
    path = tmp_path / "master.key"
    cipher = EncryptionManager.load(path, environment_key=encoded)

    assert cipher.decrypt_bytes(
        cipher.encrypt_bytes(b"audio", context="audio:test"), context="audio:test"
    ) == b"audio"
    assert not path.exists()


def test_private_key_file_preserves_binary_bytes_on_windows(tmp_path):
    payload = b"\x00\n\xff\r\nDPAPI\n" * 20
    path = tmp_path / "master.key.dpapi"

    _write_private_file(path, payload)

    assert path.read_bytes() == payload


def test_plaintext_with_encryption_prefix_is_still_encrypted():
    cipher = EncryptionManager(b"c" * 32)
    plaintext = "enc:v1:c2VjcmV0"

    assert cipher.decrypt_text(plaintext, context="meeting.topic") == plaintext
    sealed = cipher.encrypt_text(plaintext, context="meeting.topic")

    assert sealed != plaintext
    assert cipher.is_encrypted_text(sealed) is True
    assert cipher.is_encrypted_text(plaintext) is False
    assert cipher.decrypt_text(sealed, context="meeting.topic") == plaintext
