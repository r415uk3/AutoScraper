from __future__ import annotations

import os
import sys
import json
import stat
import time
import signal
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
import aiohttp

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
LOGIN_URL = "https://wsp.kbtu.kz/RegistrationOnline"

BUTTON_CAPTION = "span.v-button-caption"
# Vaadin renders some empty, hidden caption spans before the real ones.
VISIBLE_CAPTION = f"{BUTTON_CAPTION}:visible"
PASSWORD_INPUT = "input[type='password']"
LOGIN_CAPTIONS = ("Кіру", "Войти", "Login")
CHECK_IN_CAPTION = "Отметиться"

# Long sleeps are split into chunks so the window is re-checked after the PC wakes from sleep.
MAX_IDLE_CHUNK = 300

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"


class ConfigError(Exception):
    """Raised when .env or users.json is invalid."""


class LoginFailed(Exception):
    """Raised when all login attempts are exhausted for a user."""


class BrowserDisconnected(Exception):
    """Raised when the shared browser has died and must be relaunched."""


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}")
    if value < 1:
        raise ConfigError(f"{name} must be positive, got {value}")
    return value


def parse_days(spec: str) -> frozenset[int]:
    """Parse ISO weekdays like "1-5,7" (1 = Monday). Empty means every day."""
    if not spec.strip():
        return frozenset(range(1, 8))
    days = set()
    try:
        for part in spec.split(","):
            if "-" in part:
                first, last = part.split("-", 1)
                days.update(range(int(first), int(last) + 1))
            else:
                days.add(int(part))
    except ValueError:
        raise ConfigError(f"ACTIVE_DAYS must look like '1-6' or '1,3,5', got {spec!r}")
    if not days or not days <= set(range(1, 8)):
        raise ConfigError(f"ACTIVE_DAYS must use days 1 (Mon) to 7 (Sun), got {spec!r}")
    return frozenset(days)


def parse_hours(spec: str) -> tuple[dtime, dtime] | None:
    """Parse a daily window like "07:30-21:30". Empty means all day."""
    if not spec.strip():
        return None
    try:
        start, end = (datetime.strptime(s.strip(), "%H:%M").time() for s in spec.split("-", 1))
    except ValueError:
        raise ConfigError(f"ACTIVE_HOURS must look like '07:30-21:30', got {spec!r}")
    if start >= end:
        raise ConfigError(f"ACTIVE_HOURS start must be before end, got {spec!r}")
    return start, end


def seconds_until_active(now: datetime, days: frozenset[int], hours: tuple[dtime, dtime] | None) -> float:
    """0 if `now` is inside the active window, otherwise seconds until it next opens."""
    for offset in range(8):
        day = (now + timedelta(days=offset)).date()
        if day.isoweekday() not in days:
            continue
        if hours is None:
            start = datetime.combine(day, dtime.min)
            end = start + timedelta(days=1)
        else:
            start = datetime.combine(day, hours[0])
            end = datetime.combine(day, hours[1])
        if now < end:
            return max(0.0, (start - now).total_seconds())
    raise AssertionError("ACTIVE_DAYS is never empty")


@dataclass(frozen=True)
class Config:
    refresh_interval: int = 35
    retry_delay: int = 30
    max_retry_delay: int = 300
    login_max_attempts: int = 3
    alert_repeat_interval: int = 3600
    active_days: frozenset[int] = frozenset(range(1, 7))
    active_hours: tuple[dtime, dtime] | None = (dtime(7, 30), dtime(21, 30))
    # Empty = Playwright's bundled Chromium; "chrome" = installed Google Chrome.
    browser_channel: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            refresh_interval=env_int("REFRESH_INTERVAL", cls.refresh_interval),
            retry_delay=env_int("RETRY_DELAY", cls.retry_delay),
            max_retry_delay=env_int("MAX_RETRY_DELAY", cls.max_retry_delay),
            login_max_attempts=env_int("LOGIN_MAX_ATTEMPTS", cls.login_max_attempts),
            alert_repeat_interval=env_int("ALERT_REPEAT_INTERVAL", cls.alert_repeat_interval),
            active_days=parse_days(os.getenv("ACTIVE_DAYS", "1-6")),
            active_hours=parse_hours(os.getenv("ACTIVE_HOURS", "07:30-21:30")),
            browser_channel=os.getenv("BROWSER_CHANNEL", "").strip(),
        )


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        LOG_DIR / "autoscraper.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


