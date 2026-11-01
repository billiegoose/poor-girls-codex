from __future__ import annotations

import json
import unittest
from unittest import mock

import chatgpt_web as web
from camera_bridge import CameraBridgeError, CameraReply


class CameraOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.interface = web.ChatGPTWeb()
        self.watcher = web.WebSessionWatcher(
            self.interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=lambda result: json.dumps(result),
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
            settle_seconds=0,
        )
        self.session = web.WebSession('chat', 'Root', mock.Mock(), routing_session_id='root')

    def test_two_camera_results_stage_two_real_image_attachments(self):
        board = b'\xff\xd8\xffBOARD-pixels'
        monitor = b'\xff\xd8\xffMONITOR-pixels'
        replies = [
            CameraReply({'version': 2, 'ok': True, 'camera': 'board', 'width': 100, 'height': 100}, 'board-1.jpg', board),
            CameraReply({'version': 2, 'ok': True, 'camera': 'monitor', 'width': 200, 'height': 200}, 'monitor-2.jpg', monitor),
        ]
        calls = [
            {'id': 'b', 'tool': 'camera', 'camera': 'board'},
            {'id': 'm', 'tool': 'camera', 'camera': 'monitor'},
        ]
        images = []
        with mock.patch.object(web, 'request_capture', side_effect=replies) as capture:
            results = [self.watcher.execute_orchestration_tool(self.session, 'root', call, i, images)
                       for i, call in enumerate(calls)]
        self.assertEqual(capture.call_args_list, [mock.call('board', session_id='root'), mock.call('monitor', session_id='root')])
        self.assertTrue(all(result['ok'] for result in results))
        self.assertEqual(images, [
            web.ImageAttachment('board-1.jpg', 'image/jpeg', board),
            web.ImageAttachment('monitor-2.jpg', 'image/jpeg', monitor),
        ])
        self.assertNotIn('pixels', json.dumps(results))

    def test_denied_and_unavailable_camera_never_stage_images(self):
        images = []
        with mock.patch.object(web, 'request_capture', return_value=CameraReply({
            'version': 2, 'ok': False, 'camera': 'board', 'code': 'camera_not_allowed', 'error': 'Denied',
        })):
            result = self.watcher.execute_orchestration_tool(
                self.session, 'root', {'tool': 'camera', 'camera': 'board'}, 0, images,
            )
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'camera_not_allowed')
        self.assertEqual(images, [])
        with mock.patch.object(web, 'request_capture', side_effect=CameraBridgeError('Not running')):
            result = self.watcher.execute_orchestration_tool(
                self.session, 'root', {'tool': 'camera', 'camera': 'board'}, 0, images,
            )
        self.assertFalse(result['ok'])
        self.assertIn('Not running', result['error'])
        self.assertEqual(images, [])

    def test_camera_attachment_is_kept_in_pending_delivery(self):
        request = {'session': 'root', 'tool': 'camera', 'camera': 'board', 'id': 'observe'}
        image = b'\xff\xd8\xffpixels-not-json'
        self.interface.latest_assistant_snapshot = mock.Mock(return_value=web.AssistantSnapshot('assistant', (), ''))
        self.interface.valid_request_from_snapshot = mock.Mock(return_value=('source', request, 'fingerprint'))
        self.interface.emulate_active_page = mock.Mock(return_value=None)
        self.interface.latest_user_message = mock.Mock(return_value=('previous', 'Earlier'))
        self.interface.submit = mock.Mock()
        self.watcher.execute_request = lambda req, **kwargs: kwargs['tool_executor'](req, 0)
        session = web.WebSession('root-chat', 'Root', mock.Mock())
        with mock.patch.object(web, 'request_capture', return_value=CameraReply(
            {'version': 2, 'ok': True, 'camera': 'board'}, 'board-1.jpg', image,
        )):
            self.assertFalse(self.watcher.scan_session(session, 0))
            self.assertTrue(self.watcher.scan_session(session, 0))
        pending = session.pending_responses[0]
        self.assertEqual(pending.images, (web.ImageAttachment('board-1.jpg', 'image/jpeg', image),))
        self.assertNotIn('pixels-not-json', pending.result)
        self.assertTrue(self.watcher.deliver_session(session, 0))
        self.assertEqual(self.interface.submit.call_args.kwargs['images'], pending.images)


if __name__ == '__main__':
    unittest.main()
