import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    token: str
    allowed_users: frozenset[int]
    concurrency: int
    timeout: int
    max_duration: int
    max_upload_bytes: int
    api_base: str
    cookies_file: str

    @classmethod
    def load(cls) -> "Config":
        load_dotenv(Path(__file__).with_name('.env'))
        token = os.getenv('BOT_TOKEN', '').strip()
        if not token or token == 'PUT_YOUR_BOTFATHER_TOKEN_HERE':
            raise ValueError('Укажите BOT_TOKEN в .env (токен от @BotFather).')
        api_base = os.getenv('TELEGRAM_API_BASE', '').strip().rstrip('/')
        upload_mb = int(os.getenv('MAX_UPLOAD_MB', '49'))
        cap = 1990 if api_base else 49
        if not 1 <= upload_mb <= cap:
            raise ValueError(f'MAX_UPLOAD_MB должен быть от 1 до {cap}.')
        config = cls(
            token=token,
            allowed_users=frozenset(int(x.strip()) for x in
                                    os.getenv('ALLOWED_USER_IDS', '').split(',') if x.strip()),
            concurrency=int(os.getenv('MAX_CONCURRENT_DOWNLOADS', '2')),
            timeout=int(os.getenv('DOWNLOAD_TIMEOUT', '900')),
            max_duration=int(os.getenv('MAX_DURATION_SECONDS', '7200')),
            max_upload_bytes=upload_mb * 1_000_000,
            api_base=api_base,
            cookies_file=os.getenv('COOKIES_FILE', '').strip(),
        )
        if min(config.concurrency, config.timeout, config.max_duration) < 1:
            raise ValueError('Лимиты и таймаут должны быть положительными.')
        if config.cookies_file and not Path(config.cookies_file).is_file():
            raise ValueError('Файл COOKIES_FILE не найден.')
        return config
