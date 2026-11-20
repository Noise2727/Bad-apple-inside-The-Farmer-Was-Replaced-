"""Сайт для обычных участников сервера (вход через Discord).

Показывает: сколько времени участник провёл в войсе, баланс монет, информацию
о сервере и роли участника. Работает в том же Flask-приложении, что и панель
управления (см. web.py), на том же сервисе Render.

Переменные окружения:
  DISCORD_CLIENT_SECRET  — Client Secret из Discord Developer Portal → OAuth2 (обязательно)
  DISCORD_CLIENT_ID      — Application ID (необязательно: если не задан, берётся ID бота)
  DISCORD_REDIRECT_URI   — необязательно, например https://имя.onrender.com/auth/callback
                           (должен ТОЧНО совпадать с Redirect в Developer Portal)
"""
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from flask import Blueprint, jsonify, redirect, render_template_string, request, send_from_directory, session

import bot as bot_module
import storage
import economy
import polls
import random
import threading
import support

bp = Blueprint("user", __name__)
log = logging.getLogger("userweb")

GUILD_ID = bot_module.DASHBOARD_GUILD_ID
DISCORD_API = "https://discord.com/api/v10"
# Ссылка-приглашение на Discord-сервер (кнопка «Переходите на наш сервер»).
# Лучше задать на Render: Environment → DISCORD_INVITE_URL = https://discord.gg/ваш_код
DISCORD_INVITE = os.environ.get("DISCORD_INVITE_URL", "").strip() or "https://discord.gg/ВАШ_КОД"

# Голосование на главной: формат турнира по CS2.
POLL_CS2 = {"id": "cs2_format", "options": [("1v1", "1 на 1"), ("2v2", "2 на 2"), ("5v5", "5 на 5")]}

USER_AGENT = "DiscordBot (member-site, 1.0)"  # без User-Agent Discord/Cloudflare может вернуть 403


# -------------------------------------------------------------
# DISCORD OAUTH2
# -------------------------------------------------------------
def _client_id() -> str | None:
    cid = os.environ.get("DISCORD_CLIENT_ID", "").strip()
    if cid:
        return cid
    user = getattr(bot_module.bot, "user", None)
    return str(user.id) if user else None


def _client_secret() -> str:
    return os.environ.get("DISCORD_CLIENT_SECRET", "").strip()


def _login_configured() -> bool:
    return bool(_client_id() and _client_secret())


def _redirect_uri() -> str:
    env = os.environ.get("DISCORD_REDIRECT_URI", "").strip()
    if env:
        return env
    proto = request.headers.get("X-Forwarded-Proto", request.scheme).split(",")[0].strip()
    return f"{proto}://{request.host}/auth/callback"


def _discord_request(url: str, data: dict | None = None, token: str | None = None) -> dict:
    headers = {"User-Agent": USER_AGENT}
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


# -------------------------------------------------------------
# ФОРМАТИРОВАНИЕ
# -------------------------------------------------------------
def fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days} д")
    if days or hours:
        parts.append(f"{hours} ч")
    parts.append(f"{minutes} мин")
    return " ".join(parts)


def fmt_number(n) -> str:
    return f"{int(n):,}".replace(",", " ")


# -------------------------------------------------------------
# СТРАНИЦЫ
# -------------------------------------------------------------
def _load_shop(owned_role_names: set) -> dict:
    """Данные магазина для сайта (только чтение). Покупка пока идёт через /shop в Discord."""
    shop = {"roles": [], "products": [], "nick_self": 300, "nick_other": 900}
    try:
        from economy import NICKNAME_SELF_PRICE, NICKNAME_OTHER_PRICE
        shop["nick_self"], shop["nick_other"] = NICKNAME_SELF_PRICE, NICKNAME_OTHER_PRICE
    except Exception:
        pass
    try:
        names = {str(r["id"]): r["name"] for r in bot_module.get_guild_roles()}
        for r in storage.get_shop_roles():
            name = names.get(str(r.get("role_id")))
            if not name:
                continue  # роль удалена на сервере
            shop["roles"].append({
                "name": name,
                "price": fmt_number(r.get("price", 0)),
                "purchases": r.get("purchases", 0),
                "owned": name in owned_role_names,
            })
    except Exception:
        log.exception("Сайт участников: не удалось загрузить магазин")
    try:
        # Раздел «Товары»: список в настройках бота, ключ "shop_products":
        # [{"name": "...", "price": 500, "description": "..."}]. Пока пусто — покажется заготовка.
        for pr in storage.get_config().get("shop_products", []):
            shop["products"].append({
                "name": str(pr.get("name", "Товар")),
                "price": fmt_number(pr.get("price", 0)),
                "description": str(pr.get("description", "")),
            })
    except Exception:
        log.exception("Сайт участников: не удалось загрузить товары")
    return shop


# -------------------------------------------------------------
# АНОНСЫ для вкладки «Главная» (турниры, обновления, новости).
# Добавляй новые записи В НАЧАЛО списка. Поля:
#   type  — "tournament" (турнир) | "update" (обновление) | "news" (новость)
#   title — заголовок, text — описание, date — дата строкой (необязательно),
#   link  — ссылка-кнопка (необязательно), link_text — подпись кнопки (необязательно)
# -------------------------------------------------------------
ANNOUNCEMENTS = [
    {"type": "tournament", "title": "Скоро: первый турнир сервера",
     "text": "Следи за этой вкладкой — здесь появятся дата, формат и призовой фонд.", "date": ""},
    {"type": "update", "title": "Новый сайт участников",
     "text": "Добавлены вкладки «Главная» и «Профиль», а также чат с поддержкой — кнопка справа снизу.", "date": ""},
]
ANNOUNCE_LABELS = {"tournament": "Турнир", "update": "Обновление", "news": "Новости"}


@bp.route("/")
def home():
    # Всегда отвечаем 200, даже если бот ещё подключается — на эту страницу
    # может ходить проверка работоспособности Render.
    user = session.get("user")
    error = session.pop("user_error", None)
    if not user:
        # Гость: сайт открыт без входа — виден сервер и магазин. Личные данные (монеты,
        # время в войсе, роли) показываются только после входа через Discord.
        try:
            guild = bot_module.get_guild_snapshot()
        except Exception:
            guild = None  # бот ещё подключается — покажем обычный экран входа
        if guild is None:
            return render_template_string(PAGE, announcements=ANNOUNCEMENTS, casino_cfg=_casino_cfg(), discord_invite=DISCORD_INVITE, poll_options=POLL_CS2["options"], labels=ANNOUNCE_LABELS, mode="landing", error=error,
                                          login_ready=_login_configured())
        return render_template_string(PAGE, announcements=ANNOUNCEMENTS, casino_cfg=_casino_cfg(), discord_invite=DISCORD_INVITE, poll_options=POLL_CS2["options"], labels=ANNOUNCE_LABELS, mode="guest", error=error, guild=guild,
                                      shop=_load_shop(set()), login_ready=_login_configured(),
                                      sup_cfg=_sup_cfg(None))

    ctx = {"mode": "cabinet", "user": user, "error": error, "state": "ok", "sup_cfg": _sup_cfg(user)}
    try:
        cab = bot_module.get_user_cabinet(user["id"])
        guild = bot_module.get_guild_snapshot()
        if cab is None or guild is None:
            ctx["state"] = "offline"
        else:
            ctx["cab"] = cab
            ctx["guild"] = guild
            if cab.get("in_guild"):
                # Монеты и время в войсе грузим по отдельности: если одно из них
                # не сработает, остальная страница всё равно покажется.
                ctx["balance"] = ctx["voice"] = "—"
                try:
                    eco = storage.get_economy(GUILD_ID, user["id"])
                    ctx["balance"] = fmt_number(eco.get("balance", 0))
                except Exception:
                    log.exception("Сайт участников: не удалось получить баланс")
                try:
                    ctx["voice"] = fmt_duration(bot_module.get_voice_seconds(user["id"]))
                except Exception:
                    log.exception("Сайт участников: не удалось получить время в войсе")
                ctx["shop"] = _load_shop({r["name"] for r in cab.get("roles", [])})
    except RuntimeError:
        ctx["state"] = "offline"  # бот ещё не подключился к Discord
    except Exception:
        log.exception("Сайт участников: не удалось загрузить данные")
        ctx["state"] = "error"
    return render_template_string(PAGE, announcements=ANNOUNCEMENTS, casino_cfg=_casino_cfg(), discord_invite=DISCORD_INVITE, poll_options=POLL_CS2["options"], labels=ANNOUNCE_LABELS, **ctx)


# -------------------------------------------------------------
# ТЕХ. ПОДДЕРЖКА (обращения; отвечают сотрудники из SUPPORT_AGENT_IDS на Render, см. support.py)
# -------------------------------------------------------------
_support_last = {}  # user_id -> время последнего действия (защита от спама)
_staff_cache = {}   # user_id -> (время проверки, результат)


async def _member_staff_info(guild_id, user_id):
    guild = bot_module.bot.get_guild(guild_id)
    member = guild.get_member(int(user_id)) if guild else None
    if member is None:
        return set(), False
    return {r.id for r in member.roles}, member.guild_permissions.administrator


def _is_agent(user_id) -> bool:
    """Модератор поддержки: ID из SUPPORT_AGENT_IDS (Render) ИЛИ роль модера/админа из настроек бота
    ИЛИ право «Администратор» на сервере. Результат кэшируется на минуту."""
    uid = str(user_id)
    if support.is_agent(uid):
        return True
    cached = _staff_cache.get(uid)
    if cached and time.time() - cached[0] < 60:
        return cached[1]
    result = False
    try:
        cfg = storage.get_config()
        staff_roles = {int(x) for x in (cfg.get("mod_role_ids", []) + cfg.get("admin_role_ids", []))}
        roles, is_admin = bot_module.run_coro(_member_staff_info(GUILD_ID, uid))
        result = bool(is_admin or (staff_roles & roles))
    except Exception:
        log.exception("Сайт участников: не удалось проверить роль модератора")
    _staff_cache[uid] = (time.time(), result)
    return result


def _csrf_token() -> str:
    tok = session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        session["csrf"] = tok
    return tok


def _sup_cfg(user) -> dict:
    """Данные для скрипта вкладки «Поддержка» (заготовки видны и гостям)."""
    return {
        "loggedIn": bool(user),
        "csrf": _csrf_token() if user else "",
        "isAgent": bool(user) and _is_agent(user["id"]),
        "templates": support.TEMPLATES,
    }


def _sup_guard():
    """Проверяет вход и CSRF. Возвращает (user, ошибка-ответ)."""
    user = session.get("user")
    if not user:
        return None, (jsonify(error="Войди через Discord, чтобы писать в поддержку."), 401)
    if request.method == "POST" and request.headers.get("X-CSRF-Token") != session.get("csrf"):
        return None, (jsonify(error="Сессия устарела. Обнови страницу."), 403)
    if request.method == "POST":
        now = time.time()
        if now - _support_last.get(user["id"], 0) < 2:
            return None, (jsonify(error="Не так быстро — подожди пару секунд."), 429)
        _support_last[user["id"]] = now
    return user, None


def _sup_can_view(user, ticket) -> bool:
    return ticket is not None and (ticket["user_id"] == user["id"] or _is_agent(user["id"]))


def _sup_ticket_json(user, t) -> dict:
    return {
        "id": t["id"], "subject": t["subject"], "status": t["status"], "user_name": t["user_name"],
        "mine": t["user_id"] == user["id"], "isAgent": _is_agent(user["id"]),
        "messages": [{"author": m["author_name"], "staff": m["staff"], "text": m["text"], "ts": m["ts"],
                      "system": bool(m.get("system")), "mine": m["author_id"] == user["id"]} for m in t["messages"]],
    }


@bp.route("/api/support/tickets", methods=["GET", "POST"])
def support_tickets():
    user, err = _sup_guard()
    if err:
        return err
    agent = _is_agent(user["id"])

    if request.method == "GET":
        status = request.args.get("status") or None
        if status not in support.STATUSES:
            status = None
        show_all = agent and request.args.get("all") == "1"
        items = support.list_tickets(user_id=None if show_all else user["id"], status=status)
        for it in items:
            it["mine"] = it["user_id"] == user["id"]
            it.pop("user_id", None)
        return jsonify(tickets=items)

    data = request.get_json(silent=True) or {}
    subject = (data.get("subject") or "").strip()
    text = (data.get("text") or "").strip()
    if not subject or not text:
        return jsonify(error="Заполни тему и описание."), 400
    if support.count_active(user["id"]) >= support.MAX_OPEN_PER_USER:
        return jsonify(error=f"Уже открыто {support.MAX_OPEN_PER_USER} обращений. Дождись ответа или закрой старые."), 400
    t = support.create_ticket(user["id"], user["name"], data.get("template"), subject, text)
    return jsonify(ticket=_sup_ticket_json(user, t))


@bp.route("/api/support/tickets/<int:tid>")
def support_ticket(tid):
    user, err = _sup_guard()
    if err:
        return err
    t = support.get_ticket(tid)
    if not _sup_can_view(user, t):
        return jsonify(error="Обращение не найдено."), 404
    return jsonify(ticket=_sup_ticket_json(user, t))


@bp.route("/api/support/tickets/<int:tid>/reply", methods=["POST"])
def support_reply(tid):
    user, err = _sup_guard()
    if err:
        return err
    t = support.get_ticket(tid)
    if not _sup_can_view(user, t):
        return jsonify(error="Обращение не найдено."), 404
    text = ((request.get_json(silent=True) or {}).get("text") or "").strip()
    if not text:
        return jsonify(error="Напиши сообщение."), 400
    # сотрудник отвечает как «Поддержка»; автор обращения — как участник
    as_staff = _is_agent(user["id"]) and t["user_id"] != user["id"]
    t = support.add_message(tid, user["id"], user["name"], as_staff, text)
    return jsonify(ticket=_sup_ticket_json(user, t))


@bp.route("/api/support/tickets/<int:tid>/status", methods=["POST"])
def support_status(tid):
    user, err = _sup_guard()
    if err:
        return err
    t = support.get_ticket(tid)
    if not _sup_can_view(user, t):
        return jsonify(error="Обращение не найдено."), 404
    status = (request.get_json(silent=True) or {}).get("status")
    if status not in support.STATUSES:
        return jsonify(error="Неверный статус."), 400
    if not _is_agent(user["id"]) and status != "closed":
        return jsonify(error="Автор может только закрыть обращение."), 403
    by_staff = _is_agent(user["id"]) and t["user_id"] != user["id"]
    t = support.set_status(tid, status, user["name"], by_staff)
    return jsonify(ticket=_sup_ticket_json(user, t))


