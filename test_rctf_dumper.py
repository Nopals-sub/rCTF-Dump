import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock

import rctf_dumper


class TestRCTFDumper(unittest.TestCase):
    def test_root_url(self):
        self.assertEqual(rctf_dumper.root_url("https://ctf.example.com/"), "https://ctf.example.com")
        self.assertEqual(rctf_dumper.root_url("ctf.example.com/challenges"), "https://ctf.example.com")
        self.assertEqual(rctf_dumper.root_url("http://127.0.0.1:8000/login"), "http://127.0.0.1:8000")

    def test_default_output_dir(self):
        self.assertEqual(rctf_dumper.default_output_dir("https://ctf.example.com/"), "ctf.example.com")
        self.assertEqual(rctf_dumper.default_output_dir("http://ctf.local:8080/"), "ctf.local")

    def test_extract_filename_from_url_path(self):
        self.assertEqual(
            rctf_dumper.extract_filename_from_url_path("https://example.com/files/handout.zip"),
            "handout.zip",
        )
        self.assertEqual(
            rctf_dumper.extract_filename_from_url_path(
                "https://sourceforge.net/projects/dejavu/files/dejavu/2.37/dejavu-fonts-ttf-2.37.zip/download"
            ),
            "dejavu-fonts-ttf-2.37.zip",
        )
        self.assertEqual(
            rctf_dumper.extract_filename_from_url_path("/uploads/abc/chall.py"),
            "chall.py",
        )

    def test_is_likely_file(self):
        self.assertTrue(rctf_dumper.is_likely_file("handout.zip", "https://drive.google.com/file/d/123/view"))
        self.assertTrue(rctf_dumper.is_likely_file("archive", "/uploads/123/file.bin"))
        self.assertTrue(rctf_dumper.is_likely_file("download font", "https://example.com/font.zip"))
        self.assertFalse(rctf_dumper.is_likely_file("wikipedia", "https://en.wikipedia.org/wiki/Microsoft_Paint"))
        self.assertFalse(rctf_dumper.is_likely_file("repo", "https://github.com/foo/bar/"))

    def test_extract_description_files(self):
        desc = """
        Read wiki: [paint](https://en.wikipedia.org/wiki/Microsoft_Paint)
        Download: [handout.zip](https://drive.google.com/file/d/abc/view)
        Upload link: /uploads/feedbeef/patch.diff
        """
        existing = [{"name": "existing.txt", "url": "/uploads/111/existing.txt"}]
        files = rctf_dumper.extract_description_files(desc, "https://ctf.example.com", existing)
        names = {f["name"] for f in files}
        self.assertIn("handout.zip", names)
        self.assertIn("patch.diff", names)
        self.assertNotIn("paint", names)

    def test_filter_challenges(self):
        challs = [
            {"id": "c1", "name": "Baby Web", "category": "web", "solves": 100},
            {"id": "c2", "name": "Heap Pwn", "category": "pwn", "solves": 10},
            {"id": "c3", "name": "Baby Crypto", "category": "crypto", "solves": 50},
        ]

        # Category filter
        args_cat = SimpleNamespace(category="web,pwn", chall=None, solved=False, unsolved=False, min_solves=None, max_solves=None)
        res = rctf_dumper.filter_challenges(challs, args_cat)
        self.assertEqual([c["id"] for c in res], ["c1", "c2"])

        # Chall name filter
        args_name = SimpleNamespace(category=None, chall="baby", solved=False, unsolved=False, min_solves=None, max_solves=None)
        res = rctf_dumper.filter_challenges(challs, args_name)
        self.assertEqual([c["id"] for c in res], ["c1", "c3"])

        # Solves range filter
        args_solves = SimpleNamespace(category=None, chall=None, solved=False, unsolved=False, min_solves=20, max_solves=150)
        res = rctf_dumper.filter_challenges(challs, args_solves)
        self.assertEqual([c["id"] for c in res], ["c1", "c3"])

        # Solved / Unsolved filter
        user_solves = {"c1"}
        args_solved = SimpleNamespace(category=None, chall=None, solved=True, unsolved=False, min_solves=None, max_solves=None)
        self.assertEqual([c["id"] for c in rctf_dumper.filter_challenges(challs, args_solved, user_solves)], ["c1"])
        args_unsolved = SimpleNamespace(category=None, chall=None, solved=False, unsolved=True, min_solves=None, max_solves=None)
        self.assertEqual([c["id"] for c in rctf_dumper.filter_challenges(challs, args_unsolved, user_solves)], ["c2", "c3"])

    def test_download_file_incremental_cache(self):
        with TemporaryDirectory() as tmpdir:
            dest = Path(tmpdir) / "test.zip"
            dest.write_bytes(b"HELLO_WORLD_12345")
            file_len = dest.stat().st_size

            mock_session = MagicMock()

            # 1. Matching declared_size skips network call completely
            ok, info = rctf_dumper.download_file(
                mock_session,
                "https://ctf.example.com",
                "/uploads/abc/test.zip",
                dest,
                max_bytes=1000000,
                timeout=10,
                allow_external=False,
                declared_size=file_len,
                overwrite=False,
            )
            self.assertTrue(ok)
            self.assertIn("cached", info)
            mock_session.get.assert_not_called()

            # 2. Overwrite flag triggers network download
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.headers = {"Content-Length": str(file_len)}
            mock_resp.iter_content.return_value = [b"NEW_CONTENT"]
            mock_session.get.return_value = mock_resp

            ok, info = rctf_dumper.download_file(
                mock_session,
                "https://ctf.example.com",
                "/uploads/abc/test.zip",
                dest,
                max_bytes=1000000,
                timeout=10,
                allow_external=False,
                declared_size=file_len,
                overwrite=True,
            )
            self.assertTrue(ok)
            self.assertNotIn("cached", info)
            self.assertEqual(dest.read_bytes(), b"NEW_CONTENT")


if __name__ == "__main__":
    unittest.main()
