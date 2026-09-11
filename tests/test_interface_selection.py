from __future__ import annotations

import subprocess
import sys
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


if __name__ == '__main__':
    unittest.main()