@bp.route("/logo.png")
def logo_png():
    """Логотип лежит в корневой папке проекта: logo.png (рядом с main.py)."""
    root = os.path.dirname(os.path.abspath(__file__))
    resp = send_from_directory(root, "logo.png", max_age=3600)
    return resp


# -------------------------------------------------------------
# ОНЛАЙН САЙТА: сколько людей (вошедших и гостей) сейчас держат сайт открытым.
# Страница раз в ~20 секунд шлёт сигнал; кто молчит дольше PRESENCE_TTL — считается ушедшим.
# Счёт хранится в памяти сервиса (при перезапуске обнуляется и тут же набирается заново).
# -------------------------------------------------------------
PRESENCE_TTL = 45
_presence = {}  # id посетителя -> время последнего сигнала


def _presence_touch() -> int:
    vid = session.get("vid")
    if not vid:
        vid = secrets.token_hex(8)
        session["vid"] = vid
    now = time.time()
    _presence[vid] = now
    for k in [k for k, t in _presence.items() if now - t > PRESENCE_TTL]:
        _presence.pop(k, None)
    return max(1, len(_presence))


@bp.route("/api/presence")
def presence():
    resp = jsonify(online=_presence_touch())
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/icon.png")
@bp.route("/favicon.ico")
def icon_png():
    """Иконка вкладки браузера: icon.png в корневой папке проекта (рядом с main.py)."""
    root = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(root, "icon.png", max_age=3600)


@bp.route("/coin.png")
def coin_png():
    """Иконка монеты лежит в корневой папке проекта: coin.png (рядом с main.py)."""
    root = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(root, "coin.png", max_age=3600)


# -------------------------------------------------------------
# КАЗИНО И ТАЙМЕР-НАГРАДА НА САЙТЕ (те же правила и те же монеты, что у /casino и /timely в боте;
# константы берутся прямо из economy.py, поэтому правишь шансы в одном месте).
# Исход всегда считает сервер — браузер только показывает анимацию.
# -------------------------------------------------------------
CASINO_MIN_BET = 10
_casino_lock = threading.Lock()
_casino_last = {}      # user_id -> время последнего действия (защита от спама)
_rng = random.SystemRandom()


def _casino_cfg() -> dict:
    """Настройки для страницы (картинка колеса, слоты, таблица выплат)."""
    wheel = []
    for mult, weight, label in economy._WHEEL_SEGMENTS:
        wheel.append({"mult": mult, "weight": weight, "label": label, "icon": label.split(" ")[0]})
    return {
        "minBet": CASINO_MIN_BET,
        "wheel": wheel,
        "slots": [{"sym": sym, "weight": w, "payout": economy._SLOT_PAYOUT.get(sym, 0)}
                  for sym, w in economy._SLOT_SYMBOLS],
        "timely": {"cooldown": economy.TIMELY_COOLDOWN_SECONDS,
                   "min": economy.TIMELY_MIN_REWARD, "max": economy.TIMELY_MAX_REWARD},
    }


def _casino_guard():
    """Вход + CSRF + участник сервера + антиспам. Возвращает (user, ошибка-ответ)."""
    user = session.get("user")
    if not user:
        return None, (jsonify(error="Войди через Discord, чтобы играть."), 401)
    if request.method == "POST":
        if request.headers.get("X-CSRF-Token") != session.get("csrf"):
            return None, (jsonify(error="Сессия устарела. Обнови страницу."), 403)
        now = time.time()
        if now - _casino_last.get(user["id"], 0) < 1.2:
            return None, (jsonify(error="Не так быстро — подожди секунду."), 429)
        _casino_last[user["id"]] = now
    try:
        cab = bot_module.get_user_cabinet(user["id"])
    except Exception:
        return None, (jsonify(error="Бот ещё подключается, попробуй через минуту."), 503)
    if not cab or not cab.get("in_guild"):
        return None, (jsonify(error="Казино доступно только участникам сервера."), 403)
    return user, None


def _timely_left(eco) -> int:
    return max(0, int(economy.TIMELY_COOLDOWN_SECONDS - (time.time() - eco.get("last_daily", 0))))


def _casino_bet(user, data):
    """Проверяет ставку. Возвращает (bet, eco, ошибка-ответ)."""
    bet = data.get("bet")
    if isinstance(bet, bool) or not isinstance(bet, int):
        return None, None, (jsonify(error="Ставка должна быть целым числом."), 400)
    if bet < CASINO_MIN_BET:
        return None, None, (jsonify(error=f"Минимальная ставка — {CASINO_MIN_BET} монет."), 400)
    eco = storage.get_economy(GUILD_ID, user["id"])
    if eco["balance"] < bet:
        return None, None, (jsonify(error=f"Недостаточно монет. Баланс: {fmt_number(eco['balance'])}."), 400)
    return bet, eco, None


def _casino_reply(row, **extra):
    return jsonify(ok=True, balance=int(row["balance"]), balanceFmt=fmt_number(row["balance"]), **extra)


@bp.route("/api/casino/state")
def casino_state():
    user, err = _casino_guard()
    if err:
        return err
    eco = storage.get_economy(GUILD_ID, user["id"])
    return jsonify(balance=int(eco["balance"]), balanceFmt=fmt_number(eco["balance"]), timelyLeft=_timely_left(eco))


@bp.route("/api/casino/timely", methods=["POST"])
def casino_timely():
    user, err = _casino_guard()
    if err:
        return err
    with _casino_lock:
        eco = storage.get_economy(GUILD_ID, user["id"])
        left = _timely_left(eco)
        if left > 0:
            return jsonify(error="Награда ещё не готова.", timelyLeft=left), 400
        reward = _rng.randint(economy.TIMELY_MIN_REWARD, economy.TIMELY_MAX_REWARD)
        row = storage.update_economy(GUILD_ID, user["id"], sets={"last_daily": time.time()}, deltas={"balance": reward})
    return _casino_reply(row, reward=reward, timelyLeft=economy.TIMELY_COOLDOWN_SECONDS)


@bp.route("/api/casino/wheel", methods=["POST"])
def casino_wheel():
    user, err = _casino_guard()
    if err:
        return err
    with _casino_lock:
        bet, eco, err = _casino_bet(user, request.get_json(silent=True) or {})
        if err:
            return err
        segs = economy._WHEEL_SEGMENTS
        idx = _rng.choices(range(len(segs)), weights=[sg[1] for sg in segs], k=1)[0]
        mult, _, label = segs[idx]
        winnings = int(bet * mult)
        row = storage.update_economy(GUILD_ID, user["id"], deltas={"balance": winnings - bet})
    return _casino_reply(row, index=idx, label=label, mult=mult, bet=bet, winnings=winnings)


@bp.route("/api/casino/slots", methods=["POST"])
def casino_slots():
    user, err = _casino_guard()
    if err:
        return err
    with _casino_lock:
        bet, eco, err = _casino_bet(user, request.get_json(silent=True) or {})
        if err:
            return err
        syms = economy._SLOT_SYMBOLS
        weights = [sy[1] for sy in syms]
        reel = [_rng.choices(syms, weights=weights, k=1)[0][0] for _ in range(3)]
        if reel[0] == reel[1] == reel[2]:
            mult = economy._SLOT_PAYOUT[reel[0]]
            text = f"🎉 Три одинаковых! Множитель x{mult}"
        elif reel[0] == reel[1] or reel[1] == reel[2] or reel[0] == reel[2]:
            mult = 1
            text = "🙂 Две одинаковые — ставка возвращена"
        else:
            mult = 0
            text = "💨 Ничего не выпало"
        winnings = int(bet * mult)
        row = storage.update_economy(GUILD_ID, user["id"], deltas={"balance": winnings - bet})
    return _casino_reply(row, reel=reel, text=text, mult=mult, bet=bet, winnings=winnings)


@bp.route("/api/poll/cs2", methods=["GET", "POST"])
def poll_cs2():
    user = session.get("user")
    valid = {k for k, _ in POLL_CS2["options"]}
    if request.method == "POST":
        if not user:
            return jsonify(error="Войди через Discord, чтобы голосовать."), 401
        if request.headers.get("X-CSRF-Token") != session.get("csrf"):
            return jsonify(error="Сессия устарела. Обнови страницу."), 403
        choice = (request.get_json(silent=True) or {}).get("choice")
        if choice not in valid:
            return jsonify(error="Неизвестный вариант."), 400
        try:
            polls.vote(POLL_CS2["id"], user["id"], choice)
        except Exception:
            log.exception("Голосование: не удалось сохранить голос")
            return jsonify(error="Не удалось сохранить голос. Попробуй позже."), 500
    try:
        res = polls.results(POLL_CS2["id"], user["id"] if user else None)
    except Exception:
        log.exception("Голосование: не удалось получить результаты")
        return jsonify(error="Не удалось загрузить результаты."), 500
    return jsonify(counts={k: res["counts"].get(k, 0) for k in valid}, mine=res["mine"])


@bp.route("/auth/discord")
def auth_discord():
    if not _login_configured():
        session["user_error"] = "Вход через Discord ещё не настроен администратором."
        return redirect("/")
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    query = urllib.parse.urlencode({
        "client_id": _client_id(),
        "response_type": "code",
        "redirect_uri": _redirect_uri(),
        "scope": "identify",
        "state": state,
        "prompt": "none",  # не спрашивать повторно, если человек уже разрешал доступ
    })
    return redirect(f"https://discord.com/oauth2/authorize?{query}")


@bp.route("/auth/callback")
def auth_callback():
    expected = session.pop("oauth_state", None)
    if request.args.get("error"):
        session["user_error"] = "Вход отменён."
        return redirect("/")
    code = request.args.get("code", "")
    if not code or not expected or request.args.get("state") != expected:
        session["user_error"] = "Не удалось проверить вход. Попробуй ещё раз."
        return redirect("/")
    try:
        token = _discord_request(f"{DISCORD_API}/oauth2/token", data={
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _redirect_uri(),
        })
        me = _discord_request(f"{DISCORD_API}/users/@me", token=token["access_token"])
    except (urllib.error.URLError, KeyError, ValueError) as e:
        print(f"⚠️ Ошибка входа через Discord: {e}")
        session["user_error"] = "Discord не принял вход. Попробуй ещё раз позже."
        return redirect("/")

    session.permanent = True
    session["user"] = {"id": str(me["id"]), "name": me.get("global_name") or me.get("username", "")}
    return redirect("/")


@bp.route("/auth/logout", methods=["POST"])
def auth_logout():
    session.pop("user", None)
    return redirect("/")


