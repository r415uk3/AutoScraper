import asyncio
import json
import os
import stat
from datetime import datetime, time as dtime

import aiohttp
import pytest

import open_kbtu as ok


def run(coro):
    return asyncio.run(coro)


# --- config ---------------------------------------------------------------

def test_parse_days():
    assert ok.parse_days("1-6") == frozenset({1, 2, 3, 4, 5, 6})
    assert ok.parse_days("1,3, 5") == frozenset({1, 3, 5})
    assert ok.parse_days("") == frozenset(range(1, 8))
    for bad in ("0-3", "1-8", "mon", "5-1"):
        with pytest.raises(ok.ConfigError):
            ok.parse_days(bad)


def test_parse_hours():
    assert ok.parse_hours("7:30-21:30") == (dtime(7, 30), dtime(21, 30))
    assert ok.parse_hours(" ") is None
    for bad in ("21:00-07:00", "7-21", "nonsense"):
        with pytest.raises(ok.ConfigError):
            ok.parse_hours(bad)


def test_env_int(monkeypatch):
    monkeypatch.setenv("X", "")
    assert ok.env_int("X", 5) == 5
    monkeypatch.setenv("X", "12")
    assert ok.env_int("X", 5) == 12
    for bad in ("abc", "0", "-3"):
        monkeypatch.setenv("X", bad)
        with pytest.raises(ok.ConfigError):
            ok.env_int("X", 5)


def test_config_defaults_from_empty_env(monkeypatch):
    for name in ("REFRESH_INTERVAL", "RETRY_DELAY", "MAX_RETRY_DELAY", "LOGIN_MAX_ATTEMPTS",
                 "ALERT_REPEAT_INTERVAL", "ACTIVE_DAYS", "ACTIVE_HOURS"):
        monkeypatch.delenv(name, raising=False)
    assert ok.Config.from_env() == ok.Config()


# --- active window ----------------------------------------------------------

WEEKDAYS = frozenset(range(1, 6))
HOURS = (dtime(8, 0), dtime(20, 0))


@pytest.mark.parametrize("now, expected_hours", [
    (datetime(2026, 10, 7, 12, 0), 0),        # Wed noon: inside
    (datetime(2026, 10, 7, 7, 0), 1),         # Wed before opening
    (datetime(2026, 10, 7, 20, 0), 12),       # Wed at closing -> Thu 08:00
    (datetime(2026, 10, 9, 21, 0), 59),       # Fri evening -> Mon 08:00
    (datetime(2026, 10, 10, 12, 0), 44),      # Sat noon -> Mon 08:00
])
def test_seconds_until_active(now, expected_hours):
    assert ok.seconds_until_active(now, WEEKDAYS, HOURS) == expected_hours * 3600


def test_seconds_until_active_all_day():
    sat = datetime(2026, 10, 10, 23, 0)
    assert ok.seconds_until_active(sat, frozenset(range(1, 8)), None) == 0
    assert ok.seconds_until_active(sat, WEEKDAYS, None) == 25 * 3600


# --- users.json -------------------------------------------------------------

def write_users(tmp_path, content, bom=False):
    path = tmp_path / "users.json"
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    path.write_text(text, encoding="utf-8-sig" if bom else "utf-8")
    return path


def test_load_users_ok_with_bom_and_cyrillic(tmp_path):
    users = [{"username": "студент", "password": "пароль", "telegram": "@Aidar_K"}]
    assert ok.load_users(write_users(tmp_path, users, bom=True)) == users


@pytest.mark.parametrize("content", [
    "{not json",
    [],
    {"username": "a", "password": "b"},
    ["a"],
    [{"username": "a"}],
    [{"username": " ", "password": "b"}],
    [{"username": "a", "password": "b", "telegram": ["x"]}],
    [{"username": "a", "password": "b", "telegram": "@"}],
    [{"username": "a", "password": "b"}, {"username": "a", "password": "c"}],
])
def test_load_users_invalid(tmp_path, content):
    with pytest.raises(ok.ConfigError):
        ok.load_users(write_users(tmp_path, content))


def test_load_users_missing_file(tmp_path):
    with pytest.raises(ok.ConfigError, match="not found"):
        ok.load_users(tmp_path / "users.json")


# --- notifications ------------------------------------------------------------

