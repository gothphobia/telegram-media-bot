import asyncio
import logging
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand, CallbackQuery, FSInputFile, InlineKeyboardButton,
    InlineKeyboardMarkup, Message,
)

from config import Config
from media import MediaError, download, extract_url

HELP = (
    'Пришлите ссылку на YouTube, Shorts или TikTok и выберите формат:\n\n'
    '🎬 MP4 — лучшее доступное качество, отправляется файлом без сжатия Telegram.\n'
    '🎵 MP3 — аудио с VBR высокого качества.\n\n'
    'Бот не добавляет водяные знаки. Для TikTok используется исходный поток, '
    'когда он доступен; встроенный в картинку знак не удаляется.\n'
    'Скачивайте свои видео или материалы, на сохранение которых есть разрешение.\n\n'
    '/id — ваш Telegram ID для настройки доступа.'
)


@dataclass(frozen=True)
class Request:
    user_id: int
    chat_id: int
    url: str
    created: float


def build_dispatcher(config: Config) -> Dispatcher:
    dp = Dispatcher()
    pending: dict[str, Request] = {}
    active: set[int] = set()

    def allowed(user_id: int) -> bool:
        return not config.allowed_users or user_id in config.allowed_users

    @dp.message(Command('id'))
    async def identity(message: Message) -> None:
        if message.from_user:
            await message.answer(f'Ваш Telegram ID: {message.from_user.id}')

    @dp.message(CommandStart())
    @dp.message(Command('help'))
    async def help_message(message: Message) -> None:
        if message.from_user and allowed(message.from_user.id):
            await message.answer(HELP)
        else:
            await message.answer('Доступ к боту ограничен администратором.')

    @dp.message(F.text)
    async def receive_link(message: Message) -> None:
        if not message.from_user or not allowed(message.from_user.id):
            await message.answer('Доступ к боту ограничен администратором.')
            return
        try:
            url = extract_url(message.text or '')
        except MediaError as exc:
            await message.answer(str(exc))
            return
        # One pending selection per user; expiry and a hard cap bound memory.
        now = time.monotonic()
        for key, request in list(pending.items()):
            if now - request.created > 900 or request.user_id == message.from_user.id:
                pending.pop(key, None)
        if len(pending) >= 2000:
            await message.answer('Бот занят. Попробуйте немного позже.')
            return
        key = secrets.token_hex(8)
        pending[key] = Request(message.from_user.id, message.chat.id, url, now)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='🎬 MP4 · лучшее качество', callback_data=f'get:mp4:{key}'),
            InlineKeyboardButton(text='🎵 MP3 · аудио', callback_data=f'get:mp3:{key}'),
        ]])
        await message.answer('В каком формате сохранить?', reply_markup=keyboard)

    @dp.callback_query(F.data.startswith('get:'))
    async def choose_format(callback: CallbackQuery, bot: Bot) -> None:
        parts = (callback.data or '').split(':')
        if len(parts) != 3 or parts[1] not in {'mp3', 'mp4'}:
            await callback.answer('Некорректная кнопка.', show_alert=True)
            return
        _, kind, key = parts
        request = pending.get(key)
        if not request or time.monotonic() - request.created > 900:
            pending.pop(key, None)
            await callback.answer('Кнопка устарела. Пришлите ссылку заново.', show_alert=True)
            return
        if request.user_id != callback.from_user.id or not allowed(callback.from_user.id):
            await callback.answer('Эта кнопка доступна только отправителю ссылки.', show_alert=True)
            return
        if request.user_id in active:
            await callback.answer('Дождитесь предыдущей загрузки.', show_alert=True)
            return
        if len(active) >= config.concurrency:
            await callback.answer('Все загрузки заняты. Нажмите снова чуть позже.', show_alert=True)
            return
        # No awaits between capacity checks and reservation.
        active.add(request.user_id)
        pending.pop(key, None)
        status = None
        try:
            await callback.answer('Загружаю…')
            status = await bot.send_message(request.chat_id, '⏳ Получаю файл в лучшем доступном качестве…')
            with TemporaryDirectory(prefix='tg-media-') as temp:
                result = await download(config, request.url, kind, Path(temp))
                await status.edit_text('📤 Отправляю файл…')
                file = FSInputFile(result.path, filename=f'{result.title.replace(chr(47), "_")}.{kind}')
                if kind == 'mp3':
                    await bot.send_audio(request.chat_id, file, title=result.title,
                                         request_timeout=config.timeout)
                else:
                    await bot.send_document(request.chat_id, file, caption=result.title,
                                            disable_content_type_detection=True,
                                            request_timeout=config.timeout)
            await status.edit_text('✅ Готово! Пришлите следующую ссылку.')
        except MediaError as exc:
            await bot.send_message(request.chat_id, str(exc))
        except TelegramAPIError:
            logging.warning('Telegram rejected a request or file upload')
            if status:
                try:
                    await status.edit_text('Не удалось отправить файл в Telegram. Попробуйте позже.')
                except TelegramAPIError:
                    pass
        except Exception as exc:
            logging.error('Download failed (%s)', type(exc).__name__)
            await bot.send_message(request.chat_id, 'Не удалось обработать видео. Попробуйте другую ссылку.')
        finally:
            active.discard(request.user_id)

    return dp


async def main() -> None:
    config = Config.load()
    for executable in ('ffmpeg', 'ffprobe'):
        if not shutil.which(executable):
            raise RuntimeError(f'Не найден {executable}. Установите FFmpeg по README.md.')
    if not (shutil.which('deno') or shutil.which('node')):
        raise RuntimeError('Установите Deno или Node.js >= 22 для поддержки YouTube.')
    session_args = {'timeout': config.timeout}
    if config.api_base:
        session_args['api'] = TelegramAPIServer.from_base(config.api_base)
    session = AiohttpSession(**session_args)
    async with Bot(token=config.token, session=session) as bot:
        await bot.set_my_commands([
            BotCommand(command='start', description='Как пользоваться ботом'),
            BotCommand(command='help', description='Помощь'),
            BotCommand(command='id', description='Мой Telegram ID'),
        ])
        # Keep queued messages; long polling needs no public server or webhook.
        await bot.delete_webhook(drop_pending_updates=False)
        await build_dispatcher(config).start_polling(bot, tasks_concurrency_limit=32)


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s: %(message)s')
    try:
        asyncio.run(main())
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None
