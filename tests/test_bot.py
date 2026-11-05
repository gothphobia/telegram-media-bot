import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import build_dispatcher
from media import Download, MediaError
from test_media import config


class BotFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dp = build_dispatcher(config(concurrency=1))
        self.receive = self.dp.message.handlers[-1].callback
        self.choose = self.dp.callback_query.handlers[0].callback
        self.bot = SimpleNamespace(send_message=AsyncMock(), send_audio=AsyncMock(), send_document=AsyncMock())

    async def request(self, user=1):
        message = SimpleNamespace(from_user=SimpleNamespace(id=user), chat=SimpleNamespace(id=10),
                                  text='https://youtu.be/abc', answer=AsyncMock())
        await self.receive(message)
        markup = message.answer.call_args.kwargs['reply_markup']
        data = markup.inline_keyboard[0][0].callback_data
        return SimpleNamespace(data=data, from_user=SimpleNamespace(id=user), answer=AsyncMock())

    async def test_other_user_cannot_consume_button(self):
        callback = await self.request()
        callback.from_user.id = 2
        with patch('bot.download', new_callable=AsyncMock) as downloader:
            await self.choose(callback, self.bot)
            downloader.assert_not_awaited()
        self.assertIn('отправителю', callback.answer.call_args.args[0])

    async def test_double_click_and_capacity_then_recovery(self):
        first, second = await self.request(1), await self.request(2)
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked(*args):
            entered.set()
            await release.wait()
            raise MediaError('test failure')

        with patch('bot.download', side_effect=blocked) as downloader:
            task = asyncio.create_task(self.choose(first, self.bot))
            await entered.wait()
            await self.choose(first, self.bot)
            await self.choose(second, self.bot)
            self.assertIn('заняты', second.answer.call_args.args[0])
            self.assertEqual(downloader.await_count, 1)
            release.set()
            await task
        # Busy buttons remain valid; capacity is released on a download error.
        with patch('bot.download', side_effect=MediaError('retry')) as downloader:
            await self.choose(second, self.bot)
            downloader.assert_awaited_once()

    async def test_mp4_is_uploaded_as_document_and_temp_files_removed(self):
        callback = await self.request()
        folders = []

        async def fake_download(conf, url, kind, folder):
            folders.append(folder)
            path = folder / 'media.mp4'
            path.write_bytes(b'video')
            return Download(path, 'Example')

        with patch('bot.download', side_effect=fake_download):
            await self.choose(callback, self.bot)
        self.bot.send_document.assert_awaited_once()
        self.assertFalse(folders[0].exists())
        self.bot.send_audio.assert_not_awaited()
