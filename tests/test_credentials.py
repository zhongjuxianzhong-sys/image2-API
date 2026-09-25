import os
import tempfile
import unittest
from pathlib import Path

import client


class ClientPreferenceTests(unittest.TestCase):
    def test_default_k_level_prefers_1k(self):
        self.assertEqual(client.default_k_level(["1K", "2K", "4K"]), "1K")

    def test_default_k_level_falls_back_to_first_available(self):
        self.assertEqual(client.default_k_level(["2K", "4K"]), "2K")
        self.assertEqual(client.default_k_level([]), "")


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
            self.assertEqual(loaded["base_url"], "https://example.com/v1")
            self.assertEqual(loaded["api_key"], "sk-secret")
            self.assertEqual(loaded["model"], "")

    def test_round_trip_remembers_selected_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "client-credentials.dat"
            client.save_encrypted_credentials(
                path, "https://example.com/v1", "sk-secret", "gpt-image-2.5-flare"
            )
            loaded = client.load_encrypted_credentials(path)
            self.assertEqual(loaded["model"], "gpt-image-2.5-flare")

    def test_replacing_credentials_does_not_leave_plaintext(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "client-credentials.dat"
            client.save_encrypted_credentials(path, "https://old.example/v1", "old-secret")
            client.save_encrypted_credentials(path, "https://new.example/v1", "new-secret")

            raw = path.read_bytes()
            self.assertNotIn(b"old-secret", raw)
            self.assertNotIn(b"new-secret", raw)

            loaded = client.load_encrypted_credentials(path)
            self.assertEqual(loaded["base_url"], "https://new.example/v1")
            self.assertEqual(loaded["api_key"], "new-secret")

    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "nope.dat"
            self.assertIsNone(client.load_encrypted_credentials(path))


if __name__ == "__main__":
    unittest.main()
