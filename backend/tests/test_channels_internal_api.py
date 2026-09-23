import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from app.channels_internal_api import (
    BrowserJsonClient,
    ChannelsCdnUploader,
    ChannelsInternalApiError,
    ChannelsInternalPublisher,
    ChannelsPublishUncertain,
    target_size,
    _annotation_info,
    ANNOTATION_TYPES,
)


def response(payload, status=200):
    item = MagicMock()
    item.json.return_value = payload
    item.status_code = status
    item.raise_for_status.side_effect = None
    return item


class ChannelsInternalApiTests(unittest.TestCase):
    def test_current_platform_shared_annotation_key_and_legacy_inline_key(self):
        for annotation, tag_type in ANNOTATION_TYPES.items():
            with self.subTest(annotation=annotation):
                payload = {'errCode':0,'data':{'tags':[{'tagType':0}, {'tagType':tag_type}], 'tagKey':'fresh-list-key'}}
                self.assertEqual(_annotation_info(payload, annotation), {'tagType':tag_type,'tagKey':'fresh-list-key'})
                self.assertEqual(_annotation_info({'data':{'tags':[{'tagType':tag_type,'tagKey':'inline-key'}]}},annotation)['tagKey'],'inline-key')

    def test_annotation_rejects_missing_key_or_unoffered_type_and_cross_group_key(self):
        for payload in ({'data':{'tags':[{'tagType':1}]}},
                        {'data':{'tags':[{'tagType':2}],'tagKey':'fresh-key'}},
                        {'groups':[{'tags':[{'tagType':1}]},{'tagKey':'unrelated-key'}]}):
            with self.subTest(payload=payload), self.assertRaises(ChannelsInternalApiError):
                _annotation_info(payload,'ai_generated')

    def test_nested_base_response_failure_is_not_hidden_by_top_level_success(self):
        page = MagicMock()
        page.evaluate.return_value = {
            "httpStatus": 200,
            "json": True,
            "data": {"errCode": 0, "data": {"baseResp": {"ret": 9001, "errMsg": "rejected"}}},
        }
        with self.assertRaises(ChannelsInternalApiError) as caught:
            BrowserJsonClient(page).call("/auth/auth_data", {})
        self.assertIn("9001", str(caught.exception))

    def test_side_effect_network_failure_is_result_uncertain(self):
        page = MagicMock()
        page.evaluate.return_value = {"networkError": "connection reset"}
        with self.assertRaises(ChannelsPublishUncertain):
            BrowserJsonClient(page).call(
                "/post/post_create",
                {},
                micro=True,
                side_effect=True,
                stage="submit_publish",
            )

    def test_cdn_uses_real_part_lengths_and_retries_only_failed_part(self):
        params = {
            "authKey": "secret",
            "uin": "100",
            "videoFileType": 20302,
            "cdnHostList": ["finder.video.qq.com"],
        }
        uploader = ChannelsCdnUploader(params, chunk_size=6, part_retries=3)
        session = MagicMock()
        session.put.side_effect = [
            response({"UploadID": "upload-1"}),
            requests.ConnectionError("reset"),
            response({"ETag": "etag-1", "TransFlag": "flag-1"}),
            response({"ETag": "etag-2", "TransFlag": "flag-2"}),
        ]
        session.post.return_value = response({"DownloadURL": "http://wxapp.tc.qq.com/file.mp4"})
        uploader.session = session
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ten.bin"
            path.write_bytes(b"0123456789")
            result = uploader.upload(path, "videoFileType")

        apply_call = session.put.call_args_list[0]
        self.assertEqual(apply_call.kwargs["json"]["BlockPartLength"], [6, 4])
        self.assertEqual(session.put.call_count, 4)
        first_successful_part = session.put.call_args_list[2]
        self.assertEqual(
            first_successful_part.kwargs["headers"]["Content-MD5"],
            hashlib.md5(b"012345").hexdigest(),
        )
        self.assertEqual(
            session.post.call_args.kwargs["json"],
            {
                "TransFlag": "flag-2",
                "PartInfo": [
                    {"PartNumber": 1, "ETag": "etag-1"},
                    {"PartNumber": 2, "ETag": "etag-2"},
                ],
            },
        )
        self.assertEqual(result["url"], "https://finder.video.qq.com/file.mp4")

    def test_account_mismatch_stops_before_upload(self):
        publisher = ChannelsInternalPublisher(MagicMock())
        publisher.client = MagicMock()
        publisher.client.call.return_value = {
            "errCode": 0,
            "data": {"finderUser": {"finderUsername": "actual-account"}},
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "app.channels_internal_api.ChannelsCdnUploader"
        ) as uploader:
            video = Path(temp_dir) / "video.mp4"
            cover = Path(temp_dir) / "cover.jpg"
            video.write_bytes(b"video")
            cover.write_bytes(b"cover")
            with self.assertRaises(ChannelsInternalApiError) as caught:
                publisher.publish(
                    video_path=video,
                    cover_path=cover,
                    expected_external_account_id="expected-account",
                    title="title",
                    description="description",
                    tags=[],
                )
        self.assertEqual(caught.exception.code, "ACCOUNT_ID_MISMATCH")
        uploader.assert_not_called()

    def test_post_create_is_called_once_and_only_after_durable_checkpoint(self):
        publisher = ChannelsInternalPublisher(MagicMock(), clip_timeout=30)
        calls = []

        def api_call(path, body, **kwargs):
            if path == "/auth/auth_data":
                return {"errCode": 0, "data": {"finderUser": {"finderUsername": "finder-1"}, "envInfo": {"cdnHost": "finder.video.qq.com"}}}
            if path == "/helper/helper_upload_params":
                return {"errCode": 0, "data": {"authKey": "secret", "uin": "1", "videoFileType": 1, "pictureFileType": 2}}
            if path == "/post/get-finder-post-trace-key":
                return {"errCode": 0, "data": {"traceKey": "trace-1"}}
            if path == "/post/finder_get_object_tag_list":
                return {"errCode":0,"data":{"tags":[{"tagType":1}],"tagKey":"fresh-key"}}
            if path == "/post/check_window_limit_status":
                return {"errCode":0,"data":{"reachLimit":0}}
            if path == "/post/post_clip_video":
                return {"errCode": 0, "data": {"draftId": "draft-1", "clipKey": "clip-1"}}
            if path == "/post/post_clip_video_result":
                return {"errCode": 0, "data": {"flag": 1, "url": "https://finder.video.qq.com/final.mp4", "width": 1080, "height": 1920, "thumbUrl":"https://finder.video.qq.com/auto.jpg", "coverUrl":"https://finder.video.qq.com/auto.jpg"}}
            if path == "/post/post_create":
                calls.append(("post", body["clientid"]))
                return {"errCode": 0, "data": {}}
            raise AssertionError(path)

        publisher.client = MagicMock()
        publisher.client.call.side_effect = api_call
        uploader = MagicMock()
        uploader.upload.side_effect = [
            {"url": "https://finder.video.qq.com/video.mp4", "file_size": 10, "task_id": "video-task", "md5sum": "video-task"},
            {"url": "https://finder.video.qq.com/full-cover.jpg", "file_size": 5},
            {"url": "https://finder.video.qq.com/profile-cover.jpg", "file_size": 5},
        ]
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "app.channels_internal_api.probe_video",
            return_value={"width": 1080, "height": 1922, "duration": 12.4, "file_size": 10},
        ), patch("app.channels_internal_api.ChannelsCdnUploader", return_value=uploader), patch(
            "app.channels_internal_api.prepare_custom_cover", return_value=(Path("full.jpg"), Path("profile.jpg")),
        ), patch("app.channels_internal_api.verify_uploaded_cover", return_value=True) as verify_cover:
            video = Path(temp_dir) / "video.mp4"
            cover = Path(temp_dir) / "cover.jpg"
            video.write_bytes(b"video")
            cover.write_bytes(b"cover")

            def checkpoint(client_id):
                calls.append(("checkpoint", client_id))

            result = publisher.publish(
                video_path=video,
                cover_path=cover,
                expected_external_account_id="finder-1",
                title="测试标题",
                description="",
                tags=["护肤"],
                custom_cover=True,
                product_id="test-product",
                product_name="测试商品",
                video_annotation="ai_generated",
                mark_submitted=checkpoint,
            )

        self.assertEqual([name for name, _value in calls], ["checkpoint", "post"])
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertEqual(result["accepted"], True)
        post_calls = [item for item in publisher.client.call.call_args_list if item.args[0] == "/post/post_create"]
        self.assertEqual(len(post_calls), 1)
        body = post_calls[0].args[1]
        self.assertEqual(body['objectDesc']['shortTitle'], [{'shortTitle':'测试标题'}])
        self.assertEqual(body['objectDesc']['description'], '测试标题 #护肤')
        self.assertEqual(body['objectDesc']['component']['id'], 'test-product')
        self.assertEqual(body['tagInfo'], {'tagType':1,'tagKey':'fresh-key'})
        self.assertEqual(verify_cover.call_count, 2)
        self.assertEqual(body['objectDesc']['media'][0]['thumbUrl'], 'https://finder.video.qq.com/auto.jpg')
        self.assertEqual(body['objectDesc']['media'][0]['coverUrl'], 'https://finder.video.qq.com/profile-cover.jpg')
        self.assertEqual(body['objectDesc']['media'][0]['fullCoverUrl'], 'https://finder.video.qq.com/full-cover.jpg')

    def test_target_size_preserves_bounds_and_even_dimensions(self):
        self.assertEqual(target_size(1080, 1922), (1078, 1920))
        self.assertEqual(target_size(1920, 1080), (1920, 1080))
        width, height = target_size(4000, 3000)
        self.assertLessEqual(max(width, height), 1920)
        self.assertLessEqual(min(width, height), 1080)
        self.assertEqual(width % 2, 0)
        self.assertEqual(height % 2, 0)


if __name__ == "__main__":
    unittest.main()
