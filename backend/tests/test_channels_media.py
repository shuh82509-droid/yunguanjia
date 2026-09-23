import errno
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.channels_media import MediaCommandError, run_media
from app.channels_internal_api import probe_video, extract_cover, ChannelsInternalApiError
from app.channels_covers import _run, CoverError


class MediaTests(unittest.TestCase):
    def test_busy_command_retries_once_and_never_logs_secret_stderr(self):
        failure = subprocess.CalledProcessError(1, ['ffprobe', 'private-path'], stderr=b'pthread_create failed secret-url')
        with patch('app.channels_media.subprocess.run', side_effect=[failure, Mock(stdout=b'ok')]) as run, \
             patch('app.channels_media.time.sleep'), self.assertLogs('app.channels_media') as logs:
            self.assertEqual(run_media(['ffprobe', 'private-path']), b'ok')
        self.assertEqual(run.call_count, 2)
        self.assertNotIn('secret-url', ''.join(logs.output))
        self.assertNotIn('private-path', ''.join(logs.output))

    def test_timeout_resource_and_missing_tool_have_distinct_safe_errors(self):
        for error, code, count in [(subprocess.TimeoutExpired('ffprobe',60), 'MEDIA_TIMEOUT',2),
                                   (BlockingIOError(errno.EAGAIN,'busy'), 'MEDIA_BUSY',2),
                                   (FileNotFoundError('private tool path'), 'MEDIA_TOOL_MISSING',1),
                                   (subprocess.CalledProcessError(1,'ffprobe',stderr=b'Invalid data private'), 'MEDIA_INVALID',1)]:
            with self.subTest(code=code), patch('app.channels_media.subprocess.run',side_effect=error) as run, \
                 patch('app.channels_media.time.sleep'), self.assertRaises(MediaCommandError) as caught:
                run_media(['ffprobe', 'source'])
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(run.call_count, count)
        with patch('app.channels_media.subprocess.run',return_value=Mock(stdout=b'released')):
            self.assertEqual(run_media(['ffprobe']),b'released')

    def test_probe_validates_duration_and_uses_stream_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'video.mp4'; path.write_bytes(b'video')
            for format_duration in ('N/A', 'NaN', 'inf', 0, None):
                payload={'streams':[{'width':1080,'height':1920,'duration':'82.8'}],'format':{'duration':format_duration}}
                with patch('app.channels_internal_api.run_media',return_value=json.dumps(payload).encode()) as run:
                    self.assertEqual(probe_video(path)['duration'],82.8)
                    command=run.call_args.args[0]
                    self.assertEqual(command[command.index('-threads')+1],'1')
            for payload in ({}, {'streams':[{'width':0,'height':1920}],'format':{'duration':12}},
                            {'streams':[{'width':1080,'height':1920}],'format':{'duration':'NaN'}}):
                with patch('app.channels_internal_api.run_media',return_value=json.dumps(payload).encode()), self.assertRaises(ChannelsInternalApiError):
                    probe_video(path)

    def test_busy_probe_is_not_reported_as_invalid_video(self):
        with patch('app.channels_internal_api.run_media',side_effect=MediaCommandError('MEDIA_TIMEOUT',transient=True)), \
             self.assertRaises(ChannelsInternalApiError) as caught:
            probe_video(Path('source.mp4'))
        self.assertIn('暂忙或超时',str(caught.exception))
        self.assertFalse(caught.exception.definitive)
        self.assertEqual(caught.exception.code,'MEDIA_TIMEOUT')

    def test_header_probe_falls_back_only_for_missing_metadata(self):
        valid=json.dumps({'streams':[{'width':1080,'height':1920}],'format':{'duration':'77.485'}}).encode()
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'hevc.mp4';path.write_bytes(b'original')
            with patch('app.channels_internal_api.run_media',return_value=valid) as run:
                self.assertEqual(probe_video(path)['duration'],77.485)
                run.assert_called_once()
                self.assertIn('-nofind_stream_info',run.call_args.args[0])
            with patch('app.channels_internal_api.run_media',side_effect=[b'{}',valid]) as run:
                self.assertEqual(probe_video(path)['width'],1080)
                self.assertEqual(run.call_count,2)
                self.assertNotIn('-nofind_stream_info',run.call_args.args[0])

    def test_cover_commands_bound_input_output_and_filter_threads(self):
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/'cover.jpg'; output.write_bytes(b'jpeg')
            with patch('app.channels_internal_api.run_media') as run:
                extract_cover(Path('source.mp4'),output,90)
            command=run.call_args.args[0]
            self.assertEqual(command.count('-threads'),2)
            self.assertEqual(command[command.index('-filter_threads')+1],'1')
        with patch('app.channels_covers.run_media',return_value=b'pixels') as run:
            _run(['ffmpeg','-i','source','out'])
        self.assertIn('-filter_threads',run.call_args.args[0])
        with patch('app.channels_covers.run_media',side_effect=MediaCommandError('MEDIA_BUSY',transient=True)), self.assertRaises(CoverError) as caught:
            _run(['ffmpeg'])
        self.assertIn('资源繁忙',str(caught.exception))

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Linux media candidate')
    def test_real_h264_probe_and_frame_extraction_preserve_original(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'video.mp4'
            run_media(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:size=180x320:rate=10',
                       '-t','1','-c:v','libx264','-threads','1',str(path)])
            original=path.read_bytes()
            meta=probe_video(path)
            self.assertEqual((meta['width'],meta['height']),(180,320))
            self.assertEqual(meta['duration'],1)
            self.assertTrue(extract_cover(path,Path(folder)/'cover.jpg',meta['duration']).is_file())
            self.assertEqual(path.read_bytes(),original)