class FakeTelegram:
    def __init__(self):
        self.sent = []      # message texts
        self.to = []        # (chat_id, text)
        self.calls = []     # (method, params)

    async def send(self, chat_id, message):
        if not chat_id:
            return False
        self.sent.append(message)
        self.to.append((chat_id, message))
        return True

    async def call(self, method, http_timeout=10, **params):
        self.calls.append((method, params))
        return True


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_notifier_dedupes_problems_and_announces_recovery():
    tg, clock = FakeTelegram(), FakeClock()
    n = ok.Notifier(tg, lambda: "1", "u", repeat_interval=3600, clock=clock)

    async def scenario():
        await n.problem("failing", "down")
        clock.now = 100
        await n.problem("failing", "down")       # suppressed
        clock.now = 3700
        await n.problem("failing", "still down")  # repeat interval passed
        await n.resolved("failing", "up")
        await n.resolved("failing", "up")        # nothing active: no message
        await n.resolved("check-in")             # silent clear

    run(scenario())
    assert tg.sent == ["[u] down", "[u] still down", "[u] up"]


def test_telegram_skips_without_token_or_chat():
    assert run(ok.Telegram(None, None).send("1", "x")) is False
    assert run(ok.Telegram("token", None).send(None, "x")) is False


def test_notifier_drops_messages_for_unlinked_accounts():
    tg = FakeTelegram()
    run(ok.Notifier(tg, lambda: None, "u", 3600).info("hi"))
    assert tg.sent == []


# --- login retries ------------------------------------------------------------

def test_login_with_retries(monkeypatch):
    results = iter([RuntimeError("boom"), False, True])
    calls = []

    async def fake_login(page, username, password, log):
        calls.append(1)
        result = next(results)
        if isinstance(result, Exception):
            raise result
        return result

    async def no_sleep(_):
        pass

    monkeypatch.setattr(ok, "do_login", fake_login)
    monkeypatch.setattr(ok.asyncio, "sleep", no_sleep)
    run(ok.login_with_retries(None, "u", "p", 3, ok.logging.getLogger("t")))
    assert len(calls) == 3

    results = iter([False, False])
    with pytest.raises(ok.LoginFailed):
        run(ok.login_with_retries(None, "u", "p", 2, ok.logging.getLogger("t")))


# --- UserRunner.run_forever ---------------------------------------------------

class FakeBrowser:
    def __init__(self, connected=True):
        self.connected = connected

    def is_connected(self):
        return self.connected


def make_runner(tmp_path, **cfg):
    tg = FakeTelegram()
    state = ok.BotState(tmp_path / "bot_state.json")
    state.links["u"] = {"user_id": 7, "chat_id": 70, "handle": "u_tg"}
    runner = ok.UserRunner(
        {"username": "u", "password": "p", "telegram": "@u_tg"},
        ok.Config(retry_delay=10, max_retry_delay=25, **cfg),
        tg,
        state,
    )
    return runner, tg


def test_run_forever_backs_off_and_notifies_once(monkeypatch, tmp_path):
    runner, tg = make_runner(tmp_path)
    sleeps = []

    async def fake_nap(delay):
        sleeps.append(delay)
        if len(sleeps) == 4:
            raise asyncio.CancelledError

    async def failing_run_once(browser):
        raise ok.LoginFailed("Login failed after 3 attempts")

    monkeypatch.setattr(runner, "nap", fake_nap)
    monkeypatch.setattr(runner, "run_once", failing_run_once)
    with pytest.raises(asyncio.CancelledError):
        run(runner.run_forever(FakeBrowser()))

    assert sleeps == [10, 20, 25, 25]
    assert tg.to == [(70, tg.sent[0])] and "/password" in tg.sent[0]
    assert "login failing" in runner.status

    run(runner.mark_healthy())
    assert runner.failures == 0
    assert tg.sent[-1] == "[u] Working again"


def test_run_forever_escalates_when_browser_is_dead(monkeypatch, tmp_path):
    runner, tg = make_runner(tmp_path)

    async def crash(browser):
        raise RuntimeError("Target closed")

    monkeypatch.setattr(runner, "run_once", crash)
    with pytest.raises(ok.BrowserDisconnected):
        run(runner.run_forever(FakeBrowser(connected=False)))
    assert tg.sent == []


def test_run_forever_propagates_cancellation(monkeypatch, tmp_path):
    runner, _ = make_runner(tmp_path)

    async def cancelled(browser):
        raise asyncio.CancelledError

    monkeypatch.setattr(runner, "run_once", cancelled)
    with pytest.raises(asyncio.CancelledError):
        run(runner.run_forever(FakeBrowser()))


def test_paused_runner_waits_and_wakes_on_resume(tmp_path):
    runner, _ = make_runner(tmp_path)
    runner.bot_state.paused.add("u")

    async def scenario():
        waiter = asyncio.create_task(runner.wait_until_runnable())
        await asyncio.sleep(0.05)
        assert not waiter.done() and runner.status.startswith("paused")
        runner.bot_state.paused.discard("u")
        runner.wakeup.set()
        await asyncio.wait_for(waiter, 1)

    run(scenario())


# --- bot state ----------------------------------------------------------------

