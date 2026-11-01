from __future__ import annotations

import hashlib
import json
import socketserver
import tempfile
import threading
import unittest
from pathlib import Path

import camera_bridge
import poor_girls_codex as pgc


class FakeCameraHandler(socketserver.StreamRequestHandler):
    def handle(self):
        request = json.loads(self.rfile.readline())
        self.server.requests.append(request)
        response = self.server.response_for(request)
        self.wfile.write(json.dumps(response).encode() + b'\n')


class CameraBridgeTests(unittest.TestCase):
    def test_default_socket_is_in_applepie_directory(self):
        self.assertEqual(
            camera_bridge.default_socket_path().parts[-3:],
            ('Application Support', 'APPLEPIE', 'capture.sock'),
        )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='applepie-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.captures = self.root / 'captures'
        self.captures.mkdir()
        self.socket_path = self.root / 'camera.sock'
        self.server = socketserver.UnixStreamServer(str(self.socket_path), FakeCameraHandler)
        self.server.requests = []
        self.server.response_for = lambda req: self.denied(req['camera'])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    @staticmethod
    def denied(camera):
        return {'version': 2, 'ok': False, 'camera': camera, 'code': 'unattended_access_inactive', 'error': 'Locked'}

    def capture_reply(self, camera='board', *, jpeg=None, directory=None):
        jpeg = jpeg if jpeg is not None else b'\xff\xd8\xff\xe0fake photo bytes'
        output = directory or self.captures
        filename = output / f'{camera}-12345678-1234-1234-1234-123456789abc.jpg'
        filename.write_bytes(jpeg)
        return {
            'version': 2,
            'ok': True,
            'camera': camera,
            'filename': str(filename),
            'byte_count': len(jpeg),
            'sha256': hashlib.sha256(jpeg).hexdigest(),
            'width': 1280,
            'height': 720,
            'captured_at': '2026-10-31T13:00:00Z',
        }

    def ask(self, camera='board'):
        return camera_bridge.request_capture(
            camera, session_id='root-session', socket_path=self.socket_path, capture_directory=self.captures,
        )

    def test_denied_capture_has_no_image(self):
        reply = self.ask('monitor')
        self.assertFalse(reply.metadata['ok'])
        self.assertIsNone(reply.jpeg_data)
        self.assertEqual(self.server.requests, [{'version': 2, 'command': 'capture', 'camera': 'monitor', 'session': 'root-session'}])

    def test_captured_image_bytes_are_verified_and_separate_from_json(self):
        metadata = self.capture_reply()
        self.server.response_for = lambda request: metadata
        reply = self.ask()
        self.assertEqual(reply.jpeg_data, b'\xff\xd8\xff\xe0fake photo bytes')
        self.assertEqual(reply.image_name, Path(metadata['filename']).name)
        self.assertNotIn('jpeg_data', reply.metadata)

    def test_tampered_bytes_are_rejected(self):
        metadata = self.capture_reply()
        self.server.response_for = lambda request: metadata
        Path(metadata['filename']).write_bytes(b'\xff\xd8\xff\xe0tampered photo')
        with self.assertRaises(camera_bridge.CameraBridgeError):
            self.ask()

    def test_outside_capture_directory_is_rejected(self):
        outside = self.root / 'elsewhere'
        outside.mkdir()
        metadata = self.capture_reply(directory=outside)
        self.server.response_for = lambda request: metadata
        with self.assertRaisesRegex(camera_bridge.CameraBridgeError, 'outside'):
            self.ask()

    def test_symlinked_capture_is_rejected(self):
        metadata = self.capture_reply()
        file = Path(metadata['filename'])
        target = self.root / 'target.jpg'
        file.rename(target)
        file.symlink_to(target)
        self.server.response_for = lambda request: metadata
        with self.assertRaises(camera_bridge.CameraBridgeError):
            self.ask()

    def test_missing_camera_app_fails_without_opening_a_camera(self):
        with self.assertRaisesRegex(camera_bridge.CameraBridgeError, 'socket unavailable'):
            camera_bridge.request_capture('board', session_id='root-session', socket_path=self.root / 'absent.sock')

    def test_pgc_validates_camera_name_and_advertises_tool(self):
        pgc.validate_web_session_request({'session': 'abc', 'tool': 'camera', 'camera': 'board'}, 'abc')
        pgc.validate_web_session_request({'session': 'abc', 'tool': 'camera', 'camera': 'monitor'}, 'abc')
        for invalid in ('other', '../monitor', None, '', 4):
            with self.assertRaisesRegex(ValueError, 'camera must'):
                pgc.validate_web_session_request({'session': 'abc', 'tool': 'camera', 'camera': invalid}, 'abc')
        self.assertIn('camera {camera}', pgc.root_web_bootstrap_prompt('abc'))
        self.assertIn('camera {camera}', pgc.subagent_web_bootstrap_prompt('abc::worker'))
        with self.assertRaisesRegex(ValueError, 'unsupported tool'):
            pgc.validate_request({'tool': 'camera', 'camera': 'board'})


if __name__ == '__main__':
    unittest.main()
