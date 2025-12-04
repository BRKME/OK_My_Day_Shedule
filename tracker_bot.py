#!/usr/bin/env python3
"""
Telegram Task Tracker Bot - PRODUCTION VERSION
Версия 3.0.0 (исправлено: reply_markup, парсер, callback, IP)
"""

import asyncio
import aiohttp
from aiohttp import web
import json
import logging
from datetime import datetime, timedelta
import os
import re
import signal
import sys
import html
import time
import hashlib
import ipaddress
import random
from typing import Dict, List, Optional, Any, Tuple, Set
from collections import OrderedDict
from asyncio import Lock

# ============================================================================
# КОНСТАНТЫ И НАСТРОЙКИ
# ============================================================================

MAX_STATE_SIZE = 1000
STATE_TTL_SECONDS = 86400
MAX_TASK_DISPLAY_LENGTH = 30
PROGRESS_BAR_LENGTH = 10
MAX_CALLBACK_DATA_BYTES = 64
MAX_MESSAGE_LENGTH = 4000
TELEGRAM_API_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0
RATE_LIMIT_REQUESTS = 100
RATE_LIMIT_WINDOW = 60

# Актуальные на 2025 год диапазоны Telegram
TELEGRAM_IP_RANGES = [
    ipaddress.ip_network('149.154.160.0/20'),
    ipaddress.ip_network('91.108.4.0/22'),
    ipaddress.ip_network('91.108.8.0/22'),
    ipaddress.ip_network('91.108.12.0/22'),
    ipaddress.ip_network('91.108.16.0/22'),
    ipaddress.ip_network('91.108.20.0/22'),
    ipaddress.ip_network('91.108.56.0/22'),
    ipaddress.ip_network('91.105.192.0/23'),
    ipaddress.ip_network('91.108.60.0/22'),
]

# Улучшенные паттерны — работают и с эмодзи, и без
SECTION_PATTERNS = {
    'day': re.compile(r'(?:☀️\s*)?(?:Дневные\s+)?[Зз]адачи\s*:?\s*(.*?)(?=⛔|🌙|🎯|$)', re.IGNORECASE | re.DOTALL),
    'cant_do': re.compile(r'(?:⛔\s*)?(?:Нельзя\s+)?[Дд]елать\s*:?\s*(.*?)(?=🌙|🎯|$)', re.IGNORECASE | re.DOTALL),
    'evening': re.compile(r'(?:🌙\s*)?(?:Вечерние\s+)?[Зз]адачи\s*:?\s*(.*?)(?=🎯|$)', re.IGNORECASE | re.DOTALL),
}

TASK_PATTERN = re.compile(r'•\s*(.+?)(?:\s*\([^)]+\))?\s*$')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s')
logger = logging.getLogger(__name__)

# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ КЛАССЫ
# ============================================================================

class RateLimiter:
    def __init__(self, max_requests: int = RATE_LIMIT_REQUESTS, window: int = RATE_LIMIT_WINDOW):
        self.max_requests = max_requests
        self.window = window
        self.requests: Dict[str, List[float]] = {}
        self.lock = Lock()

    async def is_allowed(self, key: str) -> bool:
        async with self.lock:
            now = time.time()
            if key in self.requests:
                self.requests[key] = [t for t in self.requests[key] if now - t < self.window]
            else:
                self.requests[key] = []
            if len(self.requests[key]) >= self.max_requests:
                return False
            self.requests[key].append(now)
            return True


class StateManager:
    def __init__(self, max_size: int = MAX_STATE_SIZE, ttl: int = STATE_TTL_SECONDS):
        self.max_size = max_size
        self.ttl = ttl
        self.state_store: OrderedDict[str, Tuple[float, Set[int]]] = OrderedDict()
        self.lock = Lock()

    async def get(self, key: str) -> Optional[Set[int]]:
        async with self.lock:
            self._cleanup()
            if key in self.state_store:
                ts, state = self.state_store[key]
                if time.time() - ts < self.ttl:
                    self.state_store.move_to_end(key)
                    return state.copy()
            return None

    async def set(self, key: str, state: Set[int]) -> None:
        async with self.lock:
            self._cleanup()
            if len(self.state_store) >= self.max_size:
                self.state_store.popitem(last=False)
            self.state_store[key] = (time.time(), state.copy())
            self.state_store.move_to_end(key)

    def _cleanup(self):
        now = time.time()
        expired = [k for k, (ts, _) in self.state_store.items() if now - ts > self.ttl]
        for k in expired:
            del self.state_store[k]


