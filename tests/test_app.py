import base64
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import app as gpt_app
from PIL import Image


def png_bytes(size: tuple[int, int] = (8, 8), color: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", content=b"", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self.content = content
        self.headers = headers or {"Content-Type": "application/json"}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise gpt_app.requests.HTTPError(f"HTTP {self.status_code}")


class ModelDiscoveryTests(unittest.TestCase):
    def test_only_gpt_image_models_are_listed(self):
        ids = [
            "gpt-4o",
            "Doubao-Seedream-5.0-pro",
            "gemini-3.0-pro-image",
            "dall-e-3",
            "some-image-model",
            "gpt-image-2",
            "gpt-image-1.5",
        ]
        items = gpt_app.list_image_models(ids)
        self.assertEqual(
            [item["id"] for item in items],
            ["gpt-image-2", "gpt-image-1.5"],
        )
        self.assertNotIn("Doubao-Seedream-5.0-pro", [item["id"] for item in items])
        self.assertNotIn("dall-e-3", [item["id"] for item in items])

    def test_documented_models_have_documented_capabilities(self):
        for model_id in (
            "gpt-image-2",
            "gpt-image-2.5-flare",
            "gpt-image-2.5-sunburst",
            "gpt-image-2.5",
        ):
            with self.subTest(model_id=model_id):
                caps = gpt_app.model_identity(model_id)["capabilities"]
                self.assertEqual(caps["ratios"], gpt_app.GPT_RATIOS)
                self.assertEqual(caps["k_levels"], ["1K", "2K", "4K"])
                self.assertEqual(caps["max_n"], 4)
                self.assertEqual(caps["max_references"], 4)
                self.assertNotIn("auto", caps["ratios"])
                self.assertNotIn("custom", caps["ratios"])

    def test_unknown_and_non_gpt_models_are_rejected(self):
        self.assertEqual(gpt_app.model_identity("doubao-seedream-4-0"), {})
        self.assertEqual(gpt_app.model_identity("gpt-4o"), {})
        self.assertEqual(gpt_app.model_identity("dall-e-3"), {})
        self.assertEqual(gpt_app.model_identity(""), {})

    def test_labels_and_order(self):
        items = gpt_app.list_image_models(
            ["gpt-image-2", "gpt-image-1.5", "gpt-image-2.5-flare", "gpt-image-2-fa"]
        )
        self.assertEqual(
            [item["id"] for item in items],
            ["gpt-image-2.5-flare", "gpt-image-2", "gpt-image-2-fa", "gpt-image-1.5"],
        )
        self.assertEqual(items[0]["label"], "GPT Image 2.5 Flare")
        self.assertEqual(items[1]["label"], "GPT Image 2")

    def test_endpoint_types_decide_reference_transport(self):
        endpoint_types = {
            "gpt-image-2": ["image-generation", "openai"],
            "gpt-image-2.5-sunburst": ["image-generation"],
        }
        items = gpt_app.list_image_models(
            ["gpt-image-2", "gpt-image-2.5-sunburst"], endpoint_types
        )
        by_id = {item["id"]: item for item in items}
        self.assertEqual(
            by_id["gpt-image-2"]["capabilities"]["edit_transport"], "chat"
        )
        self.assertEqual(
            by_id["gpt-image-2.5-sunburst"]["capabilities"]["edit_transport"], "edits"
        )

    def test_default_model_preference(self):
        items = gpt_app.list_image_models(["gpt-image-2.5", "gpt-image-1.5"])
        self.assertEqual(gpt_app._default_model_for_items(items), "gpt-image-2.5")

        preferred = gpt_app.list_image_models(
            ["gpt-image-1.5", "gpt-image-2.5-sunburst", "gpt-image-2.5-flare"]
        )
        self.assertEqual(gpt_app._default_model_for_items(preferred), "gpt-image-2.5-flare")

        exact = gpt_app.list_image_models(["gpt-image-2.5-flare", "gpt-image-2"])
        self.assertEqual(gpt_app._default_model_for_items(exact), "gpt-image-2")

        self.assertIsNone(gpt_app._default_model_for_items([]))


class SizeTableTests(unittest.TestCase):
    EXPECTED = {
        "1K": {
            "1:1": "1024x1024",
            "16:9": "1280x720",
            "9:16": "720x1280",
            "3:2": "1536x1024",
            "2:3": "1024x1536",
            "4:3": "1152x864",
            "3:4": "864x1152",
            "5:4": "1120x896",
            "4:5": "896x1120",
            "21:9": "1456x624",
        },
        "2K": {
            "1:1": "2048x2048",
            "16:9": "2560x1440",
            "9:16": "1440x2560",
            "3:2": "2496x1664",
            "2:3": "1664x2496",
            "4:3": "2304x1728",
            "3:4": "1728x2304",
            "5:4": "2240x1792",
            "4:5": "1792x2240",
            "21:9": "3024x1296",
        },
        "4K": {
            "1:1": "2480x2480",
            "16:9": "3328x1872",
            "9:16": "1872x3328",
            "3:2": "3056x2032",
            "2:3": "2032x3056",
            "4:3": "2880x2160",
            "3:4": "2160x2880",
            "5:4": "2784x2224",
            "4:5": "2224x2784",
            "21:9": "3808x1632",
        },
    }

    def test_every_ratio_and_tier_matches_documented_pixels(self):
        for k, ratios in self.EXPECTED.items():
            for ratio, expected in ratios.items():
                with self.subTest(k=k, ratio=ratio):
                    self.assertEqual(gpt_app.size_for_gpt(ratio, k), expected)

    def test_derived_sizes_are_unique(self):
        sizes = gpt_app.derived_gpt_sizes()
        self.assertEqual(len(sizes), len(set(sizes)))
        self.assertIn("3808x1632", sizes)

    def test_resolve_size_returns_documented_pixel(self):
        caps = gpt_app.gpt_capabilities()
        size, error = gpt_app.resolve_size({"ratio": "16:9", "k": "2K"}, caps)
        self.assertIsNone(error)
        self.assertEqual(size, "2560x1440")

    def test_resolve_size_rejects_auto_and_custom(self):
        caps = gpt_app.gpt_capabilities()
        for ratio in ("auto", "custom", "3:1"):
            with self.subTest(ratio=ratio):
                size, error = gpt_app.resolve_size({"ratio": ratio, "k": "1K"}, caps)
                self.assertIsNone(size)
                self.assertIn("不受支持", error)

    def test_resolve_size_rejects_mismatched_pixel_request(self):
        caps = gpt_app.gpt_capabilities()
        size, error = gpt_app.resolve_size(
            {"ratio": "16:9", "k": "2K", "size": "1024x1024"}, caps
        )
        self.assertIsNone(size)
        self.assertIn("2560x1440", error)

    def test_resolve_size_accepts_matching_pixel_request(self):
        caps = gpt_app.gpt_capabilities()
        size, error = gpt_app.resolve_size(
            {"ratio": "16:9", "k": "2K", "size": "2560x1440"}, caps
        )
        self.assertIsNone(error)
        self.assertEqual(size, "2560x1440")

    def test_resolve_size_rejects_bad_tier(self):
        caps = gpt_app.gpt_capabilities()
        size, error = gpt_app.resolve_size({"ratio": "1:1", "k": "3K"}, caps)
        self.assertIsNone(size)
        self.assertIn("分辨率", error)


class QualityAndCountTests(unittest.TestCase):
    def test_quality_follows_resolution_tier(self):
        caps = gpt_app.gpt_capabilities()
        for k, expected in (("1K", "standard"), ("2K", "hd"), ("4K", "4k")):
            with self.subTest(k=k):
                quality, error = gpt_app.resolve_quality({"k": k}, caps)
                self.assertIsNone(error)
                self.assertEqual(quality, expected)

    def test_auto_quality_maps_to_tier(self):
        caps = gpt_app.gpt_capabilities()
        quality, error = gpt_app.resolve_quality({"k": "4K", "quality": "auto"}, caps)
        self.assertIsNone(error)
        self.assertEqual(quality, "4k")

    def test_unsupported_quality_is_rejected(self):
        caps = gpt_app.gpt_capabilities()
        quality, error = gpt_app.resolve_quality({"k": "1K", "quality": "ultra"}, caps)
        self.assertIsNone(quality)
        self.assertIn("不受支持", error)

    def test_count_range_and_reference_lock(self):
        caps = gpt_app.gpt_capabilities()
        self.assertEqual(gpt_app.resolve_count({"n": 3}, caps, False), (3, None))
        n, error = gpt_app.resolve_count({"n": 5}, caps, False)
        self.assertEqual(n, 1)
        self.assertIn("1 到 4", error)
        self.assertEqual(gpt_app.resolve_count({"n": 4}, caps, True), (1, None))
        self.assertEqual(gpt_app.resolve_count({}, caps, False), (1, None))


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = gpt_app.app.test_client()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_output_dir = gpt_app.OUTPUT_DIR
        self.original_history_file = gpt_app.HISTORY_FILE
        self.original_poll_interval = gpt_app.POLL_INTERVAL_SECONDS
        self.original_budget = gpt_app.TASK_BUDGET_SECONDS
        gpt_app.OUTPUT_DIR = Path(self.temp_dir.name) / "generated"
        gpt_app.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        gpt_app.HISTORY_FILE = Path(self.temp_dir.name) / "history.json"
        gpt_app.POLL_INTERVAL_SECONDS = 0.05
        gpt_app.clear_tasks()

    def tearDown(self):
        gpt_app.OUTPUT_DIR = self.original_output_dir
        gpt_app.HISTORY_FILE = self.original_history_file
        gpt_app.POLL_INTERVAL_SECONDS = self.original_poll_interval
        gpt_app.TASK_BUDGET_SECONDS = self.original_budget
        gpt_app.clear_tasks()
        self.temp_dir.cleanup()

    def _sync_response(self):
        return FakeResponse(
            payload={
                "data": [
                    {"b64_json": base64.b64encode(png_bytes()).decode("ascii")}
                ]
            }
        )

    # ---------------- 健康检查与模型发现 ----------------
    def test_health_reports_gpt_only_parameters(self):
        payload = self.client.get("/api/health").get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["model_mode"], "gpt-image")
        self.assertEqual(payload["k_levels"], ["1K", "2K", "4K"])
        self.assertEqual(payload["ratios"], gpt_app.GPT_RATIOS)
        self.assertEqual(
            payload["qualities_by_k"], {"1K": "standard", "2K": "hd", "4K": "4k"}
        )
        self.assertNotIn("seedream", payload)

    def test_root_is_not_served(self):
        self.assertEqual(self.client.get("/").status_code, 404)

    def test_models_endpoint_filters_to_gpt_image(self):
        upstream = FakeResponse(
            payload={
                "data": [
                    {"id": "gpt-image-2", "supported_endpoint_types": ["image-generation", "openai"]},
                    {"id": "Doubao-Seedream-5.0-pro"},
                    {"id": "gpt-4o"},
                    {"id": "gpt-image-2.5-sunburst", "supported_endpoint_types": ["image-generation"]},
                ]
            }
        )
        with mock.patch.object(gpt_app.HTTP, "get", return_value=upstream):
            response = self.client.get(
                "/api/models?base_url=https://example.com/v1",
                headers={"X-Api-Key": "test-key"},
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["default_model"], "gpt-image-2")
        self.assertEqual(data["image_count"], 2)
        self.assertEqual(data["count"], 4)
        transports = {
            item["id"]: item["capabilities"]["edit_transport"] for item in data["items"]
        }
        self.assertEqual(transports["gpt-image-2"], "chat")
        self.assertEqual(transports["gpt-image-2.5-sunburst"], "edits")

    def test_models_endpoint_requires_key_and_base_url(self):
        self.assertEqual(self.client.get("/api/models").status_code, 400)
        self.assertEqual(
            self.client.get("/api/models?base_url=https://x/v1").status_code, 400
        )

    def test_models_endpoint_rejects_bad_upstream_shape(self):
        upstream = FakeResponse(payload={"data": None})
        with mock.patch.object(gpt_app.HTTP, "get", return_value=upstream):
            response = self.client.get(
                "/api/models?base_url=https://example.com/v1",
                headers={"X-Api-Key": "test-key"},
            )
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.get_json()["ok"])

    # ---------------- 文生图 ----------------
    def test_generation_uses_documented_body_and_async_flag(self):
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            captured["params"] = kwargs.get("params")
            captured["headers"] = kwargs.get("headers")
            return self._sync_response()

        with mock.patch.object(gpt_app.HTTP, "post", side_effect=fake_post):
            response = self.client.post(
                "/api/generate",
                json={
                    "model": "gpt-image-2",
                    "prompt": "test",
                    "base_url": "https://example.com/v1",
                    "api_key": "key",
                    "ratio": "4:3",
                    "k": "2K",
                    "n": 1,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(captured["url"].endswith("/images/generations"))
        self.assertEqual(captured["params"], {"async": 1})
        self.assertEqual(captured["headers"]["Prefer"], "respond-async")
        self.assertEqual(
            captured["json"],
            {
                "model": "gpt-image-2",
                "prompt": "test",
                "n": 1,
                "size": "2304x1728",
                "quality": "hd",
            },
        )

    def test_generation_quality_follows_tier_without_quality_field(self):
        for k, expected in (("1K", "standard"), ("2K", "hd"), ("4K", "4k")):
            with self.subTest(k=k):
                captured = {}

                def fake_post(url, **kwargs):
                    captured["json"] = kwargs.get("json")
                    return self._sync_response()

                with mock.patch.object(gpt_app.HTTP, "post", side_effect=fake_post):
                    response = self.client.post(
                        "/api/generate",
                        json={
                            "model": "gpt-image-2",
                            "prompt": "test",
                            "base_url": "https://example.com/v1",
                            "api_key": "key",
                            "ratio": "16:9",
                            "k": k,
                        },
                    )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(captured["json"]["quality"], expected)

    def test_generation_reports_actual_image_size(self):
        raw = png_bytes((32, 48))

        def fake_post(url, **kwargs):
            return FakeResponse(
                payload={"data": [{"b64_json": base64.b64encode(raw).decode("ascii")}]}
            )

        with mock.patch.object(gpt_app.HTTP, "post", side_effect=fake_post):
            response = self.client.post(
                "/api/generate",
                json={
                    "model": "gpt-image-2",
                    "prompt": "test",
                    "base_url": "https://example.com/v1",
                    "api_key": "key",
                    "ratio": "1:1",
                    "k": "1K",
                },
            )
        data = response.get_json()
        self.assertEqual(data["images"][0]["size"], "32x48")
        self.assertEqual(data["meta"]["size"], "32x48")

    def test_generation_writes_history(self):
        with mock.patch.object(gpt_app.HTTP, "post", side_effect=lambda *a, **k: self._sync_response()):
            self.client.post(
                "/api/generate",
                json={
                    "model": "gpt-image-2",
                    "prompt": "history test",
                    "base_url": "https://example.com/v1",
                    "api_key": "key",
                    "ratio": "1:1",
                    "k": "1K",
                },
            )
        items = self.client.get("/api/history").get_json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["model"], "gpt-image-2")
        self.assertEqual(items[0]["prompt"], "history test")
        self.assertFalse(items[0]["reference"])

    def test_generation_rejects_invalid_inputs(self):
        base = {
            "model": "gpt-image-2",
            "prompt": "p",
            "base_url": "https://example.com/v1",
            "api_key": "key",
            "ratio": "1:1",
            "k": "1K",
        }
        cases = [
            ({**base, "model": "doubao-seedream-5-0"}, "不是 GPT Image"),
            ({**base, "prompt": ""}, "prompt"),
            ({**base, "api_key": ""}, "API key"),
            ({**base, "base_url": ""}, "提供商地址"),
            ({**base, "ratio": "auto"}, "不受支持"),
            ({**base, "ratio": "custom"}, "不受支持"),
            ({**base, "k": "1.5K"}, "分辨率"),
            ({**base, "n": 9}, "数量"),
        ]
        for payload, expected in cases:
            with self.subTest(expected=expected):
                response = self.client.post("/api/generate", json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertIn(expected, response.get_json()["error"])

    def test_empty_or_null_data_returns_502(self):
        for payload in ({"data": []}, {"data": None}, {}):
            with self.subTest(payload=payload):
                with mock.patch.object(
                    gpt_app.HTTP, "post", return_value=FakeResponse(payload=payload)
                ):
                    response = self.client.post(
                        "/api/generate",
                        json={
                            "model": "gpt-image-2",
                            "prompt": "p",
                            "base_url": "https://example.com/v1",
                            "api_key": "key",
                            "ratio": "1:1",
                            "k": "1K",
                        },
                    )
                self.assertEqual(response.status_code, 502)
                self.assertFalse(response.get_json()["ok"])

    def test_upstream_error_is_summarized(self):
        upstream = FakeResponse(
            status_code=401,
            payload={"error": {"message": "invalid_api_key", "code": "invalid_api_key"}},
        )
        with mock.patch.object(gpt_app.HTTP, "post", return_value=upstream):
            response = self.client.post(
                "/api/generate",
                json={
                    "model": "gpt-image-2",
                    "prompt": "p",
                    "base_url": "https://example.com/v1",
                    "api_key": "bad",
                    "ratio": "1:1",
                    "k": "1K",
                },
            )
        self.assertEqual(response.status_code, 502)
        self.assertIn("认证失败", response.get_json()["error"])

    # ---------------- 异步任务 ----------------
    def _run_task_to_end(self, task_id, limit=200):
        state = None
        for _ in range(limit):
            payload = self.client.get(f"/api/tasks/{task_id}").get_json()
            state = payload["state"]
            if state in gpt_app.TASK_TERMINAL:
                return payload
            time.sleep(0.02)
        self.fail(f"task did not finish, last state={state}")

    def test_async_task_submits_and_polls_to_completion(self):
        polls = {"count": 0}

        def fake_post(url, **kwargs):
            return FakeResponse(
                status_code=202,
                payload={
                    "status": "pending",
                    "task_id": "task_123",
                    "status_url": "https://example.com/v1/images/generations/task_123",
                },
            )

        def fake_get(url, **kwargs):
            if "images/generations/task_123" in url:
                polls["count"] += 1
                if polls["count"] == 1:
                    return FakeResponse(
                        payload={"status": "processing", "progress": 40, "data": []}
                    )
                return FakeResponse(
                    payload={
                        "status": "completed",
                        "progress": 100,
                        "data": [
                            {"b64_json": base64.b64encode(png_bytes()).decode("ascii")}
                        ],
                    }
                )
            raise AssertionError(url)

        with mock.patch.object(gpt_app.HTTP, "post", side_effect=fake_post), mock.patch.object(
            gpt_app.HTTP, "get", side_effect=fake_get
        ):
            created = self.client.post(
                "/api/tasks",
                json={
                    "model": "gpt-image-2",
                    "prompt": "async test",
                    "base_url": "https://example.com/v1",
                    "api_key": "key",
                    "ratio": "1:1",
                    "k": "1K",
                },
            )
            self.assertEqual(created.status_code, 202)
            task_id = created.get_json()["task_id"]
            self.assertTrue(task_id)
            final = self._run_task_to_end(task_id)

        self.assertEqual(final["state"], "completed")
        self.assertEqual(final["progress"], 100)
        self.assertEqual(len(final["images"]), 1)
        self.assertGreaterEqual(polls["count"], 2)
        history = self.client.get("/api/history").get_json()["items"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["model"], "gpt-image-2")

    def test_async_task_reports_failure(self):
        def fake_post(url, **kwargs):
            return FakeResponse(
                status_code=202,
                payload={
                    "task_id": "t_fail",
                    "status_url": "https://example.com/v1/images/generations/t_fail",
                },
            )

        def fake_get(url, **kwargs):
            return FakeResponse(
                payload={"status": "failed", "error": {"message": "提示词被审核拦截"}}
            )

        with mock.patch.object(gpt_app.HTTP, "post", side_effect=fake_post), mock.patch.object(
            gpt_app.HTTP, "get", side_effect=fake_get
        ):
            task_id = self.client.post(
                "/api/tasks",
                json={
                    "model": "gpt-image-2",
                    "prompt": "p",
                    "base_url": "https://example.com/v1",
                    "api_key": "key",
                    "ratio": "1:1",
                    "k": "1K",
                },
            ).get_json()["task_id"]
            final = self._run_task_to_end(task_id)

        self.assertEqual(final["state"], "failed")
        self.assertIn("审核", final["error"])
        self.assertEqual(self.client.get("/api/history").get_json()["items"], [])

    def test_async_task_missing_status_url_uses_task_id_fallback(self):
        seen = []

        def fake_post(url, **kwargs):
            return FakeResponse(status_code=202, payload={"task_id": "only_id"})

        def fake_get(url, **kwargs):
            seen.append(url)
            return FakeResponse(
                payload={
                    "status": "completed",
                    "data": [{"b64_json": base64.b64encode(png_bytes()).decode("ascii")}],
                }
            )

        with mock.patch.object(gpt_app.HTTP, "post", side_effect=fake_post), mock.patch.object(
            gpt_app.HTTP, "get", side_effect=fake_get
        ):
            task_id = self.client.post(
                "/api/tasks",
                json={
                    "model": "gpt-image-2",
                    "prompt": "p",
                    "base_url": "https://example.com/v1",
                    "api_key": "key",
                    "ratio": "1:1",
                    "k": "1K",
                },
            ).get_json()["task_id"]
            final = self._run_task_to_end(task_id)

        self.assertEqual(final["state"], "completed")
        self.assertTrue(seen and seen[0].endswith("/images/generations/only_id"))

    def test_async_task_times_out(self):
        gpt_app.TASK_BUDGET_SECONDS = 0.05

        def fake_post(url, **kwargs):
            return FakeResponse(
                status_code=202,
                payload={
                    "task_id": "slow",
                    "status_url": "https://example.com/v1/images/generations/slow",
                },
            )

        def fake_get(url, **kwargs):
            return FakeResponse(payload={"status": "processing", "progress": 10})

        with mock.patch.object(gpt_app.HTTP, "post", side_effect=fake_post), mock.patch.object(
            gpt_app.HTTP, "get", side_effect=fake_get
        ):
            task_id = self.client.post(
                "/api/tasks",
                json={
                    "model": "gpt-image-2",
                    "prompt": "p",
                    "base_url": "https://example.com/v1",
                    "api_key": "key",
                    "ratio": "1:1",
                    "k": "1K",
                },
            ).get_json()["task_id"]
            final = self._run_task_to_end(task_id)

        self.assertEqual(final["state"], "failed")
        self.assertIn("超时", final["error"])

    def test_task_can_be_cancelled_and_stops_polling(self):
        polls = {"count": 0}

        def fake_post(url, **kwargs):
            return FakeResponse(
                status_code=202,
                payload={
                    "task_id": "c1",
                    "status_url": "https://example.com/v1/images/generations/c1",
                },
            )

        def fake_get(url, **kwargs):
            polls["count"] += 1
            return FakeResponse(payload={"status": "processing", "progress": 20})

        with mock.patch.object(gpt_app.HTTP, "post", side_effect=fake_post), mock.patch.object(
            gpt_app.HTTP, "get", side_effect=fake_get
        ):
            task_id = self.client.post(
                "/api/tasks",
                json={
                    "model": "gpt-image-2",
                    "prompt": "p",
                    "base_url": "https://example.com/v1",
                    "api_key": "key",
                    "ratio": "1:1",
                    "k": "1K",
                },
            ).get_json()["task_id"]
            time.sleep(0.1)
            cancelled = self.client.delete(f"/api/tasks/{task_id}")
            self.assertEqual(cancelled.status_code, 200)
            self.assertEqual(cancelled.get_json()["state"], gpt_app.TASK_CANCELLED)
            settled = self._run_task_to_end(task_id)
            before = polls["count"]
            time.sleep(0.2)

        self.assertEqual(settled["state"], gpt_app.TASK_CANCELLED)
        self.assertEqual(polls["count"], before)
        self.assertEqual(self.client.get("/api/history").get_json()["items"], [])

    def test_task_lookup_and_cancel_unknown_id(self):
        self.assertEqual(self.client.get("/api/tasks/missing").status_code, 404)
        self.assertEqual(self.client.delete("/api/tasks/missing").status_code, 404)

    def test_task_cleanup_removes_expired_terminal_tasks(self):
        gpt_app.clear_tasks()
        with mock.patch.object(gpt_app.HTTP, "post", side_effect=lambda *a, **k: self._sync_response()):
            task_id = self.client.post(
                "/api/tasks",
                json={
                    "model": "gpt-image-2",
                    "prompt": "p",
                    "base_url": "https://example.com/v1",
                    "api_key": "key",
                    "ratio": "1:1",
                    "k": "1K",
                },
            ).get_json()["task_id"]
            final = self._run_task_to_end(task_id)
        self.assertEqual(final["state"], "completed")

        with gpt_app.TASKS_LOCK:
            gpt_app.TASKS[task_id]["updated_at"] = time.time() - gpt_app.TASK_TTL_SECONDS - 5
        gpt_app._cleanup_tasks()
        self.assertEqual(self.client.get(f"/api/tasks/{task_id}").status_code, 404)

    def test_clear_tasks_empties_registry(self):
        gpt_app._new_task("m", "p", 1, "1024x1024", "standard")
        self.assertTrue(gpt_app.TASKS)
        gpt_app.clear_tasks()
        self.assertEqual(gpt_app.TASKS, {})

    # ---------------- 图生图 ----------------
    def test_reference_uses_chat_completions_with_data_urls(self):
        captured = {}
        raw = png_bytes()

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            return FakeResponse(
                payload={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "![image](https://cdn.example.com/out.png)",
                            }
                        }
                    ]
                }
            )

        def fake_get(url, **kwargs):
            captured["download"] = url
            return FakeResponse(content=raw, headers={"Content-Type": "image/png"})

        with mock.patch.object(gpt_app.HTTP, "post", side_effect=fake_post), mock.patch.object(
            gpt_app.HTTP, "get", side_effect=fake_get
        ):
            response = self.client.post(
                "/api/generate",
                data={
                    "model": "gpt-image-2",
                    "prompt": "换成雪景",
                    "base_url": "https://example.com/v1",
                    "api_key": "key",
                    "ratio": "1:1",
                    "k": "1K",
                    "n": "4",
                    "image": (io.BytesIO(raw), "ref.png"),
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(captured["url"].endswith("/chat/completions"))
        body = captured["json"]
        self.assertEqual(body["model"], "gpt-image-2")
        self.assertFalse(body["stream"])
        self.assertEqual(body["size"], "1024x1024")
        self.assertEqual(body["quality"], "standard")
        self.assertNotIn("n", body)
        content = body["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "换成雪景"})
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(response.get_json()["count"], 1)
        self.assertTrue(response.get_json()["meta"]["reference"])

    def test_reference_falls_back_to_images_edits(self):
        calls = []
        raw = png_bytes()

        def fake_post(url, **kwargs):
            calls.append(url)
            if url.endswith("/chat/completions"):
                return FakeResponse(
                    status_code=404, payload={"error": {"message": "not found"}}
                )
            return self._sync_response()

        with mock.patch.object(gpt_app.HTTP, "post", side_effect=fake_post):
            response = self.client.post(
                "/api/generate",
                data={
                    "model": "gpt-image-2",
                    "prompt": "p",
                    "base_url": "https://example.com/v1",
                    "api_key": "key",
                    "ratio": "1:1",
                    "k": "1K",
                    "image": (io.BytesIO(raw), "ref.png"),
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [url.rsplit("/v1", 1)[1] for url in calls],
            ["/chat/completions", "/images/edits"],
        )

    def test_sunburst_prefers_edits_transport(self):
        calls = []
        raw = png_bytes()

        def fake_post(url, **kwargs):
            calls.append(url)
            return self._sync_response()

        with mock.patch.object(gpt_app.HTTP, "post", side_effect=fake_post):
            response = self.client.post(
                "/api/generate",
                data={
                    "model": "gpt-image-2.5-sunburst",
                    "prompt": "p",
                    "base_url": "https://example.com/v1",
                    "api_key": "key",
                    "ratio": "1:1",
                    "k": "1K",
                    "edit_transport": "edits",
                    "image": (io.BytesIO(raw), "ref.png"),
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].endswith("/images/edits"))

    def test_reference_count_is_limited(self):
        raw = png_bytes()
        response = self.client.post(
            "/api/generate",
            data={
                "model": "gpt-image-2",
                "prompt": "p",
                "base_url": "https://example.com/v1",
                "api_key": "key",
                "ratio": "1:1",
                "k": "1K",
                "image": [
                    (io.BytesIO(raw), f"ref-{index}.png") for index in range(5)
                ],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("4 张参考图", response.get_json()["error"])

    def test_chat_reference_can_return_image_url_list(self):
        raw = png_bytes()
        captured = {}

        def fake_post(url, **kwargs):
            return FakeResponse(
                payload={
                    "choices": [
                        {
                            "message": {
                                "content": [
                                    {
                                        "type": "image_url",
                                        "image_url": {"url": "https://cdn.example.com/x.png"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            )

        def fake_get(url, **kwargs):
            captured["download"] = url
            return FakeResponse(content=raw, headers={"Content-Type": "image/png"})

        with mock.patch.object(gpt_app.HTTP, "post", side_effect=fake_post), mock.patch.object(
            gpt_app.HTTP, "get", side_effect=fake_get
        ):
            response = self.client.post(
                "/api/generate",
                data={
                    "model": "gpt-image-2",
                    "prompt": "p",
                    "base_url": "https://example.com/v1",
                    "api_key": "key",
                    "ratio": "1:1",
                    "k": "1K",
                    "image": (io.BytesIO(raw), "ref.png"),
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["download"], "https://cdn.example.com/x.png")

    def test_sse_chat_response_is_parsed(self):
        raw = png_bytes()
        sse = "\n".join(
            [
                'data: {"choices":[{"delta":{"content":"![i](https://cdn.example.com/s.png)"}}]}',
                "data: [DONE]",
            ]
        )

        def fake_post(url, **kwargs):
            return FakeResponse(
                text=sse,
                headers={"Content-Type": "text/event-stream"},
            )

        def fake_get(url, **kwargs):
            return FakeResponse(content=raw, headers={"Content-Type": "image/png"})

        with mock.patch.object(gpt_app.HTTP, "post", side_effect=fake_post), mock.patch.object(
            gpt_app.HTTP, "get", side_effect=fake_get
        ):
            response = self.client.post(
                "/api/generate",
                data={
                    "model": "gpt-image-2",
                    "prompt": "p",
                    "base_url": "https://example.com/v1",
                    "api_key": "key",
                    "ratio": "1:1",
                    "k": "1K",
                    "image": (io.BytesIO(raw), "ref.png"),
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["count"], 1)

    # ---------------- 历史与下载 ----------------
    def test_history_accepts_legacy_records(self):
        gpt_app._save_history(
            [{"id": "1", "filename": "a.png", "model": "gpt-image-2", "prompt": "x"}]
        )
        payload = self.client.get("/api/history").get_json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(self.client.delete("/api/history").status_code, 200)
        self.assertEqual(self.client.get("/api/history").get_json()["count"], 0)

    def test_download_returns_404_for_missing_file(self):
        self.assertEqual(self.client.get("/api/download/nope.png").status_code, 404)

    def test_download_serves_generated_file(self):
        target = gpt_app.OUTPUT_DIR / "ok.png"
        target.write_bytes(png_bytes())
        response = self.client.get("/api/download/ok.png")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])


class HelperTests(unittest.TestCase):
    def test_base_url_normalization(self):
        self.assertEqual(gpt_app.resolve_base_url(" https://x/v1/ "), "https://x/v1")
        self.assertEqual(gpt_app.resolve_base_url("x/v1"), "https://x/v1")
        self.assertEqual(gpt_app.resolve_base_url(""), "")
        self.assertEqual(gpt_app.resolve_base_url(None), "")

    def test_image_extension_detection(self):
        self.assertEqual(gpt_app._image_extension(png_bytes()), ".png")
        self.assertEqual(gpt_app._image_extension(b"GIF89a...."), ".gif")
        self.assertEqual(gpt_app._image_extension(b"\xff\xd8\xffabc"), ".jpg")
        self.assertEqual(gpt_app._image_extension(b"unknown"), ".png")

    def test_decode_base64_accepts_data_url(self):
        raw = png_bytes()
        encoded = base64.b64encode(raw).decode("ascii")
        self.assertEqual(gpt_app._decode_base64_image(encoded), raw)
        self.assertEqual(
            gpt_app._decode_base64_image(f"data:image/png;base64,{encoded}"), raw
        )

    def test_status_normalization(self):
        self.assertEqual(gpt_app._normalize_status("processing"), gpt_app.TASK_RUNNING)
        self.assertEqual(gpt_app._normalize_status("COMPLETED"), gpt_app.TASK_DONE)
        self.assertEqual(gpt_app._normalize_status("failed"), gpt_app.TASK_FAILED)
        self.assertEqual(gpt_app._normalize_status("weird"), "")

    def test_chat_text_image_extraction(self):
        found = gpt_app._images_from_chat_text(
            "here ![a](https://cdn.example.com/a.png) done"
        )
        self.assertEqual(found, [{"url": "https://cdn.example.com/a.png"}])
        self.assertEqual(gpt_app._images_from_chat_text("no image"), [])

    def test_build_request_spec_carries_transport_hint(self):
        spec, error = gpt_app.build_request_spec(
            {
                "model": "gpt-image-2.5-sunburst",
                "prompt": "p",
                "base_url": "https://x/v1",
                "api_key": "k",
                "ratio": "16:9",
                "k": "1K",
                "edit_transport": "edits",
            }
        )
        self.assertIsNone(error)
        self.assertEqual(spec["edit_transport"], "edits")
        self.assertEqual(spec["size"], "1280x720")

    def test_build_request_spec_reports_error_tuple(self):
        spec, error = gpt_app.build_request_spec(
            {"model": "dall-e-3", "prompt": "p", "base_url": "https://x/v1", "api_key": "k"}
        )
        self.assertIsNone(spec)
        payload, status = error
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])




    def test_elapsed_progress_is_estimated_when_upstream_omits_it(self):
        """同步型中转站不回报百分比时，用已用时间给出可读进度。"""
        gpt_app.TASK_EXPECTED_SECONDS = 10
        task = gpt_app._new_task("gpt-image-2", "p", 1, "1024x1024", "standard")
        try:
            task["state"] = gpt_app.TASK_RUNNING
            task["progress"] = 1
            task["created_at"] = time.time() - 5  # 5s of a 10s expectation
            payload = gpt_app._task_public(task)
            self.assertEqual(payload["state"], gpt_app.TASK_RUNNING)
            self.assertGreaterEqual(payload["progress"], 40)
            self.assertLessEqual(payload["progress"], 60)

            # 上游明确回报进度时不要被估算覆盖。
            task["progress"] = 77
            self.assertEqual(gpt_app._task_public(task)["progress"], 77)

            # 终态始终显示 100。
            task["state"] = gpt_app.TASK_DONE
            task["progress"] = 100
            self.assertEqual(gpt_app._task_public(task)["progress"], 100)
        finally:
            gpt_app.clear_tasks()

    def test_estimated_progress_is_capped(self):
        task = gpt_app._new_task("gpt-image-2", "p", 1, "1024x1024", "standard")
        try:
            task["state"] = gpt_app.TASK_RUNNING
            task["created_at"] = time.time() - 100000
            self.assertEqual(gpt_app._estimated_progress(task), 95)
        finally:
            gpt_app.clear_tasks()

if __name__ == "__main__":
    unittest.main()