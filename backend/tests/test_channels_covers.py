import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.channels_covers import (
    CoverError, _safe_platform_image_url, image_signature,
    prepare_custom_cover, signatures_match, download_platform_image,
)
from app.channels_service import ChannelsService
from app.channels_internal_api import ChannelsInternalPublisher, ChannelsInternalApiError


class CoverTests(unittest.TestCase):
    def test_matching_requires_colour_and_structure_and_complete_signatures(self):
        base = {"rgb": [80] * 768, "edges": [False] * 64}
        self.assertTrue(signatures_match(base, {"rgb": [82] * 768, "edges": [False] * 64}))
        self.assertFalse(signatures_match(base, {"rgb": [180] * 768, "edges": [False] * 64}))
        self.assertFalse(signatures_match(base, {"rgb": [80] * 768, "edges": [True] * 64}))
        self.assertFalse(signatures_match({}, {}))

    def test_remote_cover_download_rejects_non_platform_urls_and_redirects(self):
        self.assertTrue(_safe_platform_image_url("https://finder.video.qq.com/test?a=private"))
        for url in ("http://finder.video.qq.com/test", "https://qq.com.evil.test/x",
                    "https://127.0.0.1/x", "https://evil@qq.com/x", "file:///x",
                    "https://finder.video.qq.com:444/x"):
            self.assertFalse(_safe_platform_image_url(url))
        response = MagicMock(status_code=302, headers={"Location": "http://169.254.169.254/"})
        response.__enter__.return_value = response
        with patch("app.channels_covers.requests.get", return_value=response) as get:
            with self.assertRaises(CoverError):
                download_platform_image("https://finder.video.qq.com/test", Path("unused.jpg"))
            get.assert_called_once()

    def test_missing_profile_cover_never_falls_back_to_video_thumbnail(self):
        with patch("app.channels_covers.verify_uploaded_cover") as verify:
            result = ChannelsService()._verify_selected_cover({"desc": {"media": [{"thumbUrl": "https://finder.video.qq.com/frame.jpg"}]}}, "selected")
            self.assertEqual(result["cover_status"], "unverified")
            verify.assert_not_called()

    def test_actual_profile_picture_difference_is_reported_without_publication(self):
        svc = ChannelsService()
        record = {"desc": {"media": [{"coverUrl": "https://finder.video.qq.com/profile.jpg", "fullCoverUrl": "https://finder.video.qq.com/full.jpg"}]}}
        with patch("app.channels_service.oss_service.url_for", return_value="https://storage.invalid/selected"), \
             patch.object(svc, "_download_with_resume"), \
             patch("app.channels_covers.prepare_custom_cover", return_value=(Path("full.jpg"), Path("profile.jpg"))), \
             patch("app.channels_covers.verify_uploaded_cover", return_value=False):
            self.assertEqual(svc._verify_selected_cover(record, "selected")["cover_status"], "mismatch")
        with patch("app.channels_service.oss_service.url_for", return_value="https://storage.invalid/selected"), \
             patch.object(svc, "_download_with_resume"), \
             patch("app.channels_covers.prepare_custom_cover", return_value=(Path("full.jpg"), Path("profile.jpg"))), \
             patch("app.channels_covers.verify_uploaded_cover", side_effect=CoverError("unavailable")):
            self.assertEqual(svc._verify_selected_cover(record, "selected")["cover_status"], "unverified")

    def test_duplicate_batch_titles_do_not_select_an_arbitrary_work(self):
        rows = [{"objectId": n, "exportId": n, "desc": {"description": "同一个自动混剪标题"}} for n in ("one", "two")]
        self.assertIsNone(ChannelsService._matching_post_api_record(rows, ["同一个自动混剪标题"], ""))
        self.assertEqual(ChannelsService._matching_post_api_record(rows, ["同一个自动混剪标题"], "two")["objectId"], "two")

    def test_cover_verification_failure_stops_before_post_create_and_checkpoint(self):
        client = MagicMock()
        publisher = ChannelsInternalPublisher(client=client)
        uploader = MagicMock()
        uploader.upload.return_value = {"url": "https://finder.video.qq.com/upload"}
        checkpoint = MagicMock()
        with patch("app.channels_internal_api.probe_video", return_value={"width": 1080, "height": 1920, "duration": 8, "file_size": 10}), \
             patch("app.channels_internal_api.ChannelsCdnUploader", return_value=uploader), \
             patch("app.channels_internal_api.prepare_custom_cover", return_value=(Path("full.jpg"), Path("profile.jpg"))), \
             patch("app.channels_internal_api.verify_uploaded_cover", return_value=False):
            with self.assertRaises(ChannelsInternalApiError) as error:
                publisher.publish(video_path=Path("video.mp4"), cover_path=Path("selected.jpg"), custom_cover=True,
                                  title="封面校验测试", description="", tags=[], mark_submitted=checkpoint,
                                  preflight_state={"identity": {"external_account_id": "a"}, "finder_id": "a", "upload_params": {}, "tag_info": {}, "trace_key": "trace"})
        self.assertEqual(error.exception.stage, "set_cover")
        checkpoint.assert_not_called()
        client.call.assert_not_called()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required; runs in Linux candidate")
    def test_real_normalization_and_compressed_image_matching(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            original = directory / "selected.ppm"
            pixels = bytes(v for y in range(120) for x in range(120) for v in (x * 2, y * 2, 90))
            original.write_bytes(b"P6\n120 120\n255\n" + pixels)
            full, profile = prepare_custom_cover(original, directory)
            self.assertEqual(original.read_bytes(), b"P6\n120 120\n255\n" + pixels)
            recompressed = directory / "cdn.jpg"
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(profile), "-vf", "scale=480:640", "-q:v", "5", str(recompressed)], check=True, capture_output=True, timeout=30)
            self.assertTrue(signatures_match(image_signature(profile), image_signature(recompressed)))
            invalid = directory / "invalid.jpg"
            invalid.write_bytes(b"not an image")
            with self.assertRaises(CoverError):
                prepare_custom_cover(invalid, directory)