def test_bot_state_roundtrip_and_prune(tmp_path):
    path = tmp_path / "bot_state.json"
    state = ok.BotState(path)
    state.links = {
        "keep": {"user_id": 1, "chat_id": 10, "handle": "a"},
        "changed": {"user_id": 2, "chat_id": 20, "handle": "old"},
        "removed": {"user_id": 3, "chat_id": 30, "handle": "c"},
    }
    state.paused = {"keep", "removed"}
    state.save()

    state = ok.BotState(path)
    state.prune([
        {"username": "keep", "password": "p", "telegram": "@A"},
        {"username": "changed", "password": "p", "telegram": "@new"},
    ])
    assert list(state.links) == ["keep"]
    assert state.paused == {"keep"}
    assert ok.BotState(path).links == state.links


def test_bot_state_corrupt_file(tmp_path):
    (tmp_path / "bot_state.json").write_text("{oops")
    with pytest.raises(ok.ConfigError):
        ok.BotState(tmp_path / "bot_state.json")


# --- bot commands -------------------------------------------------------------

def make_bot(tmp_path, users):
    users_path = tmp_path / "users.json"
    users_path.write_text(json.dumps(users), encoding="utf-8")
    tg = FakeTelegram()
    state = ok.BotState(tmp_path / "bot_state.json")
    runners = [ok.UserRunner(u, ok.Config(), tg, state) for u in users]
    return ok.Bot(tg, runners, state, users_path), tg


def msg(text, user_id=1, username="aidar_k", chat_type="private", message_id=5):
    sender = {"id": user_id}
    if username:
        sender["username"] = username
    return {"message_id": message_id, "from": sender, "chat": {"id": user_id * 10, "type": chat_type}, "text": text}


USERS = [
    {"username": "s1", "password": "p1", "telegram": "@Aidar_K"},
    {"username": "s2", "password": "p2", "telegram": "@other"},
]


def test_start_links_matching_account_case_insensitively(tmp_path):
    bot, tg = make_bot(tmp_path, USERS)
    run(bot.handle(msg("/start")))
    assert bot.bot_state.links == {"s1": {"user_id": 1, "chat_id": 10, "handle": "aidar_k"}}
    assert "Linked to KBTU account: s1" in tg.sent[-1]
    assert json.loads((tmp_path / "bot_state.json").read_text())["links"]["s1"]["chat_id"] == 10

    run(bot.runners["s1"].notifier.info("hello"))
    assert tg.to[-1] == (10, "[s1] hello")


def test_start_unknown_or_missing_username(tmp_path):
    bot, tg = make_bot(tmp_path, USERS)
    run(bot.handle(msg("/start", username="stranger")))
    assert "I don't know @stranger" in tg.sent[-1]
    run(bot.handle(msg("/start", username=None)))
    assert "don't have a Telegram @username" in tg.sent[-1]
    assert bot.bot_state.links == {}


def test_start_relink_notifies_previous_chat(tmp_path):
    bot, tg = make_bot(tmp_path, USERS)
    run(bot.handle(msg("/start", user_id=1)))
    run(bot.handle(msg("/start", user_id=2)))
    assert (10, "[s1] This account was linked to another Telegram account.") in tg.to
    assert bot.bot_state.links["s1"]["user_id"] == 2


def test_group_messages_are_ignored(tmp_path):
    bot, tg = make_bot(tmp_path, USERS)
    run(bot.handle(msg("/start", chat_type="group")))
    assert tg.sent == [] and bot.bot_state.links == {}


def test_commands_require_link(tmp_path):
    bot, tg = make_bot(tmp_path, USERS)
    for command in ("/status", "/pause", "/resume", "/stop"):
        run(bot.handle(msg(command)))
        assert "Send /start first" in tg.sent[-1]


def test_status_pause_resume_stop(tmp_path):
    bot, tg = make_bot(tmp_path, USERS)
    run(bot.handle(msg("/start")))

    run(bot.handle(msg("/status")))
    assert tg.sent[-1].startswith("s1: starting") and "Last marked: never" in tg.sent[-1]

    run(bot.handle(msg("/pause")))
    assert bot.bot_state.paused == {"s1"} and bot.runners["s1"].paused
    assert bot.runners["s1"].wakeup.is_set()
    assert json.loads((tmp_path / "bot_state.json").read_text())["paused"] == ["s1"]

    run(bot.handle(msg("/resume")))
    assert bot.bot_state.paused == set()

    run(bot.handle(msg("/stop")))
    assert bot.bot_state.links == {}
    assert "Unlinked s1" in tg.sent[-1]


