import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import Config
from media import MediaError, command, download, extract_url, run


def config(**changes):
    values = dict(token='123:token', allowed_users=frozenset(), concurrency=2,
                  timeout=10, max_duration=7200, max_upload_bytes=1000,
                  api_base='', cookies_file='')
    return Config(**(values | changes))


class URLTests(unittest.TestCase):
    def test_supported_links_and_tracking_removal(self):
        cases = {
            'https://youtube.com/watch?v=abc_123&list=PLtest': 'https://www.youtube.com/watch?v=abc_123',
            'https://youtu.be/abc-123?si=test': 'https://youtu.be/abc-123',
            'https://www.youtube.com/shorts/abc123?feature=share': 'https://www.youtube.com/shorts/abc123',
            'Вот видео https://www.tiktok.com/@name/video/12345.': 'https://www.tiktok.com/@name/video/12345',
            'https://vm.tiktok.com/ZMtest/': 'https://vm.tiktok.com/ZMtest/',
            'https://www.tiktok.com/t/ZMtest/': 'https://www.tiktok.com/t/ZMtest/',
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(extract_url(value), expected)

    def test_reject_unrelated_or_ambiguous_links(self):
        for value in ['https://youtube.com.evil.test/watch?v=x',
                      'https://youtube.com@localhost/watch?v=x',
                      'https://youtube.com:9000/watch?v=x',
                      'https://127.0.0.1/video/123', 'file:///etc/passwd',
                      'https://youtube.com/playlist?list=abc',
                      'https://youtu.be/abc https://youtu.be/def']:
            with self.subTest(value=value), self.assertRaises(MediaError):
                extract_url(value)

    def test_commands_do_not_use_shell_or_user_config(self):
        args = command(config(), 'https://youtu.be/abc', 'mp4', Path('/tmp/work space'))
        self.assertIn('--ignore-config', args)
        self.assertEqual(args[-2:], ['--', 'https://youtu.be/abc'])
        self.assertIn('bestvideo+bestaudio/best', args)
        self.assertIn('/tmp/work space/media.%(ext)s', args)
        audio = command(config(), 'https://youtu.be/abc', 'mp3', Path('/tmp'))
        self.assertIn('--audio-format', audio)
        self.assertNotIn('--merge-output-format', audio)


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_size_after_conversion_is_checked(self):
        async def fake_run(args, folder, timeout, disk_limit):
            if '--dump-single-json' in args:
                return '{"title":"Example","duration":60}'
            (folder / 'media.mp3').write_bytes(b'x' * 1001)
            return ''
        with tempfile.TemporaryDirectory() as tmp, patch('media.run', side_effect=fake_run):
            with self.assertRaisesRegex(MediaError, 'превышает лимит'):
                await download(config(), 'https://youtu.be/abc', 'mp3', Path(tmp))

    async def test_live_and_long_videos_are_not_downloaded(self):
        for data in ['{"is_live":true}', '{"duration":8000}', '{"_type":"playlist"}']:
            with tempfile.TemporaryDirectory() as tmp, patch('media.run', return_value=data) as mocked:
                with self.assertRaises(MediaError):
                    await download(config(), 'https://youtu.be/abc', 'mp4', Path(tmp))
                self.assertEqual(mocked.call_count, 1)

    async def test_successful_download(self):
        async def fake_run(args, folder, timeout, disk_limit):
            if '--dump-single-json' in args:
                return '{"title":"Example","duration":60}'
            (folder / 'media.mp4').write_bytes(b'video')
            return ''
        with tempfile.TemporaryDirectory() as tmp, patch('media.run', side_effect=fake_run):
            result = await download(config(), 'https://youtu.be/abc', 'mp4', Path(tmp))
            self.assertEqual(result.title, 'Example')
            self.assertEqual(result.path.read_bytes(), b'video')

    async def test_deadline_stops_real_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            with self.assertRaises(TimeoutError):
                await run([sys.executable, '-c',
                           'import os,time; open("' + str(folder / 'pid') + '","w").write(str(os.getpid())); time.sleep(60)'],
                          folder, timeout=0.3, disk_limit=1_000_000)
            pid = int((folder / 'pid').read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    async def test_disk_limit_stops_unknown_size_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            with self.assertRaisesRegex(MediaError, 'слишком большое'):
                await run([sys.executable, '-c',
                           'import time; print("x"*10000,flush=True); time.sleep(60)'],
                          folder, timeout=5, disk_limit=100)


if __name__ == '__main__':
    unittest.main()