def load_users(path: Path = BASE_DIR / "users.json") -> list[dict]:
    try:
        # utf-8-sig also accepts the BOM that Windows Notepad adds.
        with open(path, "r", encoding="utf-8-sig") as f:
            users = json.load(f)
    except FileNotFoundError:
        raise ConfigError(f"{path.name} not found, create it as described in README.md")
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path.name} is not valid JSON: {e}")

    if not isinstance(users, list) or not users:
        raise ConfigError(f"{path.name} must be a non-empty list of users")
    seen = set()
    for i, user in enumerate(users, 1):
        if not isinstance(user, dict):
            raise ConfigError(f"{path.name}: user #{i} must be an object")
        for key in ("username", "password"):
            value = user.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"{path.name}: user #{i} has a missing or empty '{key}'")
        if user["username"] in seen:
            raise ConfigError(f"{path.name}: user '{user['username']}' is listed twice")
        seen.add(user["username"])
        handle = user.get("telegram")
        if handle is not None and (not isinstance(handle, str) or not normalize_handle(handle)):
            raise ConfigError(f"{path.name}: user #{i} 'telegram' must be a Telegram @username")
        if "telegram_chat_id" in user:
            logging.warning(
                "%s: '%s' has 'telegram_chat_id', which is no longer used. "
                "Add \"telegram\": \"@their_username\" and have them send /start to the bot.",
                path.name, user["username"],
            )
    return users


def normalize_handle(handle: str | None) -> str:
    return (handle or "").strip().lstrip("@").lower()


def write_json_atomic(path: Path, data):
    """Replace `path` without leaving a half-written file, keeping its permissions (e.g. chmod 600)."""
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        mode = 0o600
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, mode)  # the mode given to os.open is reduced by umask
    os.replace(tmp, path)


def update_password(path: Path, username: str, password: str):
    """Change one user's password in users.json, keeping everything else."""
    with open(path, "r", encoding="utf-8-sig") as f:
        users = json.load(f)
    for user in users:
        if user.get("username") == username:
            user["password"] = password
            break
    else:
        raise KeyError(username)
    write_json_atomic(path, users)


class Telegram:
    def __init__(self, token: str | None, session: aiohttp.ClientSession | None, api_url: str = "https://api.telegram.org"):
        self.token = token
        self.session = session
        self.api_url = api_url

    async def call(self, method: str, http_timeout: float = 10, **params):
        """Call a Bot API method. Returns its result, or None on any failure."""
        if not self.token:
            return None
        url = f"{self.api_url}/bot{self.token}/{method}"
        try:
            async with self.session.post(
                url, json=params, timeout=aiohttp.ClientTimeout(total=http_timeout)
            ) as resp:
                data = await resp.json(content_type=None)
        except Exception as e:
            logging.warning("Telegram %s failed: %s", method, e)
            return None
        if not data.get("ok"):
            logging.warning("Telegram %s failed: %s", method, data.get("description"))
            return None
        return data["result"]

    async def send(self, chat_id, message: str) -> bool:
        if not chat_id:
            return False
        return await self.call("sendMessage", chat_id=chat_id, text=message) is not None