class TelegramAPIClient:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        timeout = aiohttp.ClientTimeout(total=TELEGRAM_API_TIMEOUT)
        self.session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session and not self.session.closed:
            await self.session.close()

    async def _make_request(self, method: str, endpoint: str, **kwargs) -> Optional[Dict]:
        for attempt in range(MAX_RETRIES):
            try:
                url = f"{self.base_url}/{endpoint}"
                async with self.session.request(method, url, **kwargs) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get('ok'):
                            return data.get('result')
                        else:
                            logger.error(f"Telegram API error: {data.get('description')}")
                    else:
                        text = await resp.text()
                        logger.error(f"HTTP {resp.status}: {text}")
                    if attempt == MAX_RETRIES - 1:
                        return None
                    await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.5))
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning(f"Network error (attempt {attempt+1}): {e}")
                if attempt == MAX_RETRIES - 1:
                    return None
                await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))
        return None

    async def send_message(self, text: str, **kwargs) -> Optional[Dict]:
        if len(text) > MAX_MESSAGE_LENGTH:
            text = text[:MAX_MESSAGE_LENGTH-100] + "\n...[сообщение обрезано]"
        payload = {'chat_id': self.chat_id, 'text': text, 'parse_mode': 'HTML', 'disable_web_page_preview': True, **kwargs}
        return await self._make_request('POST', 'sendMessage', json=payload)

    async def edit_message(self, message_id: int, text: str, **kwargs) -> bool:
        if len(text) > MAX_MESSAGE_LENGTH:
            text = text[:MAX_MESSAGE_LENGTH-100] + "\n...[сообщение обрезано]"
        payload = {'chat_id': self.chat_id, 'message_id': message_id, 'text': text, 'parse_mode': 'HTML', **kwargs}
        return await self._make_request('POST', 'editMessageText', json=payload) is not None

    async def answer_callback_query(self, callback_query_id: str, **kwargs) -> bool:
        payload = {'callback_query_id': callback_query_id, **kwargs}
        return await self._make_request('POST', 'answerCallbackQuery', json=payload) is not None

    async def set_webhook(self, url: str) -> bool:
        payload = {'url': url, 'drop_pending_updates': True, 'max_connections': 40}
        return await self._make_request('POST', 'setWebhook', json=payload) is not None


class MessageParser:
    @staticmethod
    def sanitize_text(text: str) -> str:
        return '\n'.join(html.escape(line.strip()) for line in text.splitlines() if line.strip())

    @staticmethod
    def parse_tasks(message_text: str) -> Dict[str, List[str]]:
        tasks = {'day': [], 'cant_do': [], 'evening': []}
        safe_text = MessageParser.sanitize_text(message_text)

        for section, pattern in SECTION_PATTERNS.items():
            match = pattern.search(safe_text)
            if match:
                section_text = match.group(1).strip()
                for line in section_text.split('\n'):
                    line = line.strip()
                    if line.startswith('•'):
                        m = TASK_PATTERN.search(line)
                        if m:
                            tasks[section].append(m.group(1).strip())
        logger.info(f"Parsed → Day: {len(tasks['day'])}, Can't do: {len(tasks['cant_do'])}, Evening: {len(tasks['evening'])}")
        return tasks

    @staticmethod
    def truncate_task(task: str, max_length: int = MAX_TASK_DISPLAY_LENGTH) -> str:
        if len(task) <= max_length:
            return task
        truncated = task[:max_length-3]
        last_space = truncated.rfind(' ')
        if last_space > max_length - 10:
            truncated = truncated[:last_space]
        return truncated + '...'


# ============================================================================
# ОСНОВНОЙ БОТ
# ============================================================================

