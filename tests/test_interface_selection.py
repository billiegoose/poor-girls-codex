from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import poor_girls_codex as pgc
import toolcall_lib


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
            mock.patch.object(
                sys,
                'argv',
                ['poor_girls_codex.py', 'copy', '--interface', 'chatgpt-macos'],
            ),
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

    def test_web_session_validation_accepts_current_session_and_descendants(self) -> None:
        pgc.validate_web_session_request({'session': 'abc', 'id': 'x', 'tool': 'status'}, 'abc')
        pgc.validate_web_session_request({'session': 'abc::child', 'id': 'x', 'tool': 'status'}, 'abc')
        pgc.validate_web_session_request({'session': 'abc::child::grandchild', 'id': 'x', 'tool': 'status'}, 'abc')
        with self.assertRaisesRegex(ValueError, 'different PGC session'):
            pgc.validate_web_session_request({'session': 'other', 'id': 'x', 'tool': 'status'}, 'abc')
        with self.assertRaisesRegex(ValueError, 'different PGC session'):
            pgc.validate_web_session_request({'session': 'abcd', 'id': 'x', 'tool': 'status'}, 'abc')
        with self.assertRaisesRegex(ValueError, 'different PGC session'):
            pgc.validate_web_session_request({'id': 'x', 'tool': 'status'}, 'abc')

    def test_subagent_web_bootstrap_describes_role_and_session_tree_routing(self) -> None:
        prompt = pgc.subagent_web_bootstrap_prompt('abc123')
        self.assertIn('You are a subagent.', prompt)
        self.assertIn('"session": "abc123"', prompt)
        self.assertIn('abc123::', prompt)
        self.assertIn('subagent {name,prompt}', prompt)
        self.assertIn('handoff {result}', prompt)
        self.assertIn('Requests outside that session tree are inert', prompt)
        self.assertNotIn('subagent {name,prompt}', pgc.BOOTSTRAP_PROMPT)

    def test_root_web_bootstrap_includes_working_directory_and_omits_handoff(self) -> None:
        prompt = pgc.root_web_bootstrap_prompt('abc123', '/tmp/projects/asgard')
        self.assertTrue(
            prompt.startswith(
                'You are working on the `asgard` codebase.\n'
                'Working directory: asgard\n\n'
            )
        )
        self.assertIn('You are the root agent', prompt)
        self.assertIn('Finish normally in this conversation', prompt)
        self.assertIn('subagent {name,prompt}', prompt)
        self.assertNotIn('handoff', prompt.lower())
        self.assertIn('\"session\": \"abc123\"', prompt)
        self.assertNotIn('Working directory:', pgc.subagent_web_bootstrap_prompt('abc123'))

    def test_web_bootstrap_requires_explicit_known_role(self) -> None:
        with self.assertRaises(TypeError):
            pgc.web_bootstrap_prompt('abc123')
        with self.assertRaisesRegex(ValueError, 'unsupported web bootstrap role'):
            pgc.web_bootstrap_prompt('abc123', role='manager')

    def test_root_web_bootstrap_handles_filesystem_root(self) -> None:
        prompt = pgc.root_web_bootstrap_prompt('abc123', '/')
        self.assertTrue(
            prompt.startswith(
                'You are working on the `/` codebase.\n'
                'Working directory: /\n\n'
            )
        )

    def test_bootstrap_prompt_discourages_redundant_cwd_overrides(self) -> None:
        self.assertIn(
            'Local tools already execute from the current working directory.',
            pgc.BOOTSTRAP_PROMPT,
        )
        self.assertIn(
            'Omit `cwd` unless you intentionally need to operate in a different directory.',
            pgc.BOOTSTRAP_PROMPT,
        )
        self.assertIn('cwd? (only to change directories)', pgc.BOOTSTRAP_PROMPT)

    def test_bootstrap_prompt_uses_timeout_seconds(self) -> None:
        self.assertIn('timeout_seconds?', pgc.BOOTSTRAP_PROMPT)
        self.assertNotIn('timeout?', pgc.BOOTSTRAP_PROMPT)

    def test_timeout_seconds_takes_precedence_over_legacy_timeout(self) -> None:
        self.assertEqual(
            toolcall_lib.timeout_seconds({'timeout_seconds': 7, 'timeout': 99}),
            7,
        )

    def test_legacy_timeout_remains_supported(self) -> None:
        self.assertEqual(toolcall_lib.timeout_seconds({'timeout': 11}), 11)
        self.assertEqual(toolcall_lib.timeout_seconds({}), toolcall_lib.DEFAULT_TIMEOUT)

    def test_patch_tool_is_not_supported(self) -> None:
        self.assertNotIn('patch', pgc.LOCAL_TOOLS)
        with self.assertRaisesRegex(ValueError, 'unsupported tool'):
            pgc.validate_request(
                {'tool': 'patch', 'patch': 'not used'},
                supported_tools=pgc.SUPPORTED_TOOLS,
            )

    def test_hyphenated_subagent_name_is_valid(self) -> None:
        pgc.validate_request(
            {'tool': 'subagent', 'name': 'closure-parity', 'prompt': 'Review closures'},
            supported_tools=pgc.SUPPORTED_TOOLS,
        )

    def test_subagent_and_handoff_validation(self) -> None:
        pgc.validate_web_session_request(
            {'session': 'abc', 'id': 's', 'tool': 'subagent', 'name': 'worker2', 'prompt': 'Do work'},
            'abc',
        )
        pgc.validate_web_session_request(
            {'session': 'abc', 'id': 'h', 'tool': 'handoff', 'result': 'Done'},
            'abc',
        )
        with self.assertRaisesRegex(ValueError, 'unsupported tool'):
            pgc.validate_request({'id': 's', 'tool': 'subagent', 'name': 'worker2', 'prompt': 'Do work'})
        with self.assertRaisesRegex(ValueError, 'contain only alphanumerics'):
            pgc.validate_web_session_request(
                {'session': 'abc', 'id': 's', 'tool': 'subagent', 'name': 'worker two!', 'prompt': 'Do work'},
                'abc',
            )
        with self.assertRaisesRegex(ValueError, 'non-empty'):
            pgc.validate_web_session_request(
                {'session': 'abc', 'id': 's', 'tool': 'subagent', 'name': 'worker2', 'prompt': ''},
                'abc',
            )
        with self.assertRaisesRegex(ValueError, 'non-empty'):
            pgc.validate_web_session_request(
                {'session': 'abc', 'id': 'h', 'tool': 'handoff', 'result': ''},
                'abc',
            )

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
