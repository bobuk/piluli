import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("piluli_installer", ROOT / "install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def test_choose_install_dir_prefers_candidate_on_path(self):
        selected, on_path = installer.choose_install_dir(
            os.pathsep.join(("/usr/bin", str(installer.INSTALL_CANDIDATES[1])))
        )
        self.assertEqual(selected, installer.INSTALL_CANDIDATES[1])
        self.assertTrue(on_path)

    def test_download_uses_public_raw_url(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"print('public pill')\n"
        with mock.patch.object(installer.urllib.request, "urlopen", return_value=response) as urlopen:
            source = installer.download("piluli")
        self.assertEqual(source, "print('public pill')\n")
        urlopen.assert_called_once_with(
            "https://raw.githubusercontent.com/bobuk/piluli/main/dist/piluli.py",
            timeout=30,
        )

    def test_install_writes_executable_atomically(self):
        source = "#!/usr/bin/env python3\nprint('tiny, but employed')\n"
        with tempfile.TemporaryDirectory() as directory:
            destination = installer.install("piluli", source=source, install_dir=Path(directory))
            self.assertEqual(destination.read_text(encoding="utf-8"), source)
            self.assertTrue(destination.stat().st_mode & 0o111)
            self.assertEqual(list(Path(directory).glob(".piluli.*")), [])

    def test_install_rejects_invalid_script(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(installer.InstallError):
                installer.install("pilulit", source="def nope(:\n", install_dir=Path(directory))
            self.assertFalse((Path(directory) / "pilulit").exists())

    def test_main_requires_one_known_script(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = installer.main([])
        self.assertEqual(result, 2)
        self.assertIn("piluli", stderr.getvalue())
        self.assertIn("pilulit", stderr.getvalue())

    def test_main_installs_selected_script(self):
        with mock.patch.object(installer, "install") as install:
            self.assertEqual(installer.main(["pilulit"]), 0)
        install.assert_called_once_with("pilulit")


if __name__ == "__main__":
    unittest.main()