class TaskTrackerBot:
    def __init__(self):
        logger.info("=" * 60)
        logger.info("Initializing Task Tracker Bot v3.0.0")
        logger.info("=" * 60)
        self._load_configuration()
        self.state_manager = StateManager()
        self.rate_limiter = RateLimiter()
        self.start_time = time.time()
        self.shutdown_event = asyncio.Event()
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        logger.info("Bot initialized successfully")

    def _load_configuration(self):
        self.telegram_token = os.getenv('TELEGRAM_TOKEN')
        self.chat_id = os.getenv('TELEGRAM_CHAT_ID')
        if not self.telegram_token or not self.chat_id:
            raise ValueError("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID are required")
        if not re.match(r'^\d{9,10}:[A-Za-z0-9_-]{35}$', self.telegram_token):
            raise ValueError("Invalid token format")
        self.chat_id_int = int(self.chat_id)
        self.port = int(os.getenv('PORT', '8080'))
        domain = os.getenv('RAILWAY_PUBLIC_DOMAIN')
        self.webhook_url = f"https://{domain}/webhook" if domain else None
        logger.info(f"Config → Port: {self.port}, Chat ID: {self.chat_id_int}, Webhook: {self.webhook_url or 'None'}")

    @staticmethod
    def _validate_ip_address(ip_str: str) -> bool:
        try:
            ip = ipaddress.ip_address(ip_str)
            return any(ip in net for net in TELEGRAM_IP_RANGES)
        except ValueError:
            return False

    def _create_callback_data(self, action: str, section: str, idx: int) -> str:
        data = f"{action}_{section}_{idx}"
        if len(data.encode()) > MAX_CALLBACK_DATA_BYTES:
            h = hashlib.md5(data.encode()).hexdigest()[:12]
            data = f"{action}_{section}_{h}_{idx}"
        return data

    def create_checklist_keyboard(self, tasks: Dict[str, List[str]], completed: Dict[str, Set[int]]) -> Dict:
        keyboard = []
        sections = [('day', 'ДНЕВНЫЕ ЗАДАЧИ', 'day'), ('cant_do', 'НЕЛЬЗЯ ДЕЛАТЬ', 'cant'), ('evening', 'ВЕЧЕРНИЕ ЗАДАЧИ', 'eve')]
        for key, title, prefix in sections:
            if tasks[key]:
                keyboard.append([{'text': title, 'callback_data': f'header_{prefix}'}])
                for i, task in enumerate(tasks[key]):
                    emoji = 'Done' if i in completed.get(key, set()) else 'Not Done'
                    disp = MessageParser.truncate_task(task)
                    keyboard.append([{'text': f'{emoji} {i+1}. {disp}', 'callback_data': self._create_callback_data('toggle', prefix, i)}])
        keyboard.append([{'text': 'Сохранить прогресс', 'callback_data': 'save_progress'},
                         {'text': 'Отменить', 'callback_data': 'cancel_update'}])
        return {'inline_keyboard': keyboard}

    def format_checklist_message(self, tasks: Dict[str, List[str]], completed: Dict[str, Set[int]]) -> str:
        lines = ["<b>Отметь выполненные задачи:</b>\n"]
        total = done = 0
        titles = {'day': 'ДНЕВНЫЕ ЗАДАЧИ:', 'cant_do': 'НЕЛЬЗЯ ДЕЛАТЬ:', 'evening': 'ВЕЧЕРНИЕ ЗАДАЧИ:'}
        for key, title in titles.items():
            if tasks[key]:
                lines.append(f"\n<b>{title}</b>")
                for i, task in enumerate(tasks[key]):
                    emoji = 'Done' if i in completed.get(key, set()) else 'Not Done'
                    disp = MessageParser.truncate_task(task)
                    lines.append(f"{emoji} {disp}")
                    total += 1
                    if i in completed.get(key, set()):
                        done += 1
        if total:
            perc = int(done / total * 100)
            bar = 'Full' * (perc // 10) + 'Empty' * (10 - perc // 10)
            lines.append(f"\n<b>ПРОГРЕСС:</b>")
            lines.append(f"{bar} {done}/{total} ({perc}%)")
        lines.append("\n<i>Нажми на задачу, чтобы отметить</i>")
        return '\n'.join(lines)

    async def process_callback_query(self, data: str, query_id: str, msg_id: int, text: str):
        try:
            if data == 'save_progress':
                await self._handle_save_progress(query_id, msg_id, text)
            elif data == 'cancel_update':
                await self._handle_cancel_update(query_id, msg_id)
            elif data.startswith('toggle_'):
                await self._handle_toggle_task(data, query_id, msg_id, text)
            else:
                async with TelegramAPIClient(self.telegram_token, self.chat_id) as client:
                    await client.answer_callback_query(query_id)
        except Exception as e:
            logger.error(f"Callback error: {e}", exc_info=True)

    async def _handle_save_progress(self, query_id: str, msg_id: int, old_text: str):
        async with TelegramAPIClient(self.telegram_token, self.chat_id) as client:
            await client.answer_callback_query(query_id, text="Прогресс сохранён!", show_alert=False)
            new_text = old_text.replace("Отметь выполненные задачи:", "ПРОГРЕСС СОХРАНЁН\n\nВЫПОЛНЕННЫЕ ЗАДАЧИ:")
            await client.edit_message(msg_id, new_text)  # reply_markup не передаём → Telegram уберёт клавиатуру
            logger.info(f"Progress saved for message {msg_id}")

    async def _handle_cancel_update(self, query_id: str, msg_id: int):
        async with TelegramAPIClient(self.telegram_token, self.chat_id) as client:
            await client.answer_callback_query(query_id, text="Отменено", show_alert=False)
            await client.edit_message(msg_id, "ОБНОВЛЕНИЕ ОТМЕНЕНО")  # без клавиатуры

    async def _handle_toggle_task(self, data: str, query_id: str, msg_id: int, old_text: str):
        try:
            parts = data.split('_')
            if len(parts) < 3:
                raise ValueError("Invalid callback")
            section_code = parts[1]
            idx = int(parts[-1])
            section_map = {'day': 'day', 'cant': 'cant_do', 'eve': 'evening'}
            section = section_map.get(section_code)
            if not section:
                raise ValueError("Unknown section")

            state_key = f"{msg_id}_{section}"
            state = await self.state_manager.get(state_key) or set()
            state.symmetric_difference_update([idx])
            await self.state_manager.set(state_key, state)

            tasks = MessageParser.parse_tasks(old_text)
            completed = {section: state}
            new_text = self.format_checklist_message(tasks, completed)
            keyboard = self.create_checklist_keyboard(tasks, completed)

            async with TelegramAPIClient(self.telegram_token, self.chat_id) as client:
                await client.answer_callback_query(query_id)
                await client.edit_message(msg_id, new_text, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Toggle error: {e}")
            async with TelegramAPIClient(self.telegram_token, self.chat_id) as client:
                await client.answer_callback_query(query_id, text="Ошибка", show_alert=True)

    async def process_schedule_message(self, text: str) -> bool:
        tasks = MessageParser.parse_tasks(text)
        if not any(tasks.values()):
            return False
        msg = self.format_checklist_message(tasks, {})
        kb = self.create_checklist_keyboard(tasks, {}) or {}
        async with TelegramAPIClient(self.telegram_token, self.chat_id) as client:
            result = await client.send_message(msg, reply_markup=kb)
            logger.info(f"Checklist sent: {result.get('message_id') if result else 'failed'}")
            return bool(result)

    async def setup_webhook(self) -> bool:
        if not self.webhook_url:
            return False
        async with TelegramAPIClient(self.telegram_token, self.chat_id) as client:
            logger.info(f"Setting webhook → {self.webhook_url}")
            return await client.set_webhook(self.webhook_url)

    async def handle_webhook_request(self, request: web.Request) -> web.Response:
        try:
            client_ip = (request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or request.remote)
            if not self._validate_ip_address(client_ip):
                logger.warning(f"Blocked IP: {client_ip}")
                return web.Response(text="Unauthorized", status=403)

            if not await self.rate_limiter.is_allowed(f"webhook_{client_ip}"):
                return web.Response(text="Too Many Requests", status=429)

            data = await request.json()

            if 'callback_query' in data:
                cq = data['callback_query']
                await self.process_callback_query(
                    cq.get('data', ''),
                    cq.get('id', ''),
                    cq['message'].get('message_id'),
                    cq['message'].get('text', '')
                )
            elif 'message' in data:
                msg = data['message']
                if msg.get('chat', {}).get('id') == self.chat_id_int:
                    text = msg.get('text', '')
                    if any(m in text for m in ['•', 'Дневные', 'Вечерние', 'Нельзя']):
                        await self.process_schedule_message(text)

            return web.Response(text="OK")
        except Exception as e:
            logger.error(f"Webhook error: {e}", exc_info=True)
            return web.Response(status=500)

    def _handle_signal(self, signum, frame):
        logger.info(f"Signal {signum} → shutdown")
        self.shutdown_event.set()

    async def start_http_server(self):
        app = web.Application(client_max_size=10*1024*1024)
        app.router.add_get('/', lambda r: web.Response(text=f"Task Tracker Bot v3.0.0\nChat: {self.chat_id_int}\nUptime: {timedelta(seconds=int(time.time()-self.start_time))}"))
        app.router.add_get('/health', lambda r: web.json_response({'status': 'ok', 'version': '3.0.0'}))
        app.router.add_post('/webhook', self.handle_webhook_request)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', self.port)
        await site.start()
        logger.info(f"HTTP server on port {self.port}")
        return runner

    async def run(self):
        logger.info("Starting Task Tracker Bot...")
        runner = await self.start_http_server()
        if self.webhook_url:
            await self.setup_webhook()
            logger.info("Webhook mode")
        logger.info("Bot is fully operational and ready")
        logger.info("=" * 60)
        await self.shutdown_event.wait()
        await runner.cleanup()
        logger.info("Bot stopped")


if __name__ == "__main__":
    try:
        bot = TaskTrackerBot()
        asyncio.run(bot.run())
    except Exception as e:
        logger.error(f"Fatal: {e}", exc_info=True)
        sys.exit(1)