class BotState:
    """Persistent bot data: which Telegram chat belongs to which KBTU account, and who is paused.

    links: {kbtu_username: {"user_id": int, "chat_id": int, "handle": str}}
    """

    def __init__(self, path: Path):
        self.path = path
        self.links: dict[str, dict] = {}
        self.paused: set[str] = set()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except json.JSONDecodeError as e:
            raise ConfigError(f"{path.name} is not valid JSON: {e}")
        self.links = data.get("links", {})
        self.paused = set(data.get("paused", []))

    def save(self):
        write_json_atomic(self.path, {"links": self.links, "paused": sorted(self.paused)})

    def prune(self, users: list[dict]):
        """Drop links whose account is gone or whose configured @username changed."""
        handles = {u["username"]: normalize_handle(u.get("telegram")) for u in users}
        stale = [
            login for login, link in self.links.items()
            if not handles.get(login) or handles[login] != link.get("handle")
        ]
        for login in stale:
            logging.info("Unlinking '%s' from Telegram: its @username in users.json changed", login)
            del self.links[login]
        gone = self.paused - set(handles)
        self.paused -= gone
        if stale or gone:
            self.save()

    def chat_id(self, login: str):
        link = self.links.get(login)
        return link["chat_id"] if link else None

    def accounts_of(self, user_id: int) -> list[str]:
        return sorted(login for login, link in self.links.items() if link["user_id"] == user_id)


class Notifier:
    """Per-user Telegram messages that don't spam.

    A problem is sent when it first appears and then at most once per
    `repeat_interval` while it persists. Recovery is announced once.
    `chat_id` is a callable, so messages go wherever the account is linked right now.
    """

    def __init__(self, telegram: Telegram, chat_id, username: str, repeat_interval: int, clock=time.monotonic):
        self.telegram = telegram
        self.chat_id = chat_id
        self.username = username
        self.repeat_interval = repeat_interval
        self.clock = clock
        self.active: dict[str, float] = {}

    async def info(self, text: str):
        await self.telegram.send(self.chat_id(), f"[{self.username}] {text}")

    async def problem(self, key: str, text: str):
        now = self.clock()
        last_sent = self.active.get(key)
        if last_sent is None or now - last_sent >= self.repeat_interval:
            self.active[key] = now
            await self.info(text)

    async def resolved(self, key: str, text: str | None = None):
        if self.active.pop(key, None) is not None and text:
            await self.info(text)


async def open_portal(page):
    """Load the portal and wait until the Vaadin UI has rendered its buttons."""
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
    await page.locator(VISIBLE_CAPTION).first.wait_for(state="visible", timeout=30_000)


async def login_form_visible(page) -> bool:
    captions = await page.locator(BUTTON_CAPTION).all_text_contents()
    if any(c.strip() in LOGIN_CAPTIONS for c in captions):
        return True
    return await page.locator(PASSWORD_INPUT).count() > 0


async def do_login(page, username: str, password: str, log: logging.Logger) -> bool:
    log.info("Opening login page...")
    await open_portal(page)
    if not await login_form_visible(page):
        log.info("Already logged in")
        return True

    log.info("Filling credentials...")
    username_input = page.locator("input.v-filterselect-input").first
    await username_input.wait_for(state="visible", timeout=15_000)
    await username_input.fill(username)
    # Give the Vaadin combobox a moment to process the typed value.
    await page.wait_for_timeout(500)

    await page.locator(PASSWORD_INPUT).first.fill(password)
    await page.locator("div.v-button.primary").first.click()

    try:
        await page.locator(PASSWORD_INPUT).first.wait_for(state="hidden", timeout=15_000)
        await page.locator(VISIBLE_CAPTION).first.wait_for(state="visible", timeout=15_000)
    except PlaywrightTimeout:
        pass
    if await login_form_visible(page):
        log.warning("Login failed: login form is still visible")
        return False

    log.info("Login successful")
    return True


