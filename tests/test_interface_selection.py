from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import poor_girls_codex as pgc


class InterfaceSelectionTests(unittest.TestCase):
    def test_importing_core_does_not_import_macos_adapter(self) -> None:
        completed = subprocess.run(
            [sys.executable, '-c', 'import sys; import poor_girls_codex; print("macos_desktop_app" in sys.modules)'],
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(completed.stdout.strip(), 'False')

    def test_create_interface_lazy_imports_macos_adapter(self) -> None:
        sentinel = mock.Mock()
        module = mock.Mock()
        module.MacOSDesktopApp.return_value = sentinel
        with mock.patch.dict(sys.modules, {'macos_desktop_app': module}):
            interface = pgc.create_interface('chatgpt-macos')
        self.assertIs(interface, sentinel)

    def test_unknown_interface_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, 'unknown interface'):
            pgc.create_interface('definitely-not-real')

    def test_web_default_cdp_url_is_plain_url(self) -> None:
        import chatgpt_web

        self.assertEqual(chatgpt_web.DEFAULT_CDP_URL, 'http' + '://127.0.0.1:9222')

    def test_nonwatch_macos_mode_uses_configured_interface(self) -> None:
        interface = mock.Mock()
        interface.trusted.return_value = True
        interface.root.return_value = ('app', 'root')

        with (
            mock.patch.object(sys, 'argv', ['poor_girls_codex.py', 'copy']),
            mock.patch.object(pgc, 'create_interface', return_value=interface),
            mock.patch.object(pgc, 'latest_assistant_toolcall', return_value=('{}', {})),
            mock.patch.object(sys, 'stdout'),
        ):
            pgc.main()

        interface.root.assert_called_once_with()

    def test_session_id_is_persistent_and_locally_git_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            subprocess.run(['git', 'init', '-q'], cwd=cwd, check=True)
            first = pgc.load_or_create_session_id(cwd)
            second = pgc.load_or_create_session_id(cwd)
            self.assertEqual(first, second)
            self.assertTrue(first)
            with open(os.path.join(cwd, '.pgc', 'session'), encoding='utf-8') as session_file:
                self.assertEqual(session_file.read().strip(), first)
            ignored = subprocess.run(
                ['git', 'check-ignore', '-q', '.pgc/session'],
                cwd=cwd,
                check=False,
            )
            self.assertEqual(ignored.returncode, 0)

    def test_web_session_validation_requires_exact_session(self) -> None:
        pgc.validate_web_session_request({'session': 'abc', 'id': 'x', 'tool': 'status'}, 'abc')
        with self.assertRaisesRegex(ValueError, 'different PGC session'):
            pgc.validate_web_session_request({'session': 'other', 'id': 'x', 'tool': 'status'}, 'abc')
        with self.assertRaisesRegex(ValueError, 'different PGC session'):
            pgc.validate_web_session_request({'id': 'x', 'tool': 'status'}, 'abc')

    def test_web_bootstrap_requires_session_on_every_executable_request(self) -> None:
        prompt = pgc.web_bootstrap_prompt('abc123')
        self.assertIn('"session": "abc123"', prompt)
        self.assertIn('Requests without this exact session value are inert', prompt)

    def test_web_execution_strips_session_from_single_call(self) -> None:
        with mock.patch.object(pgc.toolcall_lib, 'execute', return_value={'ok': True}) as execute:
            result = pgc.execute_web_session_request(
                {'session': 'abc', 'id': 'x', 'tool': 'status'},
                'abc',
            )
        self.assertEqual(result, {'ok': True})
        execute.assert_called_once_with({'id': 'x', 'tool': 'status'}, 0)

    def test_web_execution_strips_session_from_batch_wrapper(self) -> None:
        request = {
            'session': 'abc',
            'calls': [
                {'id': 'x', 'tool': 'status'},
                {'id': 'y', 'tool': 'tree'},
            ],
        }
        with mock.patch.object(
            pgc.toolcall_lib,
            'execute',
            side_effect=[{'ok': True}, {'ok': True}],
        ) as execute:
            pgc.execute_web_session_request(request, 'abc')
        self.assertEqual(
            execute.call_args_list,
            [
                mock.call({'id': 'x', 'tool': 'status'}, 0),
                mock.call({'id': 'y', 'tool': 'tree'}, 1),
            ],
        )


if __name__ == '__main__':
    unittest.main()