def test_commands_pick_one_of_several_accounts(tmp_path):
    users = [
        {"username": "s1", "password": "p1", "telegram": "@aidar_k"},
        {"username": "s2", "password": "p2", "telegram": "@aidar_k"},
    ]
    bot, tg = make_bot(tmp_path, users)
    run(bot.handle(msg("/start")))
    assert "s1, s2" in tg.sent[-1]

    run(bot.handle(msg("/pause s2")))
    assert bot.bot_state.paused == {"s2"}

    run(bot.handle(msg("/password newpass")))
    assert "several accounts" in tg.sent[-1]

    run(bot.handle(msg("/password s2 new pass with spaces")))
    saved = json.loads((tmp_path / "users.json").read_text())
    assert [u["password"] for u in saved] == ["p1", "new pass with spaces"]
    assert bot.runners["s2"].password == "new pass with spaces"


def test_password_updates_file_runner_and_deletes_message(tmp_path):
    bot, tg = make_bot(tmp_path, USERS)
    run(bot.handle(msg("/start")))
    run(bot.handle(msg("/password  s3cr3t ", message_id=42)))

    assert ("deleteMessage", {"chat_id": 10, "message_id": 42}) in tg.calls
    saved = json.loads((tmp_path / "users.json").read_text())
    assert saved[0] == {"username": "s1", "password": "s3cr3t", "telegram": "@Aidar_K"}
    assert saved[1] == USERS[1]
    runner = bot.runners["s1"]
    assert runner.password == "s3cr3t" and runner.wakeup.is_set()
    assert "updated" in tg.sent[-1] and "delete it yourself" not in tg.sent[-1]


def test_password_deleted_even_when_not_linked(tmp_path):
    bot, tg = make_bot(tmp_path, USERS)
    run(bot.handle(msg("/password hunter2", message_id=9)))
    assert ("deleteMessage", {"chat_id": 10, "message_id": 9}) in tg.calls
    assert json.loads((tmp_path / "users.json").read_text()) == USERS


def test_password_without_value(tmp_path):
    bot, tg = make_bot(tmp_path, USERS)
    run(bot.handle(msg("/start")))
    run(bot.handle(msg("/password")))
    assert tg.sent[-1].startswith("Usage")


# --- bot against a fake Bot API server ------------------------------------------

def test_bot_end_to_end_over_http(tmp_path):
    from aiohttp import web

    received = []
    updates = [
        {"update_id": 100, "message": msg("/start")},
        {"update_id": 101, "message": msg("/status")},
    ]
    offsets = []

    async def api(request):
        method = request.match_info["method"]
        body = await request.json()
        received.append((method, body))
        if method == "getUpdates":
            offsets.append(body.get("offset"))
            pending = [u for u in updates if u["update_id"] >= (body.get("offset") or 0)]
            if not pending:
                await asyncio.sleep(0.2)  # long poll with nothing new
            return web.json_response({"ok": True, "result": pending})
        if method == "sendMessage":
            return web.json_response({"ok": True, "result": {"message_id": 1}})
        return web.json_response({"ok": True, "result": True})

    async def scenario():
        app = web.Application()
        app.router.add_post("/bottoken/{method}", api)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as http:
                tg = ok.Telegram("token", http, api_url=f"http://127.0.0.1:{port}")
                users_path = tmp_path / "users.json"
                users_path.write_text(json.dumps(USERS))
                state = ok.BotState(tmp_path / "bot_state.json")
                runners = [ok.UserRunner(u, ok.Config(), tg, state) for u in USERS]
                bot_task = asyncio.create_task(ok.Bot(tg, runners, state, users_path).run())
                for _ in range(50):
                    if sum(m == "sendMessage" for m, _ in received) >= 2:
                        break
                    await asyncio.sleep(0.05)
                bot_task.cancel()
                await asyncio.gather(bot_task, return_exceptions=True)
                return state
        finally:
            await runner.cleanup()

    state = run(scenario())
    methods = [m for m, _ in received]
    assert methods[0] == "setMyCommands"
    sends = [b for m, b in received if m == "sendMessage"]
    assert sends[0]["chat_id"] == 10 and "Linked to KBTU account: s1" in sends[0]["text"]
    assert sends[1]["text"].startswith("s1: starting")
    assert offsets[:2] == [None, 102]
    assert state.links["s1"]["chat_id"] == 10


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_password_update_keeps_file_permissions(tmp_path):
    path = tmp_path / "users.json"
    path.write_text(json.dumps(USERS))
    os.chmod(path, 0o600)
    ok.update_password(path, "s1", "new")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(path.read_text())[0]["password"] == "new"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_new_json_files_are_private(tmp_path):
    ok.write_json_atomic(tmp_path / "bot_state.json", {})
    assert stat.S_IMODE(os.stat(tmp_path / "bot_state.json").st_mode) == 0o600