async def login_with_retries(page, username: str, password: str, max_attempts: int, log: logging.Logger):
    """Try to login up to `max_attempts` times. Raises LoginFailed if all fail."""
    for attempt in range(1, max_attempts + 1):
        try:
            if await do_login(page, username, password, log):
                return
        except Exception as e:
            log.warning("Login attempt %d/%d error: %s", attempt, max_attempts, e)

        if attempt < max_attempts:
            log.info("Login failed, retrying (%d/%d)...", attempt, max_attempts)
            await asyncio.sleep(5)

    raise LoginFailed(f"Login failed after {max_attempts} attempts")


async def try_check_in(page, log: logging.Logger) -> bool | None:
    """Click the check-in button if it is shown.

    Returns None if there is no button, True if the button went away after
    the click (confirmed), False if it is still there (not confirmed).
    """
    button = page.locator(
        "div.v-button",
        has=page.locator(BUTTON_CAPTION, has_text=CHECK_IN_CAPTION),
    ).first
    try:
        await button.wait_for(state="visible", timeout=5000)
    except PlaywrightTimeout:
        visible = [b for b in await page.locator(BUTTON_CAPTION).all_text_contents() if b.strip()]
        log.info("'%s' not found. Buttons: %s", CHECK_IN_CAPTION, visible)
        return None

    await button.click()
    log.info("CLICKED '%s'!", CHECK_IN_CAPTION)
    try:
        await button.wait_for(state="hidden", timeout=10_000)
        return True
    except PlaywrightTimeout:
        log.warning("'%s' is still visible after the click", CHECK_IN_CAPTION)
        return False


def fmt_time(moment: datetime | None) -> str:
    if moment is None:
        return "never"
    if moment.date() == datetime.now().date():
        return f"today {moment:%H:%M:%S}"
    return f"{moment:%d.%m %H:%M}"


class UserRunner:
    def __init__(self, user: dict, cfg: Config, telegram: Telegram, bot_state: BotState):
        self.username = user["username"]
        self.password = user["password"]
        self.telegram_handle = normalize_handle(user.get("telegram"))
        self.cfg = cfg
        self.bot_state = bot_state
        self.log = logging.getLogger(self.username)
        self.notifier = Notifier(
            telegram, lambda: bot_state.chat_id(self.username), self.username, cfg.alert_repeat_interval
        )
        self.failures = 0
        # Set by the bot (/resume, /password) to cut a sleep short.
        self.wakeup = asyncio.Event()
        self.status = "starting"
        self.last_check: datetime | None = None
        self.last_marked: datetime | None = None

    @property
    def paused(self) -> bool:
        return self.username in self.bot_state.paused

    def describe(self) -> str:
        return (
            f"{self.username}: {self.status}\n"
            f"Last check: {fmt_time(self.last_check)}\n"
            f"Last marked: {fmt_time(self.last_marked)}"
        )

    async def nap(self, seconds: float):
        """Sleep, but wake up early if the bot pokes this runner."""
        try:
            await asyncio.wait_for(self.wakeup.wait(), seconds)
        except asyncio.TimeoutError:
            pass
        self.wakeup.clear()

    async def mark_healthy(self):
        self.failures = 0
        self.status = "working"
        await self.notifier.resolved("failing", "Working again")

    async def wait_until_runnable(self):
        """Block while paused or outside the active window."""
        announced = None
        while True:
            if self.paused:
                self.status = "paused (send /resume to continue)"
                wait = MAX_IDLE_CHUNK
            else:
                wait = seconds_until_active(datetime.now(), self.cfg.active_days, self.cfg.active_hours)
                if wait > 0:
                    resume = datetime.now() + timedelta(seconds=wait)
                    self.status = f"outside active hours, idle until {resume:%a %H:%M}"
            if wait <= 0:
                return
            if self.status != announced:
                self.log.info("%s", self.status.capitalize())
                announced = self.status
            await self.nap(min(wait, MAX_IDLE_CHUNK))

    async def login(self, page):
        self.status = "logging in"
        await login_with_retries(page, self.username, self.password, self.cfg.login_max_attempts, self.log)

    async def run_once(self, browser):
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        try:
            page = await context.new_page()
            refresh_count = 0
            while True:
                await self.wait_until_runnable()

                refresh_count += 1
                self.log.info("Refresh #%d", refresh_count)
                try:
                    await open_portal(page)
                except PlaywrightTimeout:
                    self.log.warning("Page load timed out, will retry next cycle")
                    await self.nap(self.cfg.refresh_interval)
                    continue

                if await login_form_visible(page):
                    self.log.info("Not logged in, logging in...")
                    await self.login(page)
                await self.mark_healthy()

                result = await try_check_in(page, self.log)
                self.last_check = datetime.now()
                if result is True:
                    self.last_marked = self.last_check
                    await self.notifier.resolved("check-in")
                    await self.notifier.info(f"ATTENDANCE MARKED at {self.last_marked:%H:%M:%S}")
                elif result is False:
                    await self.notifier.problem(
                        "check-in",
                        f"Clicked '{CHECK_IN_CAPTION}' at {self.last_check:%H:%M:%S}, but the button "
                        "is still there. It may not have registered; will keep trying.",
                    )

                await self.nap(self.cfg.refresh_interval)
        finally:
            try:
                await context.close()
            except Exception:
                pass  # the browser may already be gone; don't hide the original error

    async def run_forever(self, browser):
        """Never gives up: restarts with increasing delay on failure, reset after a healthy cycle."""
        while True:
            try:
                await self.run_once(browser)
            except Exception as e:
                if not browser.is_connected():
                    raise BrowserDisconnected(f"browser disconnected ({e})") from e

                self.failures += 1
                delay = min(self.cfg.retry_delay * self.failures, self.cfg.max_retry_delay)
                if isinstance(e, LoginFailed):
                    self.log.error("%s. Retrying in %ds...", e, delay)
                    self.status = f"login failing, retrying in {delay}s"
                    text = f"{e}. If your password changed, send /password <new password>."
                else:
                    self.log.exception("Crashed: %s. Restarting in %ds...", e, delay)
                    self.status = f"error ({e}), retrying in {delay}s"
                    text = f"Crashed: {e}"
                await self.notifier.problem(
                    "failing", f"{text}\nRetrying in {delay}s (attempt #{self.failures}). "
                    "You'll get a message when it works again."
                )
                await self.nap(delay)


