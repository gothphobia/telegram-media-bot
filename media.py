"""Download in a separate process so deadlines can stop yt-dlp and ffmpeg."""
import asyncio
import json
import os
import re
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

from config import Config


class MediaError(Exception):
    """A message suitable for the bot's user."""


def extract_url(text: str) -> str:
    urls = re.findall(r'https?://[^\s<>]+', text)
    if len(urls) != 1:
        raise MediaError('Пришлите одну ссылку на видео YouTube, Shorts или TikTok.')
    url = urls[0].rstrip('.,!;:)\"\'»')
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
            raise ValueError
    except ValueError:
        raise MediaError('Некорректная ссылка.') from None
    youtube = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'}
    if host in youtube:
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == '/watch' and re.fullmatch(r'[A-Za-z0-9_-]+', query.get('v', [''])[0]):
            return 'https://www.youtube.com/watch?v=' + query['v'][0]
        if re.fullmatch(r'/(shorts|embed)/[A-Za-z0-9_-]+/?', path):
            return urlunsplit(('https', 'www.youtube.com', path, '', ''))
    elif host == 'youtu.be' and re.fullmatch(r'/[A-Za-z0-9_-]+/?', parsed.path):
        return urlunsplit(('https', host, parsed.path, '', ''))
    elif host in {'tiktok.com', 'www.tiktok.com', 'm.tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com'}:
        if (re.fullmatch(r'/@[^/]+/video/\d+/?', parsed.path)
                or re.fullmatch(r'/(?:t/)?[A-Za-z0-9]+/?', parsed.path)):
            return urlunsplit(('https', host, parsed.path, '', ''))
    raise MediaError('Нужна ссылка на отдельное видео YouTube / Shorts / TikTok.')


def command(config: Config, url: str, kind: str, folder: Path, *, probe: bool = False) -> list[str]:
    if kind not in {'mp3', 'mp4'}:
        raise ValueError('Unsupported format')
    args = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist',
            '--no-progress', '--no-warnings', '--socket-timeout', '30',
            '--retries', '3', '--fragment-retries', '3', '--js-runtimes', 'deno',
            '--js-runtimes', 'node',
            '--output', str(folder / 'media.%(ext)s')]
    if config.cookies_file:
        args += ['--cookies', str(Path(config.cookies_file).resolve())]
    if probe:
        return args + ['--dump-single-json', '--skip-download', '--', url]
    args += ['--max-filesize', str(config.max_upload_bytes),
             '--print', 'after_move:filepath']
    if kind == 'mp3':
        args += ['-f', 'bestaudio/best', '-x', '--audio-format', 'mp3', '--audio-quality', '0']
    else:
        # Copy source streams into MP4 without lossy video re-encoding.
        args += ['-f', 'bestvideo+bestaudio/best', '--merge-output-format', 'mp4',
                 '--remux-video', 'mp4']
    return args + ['--', url]


async def run(args: list[str], folder: Path, timeout: float, disk_limit: int) -> str:
    """Bound execution time and scratch space, including unknown-size streams."""
    with (folder / 'stdout.log').open('wb') as out, (folder / 'stderr.log').open('wb') as err:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=out, stderr=err, start_new_session=(os.name == 'posix'))
        try:
            async with asyncio.timeout(timeout):
                while process.returncode is None:
                    used = sum(p.stat().st_size for p in folder.iterdir() if p.is_file())
                    if used > disk_limit:
                        raise MediaError('Видео слишком большое для установленного лимита. '
                                         'Для больших файлов нужен локальный Bot API.')
                    try:
                        await asyncio.wait_for(process.wait(), timeout=0.5)
                    except TimeoutError:
                        continue
        finally:
            if process.returncode is None:
                try:
                    if os.name == 'posix':
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
    if process.returncode:
        # Do not expose URLs, cookies, server paths or tokens via error messages.
        error = (folder / 'stderr.log').read_text(errors='replace').lower()
        if 'too large' in error or 'max-filesize' in error:
            raise MediaError('Файл превышает лимит отправки бота. Выберите MP3 '
                             'или подключите локальный Bot API для больших MP4.')
        raise MediaError('Площадка не отдала видео. Оно может быть закрыто, удалено '
                         'или требовать входа. Попробуйте обновить yt-dlp; '
                         'если нужен вход, администратор может настроить cookies.')
    return (folder / 'stdout.log').read_text(errors='replace')


@dataclass(frozen=True)
class Download:
    path: Path
    title: str


async def download(config: Config, url: str, kind: str, folder: Path) -> Download:
    try:
        async with asyncio.timeout(config.timeout):
            raw = await run(command(config, url, kind, folder, probe=True), folder,
                            config.timeout, 32_000_000)
            try:
                info = json.loads(raw)
            except (ValueError, TypeError):
                raise MediaError('Не удалось получить информацию о видео.') from None
            if info.get('_type') in {'playlist', 'multi_video'}:
                raise MediaError('Поддерживаются только отдельные видео.')
            if info.get('is_live') or info.get('live_status') in {'is_live', 'is_upcoming', 'post_live'}:
                raise MediaError('Дождитесь завершения и обработки трансляции.')
            duration = info.get('duration')
            if duration and duration > config.max_duration:
                raise MediaError(f'Лимит длительности: {config.max_duration // 60} мин.')
            await run(command(config, url, kind, folder), folder, config.timeout,
                      config.max_upload_bytes * 4 + 32_000_000)
            path = folder / f'media.{kind}'
            if not path.is_file():
                raise MediaError('Готовый файл не получен: возможно, он превышает '
                                 'лимит размера или нужный формат недоступен.')
            if path.stat().st_size > config.max_upload_bytes:
                raise MediaError('Файл превышает лимит отправки. Для большого MP4 '
                                 'подключите локальный Bot API или выберите MP3.')
            if path.stat().st_size == 0:
                raise MediaError('Площадка вернула пустой файл.')
            return Download(path, str(info.get('title') or 'Видео')[:180])
    except TimeoutError:
        raise MediaError('Время загрузки истекло. Попробуйте более короткое видео.') from None
