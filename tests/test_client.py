import unittest
from pathlib import Path

import client


class ReferenceMimeTests(unittest.TestCase):
    def test_known_image_extensions_map_to_image_types(self):
        self.assertEqual(client.reference_mime_type("ref.png"), "image/png")
        self.assertEqual(client.reference_mime_type("ref.JPG"), "image/jpeg")
        self.assertEqual(client.reference_mime_type(Path("a") / "ref.jpeg"), "image/jpeg")
        self.assertEqual(client.reference_mime_type("ref.gif"), "image/gif")

    def test_unknown_extension_falls_back_to_octet_stream(self):
        self.assertEqual(
            client.reference_mime_type("ref.image2-unknown"),
            "application/octet-stream",
        )


if __name__ == "__main__":
    unittest.main()