HELP_TEXT = (
    "Commands:\n"
    "/status - is attendance marking working\n"
    "/pause - stop marking attendance for now\n"
    "/resume - start marking again\n"
    "/password <new password> - update your KBTU password\n"
    "/stop - stop getting messages here\n"
    "\n"
    "With several KBTU accounts, add the login: /pause <login>, /password <login> <new password>"
)

BOT_COMMANDS = [
    {"command": "status", "description": "Is attendance marking working"},
    {"command": "pause", "description": "Stop marking attendance for now"},
    {"command": "resume", "description": "Start marking attendance again"},
    {"command": "password", "description": "Update your KBTU password"},
    {"command": "stop", "description": "Stop getting messages here"},
]


class Bot:
    """Telegram bot that links people to their KBTU accounts and answers commands in private chats."""

    def __init__(self, telegram: Telegram, runners: list[UserRunner], bot_state: BotState, users_path: Path):
        self.telegram = telegram
        self.runners = {r.username: r for r in runners}
        self.bot_state = bot_state
        self.users_path = users_path

    async def run(self):
        await self.telegram.call("setMyCommands", commands=BOT_COMMANDS)
        for login, runner in self.runners.items():
            if not runner.telegram_handle:
                logging.info("'%s' has no \"telegram\" in users.json, it won't get messages", login)
            elif login not in self.bot_state.links:
                logging.info("'%s' is not linked yet: @%s should send /start to the bot", login, runner.telegram_handle)

        offset = None
        delay = 5
        while True:
            updates = await self.telegram.call(
                "getUpdates", http_timeout=60, offset=offset, timeout=50, allowed_updates=["message"]
            )
            if updates is None:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)
                continue
            delay = 5
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    await self.handle(update.get("message"))
                except Exception:
                    logging.exception("Bot failed to handle a message")

    async def reply(self, message: dict, text: str):
        await self.telegram.send(message["chat"]["id"], text)

    async def handle(self, message: dict | None):
        if not message or message.get("chat", {}).get("type") != "private" or "from" not in message:
            return
        text = (message.get("text") or "").strip()
        command, _, args = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        args = args.strip()

        if command == "/start":
            await self.cmd_start(message)
        elif command == "/help":
            await self.reply(message, HELP_TEXT)
        elif command == "/status":
            await self.cmd_status(message, args)
        elif command == "/pause":
            await self.cmd_pause(message, args, pause=True)
        elif command == "/resume":
            await self.cmd_pause(message, args, pause=False)
        elif command == "/stop":
            await self.cmd_stop(message, args)
        elif command == "/password":
            await self.cmd_password(message, args)
        else:
            await self.reply(message, "Unknown command.\n\n" + HELP_TEXT)

    def target_accounts(self, message: dict, args: str) -> tuple[list[str], str]:
        """Accounts a command applies to: the one named in the first argument, or all linked ones."""
        linked = self.bot_state.accounts_of(message["from"]["id"])
        first, _, rest = args.partition(" ")
        if first in linked:
            return [first], rest.strip()
        return linked, args

    async def cmd_start(self, message: dict):
        user = message["from"]
        handle = normalize_handle(user.get("username"))
        matches = [login for login, r in self.runners.items() if handle and r.telegram_handle == handle]

        for login in matches:
            previous = self.bot_state.links.get(login)
            if previous and previous["user_id"] != user["id"]:
                await self.telegram.send(
                    previous["chat_id"], f"[{login}] This account was linked to another Telegram account."
                )
            self.bot_state.links[login] = {"user_id": user["id"], "chat_id": message["chat"]["id"], "handle": handle}
        if matches:
            self.bot_state.save()
            logging.info("Linked %s to @%s", ", ".join(matches), handle)

        linked = self.bot_state.accounts_of(user["id"])
        if linked:
            await self.reply(
                message,
                f"Linked to KBTU account: {', '.join(linked)}. "
                f"You'll get attendance notifications here.\n\n{HELP_TEXT}",
            )
        elif handle:
            await self.reply(message, f"I don't know @{handle}. Ask the admin to add it to users.json, then send /start again.")
        else:
            await self.reply(
                message,
                "You don't have a Telegram @username. Set one in Telegram settings, "
                "send it to the admin, then send /start again.",
            )

    async def require_accounts(self, message: dict, args: str) -> tuple[list[str], str]:
        accounts, rest = self.target_accounts(message, args)
        if not accounts:
            await self.reply(message, "Your Telegram isn't linked to a KBTU account. Send /start first.")
        return accounts, rest

    async def cmd_status(self, message: dict, args: str):
        accounts, _ = await self.require_accounts(message, args)
        if accounts:
            await self.reply(message, "\n\n".join(self.runners[login].describe() for login in accounts))

    async def cmd_pause(self, message: dict, args: str, pause: bool):
        accounts, _ = await self.require_accounts(message, args)
        if not accounts:
            return
        if pause:
            self.bot_state.paused.update(accounts)
        else:
            self.bot_state.paused.difference_update(accounts)
        self.bot_state.save()
        for login in accounts:
            self.runners[login].wakeup.set()
        names = ", ".join(accounts)
        logging.info("%s %s via Telegram", names, "paused" if pause else "resumed")
        if pause:
            await self.reply(message, f"Paused {names}. Attendance won't be marked until you send /resume.")
        else:
            await self.reply(message, f"Resumed {names}. Attendance marking is back on.")

    async def cmd_stop(self, message: dict, args: str):
        accounts, _ = await self.require_accounts(message, args)
        if not accounts:
            return
        for login in accounts:
            del self.bot_state.links[login]
        self.bot_state.save()
        logging.info("%s unlinked from Telegram via /stop", ", ".join(accounts))
        await self.reply(
            message,
            f"Unlinked {', '.join(accounts)}. Attendance is still marked, but you won't get messages. "
            "Send /start to link again, or /pause to stop marking.",
        )

    async def cmd_password(self, message: dict, args: str):
        # Delete the message first so the password doesn't stay in the chat history.
        deleted = await self.telegram.call(
            "deleteMessage", chat_id=message["chat"]["id"], message_id=message["message_id"]
        ) is not None
        cleanup = "" if deleted else "\nI couldn't delete your message, please delete it yourself."

        accounts, password = await self.require_accounts(message, args)
        if not accounts:
            return
        if len(accounts) > 1:
            await self.reply(message, f"You have several accounts, use /password <login> <new password>.{cleanup}")
            return
        if not password:
            await self.reply(message, f"Usage: /password <new password>{cleanup}")
            return

        login = accounts[0]
        try:
            update_password(self.users_path, login, password)
        except (OSError, ValueError, KeyError) as e:
            logging.error("Couldn't save the new password for '%s': %s", login, e)
            await self.reply(message, f"Couldn't save the new password, tell the admin.{cleanup}")
            return
        runner = self.runners[login]
        runner.password = password
        runner.wakeup.set()
        logging.info("Password for '%s' updated via Telegram", login)
        await self.reply(message, f"Password for {login} updated. I'll use it on the next login.{cleanup}")


