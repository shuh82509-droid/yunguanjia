import tempfile
import threading
import unittest
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.channels_service import ChannelsError, ChannelsService
from fastapi import HTTPException
from types import SimpleNamespace


class ChannelsLoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = ChannelsService()
        self.service.auth_root = Path(self.temp.name)
        with patch('app.channels_service.threading.Thread'):
            self.session = self.service.start_authorization('owner', 'tester')

    def test_repeated_start_reuses_owner_active_session(self):
        with patch('app.channels_service.threading.Thread') as thread:
            again = self.service.start_authorization('owner', 'tester')
        self.assertEqual(again['id'], self.session['id'])
        thread.assert_not_called()

    def test_failed_session_can_restart(self):
        self.service._update_session(self.session['id'], status='failed')
        with patch('app.channels_service.threading.Thread') as thread:
            again = self.service.start_authorization('owner', 'tester')
        self.assertNotEqual(again['id'], self.session['id'])
        thread.return_value.start.assert_called_once()

    def test_thread_exhaustion_is_a_terminal_state(self):
        with patch('app.channels_service.threading.Thread') as thread:
            thread.return_value.start.side_effect = RuntimeError("can't start new thread")
            result = self.service.start_authorization('other', 'other')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('资源暂忙', result['message'])

    def test_slot_wait_is_bounded_without_opening_browser(self):
        self.service._authorization_slot = MagicMock()
        self.service._authorization_slot.acquire.return_value = False
        with patch.object(self.service, '_run_authorization_session') as run:
            self.service._authorization_worker(self.session['id'])
        self.assertEqual(self.service.session_status(self.session['id'], 'owner')['status'], 'failed')
        self.service._authorization_slot.acquire.assert_called_once_with(timeout=30)
        run.assert_not_called()

    def test_slot_released_after_worker_exception(self):
        with patch.object(self.service, '_run_authorization_session', side_effect=ValueError()):
            with self.assertRaises(ValueError):
                self.service._authorization_worker(self.session['id'])
        self.assertTrue(self.service._authorization_slot.acquire(blocking=False))
        self.service._authorization_slot.release()

    def test_capacity_guard_leaves_api_headroom(self):
        with patch('app.channels_service.Path.read_text', side_effect=['256', '190']):
            with self.assertRaises(ChannelsError) as caught:
                self.service._require_browser_capacity()
        self.assertEqual(caught.exception.code, 'BROWSER_BUSY')
        with patch('app.channels_service.Path.read_text', side_effect=['256', '89']):
            self.service._require_browser_capacity()

    def test_capacity_guard_works_without_linux_cgroup(self):
        with patch('app.channels_service.Path.read_text', side_effect=FileNotFoundError()):
            self.service._require_browser_capacity()

    def test_strict_auth_launch_does_not_open_second_fallback_browser(self):
        playwright = MagicMock()
        playwright.chromium.launch_persistent_context.side_effect = RuntimeError("can't start new thread")
        with patch.object(self.service, '_launch_publish_browser') as fallback:
            with self.assertRaises(ChannelsError):
                self.service._launch_account_session(playwright, self.service.auth_root / 'new.json', allow_fallback=False)
        fallback.assert_not_called()

    def run_scan(self, *, fail=False):
        browser = MagicMock()
        scan, validation = MagicMock(), MagicMock()
        browser.new_context.side_effect = [scan, validation]
        scan.new_page.return_value.url = 'https://channels.weixin.qq.com/platform'
        validation.new_page.return_value.url = 'https://channels.weixin.qq.com/platform'
        validation.new_page.return_value.locator.return_value.first.count.return_value = 0
        runtime = MagicMock()
        runtime.chromium.launch.return_value = browser
        verified = {'external_account_id': 'finder-test', 'nickname': 'test'}

        def verify(*args):
            # Core regression: the initial browser must already be closed.
            validation.close.assert_called_once()
            scan.close.assert_called_once()
            browser.close.assert_called_once()
            if fail:
                raise RuntimeError('sessionid=do-not-log-this')
            return verified, 'encrypted', 'session-encrypted'

        with patch('playwright.sync_api.sync_playwright') as sync, patch.object(self.service, '_require_browser_capacity'), patch.object(self.service, '_persist_context_state'), patch.object(self.service, '_goto_creator_page'), patch.object(self.service, '_authorization_expired', return_value=False), patch('app.channels_service.read_authenticated_identity', return_value=verified), patch.object(self.service, '_verify_persistent_authorization', side_effect=verify), patch.object(self.service, '_save_authorization', return_value=('account', 0)) as save:
            sync.return_value.__enter__.return_value = runtime
            self.service._run_authorization_session(self.session['id'])
        return save

    def test_scan_browser_closed_before_profile_verification_and_save(self):
        save = self.run_scan()
        self.assertEqual(self.service.session_status(self.session['id'], 'owner')['status'], 'authorized')
        save.assert_called_once()

    def test_failed_verification_clears_old_picker_and_logs_no_secrets(self):
        self.service._update_session(self.session['id'], qr=b'old', capture_mode='account_list', account_choices=[{'choice_id': 'old'}], interaction_required=True)
        with self.assertLogs('app.channels_service', level='WARNING') as captured:
            save = self.run_scan(fail=True)
        status = self.service.session_status(self.session['id'], 'owner')
        self.assertEqual(status['status'], 'failed')
        self.assertFalse(status['qr_ready'])
        self.assertFalse(status['interaction_required'])
        self.assertEqual(status['capture_mode'], 'qr')
        self.assertEqual(status['account_choices'], [])
        self.assertNotIn('do-not-log-this', ' '.join(captured.output))
        self.assertIn('verify_profile', ' '.join(captured.output))
        save.assert_not_called()


class LoginResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_permissions_do_not_block_liveness(self):
        from app.main import cloud_manager_module_access_middleware, liveness
        started, release = threading.Event(), threading.Event()
        loop_thread = threading.get_ident()

        def slow_permission(request):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            started.set()
            if not release.wait(timeout=2):
                raise RuntimeError('permission wait blocked the event loop')
            return {'number': 'test'}

        async def next_handler(request):
            return 'response'

        with patch('app.main.require_user', side_effect=slow_permission), patch('app.main.require_module_access') as gate:
            task = asyncio.create_task(cloud_manager_module_access_middleware(SimpleNamespace(url=SimpleNamespace(path='/api/channels/status')), next_handler))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                self.assertEqual(await asyncio.wait_for(liveness(), timeout=0.1), {'status': 'ok'})
            finally:
                release.set()
            self.assertEqual(await task, 'response')
            gate.assert_called_once_with({'number': 'test'}, 'cloud-manager')

    async def test_module_denial_is_still_enforced(self):
        from app.main import cloud_manager_module_access_middleware
        next_handler = MagicMock()
        with patch('app.main.require_user', return_value={'number': 'test'}), patch('app.main.require_module_access', side_effect=HTTPException(403, 'denied')):
            response = await cloud_manager_module_access_middleware(SimpleNamespace(url=SimpleNamespace(path='/api/channels/status')), next_handler)
        self.assertEqual(response.status_code, 403)
        next_handler.assert_not_called()


if __name__ == '__main__':
    unittest.main()
