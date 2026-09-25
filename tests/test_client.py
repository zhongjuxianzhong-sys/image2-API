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
            client.reference_mime_type("ref.gpt-image-unknown"),
            "application/octet-stream",
        )


class BrandingTests(unittest.TestCase):
    def test_window_title_is_gpt_branded(self):
        self.assertEqual(client.APP_TITLE, "GPT 生图工坊")
        self.assertNotIn("Image2", client.APP_TITLE)

    def test_cancelled_error_type_exists(self):
        self.assertTrue(issubclass(client.TaskCancelled, RuntimeError))


if __name__ == "__main__":
    unittest.main()