async def main():
    # systemd stops the service with SIGTERM: shut down the same way as on Ctrl+C.
    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    except NotImplementedError:
        pass  # Windows

    cfg = Config.from_env()
    users_path = BASE_DIR / "users.json"
    users = load_users(users_path)
    logging.info("Loaded %d user(s) from users.json", len(users))
    if cfg.active_hours:
        logging.info(
            "Active days %s, hours %s-%s",
            ",".join(map(str, sorted(cfg.active_days))),
            f"{cfg.active_hours[0]:%H:%M}",
            f"{cfg.active_hours[1]:%H:%M}",
        )
    bot_state = BotState(BASE_DIR / "bot_state.json")
    bot_state.prune(users)

    async with aiohttp.ClientSession() as http:
        telegram = Telegram(TELEGRAM_BOT_TOKEN, http)
        runners = [UserRunner(user, cfg, telegram, bot_state) for user in users]

        bot_task = None
        if TELEGRAM_BOT_TOKEN:
            bot_task = asyncio.create_task(Bot(telegram, runners, bot_state, users_path).run())
        else:
            logging.warning("TELEGRAM_BOT_TOKEN is not set, Telegram bot and notifications are off")

        try:
            while True:
                try:
                    async with async_playwright() as pw:
                        browser = await pw.chromium.launch(
                            channel=cfg.browser_channel or None,
                            headless=True,
                            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
                        )
                        logging.info("Browser launched")
                        tasks = [asyncio.create_task(r.run_forever(browser)) for r in runners]
                        try:
                            # run_forever only returns by raising, e.g. BrowserDisconnected.
                            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
                            for task in done:
                                task.result()
                        finally:
                            for task in tasks:
                                task.cancel()
                            await asyncio.gather(*tasks, return_exceptions=True)
                            try:
                                await browser.close()
                            except Exception:
                                pass
                except Exception as e:
                    logging.error("Browser crashed: %s. Restarting in %ds...", e, cfg.retry_delay)
                    await asyncio.sleep(cfg.retry_delay)
        finally:
            if bot_task:
                bot_task.cancel()
                await asyncio.gather(bot_task, return_exceptions=True)


if __name__ == "__main__":
    setup_logging()
    try:
        asyncio.run(main())
    except ConfigError as e:
        logging.error("Config error: %s", e)
        sys.exit(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logging.info("Stopped")
