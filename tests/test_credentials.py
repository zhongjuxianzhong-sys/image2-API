import os
import tempfile
import unittest
from pathlib import Path

import client


class ClientPreferenceTests(unittest.TestCase):
    def test_preferred_k_level_prefers_2k(self):
        self.assertEqual(client.preferred_k_level(["1K", "2K", "4K"]), "2K")

    def test_preferred_k_level_uses_first_available_without_2k(self):
        self.assertEqual(client.preferred_k_level(["1K"]), "1K")


@unittest.skipUnless(os.name == "nt", "Windows DPAPI is only available on Windows")
class CredentialStorageTests(unittest.TestCase):
    def test_round_trip_encrypted_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "client-credentials.dat"
            client.save_encrypted_credentials(path, "https://example.com/v1", "sk-secret")

            raw = path.read_bytes()
            self.assertNotIn(b"sk-secret", raw)
            self.assertNotIn(b"https://example.com/v1", raw)

            loaded = client.load_encrypted_credentials(path)
            self.assertEqual(
                loaded,
                {"base_url": "https://example.com/v1", "api_key": "sk-secret"},
            )

    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "missing.dat"
            self.assertIsNone(client.load_encrypted_credentials(path))

    def test_corrupt_file_raises_credential_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "broken.dat"
            path.write_bytes(b"not-dpapi-data")
            with self.assertRaises(client.CredentialError):
                client.load_encrypted_credentials(path)

    def test_replacing_credentials_does_not_leave_plaintext(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "client-credentials.dat"
            client.save_encrypted_credentials(path, "https://old.example/v1", "old-secret")
            client.save_encrypted_credentials(path, "https://new.example/v1", "new-secret")

            raw = path.read_bytes()
            self.assertNotIn(b"old-secret", raw)
            self.assertNotIn(b"new-secret", raw)
            loaded = client.load_encrypted_credentials(path)
            self.assertEqual(
                loaded,
                {"base_url": "https://new.example/v1", "api_key": "new-secret"},
            )
