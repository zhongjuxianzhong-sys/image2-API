import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as image_app
from PIL import Image


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", content=b""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.content = content
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise image_app.requests.HTTPError(f"HTTP {self.status_code}")


class ModelDiscoveryTests(unittest.TestCase):
    def test_classifies_and_sorts_models(self):
        ids = [
            "gpt-4o",
            "some-image-model",
            "doubao-seedream-4-0-250828",
            "dall-e-3",
            "gpt-image-1-mini",
            "gpt-image-2",
        ]
        items = image_app.list_image_models(ids)
        self.assertEqual(
            [item["id"] for item in items],
            [
                "gpt-image-2",
                "gpt-image-1-mini",
                "doubao-seedream-4-0-250828",
                "dall-e-3",
                "some-image-model",
            ],
        )
        self.assertNotIn("gpt-4o", [item["id"] for item in items])
        self.assertEqual(items[0]["group"], "recommended")
        self.assertEqual(items[-1]["group"], "other")

    def test_seedream_capabilities_are_version_specific(self):
        cases = {
            "doubao-seedream-5-0-pro-260628": ["1K", "2K"],
            "doubao-seedream-5-0-260128": ["2K", "3K"],
            "doubao-seedream-4-5-251128": ["2K", "4K"],
            "doubao-seedream-4-0-250828": ["1K", "2K", "4K"],
        }
        for model_id, levels in cases.items():
            with self.subTest(model_id=model_id):
                item = image_app.model_identity(model_id)
                self.assertEqual(item["family"], "doubao-seedream")
                self.assertEqual(item["capabilities"]["k_levels"], levels)
                self.assertEqual(
                    item["capabilities"]["edit_transport"],
                    "generation_image_field",
                )
                self.assertIn("auto", item["capabilities"]["ratios"])

        t2i = image_app.model_identity("doubao-seedream-3-0-t2i-250415")
        self.assertFalse(t2i["capabilities"]["edits"])

    def test_dalle_constraints(self):
        dalle3 = image_app.model_identity("dall-e-3")
        self.assertFalse(dalle3["capabilities"]["edits"])
        self.assertEqual(dalle3["capabilities"]["max_n"], 1)
        self.assertEqual(dalle3["capabilities"]["qualities"], ["standard", "hd"])

        dalle2 = image_app.model_identity("dall-e-2")
        self.assertTrue(dalle2["capabilities"]["edits"])
        self.assertEqual(dalle2["capabilities"]["max_references"], 1)

    def test_default_model_preference(self):
        items = image_app.list_image_models(
            ["some-image-model", "dall-e-3", "gpt-image-1.5"]
        )
        self.assertEqual(image_app._default_model_for_items(items), "gpt-image-1.5")

        newer = image_app.list_image_models(["gpt-image-1.5", "gpt-image-2.5-flare"])
        self.assertEqual(
            image_app._default_model_for_items(newer),
            "gpt-image-2.5-flare",
        )

    def test_custom_ratio_parsing_and_size(self):
        self.assertEqual(image_app.parse_custom_ratio("21:9"), (21, 9))
        self.assertEqual(image_app.parse_custom_ratio("21：9"), (21, 9))
        self.assertEqual(image_app.size_for_custom_ratio("21:9", "1K"), "1024x439")
        self.assertEqual(image_app.size_for_custom_ratio("9:21", "1K"), "439x1024")
        self.assertIsNone(image_app.parse_custom_ratio("0:1"))
        self.assertIsNone(image_app.parse_custom_ratio("17:1"))
        self.assertIsNone(image_app.parse_custom_ratio("abc"))


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = image_app.app.test_client()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_output_dir = image_app.OUTPUT_DIR
        self.original_history_file = image_app.HISTORY_FILE
        image_app.OUTPUT_DIR = Path(self.temp_dir.name) / "generated"
        image_app.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        image_app.HISTORY_FILE = Path(self.temp_dir.name) / "history.json"

    def tearDown(self):
        image_app.OUTPUT_DIR = self.original_output_dir
        image_app.HISTORY_FILE = self.original_history_file
        self.temp_dir.cleanup()

    def test_models_endpoint_returns_structured_items(self):
        upstream = FakeResponse(
            payload={
                "data": [
                    {"id": "gpt-image-2"},
                    {"id": "doubao-seedream-4-0-250828"},
                    {"id": "gpt-4o"},
                ]
            }
        )
        with mock.patch.object(image_app.HTTP, "get", return_value=upstream):
            response = self.client.get(
                "/api/models?base_url=https://example.com/v1",
                headers={"X-Api-Key": "test-key"},
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["default_model"], "gpt-image-2")
        self.assertEqual(data["image_count"], 2)
        self.assertEqual(data["items"][1]["family"], "doubao-seedream")

    def test_models_endpoint_rejects_bad_upstream_shape(self):
        upstream = FakeResponse(payload={"data": None})
        with mock.patch.object(image_app.HTTP, "get", return_value=upstream):
            response = self.client.get(
                "/api/models?base_url=https://example.com/v1",
                headers={"X-Api-Key": "test-key"},
            )
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.get_json()["ok"])

    def test_generate_gpt_image_uses_generation_endpoint(self):
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            return FakeResponse(
                payload={
                    "data": [
                        {
                            "b64_json": base64.b64encode(b"\x89PNG\r\n\x1a\nabc").decode(
                                "ascii"
                            )
                        }
                    ]
                }
            )

        with mock.patch.object(image_app.HTTP, "post", side_effect=fake_post):
            response = self.client.post(
                "/api/generate",
                json={
                    "model": "gpt-image-2",
                    "prompt": "test",
                    "base_url": "https://example.com/v1",
                    "api_key": "key",
                    "ratio": "4:3",
                    "k": "2K",
                    "quality": "high",
                    "n": 1,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(captured["url"].endswith("/images/generations"))
        self.assertEqual(captured["json"]["model"], "gpt-image-2")
        self.assertEqual(captured["json"]["size"], "2304x1728")
        self.assertEqual(captured["json"]["quality"], "high")

    def test_generate_gpt_standard_size_table(self):
        captured = {}

        def fake_post(url, **kwargs):
            captured["json"] = kwargs.get("json")
            return FakeResponse(
                payload={
                    "data": [
                        {
                            "b64_json": base64.b64encode(b"\x89PNG\r\n\x1a\nabc").decode(
                                "ascii"
                            )
                        }
                    ]
                }
            )

        with mock.patch.object(image_app.HTTP, "post", side_effect=fake_post):
            response = self.client.post(
                "/api/generate",
                json={
                    "model": "gpt-image-2",
                    "prompt": "test",
                    "base_url": "https://example.com/v1",
                    "api_key": "key",
                    "ratio": "16:9",
                    "k": "2K",
                    "n": 1,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["json"]["size"], "2560x1440")
        self.assertEqual(captured["json"]["quality"], "hd")

    def test_generate_gpt_reference_uses_images_edits(self):
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["files"] = kwargs.get("files")
            captured["data"] = kwargs.get("data")
            return FakeResponse(
                payload={
                    "data": [
                        {
                            "b64_json": base64.b64encode(b"\x89PNG\r\n\x1a\nabc").decode(
                                "ascii"
                            )
                        }
                    ]
                }
            )

        with mock.patch.object(image_app.HTTP, "post", side_effect=fake_post):
            response = self.client.post(
                "/api/generate",
                data={
                    "model": "gpt-image-2",
                    "prompt": "make it astronaut",
                    "base_url": "https://example.com/v1",
                    "api_key": "key",
                    "ratio": "1:1",
                    "k": "1K",
                    "n": "1",
                    "image": (io.BytesIO(b"\x89PNG\r\n\x1a\nabc"), "ref.png"),
                },
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(captured["url"].endswith("/images/edits"))
        self.assertEqual(captured["data"]["model"], "gpt-image-2")
        self.assertEqual(captured["data"]["size"], "1024x1024")

    def test_generate_seedream_uses_json_image_field(self):
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            return FakeResponse(
                payload={
                    "data": [
                        {
                            "b64_json": base64.b64encode(b"\x89PNG\r\n\x1a\nabc").decode(
                                "ascii"
                            )
                        }
                    ]
                }
            )

        with mock.patch.object(image_app.HTTP, "post", side_effect=fake_post):
            response = self.client.post(
                "/api/generate",
                data={
                    "model": "doubao-seedream-4-0-250828",
                    "prompt": "test",
                    "base_url": "https://example.com/v1",
                    "api_key": "key",
                    "ratio": "1:1",
                    "k": "2K",
                    "n": "2",
                    "image": (io.BytesIO(b"\x89PNG\r\n\x1a\nabc"), "ref.png"),
                },
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(captured["url"].endswith("/images/generations"))
        body = captured["json"]
        self.assertTrue(body["image"].startswith("data:image/png;base64,"))
        self.assertEqual(body["sequential_image_generation"], "auto")
        self.assertEqual(body["sequential_image_generation_options"]["max_images"], 2)
        self.assertNotIn("quality", body)
        self.assertNotIn("watermark", body)

    def test_generate_seedream_single_uses_minimal_openai_shape(self):
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            return FakeResponse(
                payload={
                    "data": [
                        {
                            "b64_json": base64.b64encode(b"\x89PNG\r\n\x1a\nabc").decode(
                                "ascii"
                            )
                        }
                    ]
                }
            )

        with mock.patch.object(image_app.HTTP, "post", side_effect=fake_post):
            response = self.client.post(
                "/api/generate",
                json={
                    "model": "Doubao-Seedream-5.0-pro",
                    "prompt": "test",
                    "base_url": "http://101.35.226.117:3500/v1",
                    "api_key": "key",
                    "ratio": "1:1",
                    "k": "1K",
                    "n": 1,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(captured["url"].endswith("/images/generations"))
        self.assertEqual(
            captured["json"],
            {
                "model": "Doubao-Seedream-5.0-pro",
                "prompt": "test\n\n画幅比例：1:1。",
                "size": "1024x1024",
            },
        )

    def test_generate_seedream_uses_exact_pixel_size(self):
        cases = [
            ("doubao-seedream-5-0-pro-260628", "1K", "3:4", "864x1152"),
            ("doubao-seedream-5-0-pro-260628", "2K", "16:9", "2816x1584"),
            ("doubao-seedream-5-0-260128", "2K", "16:9", "2848x1600"),
            ("doubao-seedream-5-0-260128", "3K", "16:9", "4096x2304"),
            ("doubao-seedream-4-5-251128", "2K", "16:9", "2560x1440"),
            ("doubao-seedream-4-0-250828", "1K", "16:9", "1280x720"),
        ]
        for model, k, ratio, expected in cases:
            with self.subTest(model=model, k=k, ratio=ratio):
                captured = {}

                def fake_post(url, **kwargs):
                    captured["json"] = kwargs.get("json")
                    return FakeResponse(
                        payload={
                            "data": [
                                {
                                    "b64_json": base64.b64encode(
                                        b"\x89PNG\r\n\x1a\nabc"
                                    ).decode("ascii")
                                }
                            ]
                        }
                    )

                with mock.patch.object(image_app.HTTP, "post", side_effect=fake_post):
                    response = self.client.post(
                        "/api/generate",
                        json={
                            "model": model,
                            "prompt": "test",
                            "base_url": "https://example.com/v1",
                            "api_key": "key",
                            "ratio": ratio,
                            "k": k,
                            "n": 1,
                        },
                    )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(captured["json"]["size"], expected)

    def test_generate_seedream_falls_back_to_tier_then_omits_size(self):
        bodies = []

        def fake_post(url, **kwargs):
            bodies.append(kwargs.get("json"))
            if len(bodies) < 3:
                return FakeResponse(
                    status_code=400,
                    payload={
                        "error": {
                            "message": "openai_error",
                            "type": "bad_response_status_code",
                            "code": "bad_response_status_code",
                        }
                    },
                    text='{"error":{"message":"openai_error","code":"bad_response_status_code"}}',
                )
            return FakeResponse(
                payload={
                    "data": [
                        {
                            "b64_json": base64.b64encode(b"\x89PNG\r\n\x1a\nabc").decode(
                                "ascii"
                            )
                        }
                    ]
                }
            )

        with mock.patch.object(image_app.HTTP, "post", side_effect=fake_post):
            response = self.client.post(
                "/api/generate",
                json={
                    "model": "Doubao-Seedream-5.0-pro",
                    "prompt": "test",
                    "base_url": "https://example.com/v1",
                    "api_key": "key",
                    "ratio": "3:4",
                    "k": "1K",
                    "n": 1,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(bodies[0]["size"], "864x1152")
        self.assertEqual(bodies[1]["size"], "1K")
        self.assertNotIn("size", bodies[2])

    def test_seedream_5_pro_rejects_multiple_images(self):
        response = self.client.post(
            "/api/generate",
            json={
                "model": "Doubao-Seedream-5.0-pro",
                "prompt": "test",
                "base_url": "https://example.com/v1",
                "api_key": "key",
                "ratio": "1:1",
                "k": "1K",
                "n": 2,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("数量只能在 1 到 1 之间", response.get_json()["error"])

    def test_generate_seedream_auto_omits_size(self):
        captured = {}

        def fake_post(url, **kwargs):
            captured["json"] = kwargs.get("json")
            return FakeResponse(
                payload={
                    "data": [
                        {
                            "b64_json": base64.b64encode(b"\x89PNG\r\n\x1a\nabc").decode(
                                "ascii"
                            )
                        }
                    ]
                }
            )

        with mock.patch.object(image_app.HTTP, "post", side_effect=fake_post):
            response = self.client.post(
                "/api/generate",
                json={
                    "model": "Doubao-Seedream-5.0-pro",
                    "prompt": "test",
                    "base_url": "https://example.com/v1",
                    "api_key": "key",
                    "ratio": "auto",
                    "n": 1,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("size", captured["json"])

    def test_generate_custom_ratio_for_seedream(self):
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            return FakeResponse(
                payload={
                    "data": [
                        {
                            "b64_json": base64.b64encode(b"\x89PNG\r\n\x1a\nabc").decode(
                                "ascii"
                            )
                        }
                    ]
                }
            )

        with mock.patch.object(image_app.HTTP, "post", side_effect=fake_post):
            response = self.client.post(
                "/api/generate",
                json={
                    "model": "Doubao-Seedream-5.0-pro",
                    "prompt": "test",
                    "base_url": "https://example.com/v1",
                    "api_key": "key",
                    "ratio": "custom",
                    "custom_ratio": "21:9",
                    "k": "1K",
                    "n": 1,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["json"]["size"], "1568x672")
        self.assertIn("21:9", captured["json"]["prompt"])

    def test_generate_seedream_keeps_ratio_in_prompt(self):
        captured = {}

        def fake_post(url, **kwargs):
            captured["json"] = kwargs.get("json")
            return FakeResponse(
                payload={
                    "data": [
                        {
                            "b64_json": base64.b64encode(b"\x89PNG\r\n\x1a\nabc").decode(
                                "ascii"
                            )
                        }
                    ]
                }
            )

        with mock.patch.object(image_app.HTTP, "post", side_effect=fake_post):
            response = self.client.post(
                "/api/generate",
                json={
                    "model": "doubao-seedream-5-0-pro-260628",
                    "prompt": "test",
                    "base_url": "https://example.com/v1",
                    "api_key": "key",
                    "ratio": "3:4",
                    "k": "1K",
                    "n": 1,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["json"]["size"], "864x1152")
        self.assertIn("3:4", captured["json"]["prompt"])

    def test_generate_seedream_3_t2i_does_not_fall_back_to_tier(self):
        bodies = []

        def fake_post(url, **kwargs):
            bodies.append(kwargs.get("json"))
            if len(bodies) == 1:
                return FakeResponse(status_code=400, text="bad size")
            return FakeResponse(
                payload={
                    "data": [
                        {
                            "b64_json": base64.b64encode(
                                b"\x89PNG\r\n\x1a\nabc"
                            ).decode("ascii")
                        }
                    ]
                }
            )

        with mock.patch.object(image_app.HTTP, "post", side_effect=fake_post):
            response = self.client.post(
                "/api/generate",
                json={
                    "model": "doubao-seedream-3-0-t2i-250415",
                    "prompt": "test",
                    "base_url": "https://example.com/v1",
                    "api_key": "key",
                    "ratio": "16:9",
                    "k": "1K",
                    "n": 1,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(bodies[0]["size"], "1280x720")
        self.assertNotIn("size", bodies[1])

    def test_generate_reports_actual_image_size(self):
        image_buffer = io.BytesIO()
        Image.new("RGB", (321, 123), color="white").save(image_buffer, format="PNG")
        captured = {}

        def fake_post(url, **kwargs):
            captured["json"] = kwargs.get("json")
            return FakeResponse(
                payload={
                    "data": [
                        {
                            "b64_json": base64.b64encode(image_buffer.getvalue()).decode(
                                "ascii"
                            )
                        }
                    ]
                }
            )

        with mock.patch.object(image_app.HTTP, "post", side_effect=fake_post):
            response = self.client.post(
                "/api/generate",
                json={
                    "model": "doubao-seedream-4-5-251128",
                    "prompt": "test",
                    "base_url": "https://example.com/v1",
                    "api_key": "key",
                    "ratio": "16:9",
                    "k": "2K",
                    "n": 1,
                },
            )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(captured["json"]["size"], "2560x1440")
        self.assertEqual(data["images"][0]["size"], "321x123")
        self.assertEqual(data["meta"]["size"], "321x123")

    def test_generate_custom_ratio_rejects_invalid_value(self):
        response = self.client.post(
            "/api/generate",
            json={
                "model": "gpt-image-2",
                "prompt": "test",
                "base_url": "https://example.com/v1",
                "api_key": "key",
                "ratio": "custom",
                "custom_ratio": "20:0",
                "k": "1K",
                "n": 1,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("自定义比例", response.get_json()["error"])

    def test_dalle3_rejects_reference_image(self):
        response = self.client.post(
            "/api/generate",
            data={
                "model": "dall-e-3",
                "prompt": "test",
                "base_url": "https://example.com/v1",
                "api_key": "key",
                "size": "1024x1024",
                "quality": "hd",
                "n": "1",
                "image": (io.BytesIO(b"\x89PNG\r\n\x1a\nabc"), "ref.png"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("不支持参考图", response.get_json()["error"])

    def test_empty_or_null_data_returns_502(self):
        for payload in ({"data": None}, {"data": []}):
            with self.subTest(payload=payload):
                with mock.patch.object(
                    image_app.HTTP,
                    "post",
                    return_value=FakeResponse(payload=payload),
                ):
                    response = self.client.post(
                        "/api/generate",
                        json={
                            "model": "gpt-image-2",
                            "prompt": "test",
                            "base_url": "https://example.com/v1",
                            "api_key": "key",
                        },
                    )
                self.assertEqual(response.status_code, 502)
                self.assertFalse(response.get_json()["ok"])

    def test_history_accepts_legacy_records(self):
        legacy = [{"id": "1", "filename": "old.png", "prompt": "old"}]
        image_app.HISTORY_FILE.write_text(
            json.dumps(legacy, ensure_ascii=False), encoding="utf-8"
        )
        response = self.client.get("/api/history")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["items"], legacy)


if __name__ == "__main__":
    unittest.main()