# -------------------------------------------------------------
# ШАБЛОН
# -------------------------------------------------------------
PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DropOut</title>
<link rel="icon" type="image/png" href="/icon.png">
<link rel="shortcut icon" href="/icon.png">
<link rel="apple-touch-icon" href="/icon.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Unbounded:wght@500;700;800&family=Onest:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  /* Дизайн: тёмная тренировочная платформа (как skilly-training.com) —
     чёрный фон, неоново-зелёный акцент, широкий жирный шрифт.
     Интерфейс: боковое меню + верхняя панель + плитки (как cybershoke.net). */
  :root {
    --bg: #050706; --side: #080b09; --panel: #0d120e; --panel-2: #131a15;
    --line: #1a241d; --text: #f2f5f3; --dim: #8a978e;
    --green: #1ff06f; --green-dark: #0fbf55; --glow: rgba(31,240,111,.16);
    --bad: #ff6b6b; --radius: 20px;
    --display: 'Unbounded', 'Segoe UI', Arial, sans-serif;
    --body: 'Onest', 'Segoe UI', -apple-system, Roboto, sans-serif;
  }
  * { box-sizing: border-box; }
  html { scroll-behavior: smooth; }

  /* ---------- скроллбары в стиле сайта ---------- */
  :root { color-scheme: dark; }
  * { scrollbar-width: thin; scrollbar-color: #24402f var(--bg); }
  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-track { background: var(--bg); }
  ::-webkit-scrollbar-thumb { background: #24402f; border-radius: 99px; border: 2px solid var(--bg); }
  ::-webkit-scrollbar-thumb:hover { background: var(--green-dark); }
  ::-webkit-scrollbar-corner { background: var(--bg); }
  .sup-win ::-webkit-scrollbar-track, .inbox ::-webkit-scrollbar-track, .pills::-webkit-scrollbar-track { background: transparent; }
  .sup-win ::-webkit-scrollbar-thumb, .inbox ::-webkit-scrollbar-thumb, .pills::-webkit-scrollbar-thumb { border-color: transparent; }
  .sup-win *, .inbox *, .pills { scrollbar-color: #24402f transparent; }
  body {
    margin: 0; min-height: 100vh; background: var(--bg); color: var(--text);
    font-family: var(--body); display: flex;
    background-image: radial-gradient(700px 320px at 60% -80px, var(--glow), transparent);
    background-repeat: no-repeat;
  }
  a { color: inherit; text-decoration: none; }
  :focus-visible { outline: 2px solid var(--green); outline-offset: 2px; }

  /* ---------- боковое меню ---------- */
  .side {
    width: 232px; flex-shrink: 0; background: var(--side); border-right: 1px solid var(--line);
    padding: 20px 14px; position: sticky; top: 0; height: 100vh; display: flex; flex-direction: column; gap: 6px;
  }
  .logo { display: flex; align-items: center; gap: 10px; padding: 4px 8px 18px; font-family: var(--display); font-weight: 800; font-size: 15px; }
  .logo img, .logo .ph { width: 34px; height: 34px; border-radius: 10px; flex-shrink: 0; }
  .logo .ph { background: var(--green); }
  .logo span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .nav {
    display: flex; align-items: center; gap: 12px; width: 100%; padding: 12px 14px; border-radius: 14px;
    background: none; border: 0; color: var(--dim); font: 600 14px var(--body); cursor: pointer; text-align: left;
  }
  .nav svg { width: 20px; height: 20px; flex-shrink: 0; }
  .nav:hover { background: var(--panel); color: var(--text); }
  .nav.active { background: var(--green); color: #04130a; }
  .nav .tag { margin-left: auto; font-size: 10px; font-weight: 700; padding: 2px 7px; border-radius: 99px; background: var(--panel-2); color: var(--green); }
  .nav.active .tag { background: rgba(0,0,0,.18); color: #04130a; }
  .side-note { margin-top: auto; padding: 14px; border: 1px solid var(--line); border-radius: 16px; background: var(--panel); font-size: 12px; color: var(--dim); line-height: 1.5; }
  .side-note b { color: var(--text); display: block; margin-bottom: 4px; font-size: 13px; }

  /* ---------- основная часть ---------- */
  .main { flex: 1; min-width: 0; }
  .top {
    position: sticky; top: 0; z-index: 5; display: flex; align-items: center; justify-content: space-between; gap: 12px;
    padding: 12px 28px; background: rgba(5,7,6,.85); backdrop-filter: blur(10px); border-bottom: 1px solid var(--line);
  }
  .top-title { order: 0; flex-shrink: 0; }
  .top .pills { order: 1; flex: 1 1 auto; min-width: 0; margin: 0; padding: 0; gap: clamp(4px, .8vw, 8px); justify-content: center; scrollbar-width: none; }
  .top .pills::-webkit-scrollbar { display: none; }
  .top .pill { flex: 0 1 auto; padding: clamp(7px, 1vw, 10px) clamp(9px, 1.5vw, 18px); font-size: clamp(9.5px, 1.25vw, 12px); white-space: nowrap; }
  .dc-round { order: 2; margin-left: auto; flex-shrink: 0; width: 40px; height: 40px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center;
    background: #5865f2; color: #fff; border: 1px solid rgba(255,255,255,.12); transition: transform .15s, box-shadow .15s; }
  .dc-round:hover { transform: translateY(-1px); box-shadow: 0 0 0 4px rgba(88,101,242,.28); }
  .dc-round svg { width: 22px; height: 22px; }
  .chips { order: 3; flex-shrink: 0; }
  @media (max-width: 640px) {
    .top { flex-wrap: wrap; row-gap: 8px; }
    .top .pills { order: 4; flex: 0 0 100%; justify-content: flex-start; }
    .dc-round { margin-left: auto; }
  }
  .top-title { font-family: var(--display); font-weight: 700; font-size: 14px; letter-spacing: .02em; }
  .chips { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; justify-content: flex-end; }
  .chip { display: inline-flex; align-items: center; gap: 8px; padding: 8px 14px; border-radius: 99px; background: var(--panel); border: 1px solid var(--line); font-weight: 600; font-size: 14px; }
  .online { order: 1; display: flex; align-items: center; gap: 16px; margin-left: 6px; padding: 7px 14px; border-radius: 99px; background: rgba(255,255,255,.04); border: 1px solid var(--line); font-size: 13px; color: var(--dim); white-space: nowrap; }
  .on-item { display: inline-flex; align-items: center; gap: 7px; }
  .on-item b { color: var(--text); font-weight: 700; }
  .on-dot { width: 9px; height: 9px; border-radius: 50%; background: #c9d1cc; flex-shrink: 0; }
  .on-dot.live-dot { background: var(--green); box-shadow: 0 0 10px var(--green); animation: pillPulse 1.8s ease-in-out infinite; }
  @media (max-width: 640px) { .on-members { display: none; } }
  .site-online { display: inline-flex; align-items: center; gap: 8px; align-self: flex-start; margin: -6px 0 14px 8px; padding: 6px 14px; border-radius: 99px; background: rgba(31,240,111,.08); border: 1px solid rgba(31,240,111,.22); font-size: 13px; color: var(--dim); }
  .site-online b { color: var(--text); font-weight: 700; }
  .on-site-sm { display: none; }
  @media (max-width: 860px) { .on-site-sm { display: inline-flex; } }
  .chip-btn { cursor: pointer; font-family: inherit; color: inherit; }
  .chip-btn:hover { border-color: rgba(31,240,111,.55); transform: translateY(-1px); box-shadow: 0 0 0 3px rgba(31,240,111,.12); }
  .chip-btn.active { border-color: var(--green); box-shadow: 0 0 0 3px rgba(31,240,111,.18); }
  .chip img.coin { width: 26px; height: 26px; margin: 0; border-radius: 0; object-fit: contain; flex-shrink: 0; }
  .tile img.coin-ico { width: 112px; height: 112px; object-fit: contain; opacity: .22; }
  .chip svg { width: 16px; height: 16px; color: var(--green); }
  .chip img { width: 28px; height: 28px; border-radius: 50%; margin-left: -8px; }
  .btn, button.btn {
    font: 700 13px var(--display); letter-spacing: .02em; cursor: pointer; border: 0; border-radius: 99px;
    padding: 12px 22px; background: var(--green); color: #04130a; display: inline-block; text-transform: uppercase;
  }
  .btn:hover { background: #4dff93; }
  .btn.ghost { background: var(--panel); color: var(--dim); border: 1px solid var(--line); font-family: var(--body); text-transform: none; font-size: 13px; padding: 9px 16px; }
  .btn.ghost:hover { color: var(--text); background: var(--panel-2); }

  .content { max-width: 1180px; margin: 0 auto; padding: 24px 28px 64px; }
  .pills { display: flex; gap: 8px; overflow-x: auto; padding-bottom: 4px; margin-bottom: 22px; }
  .pill {
    flex-shrink: 0; padding: 10px 18px; border-radius: 12px; background: var(--panel); border: 1px solid var(--line);
    color: var(--dim); font: 700 12px var(--display); letter-spacing: .04em; text-transform: uppercase; cursor: pointer;
  }
  .pill:hover { color: var(--text); }
  .pill.active { color: var(--green); border-color: var(--green); background: rgba(31,240,111,.07); }

  .alert { background: rgba(255,107,107,.1); border: 1px solid rgba(255,107,107,.35); color: #ffb3b3; border-radius: 14px; padding: 12px 16px; margin-bottom: 18px; }

  .tab { display: none; }
  .tab.active { display: block; }

  /* ---------- герой профиля ---------- */
  .hero {
    display: flex; align-items: center; gap: 24px; padding: 26px 30px; border-radius: 26px; margin-bottom: 18px;
    background: linear-gradient(120deg, #0f2a19 0%, var(--panel) 55%); border: 1px solid #1b3a26;
  }
  .hero img { width: 104px; height: 104px; border-radius: 50%; border: 4px solid #fff; flex-shrink: 0; object-fit: cover; }
  .hero h1 { margin: 0 0 8px; font-family: var(--display); font-weight: 800; font-size: clamp(22px, 3.4vw, 38px); line-height: 1.1; text-transform: uppercase; overflow-wrap: anywhere; }
  .muted { color: var(--dim); font-size: 14px; }
  .live { display: inline-flex; align-items: center; gap: 8px; margin-top: 10px; padding: 6px 14px; border-radius: 99px; font-size: 13px; font-weight: 600; background: rgba(31,240,111,.12); color: var(--green); }
  .live i { width: 8px; height: 8px; border-radius: 50%; background: var(--green); box-shadow: 0 0 10px var(--green); }

  /* ---------- плитки ---------- */
  .tiles { display: grid; gap: 14px; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); }
  .tile {
    position: relative; min-height: 220px; padding: 20px; border-radius: var(--radius); overflow: hidden;
    background: linear-gradient(170deg, var(--panel-2), var(--panel)); border: 1px solid var(--line);
    display: flex; flex-direction: column; justify-content: flex-end; text-align: left; color: var(--text); font-family: var(--body);
  }
  button.tile { cursor: pointer; width: 100%; }
  button.tile:hover, a.tile:hover { border-color: var(--green); }
  .tile .ico { position: absolute; top: 16px; right: 16px; width: 86px; height: 86px; color: var(--green); opacity: .13; }
  .tile .num { font-family: var(--display); font-weight: 800; font-size: clamp(26px, 3vw, 36px); color: var(--green); line-height: 1.1; overflow-wrap: anywhere; }
  .tile .lbl { margin-top: 8px; font-family: var(--display); font-weight: 700; font-size: 13px; letter-spacing: .04em; text-transform: uppercase; }
  .home-grid { display: grid; gap: 18px; grid-template-columns: minmax(0, 1fr) clamp(220px, 28vw, 320px); align-items: start; }
  .home-side { position: sticky; top: 84px; }
  .ph { width: 100%; aspect-ratio: 1 / 1.6; border-radius: var(--radius); background: #1c211e; border: 1px solid var(--line); }
  @media (max-width: 860px) {
    .home-grid { grid-template-columns: 1fr; }
    .home-side { position: static; max-width: 320px; width: 100%; }
  }
  .cs2 { background: linear-gradient(120deg, #0f2a19 0%, var(--panel) 70%); border-color: #1b3a26; }
  .vote { display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); }
  .vote-opt { position: relative; overflow: hidden; display: flex; align-items: center; justify-content: space-between; gap: 10px;
    padding: 14px 16px; border-radius: 14px; background: #0c110e; border: 1px solid var(--line); color: var(--text, #fff);
    font: 700 14px var(--display); text-transform: uppercase; cursor: pointer; transition: border-color .15s, transform .15s; }
  .vote-opt:hover { border-color: rgba(31,240,111,.5); transform: translateY(-1px); }
  .vote-opt.mine { border-color: var(--green); }
  .vote-opt.mine .vote-label { color: var(--green); }
  .vote-bar { position: absolute; left: 0; top: 0; bottom: 0; width: 0; background: rgba(31,240,111,.16); transition: width .4s; }
  .vote-label, .vote-num { position: relative; }
  .vote-num { color: var(--dim); font-size: 13px; }
  .vote-note { margin-top: 12px; font-size: 13px; color: var(--dim); min-height: 18px; }
  .vote-note a { color: var(--green); }
  .discord-cta { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
  .news { display: grid; gap: 14px; grid-template-columns: repeat(auto-fill, minmax(min(100%, 320px), 1fr)); }
  .news-card { display: flex; flex-direction: column; gap: 10px; padding: 20px; border-radius: var(--radius); background: var(--panel); border: 1px solid var(--line); }
  .news-card:hover { border-color: rgba(31,240,111,.45); }
  .news-card.tournament { background: linear-gradient(120deg, #0f2a19 0%, var(--panel) 70%); border-color: #1b3a26; }
  .news-top { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
  .news-tag { padding: 4px 12px; border-radius: 99px; font: 700 11px var(--display); text-transform: uppercase; letter-spacing: .04em; background: rgba(31,240,111,.12); color: var(--green); }
  .news-tag.update { background: rgba(255,180,84,.14); color: #ffb454; }
  .news-tag.news { background: rgba(138,151,142,.16); color: var(--dim); }
  .news-date { font-size: 12px; color: var(--dim); }
  .news-card h3 { margin: 0; font-family: var(--display); font-weight: 700; font-size: 15px; line-height: 1.3; overflow-wrap: anywhere; }
  .news-card p { margin: 0; color: var(--dim); font-size: 14px; line-height: 1.55; white-space: pre-line; overflow-wrap: anywhere; }
  .news-card .btn { align-self: flex-start; margin-top: auto; }
  .tile .sub { margin-top: 6px; font-size: 13px; color: var(--dim); line-height: 1.45; }
  .tile.soon { border-style: dashed; background: transparent; }
  .tile.soon .num { color: var(--dim); }

  /* ---------- роли ---------- */
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); padding: 22px; }
  .panel h2 { margin: 0 0 16px; font-family: var(--display); font-size: 15px; font-weight: 700; text-transform: uppercase; }
  .roles { display: flex; flex-wrap: wrap; gap: 8px; }
  .role { display: inline-flex; align-items: center; gap: 8px; padding: 8px 14px; border-radius: 99px; background: var(--panel-2); border: 1px solid var(--line); font-size: 14px; }
  .dot { width: 12px; height: 12px; border-radius: 50%; background: #99aab5; flex-shrink: 0; }

  /* ---------- сервер (только участники и онлайн) ---------- */
  .stat-row { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }
  .stat { padding: 22px; border-radius: var(--radius); background: var(--panel-2); border: 1px solid var(--line); }
  .stat span { color: var(--dim); font-size: 14px; }
  .stat b { display: block; margin-top: 6px; font-family: var(--display); font-weight: 800; font-size: 40px; color: var(--green); }
  .required {
    display: flex; gap: 12px; align-items: flex-start; margin-top: 16px; padding: 14px 16px; border-radius: 14px;
    background: rgba(31,240,111,.07); border: 1px solid rgba(31,240,111,.3); font-size: 13px; line-height: 1.5;
  }
  .required svg { width: 20px; height: 20px; color: var(--green); flex-shrink: 0; margin-top: 1px; }

  /* ---------- вход / состояния ---------- */
  .center { text-align: center; padding: 64px 22px; border-radius: 26px; background: var(--panel); border: 1px solid var(--line); }
  .center h1 { font-family: var(--display); font-weight: 800; font-size: clamp(26px, 5vw, 52px); line-height: 1.1; margin: 0 0 16px; text-transform: uppercase; }
  .center h1 em { font-style: normal; color: var(--green); display: block; }
  .center p { color: var(--dim); max-width: 480px; margin: 0 auto 26px; line-height: 1.6; }

  /* ---------- магазин ---------- */
  .shop-grid { display: grid; gap: 14px; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); }
  .item { display: flex; flex-direction: column; gap: 10px; padding: 18px; border-radius: var(--radius); background: linear-gradient(170deg, var(--panel-2), var(--panel)); border: 1px solid var(--line); }
  .item .name { display: flex; align-items: center; gap: 10px; font-family: var(--display); font-weight: 700; font-size: 14px; text-transform: uppercase; overflow-wrap: anywhere; }
  .item .price { font-family: var(--display); font-weight: 800; font-size: 24px; color: var(--green); }
  .item .price small { font-family: var(--body); font-weight: 500; font-size: 13px; color: var(--dim); }
  .item .meta { color: var(--dim); font-size: 13px; }
  .item .btn { text-align: center; margin-top: auto; }
  .btn[disabled], .btn.off { background: var(--panel-2); color: var(--dim); cursor: not-allowed; border: 1px dashed var(--line); }
  .item.soon { border-style: dashed; background: transparent; }
  .subtab { display: none; }
  .subtab.active { display: block; }
  .sub-pills { margin-bottom: 6px; }
  .shop-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
  .shop-head h2 { margin: 0; }
  .sub-title { margin: 22px 0 12px; font-family: var(--display); font-size: 13px; font-weight: 700; text-transform: uppercase; color: var(--dim); }

  /* ---------- поддержка ---------- */
  .field { width: 100%; padding: 12px 14px; border-radius: 12px; border: 1px solid var(--line); background: var(--panel-2); color: var(--text); font: 500 14px var(--body); }
  textarea.field { min-height: 130px; resize: vertical; line-height: 1.5; }
  .sup-form { display: grid; gap: 10px; margin: 16px 0 6px; padding: 18px; border-radius: var(--radius); background: var(--panel-2); border: 1px solid var(--green); }
  .sup-form label { font-size: 12px; color: var(--dim); }
  .sup-actions { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  button.item { text-align: left; color: var(--text); font-family: var(--body); cursor: pointer; }
  button.item:hover { border-color: var(--green); }
  .sup-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; width: 100%; padding: 14px 16px; margin-bottom: 8px; border-radius: 14px; background: var(--panel-2); border: 1px solid var(--line); color: var(--text); font-family: var(--body); text-align: left; cursor: pointer; }
  .sup-row:hover { border-color: var(--green); }
  .sup-row b { display: block; font-size: 14px; overflow-wrap: anywhere; }
  .sup-row small { color: var(--dim); font-size: 12px; }
  .st { flex-shrink: 0; padding: 4px 12px; border-radius: 99px; font-size: 12px; font-weight: 700; }
  .st.open { background: rgba(255,180,84,.15); color: #ffb454; }
  .st.answered { background: rgba(31,240,111,.14); color: var(--green); }
  .st.closed { background: rgba(138,151,142,.15); color: var(--dim); }
  .msgs { display: grid; gap: 10px; margin: 14px 0; }
  .msg { max-width: 82%; padding: 12px 16px; border-radius: 16px; background: var(--panel-2); border: 1px solid var(--line); }
  .msg.mine { margin-left: auto; background: rgba(31,240,111,.08); border-color: rgba(31,240,111,.3); }
  .msg.staff { border-color: var(--green); }
  .msg .who { font-size: 12px; color: var(--dim); margin-bottom: 4px; }
  .msg.staff .who b { color: var(--green); }
  .msg .body { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 14px; line-height: 1.5; }
  .sup-note { margin: 10px 0 0; padding: 10px 14px; border-radius: 12px; font-size: 13px; background: rgba(255,107,107,.1); color: #ffb3b3; border: 1px solid rgba(255,107,107,.35); }
  .sup-note.ok { background: rgba(31,240,111,.08); color: var(--green); border-color: rgba(31,240,111,.3); }
  [hidden] { display: none !important; }

  /* ---------- плавающая поддержка ---------- */
  .sup-fab {
    position: fixed; right: 20px; bottom: calc(20px + env(safe-area-inset-bottom, 0px)); z-index: 40;
    width: 62px; height: 62px; border-radius: 50%; border: 0; cursor: pointer; display: grid; place-items: center;
    background: var(--green); color: #04130a; box-shadow: 0 8px 28px rgba(31,240,111,.35), 0 4px 14px rgba(0,0,0,.55);
  }
  .sup-fab:hover { background: #4dff93; }
  .sup-fab svg { width: 30px; height: 30px; }
  .sup-win {
    position: fixed; right: 20px; bottom: calc(96px + env(safe-area-inset-bottom, 0px)); z-index: 41;
    width: 380px; height: 560px; display: flex; flex-direction: column; overflow: hidden;
    background: var(--panel); border: 1px solid var(--line); border-radius: 22px; box-shadow: 0 24px 70px rgba(0,0,0,.7);
  }
  .sup-resize { position: absolute; top: 0; left: 0; width: 26px; height: 26px; z-index: 3; cursor: nwse-resize; touch-action: none; }
  .sup-resize::after { content: ""; position: absolute; top: 7px; left: 7px; width: 10px; height: 10px; border-top: 2px solid var(--dim); border-left: 2px solid var(--dim); border-radius: 3px 0 0 0; opacity: .7; }
  .sup-resize:hover::after { border-color: var(--green); opacity: 1; }
  .sup-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 14px 14px 12px 32px; background: linear-gradient(120deg, #0f2a19, var(--panel)); border-bottom: 1px solid var(--line); flex-shrink: 0; }
  .sup-title { display: flex; flex-direction: column; gap: 2px; font-family: var(--display); font-size: 14px; text-transform: uppercase; }
  .sup-team { font: 500 11px var(--body); color: var(--green); text-transform: none; }
  .sup-ctl { display: flex; gap: 6px; }
  .sup-ctl button { width: 34px; height: 34px; display: grid; place-items: center; padding: 0; border-radius: 50%; border: 1px solid var(--line); background: var(--panel-2); color: var(--dim); cursor: pointer; }
  .sup-ctl button:hover { color: var(--green); border-color: var(--green); }
  .sup-ctl svg { width: 16px; height: 16px; }
  .sup-body { flex: 1; min-height: 0; display: flex; flex-direction: column; padding: 14px 16px 16px; }
  #sup-list-view { flex: 1; min-height: 0; overflow-y: auto; }
  #sup-thread-view { flex: 1; min-height: 0; display: flex; flex-direction: column; }
  #sup-thread-view .msgs { flex: 1; min-height: 0; overflow-y: auto; margin: 12px 0; align-content: start; }
  #sup-thread-view textarea.field { min-height: 70px; max-height: 160px; flex-shrink: 0; }
  .sup-th-title { margin-top: 12px; font-family: var(--display); font-weight: 700; font-size: 14px; overflow-wrap: anywhere; }
  .sup-win .shop-grid { grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px; }
  .sup-win .item { padding: 12px 14px; gap: 6px; }
  .sup-win .item .name { font-size: 12px; }
  .sup-win .msg { max-width: 92%; }

  /* ---------- входящие (вкладка «Чаты» для модераторов) ---------- */
  .inbox { display: grid; grid-template-columns: 340px minmax(0, 1fr); gap: 16px; height: calc(100vh - 250px); min-height: 480px; }
  .inbox-list { overflow-y: auto; min-height: 0; padding-right: 4px; }
  #in-filters { flex-wrap: wrap; overflow: visible; }
  .inbox-chat { display: flex; flex-direction: column; min-height: 0; padding: 18px; border-radius: var(--radius); background: var(--panel-2); border: 1px solid var(--line); }
  #in-thread { display: flex; flex-direction: column; flex: 1; min-height: 0; }
  #in-thread .msgs { flex: 1; min-height: 0; overflow-y: auto; margin: 14px 0; align-content: start; }
  #in-thread textarea.field { min-height: 80px; max-height: 180px; flex-shrink: 0; background: var(--panel); }
  .sup-row.on { border-color: var(--green); background: rgba(31,240,111,.07); }
  .sys { text-align: center; font-size: 12px; color: var(--dim); padding: 4px 0; }
  .pill-n { display: inline-block; min-width: 18px; padding: 1px 6px; margin-left: 4px; border-radius: 99px; background: var(--green); color: #04130a; font-size: 11px; text-align: center; }
  .nav .tag.inbox-badge { background: var(--green); color: #04130a; }
  @media (max-width: 860px) { .inbox { grid-template-columns: 1fr; height: auto; } .inbox-list { max-height: 260px; } .inbox-chat { min-height: 420px; } }

  /* ---------- помощь ---------- */
  .help-list { display: grid; gap: 10px; }
  .help-item { border-radius: 16px; background: var(--panel-2); border: 1px solid var(--line); }
  .help-item[open] { border-color: var(--green); }
  .help-item summary { cursor: pointer; padding: 16px 18px; font-weight: 600; list-style: none; display: flex; justify-content: space-between; gap: 12px; }
  .help-item summary::-webkit-details-marker { display: none; }
  .help-item summary::after { content: "+"; color: var(--green); font-size: 20px; line-height: 1; }
  .help-item[open] summary::after { content: "–"; }
  .help-body { padding: 0 18px 18px; color: var(--dim); line-height: 1.6; font-size: 14px; }

  @media (max-width: 860px) {
    .side { display: none; }
    .top { padding: 10px 14px; }
    .content { padding: 18px 14px 48px; }
    .hero { flex-direction: column; text-align: center; padding: 22px; }
    .chip.hide-sm { display: none; }
    .sup-fab { right: 14px; bottom: calc(14px + env(safe-area-inset-bottom, 0px)); width: 56px; height: 56px; }
    .sup-win { left: 8px; right: 8px; bottom: calc(80px + env(safe-area-inset-bottom, 0px)); width: auto !important; height: calc(100vh - 110px) !important; max-height: 640px; }
    .sup-resize, #sup-expand { display: none; }
  }
  @media (prefers-reduced-motion: reduce) { html { scroll-behavior: auto; } }

  /* ---------- логотип ---------- */
  .logo img.logo-img { width: 57px; height: 57px; border-radius: 15px; object-fit: contain; background: none; transition: transform .35s cubic-bezier(.2,.9,.3,1.4), filter .3s; }
  .logo:hover img.logo-img { transform: rotate(-6deg) scale(1.08); filter: drop-shadow(0 0 10px rgba(31,240,111,.55)); }
  .top-title { display: flex; align-items: center; gap: 10px; }
  .top-logo { display: none; width: 44px; height: 44px; object-fit: contain; border-radius: 12px; }
  @media (max-width: 860px) { .top-logo { display: block; } }

  /* ---------- градиент справа в верхней панели ---------- */
  .top::after {
    content: ""; position: absolute; right: 0; top: 0; bottom: 0; width: min(46%, 520px); pointer-events: none; z-index: 0;
    background: radial-gradient(120% 220% at 100% 50%, rgba(31,240,111,.30) 0%, rgba(31,240,111,.10) 38%, transparent 70%);
    animation: topGlow 5s ease-in-out infinite alternate;
  }
  .top > * { position: relative; z-index: 1; }

  /* ---------- анимации ---------- */
  @keyframes tabIn   { from { opacity: 0; transform: translateY(14px); } }
  @keyframes riseIn  { from { opacity: 0; transform: translateY(18px) scale(.98); } }
  @keyframes fadeDown{ from { opacity: 0; transform: translateY(-10px); } }
  @keyframes topGlow { from { opacity: .55; } to { opacity: 1; } }
  @keyframes pillPulse { 0%,100% { box-shadow: 0 0 0 0 rgba(31,240,111,.0); } 50% { box-shadow: 0 0 16px 0 rgba(31,240,111,.35); } }
  @keyframes dcRing  { 0% { box-shadow: 0 0 0 0 rgba(88,101,242,.55); } 70%,100% { box-shadow: 0 0 0 12px rgba(88,101,242,0); } }
  @keyframes floaty  { 0%,100% { transform: translateY(0); } 50% { transform: translateY(-4px); } }
  @keyframes shimmer { from { background-position: -200% 0; } to { background-position: 200% 0; } }

  .top { animation: fadeDown .5s ease backwards; }
  .side { animation: fadeDown .5s ease backwards; }
  .tab.active { animation: tabIn .45s cubic-bezier(.2,.8,.2,1) backwards; }
  .tab.active .hero, .tab.active .panel, .tab.active .tile, .tab.active .news-card, .tab.active .ph,
  .tab.active .item, .tab.active .vote-opt { animation: riseIn .55s cubic-bezier(.2,.8,.2,1) backwards; }
  .tab.active > *:nth-child(2), .tab.active .home-main > *:nth-child(2), .tiles > *:nth-child(2), .news > *:nth-child(2), .vote > *:nth-child(2) { animation-delay: .08s; }
  .tab.active > *:nth-child(3), .tab.active .home-main > *:nth-child(3), .tiles > *:nth-child(3), .news > *:nth-child(3), .vote > *:nth-child(3) { animation-delay: .16s; }
  .tiles > *:nth-child(4), .news > *:nth-child(4) { animation-delay: .24s; }
  .tiles > *:nth-child(5), .news > *:nth-child(5) { animation-delay: .32s; }
  .tiles > *:nth-child(n+6), .news > *:nth-child(n+6) { animation-delay: .4s; }

  .pill, .nav, .btn, button.btn, .tile, .news-card, .chip { transition: transform .18s ease, box-shadow .25s ease, border-color .2s, background .2s, color .2s; }
  .pill:hover { transform: translateY(-2px); }
  .pill.active { animation: pillPulse 2.6s ease-in-out infinite; }
  .nav:hover { transform: translateX(4px); }
  .btn:hover, button.btn:hover { transform: translateY(-2px); box-shadow: 0 8px 22px rgba(31,240,111,.28); }
  .btn:active, .pill:active, .nav:active, .vote-opt:active { transform: scale(.97); }
  .tile:hover, .news-card:hover { transform: translateY(-4px); box-shadow: 0 14px 34px rgba(0,0,0,.35), 0 0 0 1px rgba(31,240,111,.25); }
  .dc-round { animation: dcRing 2.6s ease-out infinite; }
  .dc-round:hover { animation: none; transform: translateY(-2px) rotate(-8deg) scale(1.08); }
  .tab.active .home-side .ph, .home-side .ph { background: linear-gradient(100deg, #1c211e 30%, #252c28 50%, #1c211e 70%) 0 0 / 200% 100%; animation: riseIn .55s cubic-bezier(.2,.8,.2,1) .2s backwards, shimmer 3.5s linear infinite; }
  .live i { animation: pillPulse 1.6s ease-in-out infinite; }
  .vote-opt.mine { animation: pillPulse 2.6s ease-in-out infinite; }

  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation: none !important; transition: none !important; scroll-behavior: auto !important; }
  }

  /* ---------- казино ---------- */
  .casino-top { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); margin-bottom: 14px; }
  .casino-bal .lbl-s { font: 700 12px var(--display); text-transform: uppercase; letter-spacing: .05em; color: var(--dim); }
  .bal-row { display: flex; align-items: center; gap: 12px; margin-top: 8px; }
  .coin-big { width: 44px; height: 44px; object-fit: contain; }
  .bal-num { font: 800 clamp(28px, 4vw, 40px) var(--display); color: var(--green); line-height: 1; }
  .casino-timely { display: flex; align-items: center; justify-content: space-between; gap: 14px; flex-wrap: wrap; }
  .casino-timely .btn:disabled, .play-btn:disabled { opacity: .5; cursor: not-allowed; transform: none; box-shadow: none; }
  .casino-grid { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); }
  .game { display: flex; flex-direction: column; gap: 14px; align-items: center; text-align: center; }
  .game h2 { align-self: flex-start; margin: 0; }
  .wheel-box { position: relative; width: min(100%, 280px); aspect-ratio: 1; }
  .wheel { position: absolute; inset: 0; border-radius: 50%; border: 4px solid #24402f; box-shadow: 0 0 30px rgba(31,240,111,.18); transition: transform 4.2s cubic-bezier(.12,.75,.12,1); }
  .wheel .seg { position: absolute; left: 50%; top: 0; width: 0; height: 50%; transform-origin: 50% 100%; }
  .wheel .seg span { position: absolute; left: 0; top: 10px; transform: translateX(-50%); font: 700 12px var(--display); color: #fff; text-align: center; line-height: 1.15; text-shadow: 0 1px 3px rgba(0,0,0,.8); white-space: nowrap; }
  .wheel .seg span i { display: block; font-style: normal; font-size: 18px; }
  .wheel-pointer { position: absolute; left: 50%; top: -6px; transform: translateX(-50%); z-index: 2; width: 0; height: 0; border-left: 12px solid transparent; border-right: 12px solid transparent; border-top: 22px solid var(--green); filter: drop-shadow(0 0 6px rgba(31,240,111,.8)); }
  .slots { display: flex; gap: 10px; justify-content: center; }
  .reel { width: 84px; height: 96px; border-radius: 16px; background: #0b100d; border: 1px solid #24402f; display: flex; align-items: center; justify-content: center; font-size: 46px; overflow: hidden; box-shadow: inset 0 0 18px rgba(0,0,0,.7); }
  .reel.spin span { animation: reelSpin .12s linear infinite; }
  .reel.stop span { animation: reelStop .35s cubic-bezier(.2,1.4,.4,1); }
  @keyframes reelSpin { 0% { transform: translateY(-40%); opacity: .4; } 100% { transform: translateY(40%); opacity: .4; } }
  @keyframes reelStop { from { transform: translateY(-60%); } to { transform: none; } }
  .bet-row { display: flex; gap: 8px; width: 100%; max-width: 320px; }
  .bet-input { flex: 1; min-width: 0; padding: 11px 14px; border-radius: 12px; background: #0b100d; border: 1px solid var(--line); color: var(--text); font: 700 15px var(--body); }
  .bet-input:focus { outline: none; border-color: var(--green); }
  .bet-q { padding: 0 12px; border-radius: 12px; background: var(--panel-2, #151b17); border: 1px solid var(--line); color: var(--dim); font: 700 13px var(--body); cursor: pointer; transition: color .15s, border-color .15s; }
  .bet-q:hover { color: var(--green); border-color: rgba(31,240,111,.5); }
  .play-btn { width: 100%; max-width: 320px; }
  .game-result { min-height: 44px; font-size: 14px; color: var(--dim); line-height: 1.5; }
  .game-result b { color: var(--text); }
  .game-result.win b.big { color: var(--green); }
  .game-result.lose b.big { color: #ff6b6b; }
  .game-result.err { color: #ff8a8a; }
  .payouts { display: flex; gap: 8px; flex-wrap: wrap; justify-content: center; }
  .po { padding: 4px 10px; border-radius: 99px; background: rgba(255,255,255,.04); border: 1px solid var(--line); font-size: 12px; color: var(--dim); }
  .po b { font-weight: 600; letter-spacing: 1px; margin-right: 4px; }
</style>
</head>
<body>

{# ============ БОКОВОЕ МЕНЮ ============ #}
<aside class="side">
  <div class="logo">
    <img class="logo-img" src="/icon.png" alt="Логотип" onerror="this.onerror=null;this.src='{{ guild.icon_url if guild and guild.icon_url else 'data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==' }}'">
    <span>{% if guild %}{{ guild.name }}{% else %}Сервер{% endif %}</span>
  </div>
  <div class="site-online" title="Сейчас на сайте"><i class="on-dot live-dot"></i><b class="js-site-online">1</b>&nbsp;на сайте</div>
  {% if mode == 'guest' or (mode == 'cabinet' and state == 'ok' and cab.in_guild) %}
  <button class="nav active" data-tab="home"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 3 2 12h3v8h5v-5h4v5h5v-8h3z"/></svg>Главная</button>
  <button class="nav" data-tab="announcements"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M3 10v4a1 1 0 0 0 1 1h2l4 4V5L6 9H4a1 1 0 0 0-1 1zm13.500 2A4.500 4.500 0 0 0 14 8v8a4.500 4.500 0 0 0 2.500-4zM14 3.200v2.100a7 7 0 0 1 0 13.400v2.100a9 9 0 0 0 0-17.600z"/></svg>Анонсы</button>
  <button class="nav" data-tab="server"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M16 11a4 4 0 1 0-4-4 4 4 0 0 0 4 4zM6 12a3 3 0 1 0-3-3 3 3 0 0 0 3 3zm10 2c-3 0-8 1.5-8 4.5V21h16v-2.5c0-3-5-4.5-8-4.5zM6 14c-.4 0-.9 0-1.4.1C2.800 14.500 1 15.400 1 17v4h5v-2.500c0-1.800.8-3.300 2-4.400-.8-.1-1.500-.1-2-.1z"/></svg>Сервер</button>
  <button class="nav" data-tab="shop"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 18a2 2 0 1 0 2 2 2 2 0 0 0-2-2zm10 0a2 2 0 1 0 2 2 2 2 0 0 0-2-2zM1 2v2h2l3.600 7.600-1.400 2.400A2 2 0 0 0 7 17h12v-2H7.400l1.100-2h7.500a2 2 0 0 0 1.700-1L21 5H5.200l-.9-3z"/></svg>Магазин</button>
  <button class="nav" data-tab="casino"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2zm3 4a1.500 1.500 0 1 0 0 3 1.500 1.500 0 0 0 0-3zm8 0a1.500 1.500 0 1 0 0 3 1.500 1.500 0 0 0 0-3zm-4 3.500a1.500 1.500 0 1 0 0 3 1.500 1.500 0 0 0 0-3zM8 14a1.500 1.500 0 1 0 0 3 1.500 1.500 0 0 0 0-3zm8 0a1.500 1.500 0 1 0 0 3 1.500 1.500 0 0 0 0-3z"/></svg>Казино</button>
  <button class="nav" data-tab="help"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm1 16h-2v-2h2zm2.07-7.75-.9.92A3.5 3.5 0 0 0 13 14h-2v-.5a4.5 4.5 0 0 1 1.3-3.18l1.24-1.26A2 2 0 1 0 10 7.5H8a4 4 0 1 1 7.07 2.75z"/></svg>Помощь</button>
  {% if sup_cfg and sup_cfg.isAgent %}<button class="nav" data-tab="chats"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M4 4h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H8l-5 4V6a2 2 0 0 1 1-2z"/></svg>Чаты<span class="tag inbox-badge" hidden></span></button>{% endif %}
  {% endif %}
  {% if sup_cfg and sup_cfg.isAgent %}<div class="side-note"><b>Ты модератор поддержки</b>Обращения участников — во вкладке «Чаты».</div>{% endif %}
</aside>

<div class="main">
  {# ============ ВЕРХНЯЯ ПАНЕЛЬ ============ #}
  <header class="top">
    <div class="top-title"><img class="top-logo" src="/icon.png" alt="" onerror="this.style.display='none'">{% if guild %}{{ guild.name }}{% else %}Профиль участника{% endif %}</div>
    {% if guild %}
    <div class="online" title="Онлайн сервера">
      <span class="on-item"><i class="on-dot live-dot"></i><b>{{ guild.online_count }}</b>&nbsp;в сети</span>
      <span class="on-item on-site-sm"><i class="on-dot" style="background:#4d7cff"></i><b class="js-site-online">1</b>&nbsp;на сайте</span>
      <span class="on-item on-members"><i class="on-dot"></i><b>{{ guild.human_count }}</b>&nbsp;участников</span>
    </div>
    {% endif %}
    <a class="dc-round" href="{{ discord_invite }}" target="_blank" rel="noopener noreferrer" title="Наш Discord-сервер" aria-label="Наш Discord-сервер"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M20.317 4.37a19.79 19.79 0 0 0-4.885-1.515.074.074 0 0 0-.079.037c-.21.375-.444.864-.608 1.25a18.27 18.27 0 0 0-5.487 0 12.64 12.64 0 0 0-.617-1.25.077.077 0 0 0-.079-.037A19.74 19.74 0 0 0 3.677 4.37a.07.07 0 0 0-.032.027C.533 9.046-.32 13.58.099 18.057a.082.082 0 0 0 .031.057 19.9 19.9 0 0 0 5.993 3.03.078.078 0 0 0 .084-.028c.462-.63.874-1.295 1.226-1.994a.076.076 0 0 0-.041-.106 13.1 13.1 0 0 1-1.872-.892.077.077 0 0 1-.008-.128c.126-.094.252-.192.372-.292a.074.074 0 0 1 .078-.01c3.928 1.793 8.18 1.793 12.062 0a.074.074 0 0 1 .079.009c.12.1.246.198.373.293a.077.077 0 0 1-.006.127 12.3 12.3 0 0 1-1.873.892.077.077 0 0 0-.041.107c.36.698.772 1.362 1.225 1.993a.076.076 0 0 0 .084.028 19.84 19.84 0 0 0 6.002-3.03.077.077 0 0 0 .032-.054c.5-5.177-.838-9.674-3.549-13.66a.061.061 0 0 0-.031-.03zM8.02 15.33c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.956-2.419 2.157-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.333-.956 2.418-2.157 2.418zm7.975 0c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.955-2.419 2.157-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.333-.946 2.418-2.157 2.418z"/></svg></a>
    <div class="chips">
      {% if mode == 'cabinet' and state == 'ok' and cab.in_guild %}
        <span class="chip" title="Монеты"><img class="coin" src="/coin.png" alt=""><span class="js-balance">{{ balance }}</span></span>
        <span class="chip hide-sm" title="Время в войсе"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.900V22h2v-3.100A7 7 0 0 0 19 12z"/></svg>{{ voice }}</span>
        <button type="button" class="chip chip-btn" data-tab="profile" title="Открыть профиль"><img src="{{ cab.avatar_url }}" alt="">&nbsp;&nbsp;{{ cab.display_name }}</button>
      {% endif %}
      {% if user %}
        <form method="post" action="/auth/logout" style="margin:0"><button class="btn ghost" type="submit">Выйти</button></form>
      {% elif login_ready %}
        <a class="btn" href="/auth/discord">Войти через Discord</a>
      {% endif %}
    </div>
  </header>

  <div class="content">
    {% if error %}<div class="alert">{{ error }}</div>{% endif %}

    {% if mode == 'landing' %}
      <div class="center">
        <h1>Твой профиль<em>на сервере</em></h1>
        <p>Войди через Discord, чтобы увидеть время в голосовых каналах, монеты, свои роли и онлайн сервера.</p>
        {% if login_ready %}
          <a class="btn" href="/auth/discord">Войти через Discord</a>
        {% else %}
          <p>Вход через Discord ещё не настроен.</p>
        {% endif %}
      </div>

    {% elif state == 'offline' %}
      <div class="center">
        <h1>Бот <em>подключается…</em></h1>
        <p>Данные появятся через пару секунд. Обнови страницу.</p>
        <a class="btn" href="/">Обновить</a>
      </div>

    {% elif state == 'error' %}
      <div class="center">
        <h1>Не удалось <em>загрузить данные</em></h1>
        <p>Что-то пошло не так. Попробуй обновить страницу чуть позже.</p>
        <a class="btn" href="/">Обновить</a>
      </div>

    {% elif mode != 'guest' and not cab.in_guild %}
      <div class="center">
        <h1>Тебя нет <em>на сервере</em></h1>
        <p>Аккаунт <b>{{ user.name }}</b> не найден среди участников сервера «{{ guild.name }}». Зайди на сервер и обнови страницу.</p>
        <a class="btn" href="/">Обновить</a>
      </div>

    {% else %}
      {# ---------- ГЛАВНАЯ: приветствие, турнир CS2 + голосование, ссылка на Discord ---------- #}
      <section class="tab active" id="tab-home">
        <div class="home-grid">
          <div class="home-main">
            <div class="hero" style="margin-bottom: 18px;">
              <div>
                <h1>Приветствуем на нашем сайте!</h1>
                <div class="muted">Здесь анонсы, турниры, магазин, твой профиль и поддержка — всё в одном месте.</div>
              </div>
            </div>

            <div class="panel cs2" style="margin-bottom: 18px;">
              <span class="news-tag">Турнир</span>
              <h2 style="margin: 12px 0 6px;">Турнир по CS2</h2>
              <div class="muted" style="margin-bottom: 16px;">Выбери формат, в котором хочешь играть — голосуем!</div>
              <div class="vote" id="cs2-vote">
                {% for key, label in poll_options %}
                <button type="button" class="vote-opt" data-choice="{{ key }}">
                  <span class="vote-bar"></span>
                  <span class="vote-label">{{ label }}</span>
                  <span class="vote-num">0</span>
                </button>
                {% endfor %}
              </div>
              <div class="vote-note" id="cs2-note"></div>
            </div>

            <div class="panel discord-cta">
              <div>
                <h2 style="margin: 0 0 6px;">Переходите на наш сервер</h2>
                <div class="muted">Турниры, общение и игры — жми на значок Discord вверху справа.</div>
              </div>
            </div>
          </div>

          {# серое окошко справа (пропорция 1 : 1.6) — потом заполнишь сам #}
          <aside class="home-side">
            <div class="ph" id="home-ph"></div>
          </aside>
        </div>
      </section>

      {# ---------- АНОНСЫ ---------- #}
      <section class="tab" id="tab-announcements">
        <div class="panel" style="margin-bottom: 18px;">
          <div class="shop-head" style="margin-bottom: 0;">
            <h2 style="margin: 0;">Анонсы</h2>
          </div>
        </div>
        {% if announcements %}
        <div class="news">
          {% for a in announcements %}
          <article class="news-card {{ a.type }}">
            <div class="news-top">
              <span class="news-tag {{ a.type }}">{{ labels.get(a.type, 'Новости') }}</span>
              {% if a.date %}<span class="news-date">{{ a.date }}</span>{% endif %}
            </div>
            <h3>{{ a.title }}</h3>
            {% if a.text %}<p>{{ a.text }}</p>{% endif %}
            {% if a.link %}<a class="btn" href="{{ a.link }}" target="_blank" rel="noopener noreferrer">{{ a.link_text or 'Подробнее' }}</a>{% endif %}
          </article>
          {% endfor %}
        </div>
        {% else %}
        <div class="panel"><div class="muted">Пока анонсов нет — загляни позже.</div></div>
        {% endif %}
      </section>

      {# ---------- ПРОФИЛЬ ---------- #}
      <section class="tab" id="tab-profile">
        {% if mode == 'guest' %}
        <div class="hero">
          <div>
            <h1>Что сейчас на сервере</h1>
            <div class="muted">Смотри онлайн и магазин без входа. Монеты, время в войсе и роли — после входа через Discord.</div>
            {% if login_ready %}<p style="margin:16px 0 0"><a class="btn" href="/auth/discord">Войти через Discord</a></p>{% endif %}
          </div>
        </div>
        {% else %}
        <div class="hero">
          <img src="{{ cab.avatar_url }}" alt="">
          <div>
            <h1>{{ cab.display_name }}</h1>
            {% if cab.joined_at %}<div class="muted">На сервере с {{ cab.joined_at }}</div>{% endif %}
            {% if cab.voice_channel %}<span class="live"><i></i>Сейчас в войсе: {{ cab.voice_channel }}</span>{% endif %}
          </div>
        </div>
        {% endif %}

        <div class="tiles">
          <button class="tile" data-tab="server">
            <svg class="ico" viewBox="0 0 24 24" fill="currentColor"><path d="M16 11a4 4 0 1 0-4-4 4 4 0 0 0 4 4zM6 12a3 3 0 1 0-3-3 3 3 0 0 0 3 3zm10 2c-3 0-8 1.500-8 4.500V21h16v-2.500c0-3-5-4.500-8-4.500z"/></svg>
            <div class="num">{{ guild.online_count }} / {{ guild.human_count }}</div>
            <div class="lbl">Онлайн / участники</div>
            <div class="sub">Сейчас на сервере.</div>
          </button>
          <button class="tile" data-tab="shop">
            <svg class="ico" viewBox="0 0 24 24" fill="currentColor"><path d="M7 18a2 2 0 1 0 2 2 2 2 0 0 0-2-2zm10 0a2 2 0 1 0 2 2 2 2 0 0 0-2-2zM1 2v2h2l3.600 7.600-1.400 2.400A2 2 0 0 0 7 17h12v-2H7.400l1.100-2h7.500a2 2 0 0 0 1.700-1L21 5H5.200l-.9-3z"/></svg>
            <div class="num">{{ shop.roles|length if shop else 0 }}</div>
            <div class="lbl">Магазин</div>
            <div class="sub">Роли и смена ника за монеты.</div>
          </button>
          {% if mode == 'guest' %}
          <a class="tile soon" href="/auth/discord">
            <svg class="ico" viewBox="0 0 24 24" fill="currentColor"><path d="M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.900V22h2v-3.100A7 7 0 0 0 19 12z"/></svg>
            <div class="num">Войти</div>
            <div class="lbl">Время в войсе</div>
            <div class="sub">Доступно после входа через Discord.</div>
          </a>
          <a class="tile soon" href="/auth/discord">
            <img class="ico coin-ico" src="/coin.png" alt="">
            <div class="num">Войти</div>
            <div class="lbl">Мои монеты</div>
            <div class="sub">Доступно после входа через Discord.</div>
          </a>
          {% else %}
          <div class="tile">
            <svg class="ico" viewBox="0 0 24 24" fill="currentColor"><path d="M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.900V22h2v-3.100A7 7 0 0 0 19 12z"/></svg>
            <div class="num">{{ voice }}</div>
            <div class="lbl">Время в войсе</div>
            <div class="sub">Во всех голосовых каналах с момента запуска учёта.</div>
          </div>
          <div class="tile">
            <img class="ico coin-ico" src="/coin.png" alt="">
            <div class="num js-balance">{{ balance }}</div>
            <div class="lbl">Монеты в казино</div>
            <div class="sub">Твой текущий баланс.</div>
          </div>
          {% endif %}
          <button class="tile" data-tab="help">
            <svg class="ico" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm1 16h-2v-2h2zm2.07-7.75-.9.92A3.5 3.5 0 0 0 13 14h-2v-.5a4.5 4.5 0 0 1 1.3-3.18l1.24-1.26A2 2 0 1 0 10 7.5H8a4 4 0 1 1 7.07 2.75z"/></svg>
            <div class="num">FAQ</div>
            <div class="lbl">Помощь</div>
            <div class="sub">Как всё работает на сайте и сервере.</div>
          </button>
          {% if sup_cfg and sup_cfg.isAgent %}
          <button class="tile" data-tab="chats">
            <svg class="ico" viewBox="0 0 24 24" fill="currentColor"><path d="M4 4h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H8l-5 4V6a2 2 0 0 1 1-2z"/></svg>
            <div class="num inbox-count">…</div>
            <div class="lbl">Чаты</div>
            <div class="sub">Открытые обращения участников. Отвечай прямо здесь.</div>
          </button>
          {% endif %}
        </div>
        {% if mode != 'guest' %}
        <div class="panel" style="margin-top: 18px;">
          <h2>Твои роли ({{ cab.roles|length }})</h2>
          {% if cab.roles %}
          <div class="roles">
            {% for r in cab.roles %}
            <span class="role"><span class="dot" {% if r.color %}style="background: {{ r.color }}"{% endif %}></span>{{ r.name }}</span>
            {% endfor %}
          </div>
          {% else %}
          <div class="muted">У тебя пока нет ролей.</div>
          {% endif %}
        </div>
        {% endif %}
      </section>

      {# ---------- СЕРВЕР: оставлены ТОЛЬКО «Участников» и «Сейчас онлайн» ---------- #}
      <section class="tab" id="tab-server">
        <div class="panel">
          <h2>О сервере</h2>
          <div class="stat-row">
            <div class="stat"><span>Участников</span><b>{{ guild.human_count }}</b></div>
            <div class="stat"><span>Сейчас онлайн</span><b>{{ guild.online_count }}</b></div>
          </div>
          {# ВАЖНО: эти два счётчика обязательны — на них будут опираться будущие чаты сайта. Не удалять. #}
          <div class="required">
            <svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2a10 10 0 1 0 10 10A10 10 0 0 0 12 2zm1 15h-2v-2h2zm0-4h-2V7h2z"/></svg>
            <div><b>Обязательно для сайта.</b> Эти данные нужны для будущих чатов: список участников и кто сейчас онлайн.</div>
          </div>
        </div>
      </section>

      {# ---------- МАГАЗИН (заготовка: показ товаров; покупка на сайте — следующий шаг) ---------- #}
      <section class="tab" id="tab-shop">
        <div class="panel">
          <div class="shop-head">
            <h2>Магазин</h2>
            {% if mode == 'guest' %}<a class="btn" href="/auth/discord">Войти, чтобы видеть баланс</a>{% else %}<span class="chip"><img class="coin" src="/coin.png" alt="">Баланс: <span class="js-balance">{{ balance }}</span></span>{% endif %}
          </div>

          <div class="pills sub-pills">
            <button class="pill active" data-sub="server">Для сервера</button>
            <button class="pill" data-sub="goods">Товары</button>
          </div>

          <div class="subtab active" id="sub-server">
          <div class="sub-title">Роли</div>
          {% if shop and shop.roles %}
          <div class="shop-grid">
            {% for r in shop.roles %}
            <div class="item">
              <div class="name"><span class="dot" style="background: var(--green)"></span>{{ r.name }}</div>
              <div class="price">{{ r.price }} <small>монет</small></div>
              <div class="meta">Купили: {{ r.purchases }} раз</div>
              {% if r.owned %}
                <span class="btn off">Уже есть у тебя</span>
              {% else %}
                <span class="btn off" title="Покупка на сайте скоро. Пока — командой /shop в Discord">Скоро на сайте</span>
              {% endif %}
            </div>
            {% endfor %}
          </div>
          {% else %}
          <div class="muted">Пока ни одной роли не выставлено на продажу.</div>
          {% endif %}

          <div class="sub-title">Смена ника</div>
          <div class="shop-grid">
            <div class="item">
              <div class="name">Себе</div>
              <div class="price">{{ shop.nick_self if shop else 300 }} <small>монет</small></div>
              <span class="btn off">Скоро на сайте</span>
            </div>
            <div class="item">
              <div class="name">Другому участнику</div>
              <div class="price">{{ shop.nick_other if shop else 900 }} <small>монет</small></div>
              <div class="meta">Команда /nickname в Discord</div>
            </div>
          </div>

          </div>

          <div class="subtab" id="sub-goods">
          <div class="sub-title">Товары</div>
          {% if shop and shop.products %}
          <div class="shop-grid">
            {% for pr in shop.products %}
            <div class="item">
              <div class="name">{{ pr.name }}</div>
              <div class="price">{{ pr.price }} <small>монет</small></div>
              {% if pr.description %}<div class="meta">{{ pr.description }}</div>{% endif %}
              <span class="btn off">Скоро на сайте</span>
            </div>
            {% endfor %}
          </div>
          {% else %}
          <div class="shop-grid">
            <div class="item soon"><div class="name">Товары скоро</div><div class="meta">Здесь появятся предметы и бонусы, не связанные с ролями сервера.</div></div>
          </div>
          {% endif %}
          </div>

          <div class="required">
            <svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2a10 10 0 1 0 10 10A10 10 0 0 0 12 2zm1 15h-2v-2h2zm0-4h-2V7h2z"/></svg>
            <div>Сейчас покупки работают через команду <b>/shop</b> в Discord. Покупка прямо на сайте — следующий шаг.</div>
          </div>
        </div>
      </section>

      {# ---------- КАЗИНО: колесо фортуны, слоты и награда /timely (как в боте) ---------- #}
      <section class="tab" id="tab-casino">
        {% if mode == 'guest' %}
        <div class="panel casino-login">
          <h2>Казино</h2>
          <div class="muted" style="margin-bottom: 14px;">Колесо фортуны, слоты и награда раз в 4 часа — после входа через Discord.</div>
          {% if login_ready %}<a class="btn" href="/auth/discord">Войти через Discord</a>{% endif %}
        </div>
        {% else %}
        <div class="casino-top">
          <div class="panel casino-bal">
            <div class="lbl-s">Твой баланс</div>
            <div class="bal-row"><img class="coin-big" src="/coin.png" alt=""><span class="bal-num js-balance">{{ balance }}</span></div>
          </div>
          <div class="panel casino-timely">
            <div>
              <h2 style="margin: 0 0 4px;">Награда по таймеру</h2>
              <div class="muted">{{ casino_cfg.timely.min }}–{{ casino_cfg.timely.max }} монет каждые 4 часа</div>
            </div>
            <button type="button" class="btn" id="timely-btn" disabled>Забрать</button>
          </div>
        </div>

        <div class="casino-grid">
          <div class="panel game" id="game-wheel">
            <h2>Колесо фортуны</h2>
            <div class="wheel-box">
              <div class="wheel-pointer"></div>
              <div class="wheel" id="wheel"></div>
            </div>
            <div class="bet-row">
              <input class="bet-input" id="bet-wheel" type="number" min="{{ casino_cfg.minBet }}" step="1" value="{{ casino_cfg.minBet }}" inputmode="numeric">
              <button type="button" class="bet-q" data-for="bet-wheel" data-q="half">½</button>
              <button type="button" class="bet-q" data-for="bet-wheel" data-q="dbl">×2</button>
              <button type="button" class="bet-q" data-for="bet-wheel" data-q="max">Всё</button>
            </div>
            <button type="button" class="btn play-btn" id="play-wheel">Крутить</button>
            <div class="game-result" id="res-wheel"></div>
          </div>

          <div class="panel game" id="game-slots">
            <h2>Слоты</h2>
            <div class="slots" id="slots">
              <div class="reel"><span>🍒</span></div><div class="reel"><span>🍋</span></div><div class="reel"><span>🍇</span></div>
            </div>
            <div class="bet-row">
              <input class="bet-input" id="bet-slots" type="number" min="{{ casino_cfg.minBet }}" step="1" value="{{ casino_cfg.minBet }}" inputmode="numeric">
              <button type="button" class="bet-q" data-for="bet-slots" data-q="half">½</button>
              <button type="button" class="bet-q" data-for="bet-slots" data-q="dbl">×2</button>
              <button type="button" class="bet-q" data-for="bet-slots" data-q="max">Всё</button>
            </div>
            <button type="button" class="btn play-btn" id="play-slots">Крутить</button>
            <div class="game-result" id="res-slots"></div>
            <div class="payouts">
              {% for sl in casino_cfg.slots %}<span class="po"><b>{{ sl.sym }}{{ sl.sym }}{{ sl.sym }}</b> ×{{ sl.payout }}</span>{% endfor %}
            </div>
            <div class="muted" style="font-size: 12px; margin-top: 8px;">Две одинаковые — ставка возвращается.</div>
          </div>
        </div>
        <div class="muted" style="font-size: 12px; margin-top: 14px;">Минимальная ставка — {{ casino_cfg.minBet }} монет. Играй с умом: казино в среднем чуть выигрывает.</div>
        {% endif %}
      </section>

      {# ---------- ПОМОЩЬ (вкладка для инструкций — впишите свои тексты) ---------- #}
      <section class="tab" id="tab-help">
        <div class="panel">
          <div class="shop-head">
            <h2>Помощь</h2>
            <button class="btn" type="button" data-open-support>Написать в поддержку</button>
          </div>
          {# Каждый вопрос — отдельный блок <details>. Копируйте блок и меняйте текст. #}
          <div class="help-list">
            <details class="help-item"><summary>Как купить роль или сменить ник?</summary><div class="help-body">Описание скоро появится.</div></details>
            <details class="help-item"><summary>Как получить монеты?</summary><div class="help-body">Описание скоро появится.</div></details>
            <details class="help-item"><summary>Как считается время в войсе?</summary><div class="help-body">Описание скоро появится.</div></details>
            <details class="help-item"><summary>Что делать, если не прошла оплата?</summary><div class="help-body">Описание скоро появится. Пока напиши в поддержку — кнопка справа снизу.</div></details>
            <details class="help-item"><summary>Как войти на сайт?</summary><div class="help-body">Описание скоро появится.</div></details>
          </div>
        </div>
      </section>

      {# ---------- ПОДДЕРЖКА: кружок справа снизу + окно чата (можно растягивать) ---------- #}
      <button class="sup-fab" id="sup-fab" type="button" aria-label="Поддержка" title="Поддержка" aria-expanded="false">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M4 3h16a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H9l-5 4v-4H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2zm3 5v2h10V8zm0 4v2h7v-2z"/></svg>
      </button>
      <div class="sup-win" id="sup-win" role="dialog" aria-label="Поддержка" hidden>
        <div class="sup-resize" id="sup-resize" title="Потяни, чтобы изменить размер"></div>
        <div class="sup-head">
          <div class="sup-title"><b>Поддержка</b><span class="sup-team" id="sup-agent-chip" hidden>входящие — во вкладке «Чаты»</span></div>
          <div class="sup-ctl">
            <button type="button" id="sup-expand" aria-label="Увеличить" title="Увеличить / уменьшить"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M4 4h6v2H6v4H4zm10 0h6v6h-2V6h-4zM4 14h2v4h4v2H4zm14 0h2v6h-6v-2h4z"/></svg></button>
            <button type="button" id="sup-min" aria-label="Свернуть" title="Свернуть"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M6 6l12 12M18 6 6 18" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" fill="none"/></svg></button>
          </div>
        </div>
        <div class="sup-body">
          <div id="sup-list-view">
            {% if mode == 'guest' %}<p class="muted" style="margin:0 0 12px">Чтобы написать, войди через Discord.</p><a class="btn" href="/auth/discord" style="margin-bottom:12px">Войти через Discord</a>{% endif %}
            <div class="sub-title" style="margin-top:4px">Выбери тему обращения</div>
            <div class="shop-grid" id="sup-templates"></div>

            <div class="sup-form" id="sup-form" hidden>
              <label for="sup-subject">Тема</label>
              <input class="field" id="sup-subject" maxlength="100" type="text">
              <label for="sup-text">Описание (чем подробнее, тем быстрее поможем)</label>
              <textarea class="field" id="sup-text" maxlength="2000"></textarea>
              <div class="sup-actions">
                <button class="btn" id="sup-send" type="button">Отправить</button>
                <button class="btn ghost" id="sup-cancel" type="button">Отмена</button>
              </div>
            </div>
            <div class="sup-note" id="sup-note" hidden></div>

            <div id="sup-mine" hidden>
              <div class="sub-title" id="sup-list-title">Мои обращения</div>
              <div class="pills" id="sup-filters">
                <button class="pill active" data-f="">Все</button>
                <button class="pill" data-f="open">Открытые</button>
                <button class="pill" data-f="answered">Есть ответ</button>
                <button class="pill" data-f="closed">Закрытые</button>
              </div>
              <div id="sup-tickets"></div>
            </div>
          </div>

          <div id="sup-thread-view" hidden>
            <div class="sup-actions" style="justify-content: space-between;">
              <button class="btn ghost" id="sup-back" type="button">Назад</button>
              <span class="st open" id="sup-th-status"></span>
            </div>
            <div class="sup-th-title" id="sup-th-title"></div>
            <div class="muted" id="sup-th-meta"></div>
            <div class="msgs" id="sup-msgs"></div>
            <textarea class="field" id="sup-reply" maxlength="2000" placeholder="Напиши ответ…"></textarea>
            <div class="sup-actions" style="margin-top: 10px;">
              <button class="btn" id="sup-reply-send" type="button">Отправить</button>
              <button class="btn ghost" id="sup-close" type="button">Закрыть чат</button>
            </div>
            <div class="sup-note" id="sup-th-note" hidden></div>
          </div>
        </div>
      </div>

      {# ---------- ЧАТЫ: входящие обращения (только модераторам) ---------- #}
      {% if sup_cfg and sup_cfg.isAgent %}
      <section class="tab" id="tab-chats">
        <div class="panel">
          <div class="shop-head"><h2>Чаты: обращения участников</h2></div>
          <div class="inbox">
            <div class="inbox-list">
              <div class="pills" id="in-filters">
                <button class="pill active" data-f="open">Открытые</button>
                <button class="pill" data-f="answered">Есть ответ</button>
                <button class="pill" data-f="closed">Закрытые</button>
                <button class="pill" data-f="">Все</button>
              </div>
              <div id="in-tickets"></div>
            </div>
            <div class="inbox-chat">
              <div class="muted" id="in-empty">Выбери обращение слева, чтобы открыть чат.</div>
              <div id="in-thread" hidden>
                <div class="sup-actions" style="justify-content: space-between;">
                  <div>
                    <div class="sup-th-title" id="in-title" style="margin-top:0"></div>
                    <div class="muted" id="in-meta"></div>
                  </div>
                  <div class="sup-actions">
                    <span class="st open" id="in-status"></span>
                    <button class="btn ghost" id="in-close" type="button">Закрыть чат</button>
                  </div>
                </div>
                <div class="msgs" id="in-msgs"></div>
                <textarea class="field" id="in-reply" maxlength="2000" placeholder="Ответ участнику… (Ctrl+Enter — отправить)"></textarea>
                <div class="sup-actions" style="margin-top: 10px;">
                  <button class="btn" id="in-send" type="button">Ответить</button>
                </div>
                <div class="sup-note" id="in-note" hidden></div>
              </div>
            </div>
          </div>
        </div>
      </section>
      {% endif %}
    {% endif %}
  </div>
</div>

<script>window.SUP = {{ (sup_cfg or {})|tojson }};</script>
<script>
(function () {
  var box = document.getElementById('cs2-vote');
  if (!box) return;
  var S = window.SUP || {};
  var note = document.getElementById('cs2-note');
  var btns = box.querySelectorAll('.vote-opt');
  function api(method, body) {
    var opt = {method: method, credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': S.csrf || ''}};
    if (body) opt.body = JSON.stringify(body);
    return fetch('/api/poll/cs2', opt).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (!r.ok) throw new Error(d.error || 'Ошибка. Попробуй ещё раз.');
        return d;
      });
    });
  }
  function render(d) {
    var total = 0, k;
    for (k in d.counts) total += d.counts[k];
    btns.forEach(function (b) {
      var c = d.counts[b.getAttribute('data-choice')] || 0;
      b.querySelector('.vote-num').textContent = c + (total ? ' · ' + Math.round(c * 100 / total) + '%' : '');
      b.querySelector('.vote-bar').style.width = (total ? c * 100 / total : 0) + '%';
      b.classList.toggle('mine', d.mine === b.getAttribute('data-choice'));
    });
    note.textContent = '';
    if (!S.loggedIn) {
      note.innerHTML = '<a href="/auth/discord">Войди через Discord</a>, чтобы проголосовать.';
    } else if (d.mine) {
      note.textContent = 'Твой голос учтён. Можно сменить, нажав на другой вариант.';
    }
  }
  btns.forEach(function (b) {
    b.addEventListener('click', function () {
      if (!S.loggedIn) { note.innerHTML = '<a href="/auth/discord">Войди через Discord</a>, чтобы проголосовать.'; return; }
      api('POST', {choice: b.getAttribute('data-choice')}).then(render).catch(function (e) { note.textContent = e.message; });
    });
  });
  api('GET').then(render).catch(function () {});
})();
</script>
<script>
  // тех. поддержка: заготовки, список обращений, переписка
  (function () {
    var root = document.getElementById('sup-win');
    if (!root) return;
    var S = window.SUP || {};
    var NL = String.fromCharCode(10);
    var STATUS = {open: 'Открыто', answered: 'Есть ответ', closed: 'Закрыто'};
    var $ = function (id) { return document.getElementById(id); };
    var filter = '', current = null, tpl = '', timer = null;

    function el(tag, cls, text) {
      var e = document.createElement(tag);
      if (cls) e.className = cls;
      if (text != null) e.textContent = text;
      return e;
    }
    function fmt(ts) {
      return new Date(ts * 1000).toLocaleString('ru-RU', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'});
    }
    function api(method, url, body) {
      var opt = {method: method, credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': S.csrf || ''}};
      if (body) opt.body = JSON.stringify(body);
      return fetch(url, opt).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          if (!r.ok) throw new Error(d.error || 'Ошибка. Попробуй ещё раз.');
          return d;
        });
      });
    }
    function note(id, msg, ok) {
      var n = $(id);
      if (!msg) { n.hidden = true; return; }
      n.textContent = msg; n.className = 'sup-note' + (ok ? ' ok' : ''); n.hidden = false;
    }

    // ----- заготовки -----
    function renderTemplates() {
      var box = $('sup-templates');
      box.textContent = '';
      (S.templates || []).forEach(function (t) {
        var card = S.loggedIn ? el('button', 'item') : el('div', 'item');
        if (S.loggedIn) card.type = 'button';
        card.appendChild(el('div', 'name', t.title));
        if (S.loggedIn) card.addEventListener('click', function () { openForm(t); });
        box.appendChild(card);
      });
    }
    function openForm(t) {
      tpl = t.key;
      $('sup-subject').value = t.subject || '';
      $('sup-text').value = t.text || '';
      $('sup-form').hidden = false;
      note('sup-note', '');
      $('sup-text').focus();
    }
    $('sup-cancel').addEventListener('click', function () { $('sup-form').hidden = true; note('sup-note', ''); });
    $('sup-send').addEventListener('click', function () {
      var b = $('sup-send'); b.disabled = true;
      api('POST', '/api/support/tickets', {template: tpl, subject: $('sup-subject').value, text: $('sup-text').value})
        .then(function (d) { $('sup-form').hidden = true; openThread(d.ticket.id); })
        .catch(function (e) { note('sup-note', e.message); })
        .then(function () { b.disabled = false; });
    });

    // ----- список -----
    function loadList() {
      if (!S.loggedIn) return;
      $('sup-mine').hidden = false;
      var q = '/api/support/tickets?' + (filter ? 'status=' + filter : '');
      api('GET', q).then(function (d) {
        var box = $('sup-tickets');
        box.textContent = '';
        if (!d.tickets.length) { box.appendChild(el('div', 'muted', 'Обращений пока нет.')); return; }
        d.tickets.forEach(function (t) {
          var row = el('button', 'sup-row'); row.type = 'button';
          var left = el('div');
          left.appendChild(el('b', null, '#' + t.id + ' ' + t.subject));
          left.appendChild(el('small', null, fmt(t.updated) + ' · ' + t.preview));
          row.appendChild(left);
          row.appendChild(el('span', 'st ' + t.status, STATUS[t.status]));
          row.addEventListener('click', function () { openThread(t.id); });
          box.appendChild(row);
        });
      }).catch(function (e) { note('sup-note', e.message); });
    }
    $('sup-filters').addEventListener('click', function (e) {
      var f = e.target.getAttribute('data-f');
      if (f === null) return;
      filter = f;
      Array.prototype.forEach.call($('sup-filters').children, function (b) { b.classList.toggle('active', b === e.target); });
      loadList();
    });

    // ----- переписка -----
    function renderThread(t) {
      current = t;
      $('sup-th-title').textContent = '#' + t.id + ' ' + t.subject;
      $('sup-th-meta').textContent = 'От: ' + t.user_name;
      var st = $('sup-th-status'); st.textContent = STATUS[t.status]; st.className = 'st ' + t.status;
      var box = $('sup-msgs'); box.textContent = '';
      t.messages.forEach(function (m) {
        if (m.system) { box.appendChild(el('div', 'sys', m.text)); return; }
        var d = el('div', 'msg' + (m.mine ? ' mine' : '') + (m.staff ? ' staff' : ''));
        var who = el('div', 'who');
        var nm = el('b', null, m.staff ? 'Поддержка · ' + m.author : m.author);
        who.appendChild(nm); who.appendChild(document.createTextNode(' · ' + fmt(m.ts)));
        d.appendChild(who); d.appendChild(el('div', 'body', m.text));
        box.appendChild(d);
      });
      box.scrollTop = box.scrollHeight;
      var closed = t.status === 'closed';
      $('sup-close').textContent = closed ? 'Открыть заново' : 'Закрыть чат';
      $('sup-close').hidden = closed && !t.isAgent;
      $('sup-reply').placeholder = closed ? 'Ответ откроет обращение заново…' : 'Напиши ответ…';
    }
    function openThread(id) {
      api('GET', '/api/support/tickets/' + id).then(function (d) {
        $('sup-list-view').hidden = true; $('sup-thread-view').hidden = false;
        note('sup-th-note', ''); $('sup-reply').value = '';
        renderThread(d.ticket);
        clearInterval(timer);
        timer = setInterval(function () {
          if (!current || $('sup-thread-view').hidden || document.hidden) return;
          api('GET', '/api/support/tickets/' + current.id).then(function (x) { renderThread(x.ticket); }).catch(function () {});
        }, 20000);
      }).catch(function (e) { note('sup-note', e.message); });
    }
    $('sup-back').addEventListener('click', function () {
      clearInterval(timer); current = null;
      $('sup-thread-view').hidden = true; $('sup-list-view').hidden = false;
      loadList();
    });
    $('sup-reply-send').addEventListener('click', function () {
      if (!current) return;
      var b = $('sup-reply-send'); b.disabled = true;
      api('POST', '/api/support/tickets/' + current.id + '/reply', {text: $('sup-reply').value})
        .then(function (d) { $('sup-reply').value = ''; note('sup-th-note', ''); renderThread(d.ticket); })
        .catch(function (e) { note('sup-th-note', e.message); })
        .then(function () { b.disabled = false; });
    });
    $('sup-close').addEventListener('click', function () {
      if (!current) return;
      var next = current.status === 'closed' ? 'open' : 'closed';
      api('POST', '/api/support/tickets/' + current.id + '/status', {status: next})
        .then(function (d) { renderThread(d.ticket); })
        .catch(function (e) { note('sup-th-note', e.message); });
    });

    // ----- окно: открыть / свернуть / увеличить / растянуть -----
    var fab = $('sup-fab'), win = root, handle = $('sup-resize');
    var size = {w: 380, h: 560}, big = false, prev = null;
    try {
      var sv = JSON.parse(localStorage.getItem('supSize') || 'null');
      if (sv && sv.w && sv.h) size = {w: sv.w, h: sv.h};
    } catch (e) {}
    function apply() {
      size.w = Math.max(320, Math.min(size.w, window.innerWidth - 32));
      size.h = Math.max(380, Math.min(size.h, window.innerHeight - 120));
      win.style.width = size.w + 'px';
      win.style.height = size.h + 'px';
    }
    function saveSize() { try { localStorage.setItem('supSize', JSON.stringify(size)); } catch (e) {} }
    var started = false;
    function start() { if (!started || !current) loadList(); started = true; }
    function setOpen(v) {
      win.hidden = !v;
      fab.setAttribute('aria-expanded', v ? 'true' : 'false');
      if (v) { apply(); start(); }
    }
    fab.addEventListener('click', function () { setOpen(win.hidden); });
    $('sup-min').addEventListener('click', function () { setOpen(false); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !win.hidden) setOpen(false); });
    window.addEventListener('resize', function () { if (!win.hidden) apply(); });
    document.querySelectorAll('[data-open-support]').forEach(function (b) {
      b.addEventListener('click', function () { setOpen(true); });
    });
    $('sup-expand').addEventListener('click', function () {
      if (!big) {
        prev = {w: size.w, h: size.h};
        size.w = Math.min(920, window.innerWidth - 32);
        size.h = window.innerHeight - 120;
        big = true;
      } else {
        size = prev || {w: 380, h: 560};
        big = false;
      }
      apply();
    });
    handle.addEventListener('pointerdown', function (e) {
      e.preventDefault();
      var sx = e.clientX, sy = e.clientY, w0 = size.w, h0 = size.h;
      big = false;
      handle.setPointerCapture(e.pointerId);
      function move(ev) { size.w = w0 + (sx - ev.clientX); size.h = h0 + (sy - ev.clientY); apply(); }
      function up() {
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', up);
        saveSize();
      }
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', up);
    });

    // ----- запуск -----
    renderTemplates();
    $('sup-agent-chip').hidden = !S.isAgent;
    $('sup-reply').addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) $('sup-reply-send').click();
    });
  })();
</script>
<script>
  // вкладка «Чаты» для модераторов: список обращений + переписка + закрытие чата
  (function () {
    var root = document.getElementById('tab-chats');
    var S = window.SUP || {};
    if (!root || !S.isAgent) return;
    var STATUS = {open: 'Открыто', answered: 'Есть ответ', closed: 'Закрыто'};
    var $ = function (id) { return document.getElementById(id); };
    var filter = 'open', current = null;

    function el(tag, cls, text) {
      var e = document.createElement(tag);
      if (cls) e.className = cls;
      if (text != null) e.textContent = text;
      return e;
    }
    function fmt(ts) {
      return new Date(ts * 1000).toLocaleString('ru-RU', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'});
    }
    function api(method, url, body) {
      var opt = {method: method, credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': S.csrf || ''}};
      if (body) opt.body = JSON.stringify(body);
      return fetch(url, opt).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          if (!r.ok) throw new Error(d.error || 'Ошибка. Попробуй ещё раз.');
          return d;
        });
      });
    }
    function note(msg) {
      var n = $('in-note');
      n.textContent = msg || ''; n.hidden = !msg;
    }

    // счётчик новых (открытых) обращений на кнопках «Чаты»
    function refreshBadge() {
      api('GET', '/api/support/tickets?all=1&status=open').then(function (d) {
        var n = d.tickets.length;
        document.querySelectorAll('.inbox-badge').forEach(function (b) { b.textContent = n; b.hidden = !n; });
        document.querySelectorAll('.inbox-count').forEach(function (b) { b.textContent = n; });
      }).catch(function () {});
    }

    function loadList() {
      api('GET', '/api/support/tickets?all=1' + (filter ? '&status=' + filter : '')).then(function (d) {
        var box = $('in-tickets');
        box.textContent = '';
        if (!d.tickets.length) { box.appendChild(el('div', 'muted', 'Обращений нет.')); return; }
        d.tickets.forEach(function (t) {
          var row = el('button', 'sup-row' + (current && current.id === t.id ? ' on' : '')); row.type = 'button';
          var left = el('div');
          left.appendChild(el('b', null, '#' + t.id + ' ' + t.subject));
          left.appendChild(el('small', null, t.user_name + ' · ' + fmt(t.updated) + ' · ' + t.preview));
          row.appendChild(left);
          row.appendChild(el('span', 'st ' + t.status, STATUS[t.status]));
          row.addEventListener('click', function () { openThread(t.id); });
          box.appendChild(row);
        });
      }).catch(function (e) { $('in-tickets').textContent = e.message; });
    }
    $('in-filters').addEventListener('click', function (e) {
      var f = e.target.getAttribute('data-f');
      if (f === null) return;
      filter = f;
      Array.prototype.forEach.call($('in-filters').children, function (b) { b.classList.toggle('active', b === e.target); });
      loadList();
    });

    function render(t, keepScroll) {
      current = t;
      $('in-empty').hidden = true; $('in-thread').hidden = false;
      $('in-title').textContent = '#' + t.id + ' ' + t.subject;
      $('in-meta').textContent = 'От: ' + t.user_name;
      var st = $('in-status'); st.textContent = STATUS[t.status]; st.className = 'st ' + t.status;
      var box = $('in-msgs'); var top = box.scrollTop; box.textContent = '';
      t.messages.forEach(function (m) {
        if (m.system) { box.appendChild(el('div', 'sys', m.text)); return; }
        var d = el('div', 'msg' + (m.mine ? ' mine' : '') + (m.staff ? ' staff' : ''));
        var who = el('div', 'who');
        who.appendChild(el('b', null, m.staff ? 'Поддержка · ' + m.author : m.author));
        who.appendChild(document.createTextNode(' · ' + fmt(m.ts)));
        d.appendChild(who); d.appendChild(el('div', 'body', m.text));
        box.appendChild(d);
      });
      box.scrollTop = keepScroll ? top : box.scrollHeight;
      $('in-close').textContent = t.status === 'closed' ? 'Открыть заново' : 'Закрыть чат';
    }
    function openThread(id) {
      note('');
      api('GET', '/api/support/tickets/' + id).then(function (d) { render(d.ticket); loadList(); })
        .catch(function (e) { note(e.message); });
    }
    $('in-send').addEventListener('click', function () {
      if (!current) return;
      var b = $('in-send'); b.disabled = true;
      api('POST', '/api/support/tickets/' + current.id + '/reply', {text: $('in-reply').value})
        .then(function (d) { $('in-reply').value = ''; note(''); render(d.ticket); loadList(); refreshBadge(); })
        .catch(function (e) { note(e.message); })
        .then(function () { b.disabled = false; });
    });
    $('in-reply').addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) $('in-send').click();
    });
    $('in-close').addEventListener('click', function () {
      if (!current) return;
      var next = current.status === 'closed' ? 'open' : 'closed';
      api('POST', '/api/support/tickets/' + current.id + '/status', {status: next})
        .then(function (d) { note(''); render(d.ticket); loadList(); refreshBadge(); })
        .catch(function (e) { note(e.message); });
    });

    // запуск и автообновление (раз в 20 секунд, пока вкладка открыта)
    document.querySelectorAll('[data-tab="chats"]').forEach(function (b) {
      b.addEventListener('click', function () { loadList(); refreshBadge(); });
    });
    if ((location.hash || '') === '#chats') loadList();
    refreshBadge();
    setInterval(function () {
      if (document.hidden) return;
      refreshBadge();
      if (root.classList.contains('active')) {
        loadList();
        if (current) api('GET', '/api/support/tickets/' + current.id).then(function (d) { render(d.ticket, true); }).catch(function () {});
      }
    }, 20000);
  })();
</script>
<script>
  // подразделы магазина: «Для сервера» / «Товары»
  (function () {
    var btns = document.querySelectorAll('[data-sub]');
    btns.forEach(function (b) {
      b.addEventListener('click', function () {
        var name = b.getAttribute('data-sub');
        document.querySelectorAll('.subtab').forEach(function (t) { t.classList.toggle('active', t.id === 'sub-' + name); });
        btns.forEach(function (x) { x.classList.toggle('active', x === b); });
      });
    });
  })();
</script>
<script>
  // переключение разделов: боковое меню, строка категорий и плитки используют один атрибут data-tab
  (function () {
    var tabs = document.querySelectorAll('.tab');
    if (!tabs.length) return;
    function show(name) {
      if (!document.getElementById('tab-' + name)) name = 'home';
      tabs.forEach(function (t) { t.classList.toggle('active', t.id === 'tab-' + name); });
      document.querySelectorAll('.nav, .pill, .chip-btn').forEach(function (b) {
        b.classList.toggle('active', b.getAttribute('data-tab') === name);
      });
      try { history.replaceState(null, '', '#' + name); } catch (e) {}
    }
    document.querySelectorAll('[data-tab]').forEach(function (b) {
      b.addEventListener('click', function () { show(b.getAttribute('data-tab')); window.scrollTo(0, 0); });
    });
    show((location.hash || '').replace('#', '') || 'home');
  })();
</script>
<script>window.CASINO = {{ casino_cfg|tojson }};</script>
<script>
(function () {
  var C = window.CASINO, S = window.SUP || {};
  var wheelEl = document.getElementById('wheel');
  if (!wheelEl || !C) return;
  var $ = function (id) { return document.getElementById(id); };
  var busy = false, balance = 0, wheelAngle = 0, timelyLeft = 0, timelyTimer = null;

  function api(method, url, body) {
    var opt = {method: method, credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': S.csrf || ''}};
    if (body) opt.body = JSON.stringify(body);
    return fetch(url, opt).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (!r.ok) { var e = new Error(d.error || 'Ошибка. Попробуй ещё раз.'); e.data = d; throw e; }
        return d;
      });
    });
  }
  function fmt(n) { var t = String(Math.round(n)), o = ''; for (var k = 0; k < t.length; k++) { if (k && (t.length - k) % 3 === 0) o += ' '; o += t.charAt(k); } return o; }
  function setBalance(n, f) {
    balance = n;
    document.querySelectorAll('.js-balance').forEach(function (e) { e.textContent = f || fmt(n); });
  }
  function result(id, html, cls) { var e = $(id); e.className = 'game-result' + (cls ? ' ' + cls : ''); e.innerHTML = html; }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function esc(t) { var d = document.createElement('div'); d.textContent = t; return d.innerHTML; }

  // ----- колесо: 8 равных долей, исход выбирает сервер -----
  var COLORS = ['#3a1515', '#4a2a12', '#2a2f2c', '#12391f', '#15422a', '#0f5a30', '#0a7a3f', '#b8860b'];
  (function buildWheel() {
    var n = C.wheel.length, step = 360 / n, grads = [], html = '';
    C.wheel.forEach(function (sg, i) {
      var col = COLORS[i % COLORS.length];
      grads.push(col + ' ' + (i * step) + 'deg ' + ((i + 1) * step) + 'deg');
      var mid = i * step + step / 2;
      html += '<div class="seg" style="transform: rotate(' + mid + 'deg)"><span><i>' + sg.icon + '</i>x' + sg.mult + '</span></div>';
    });
    wheelEl.style.background = 'conic-gradient(' + grads.join(',') + ')';
    wheelEl.innerHTML = html;
  })();
  function spinWheelTo(index) {
    var n = C.wheel.length, step = 360 / n;
    var target = 360 - (index * step + step / 2);            // нужная доля — под стрелкой (сверху)
    var jitter = (Math.random() - 0.5) * step * 0.6;
    wheelAngle = Math.ceil(wheelAngle / 360) * 360 + 360 * 5 + target + jitter;
    wheelEl.style.transform = 'rotate(' + wheelAngle + 'deg)';
    return sleep(4300);
  }

  // ----- слоты -----
  var symList = C.slots.map(function (x) { return x.sym; });
  function randSym() { return symList[Math.floor(Math.random() * symList.length)]; }
  function spinReels(final) {
    var reels = document.querySelectorAll('#slots .reel');
    reels.forEach(function (r) { r.classList.remove('stop'); r.classList.add('spin'); });
    var timer = setInterval(function () { reels.forEach(function (r, i) { if (r.classList.contains('spin')) r.firstChild.textContent = randSym(); }); }, 90);
    var chain = Promise.resolve();
    reels.forEach(function (r, i) {
      chain = chain.then(function () { return sleep(i === 0 ? 900 : 450); }).then(function () {
        r.classList.remove('spin'); r.firstChild.textContent = final[i]; r.classList.add('stop');
      });
    });
    return chain.then(function () { clearInterval(timer); });
  }

  // ----- ставки -----
  function readBet(id) { return parseInt($(id).value, 10); }
  document.querySelectorAll('.bet-q').forEach(function (b) {
    b.addEventListener('click', function () {
      var inp = $(b.getAttribute('data-for')), v = parseInt(inp.value, 10) || C.minBet, q = b.getAttribute('data-q');
      if (q === 'half') v = Math.max(C.minBet, Math.floor(v / 2));
      else if (q === 'dbl') v = v * 2;
      else v = Math.max(C.minBet, balance);
      inp.value = Math.min(v, Math.max(balance, C.minBet));
    });
  });
  function lock(on) {
    busy = on;
    ['play-wheel', 'play-slots'].forEach(function (id) { $(id).disabled = on; });
  }
  function outcome(d) {
    var delta = d.winnings - d.bet;
    var big = delta > 0 ? '+' + fmt(delta) : (delta < 0 ? '−' + fmt(-delta) : '±0');
    return {html: '<b class="big">' + big + '</b> монет · выплата ' + fmt(d.winnings) + ' (ставка ' + fmt(d.bet) + ')',
            cls: delta > 0 ? 'win' : (delta < 0 ? 'lose' : '')};
  }

  $('play-wheel').addEventListener('click', function () {
    if (busy) return;
    var bet = readBet('bet-wheel');
    if (!(bet >= C.minBet)) { result('res-wheel', 'Минимальная ставка — ' + C.minBet + '.', 'err'); return; }
    lock(true); result('res-wheel', 'Крутим…');
    api('POST', '/api/casino/wheel', {bet: bet}).then(function (d) {
      return spinWheelTo(d.index).then(function () {
        var o = outcome(d);
        result('res-wheel', '<b>' + esc(d.label) + '</b><br>' + o.html, o.cls);
        setBalance(d.balance, d.balanceFmt);
      });
    }).catch(function (e) { result('res-wheel', esc(e.message), 'err'); }).then(function () { lock(false); });
  });
  $('play-slots').addEventListener('click', function () {
    if (busy) return;
    var bet = readBet('bet-slots');
    if (!(bet >= C.minBet)) { result('res-slots', 'Минимальная ставка — ' + C.minBet + '.', 'err'); return; }
    lock(true); result('res-slots', 'Крутим…');
    api('POST', '/api/casino/slots', {bet: bet}).then(function (d) {
      return spinReels(d.reel).then(function () {
        var o = outcome(d);
        result('res-slots', '<b>' + esc(d.text) + '</b><br>' + o.html, o.cls);
        setBalance(d.balance, d.balanceFmt);
      });
    }).catch(function (e) { result('res-slots', esc(e.message), 'err'); }).then(function () { lock(false); });
  });

  // ----- награда по таймеру -----
  var tbtn = $('timely-btn');
  function tickTimely() {
    if (timelyLeft > 0) {
      var h = Math.floor(timelyLeft / 3600), m = Math.floor(timelyLeft % 3600 / 60), s = timelyLeft % 60;
      tbtn.textContent = (h ? h + 'ч ' : '') + m + 'м ' + (h ? '' : s + 'с');
      tbtn.disabled = true; timelyLeft--;
    } else {
      tbtn.textContent = 'Забрать'; tbtn.disabled = false;
      if (timelyTimer) { clearInterval(timelyTimer); timelyTimer = null; }
    }
  }
  function startTimely(left) {
    timelyLeft = left;
    if (timelyTimer) clearInterval(timelyTimer);
    tickTimely();
    if (timelyLeft > 0) timelyTimer = setInterval(tickTimely, 1000);
  }
  tbtn.addEventListener('click', function () {
    tbtn.disabled = true;
    api('POST', '/api/casino/timely').then(function (d) {
      setBalance(d.balance, d.balanceFmt);
      startTimely(d.timelyLeft);
      tbtn.title = '+' + d.reward + ' монет!';
      var note = document.createElement('div');
      note.className = 'muted'; note.style.cssText = 'width:100%;color:var(--green);font-weight:700';
      note.textContent = '+' + d.reward + ' монет получено!';
      tbtn.parentNode.appendChild(note); setTimeout(function () { note.remove(); }, 6000);
    }).catch(function (e) { if (e.data && e.data.timelyLeft) startTimely(e.data.timelyLeft); else tbtn.disabled = false; });
  });

  // ----- стартовое состояние -----
  api('GET', '/api/casino/state').then(function (d) { setBalance(d.balance, d.balanceFmt); startTimely(d.timelyLeft); }).catch(function () {});
})();
</script>
<script>
(function () {
  var els = document.querySelectorAll('.js-site-online');
  if (!els.length) return;
  function ping() {
    if (document.hidden) return;
    fetch('/api/presence', {credentials: 'same-origin', cache: 'no-store'})
      .then(function (r) { return r.json(); })
      .then(function (d) {
        els.forEach(function (e) { e.textContent = d.online; });
      }).catch(function () {});
  }
  ping();
  setInterval(ping, 20000);
  document.addEventListener('visibilitychange', ping);
})();
</script>
</body>
</html>
"""
