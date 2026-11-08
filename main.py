import asyncio, json, logging, math, re, time
from datetime import datetime
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup,
                           Message, ReactionTypeEmoji)

import config as C
import db

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("lumemc")
bot = Bot(C.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
dp = Dispatcher()
SESS = {}   # личные сессии: заполнение анкет / ответы / дополнения
PEND = {}   # модераторы: причина закрытия/отказа
G = F.chat.id == C.GROUP_ID
GC = F.message.chat.id == C.GROUP_ID


# ───────── helpers ─────────
def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])

BACK = kb([B(text="🏠 В меню", callback_data="menu")])
CANCEL = kb([B(text="❌ Отмена", callback_data="menu")])
now = lambda: int(time.time())
fmt = lambda ts: datetime.fromtimestamp(ts, C.MSK).strftime("%d.%m.%Y %H:%M МСК")
code = lambda t: f"{C.KINDS[t['kind']]['sym']}{t['num']:06d}"
end = lambda k, stem: stem + ("а" if k["fem"] else "о")

def mention(uid, username, name):
    return f"@{username}" if username else f'<a href="tg://user?id={uid}">{escape(name or str(uid))}</a>'

def disp(u):
    return mention(u.id, u.username, u.first_name)

def level(uid):
    if uid in C.OWNERS: return 3
    r = db.one("SELECT level FROM admins WHERE user_id=?", (uid,))
    return r["level"] if r else 0

def restricted(t):
    return C.KINDS[t["kind"]]["restricted"] or t["ctype"] == "mod"

def touch(u):
    if u and not u.is_bot:
        db.ex("INSERT INTO users VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
              "username=excluded.username, first_name=excluded.first_name",
              (u.id, u.username, u.first_name))

def find_user(m: Message, token: str):
    if m.reply_to_message and m.reply_to_message.from_user and not m.reply_to_message.forum_topic_created:
        u = m.reply_to_message.from_user
        return u.id, u.username, u.first_name
    r = db.one("SELECT * FROM users WHERE lower(username)=?", (token.lstrip("@").lower(),))
    return (r["user_id"], r["username"], r["first_name"]) if r else None

async def show(ev, text, markup=None):
    if isinstance(ev, CallbackQuery):
        try:
            await ev.message.edit_text(text, reply_markup=markup); return
        except TelegramBadRequest:
            pass
        await ev.message.answer(text, reply_markup=markup)
    else:
        await ev.answer(text, reply_markup=markup)

async def dm(uid, text, markup=None):
    try:
        await bot.send_message(uid, text, reply_markup=markup)
    except (TelegramForbiddenError, TelegramBadRequest):
        pass

def menu_kb():
    return kb([B(text="📞 Связь с модератором", callback_data="new:support")],
              [B(text="⚠️ Подать жалобу", callback_data="new:complaint")],
              [B(text="🛡 Заявка на модератора", callback_data="new:modapp"),
               B(text="🎬 Заявка на медиа", callback_data="new:media")],
              [B(text="⚖️ Обжалование наказания", callback_data="new:appeal")],
              [B(text="ℹ️ Информация", callback_data="info"),
               B(text="📂 Мои обращения", callback_data="my")])

def greet(u):
    name = f"@{u.username}" if u.username else escape(u.full_name)
    return f"👋 Здравствуйте, <b>{name}</b>, в боте <b>LumeMC</b>!\n\n📌 <b>Выбери раздел ниже:</b>"

def status_text(t):
    if t["status"] == "open": return "🟢 Ожидает модератора"
    if t["status"] == "taken": return "🟡 В работе"
    return {"approved": "✅ Одобрено", "denied": "❌ Отказано"}.get(t["result"], "⚫ Закрыто")

def topic_name(t):
    e = {"open": "🟢", "taken": "🔴", "closed": "⚫"}[t["status"]]
    who = f"@{t['username']}" if t["username"] else (t["first_name"] or str(t["user_id"]))
    return f"{e} {who} {code(t)}"[:120]

def body(t, with_user=True):
    k = C.KINDS[t["kind"]]
    L = [f"{k['icon']} <b>{k['title']} {code(t)}</b>", ""]
    if with_user:
        L.append(f"👤 <b>Telegram:</b> {mention(t['user_id'], t['username'], t['first_name'])} (<code>{t['user_id']}</code>)")
    if t["ctype"]:
        L.append("🎯 <b>Тип:</b> " + ("На игрока" if t["ctype"] == "player" else "На модератора"))
    for label, val in t["fields"]:
        L.append(f"\n▫️ <b>{escape(label)}:</b>\n{escape(val)}")
    L += ["", f"📊 <b>Статус:</b> {status_text(t)}"]
    if t["taker_name"]: L.append(f"👮 <b>Ответственный:</b> {t['taker_name']}")
    if t["reason"]: L.append(f"📝 <b>Причина:</b> {escape(t['reason'])}")
    return "\n".join(L)

def card_kb(t):
    k, i = C.KINDS[t["kind"]], t["id"]
    if t["status"] == "open":
        return kb([B(text=f"✋ Взять {k['acc']}", callback_data=f"take:{i}")])
    if t["status"] == "taken":
        drop = B(text=f"↩️ Отказаться от {k['gen']}", callback_data=f"drop:{i}")
        if k["fin"] == "close":
            return kb([drop, B(text=f"🔒 Закрыть {k['acc']}", callback_data=f"close:{i}")])
        return kb([drop], [B(text="✅ Одобрить", callback_data=f"ok:{i}"),
                           B(text="❌ Отказать", callback_data=f"no:{i}")])
    return None

async def refresh(tid):
    t = db.ticket(tid)
    try:
        await bot.edit_message_text(body(t), chat_id=C.GROUP_ID, message_id=t["card_msg_id"], reply_markup=card_kb(t))
    except TelegramBadRequest: pass
    try:
        await bot.edit_forum_topic(C.GROUP_ID, t["topic_id"], name=topic_name(t))
    except TelegramBadRequest: pass
    if t["status"] == "closed":
        try: await bot.close_forum_topic(C.GROUP_ID, t["topic_id"])
        except TelegramBadRequest: pass

def blocked(uid, kind):
    mu = db.one("SELECT * FROM mutes WHERE user_id=?", (uid,))
    if mu and mu["until"] > now():
        return f"🔇 Вам ограничено создание обращений до <b>{fmt(mu['until'])}</b>.\n📝 Причина: {escape(mu['reason'])}"
    op = db.one("SELECT * FROM tickets WHERE user_id=? AND kind=? AND status!='closed'", (uid, kind))
    if op:
        return f"⏳ У вас уже есть открытая запись: <b>{C.KINDS[kind]['title']} {code(op)}</b>.\nДождитесь её закрытия."
    cd = db.one("SELECT until FROM cooldowns WHERE user_id=? AND kind=?", (uid, kind))
    if cd and cd["until"] > now():
        return f"⏱ Новую запись можно создать через <b>{math.ceil((cd['until'] - now()) / 60)} мин.</b>"
    return None

def steps_for(kind, ctype):
    if kind == "complaint":
        who = "игрока" if ctype == "player" else "модератора"
        return [(f"Ник {who}", f"Введите ник {who}:", ""),
                ("Ник заявителя", "Введите свой ник:", ""),
                ("Суть жалобы", "Суть жалобы? (приложите доказательства, если они есть)", "media")]
    return C.KINDS[kind]["steps"]


# ───────── личка: меню ─────────
@dp.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def start(m: Message):
    touch(m.from_user); SESS.pop(m.from_user.id, None)
    await m.answer(greet(m.from_user), reply_markup=menu_kb())

@dp.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery):
    touch(c.from_user); SESS.pop(c.from_user.id, None)
    await show(c, greet(c.from_user), menu_kb()); await c.answer()

@dp.callback_query(F.data == "info")
async def cb_info(c: CallbackQuery):
    await show(c, C.INFO_TEXT, BACK); await c.answer()

@dp.callback_query(F.data.startswith("new:"))
async def cb_new(c: CallbackQuery):
    kind = c.data[4:]
    if kind == "complaint":
        await show(c, "⚠️ <b>Подача жалобы</b>\n\nНа кого жалоба?",
                   kb([B(text="👤 На игрока", callback_data="ct:player"),
                       B(text="🛡 На модератора", callback_data="ct:mod")],
                      [B(text="❌ Отмена", callback_data="menu")]))
        await c.answer(); return
    await begin(c, kind)

@dp.callback_query(F.data.startswith("ct:"))
async def cb_ct(c: CallbackQuery):
    await begin(c, "complaint", c.data[3:])

async def begin(c, kind, ctype=None):
    await c.answer(); u = c.from_user; touch(u)
    err = blocked(u.id, kind)
    if err:
        await show(c, err, BACK); return
    s = dict(mode="form", kind=kind, ctype=ctype, steps=steps_for(kind, ctype), i=0, ans=[], extra=[])
    SESS[u.id] = s
    await ask(c, s)

async def ask(target, s):
    n, i = len(s["steps"]), s["i"]
    await show(target, f"<b>Шаг {i + 1}/{n}</b>\n\n{escape(s['steps'][i][1])}\n\n✍️ <i>Напишите сообщение в чат</i>", CANCEL)


# ───────── личка: мои обращения ─────────
@dp.callback_query(F.data == "my")
async def cb_my(c: CallbackQuery):
    await c.answer()
    rs = db.rows("SELECT * FROM tickets WHERE user_id=? ORDER BY id DESC LIMIT 20", (c.from_user.id,))
    if not rs:
        await show(c, "📂 У вас пока нет обращений.", BACK); return
    rows = [[B(text=f"{status_text(t).split()[0]} {C.KINDS[t['kind']]['title']} - {code(t)}", callback_data=f"my:{t['id']}")] for t in rs]
    rows.append([B(text="🏠 В меню", callback_data="menu")])
    await show(c, "📂 <b>Мои обращения</b>\n\nВыберите запись:", kb(*rows))

@dp.callback_query(F.data.regexp(r"^my:\d+$"))
async def cb_my_one(c: CallbackQuery):
    t = db.ticket(int(c.data[3:])); await c.answer()
    if not t or t["user_id"] != c.from_user.id: return
    rows = []
    if t["status"] != "closed": rows.append([B(text="✍️ Дополнить", callback_data=f"add:{t['id']}")])
    rows.append([B(text="⬅️ Назад", callback_data="my")])
    await show(c, body(t, with_user=False), kb(*rows))

@dp.callback_query(F.data.regexp(r"^add:\d+$"))
async def cb_add(c: CallbackQuery):
    t = db.ticket(int(c.data[4:]))
    if not t or t["user_id"] != c.from_user.id or t["status"] == "closed":
        await c.answer("Запись уже закрыта", show_alert=True); return
    SESS[c.from_user.id] = dict(mode="addon", tid=t["id"])
    await show(c, f"✍️ Напишите дополнение к <b>{code(t)}</b> (текст, фото, видео — что угодно):", CANCEL); await c.answer()

@dp.callback_query(F.data.regexp(r"^reply:\d+$"))
async def cb_reply(c: CallbackQuery):
    t = db.ticket(int(c.data[6:]))
    if not t or t["user_id"] != c.from_user.id or t["status"] != "taken":
        await c.answer("Запись уже закрыта или модератор отказался от неё", show_alert=True); return
    SESS[c.from_user.id] = dict(mode="reply", tid=t["id"])
    await c.message.answer("✍️ <b>Напишите свое сообщение в чат</b>", reply_markup=CANCEL); await c.answer()


# ───────── личка: сообщения ─────────
async def relay(m: Message, t, header):
    kw = dict(message_thread_id=t["topic_id"])
    if m.text:
        await bot.send_message(C.GROUP_ID, f"{header}\n\n{escape(m.text)}", **kw)
    else:
        await bot.send_message(C.GROUP_ID, header, **kw)
        await bot.copy_message(C.GROUP_ID, m.chat.id, m.message_id, **kw)

@dp.message(F.chat.type == ChatType.PRIVATE)
async def private_msg(m: Message):
    touch(m.from_user); uid = m.from_user.id; s = SESS.get(uid)
    if not s:
        await m.answer("Выберите раздел в меню 👇", reply_markup=menu_kb()); return

    if s["mode"] in ("reply", "addon"):
        t = db.ticket(s["tid"])
        if not t or t["status"] == "closed" or (s["mode"] == "reply" and t["status"] != "taken"):
            SESS.pop(uid, None); await m.answer("🔒 Запись уже закрыта.", reply_markup=BACK); return
        who = mention(uid, m.from_user.username, m.from_user.first_name)
        await relay(m, t, f"💬 <b>Сообщение от игрока</b> {who}:" if s["mode"] == "reply" else f"➕ <b>Дополнение от</b> {who}:")
        if s["mode"] == "reply": SESS.pop(uid, None)
        await m.answer("✅ Отправлено!" + (" Модератор ответит вам здесь." if s["mode"] == "reply" else " Можете написать ещё или вернуться в меню."), reply_markup=BACK)
        return

    label, q, flag = s["steps"][s["i"]]
    text = (m.text or m.caption or "").strip()
    if m.text is None:
        if "media" not in flag:
            await m.answer("Здесь нужен текстовый ответ ✍️", reply_markup=CANCEL); return
        s["extra"].append(m.message_id); text = text or "📎 вложение"
    if len(text) > (1500 if "media" in flag else 300):
        await m.answer("⚠️ Слишком длинный ответ, сократите его.", reply_markup=CANCEL); return
    if "long" in flag and len(text) < 40:
        await m.answer("⚠️ Ответ должен быть развернутым (минимум 40 символов).", reply_markup=CANCEL); return
    r = re.search(r"int:(\d+):(\d+)", flag)
    if r and not (text.isdigit() and int(r[1]) <= int(text) <= int(r[2])):
        await m.answer(f"⚠️ Введите число от {r[1]} до {r[2]}.", reply_markup=CANCEL); return
    s["ans"].append([label, text]); s["i"] += 1
    if s["i"] < len(s["steps"]): await ask(m, s)
    else: await finish(m, s)

async def finish(m: Message, s):
    u = m.from_user; kind = s["kind"]; k = C.KINDS[kind]; SESS.pop(u.id, None)
    cur = db.ex("INSERT INTO tickets(kind,num,user_id,username,first_name,fields,ctype,created) VALUES(?,?,?,?,?,?,?,?)",
                (kind, db.next_num(kind), u.id, u.username, u.first_name, json.dumps(s["ans"], ensure_ascii=False), s["ctype"], now()))
    t = db.ticket(cur.lastrowid)
    try:
        topic = await bot.create_forum_topic(C.GROUP_ID, topic_name(t))
        card = await bot.send_message(C.GROUP_ID, body(t), message_thread_id=topic.message_thread_id, reply_markup=card_kb(t))
    except Exception as e:
        log.error("topic error: %s", e); db.ex("DELETE FROM tickets WHERE id=?", (t["id"],))
        await m.answer("❌ Не удалось создать запись. Сообщите администрации.", reply_markup=BACK); return
    db.ex("UPDATE tickets SET topic_id=?, card_msg_id=? WHERE id=?", (topic.message_thread_id, card.message_id, t["id"]))
    for mid in s["extra"]:
        try: await bot.copy_message(C.GROUP_ID, u.id, mid, message_thread_id=topic.message_thread_id)
        except TelegramBadRequest: pass
    c = code(t)
    if kind == "support":
        await m.answer(f"✅ <b>Обращение {c} создано!</b>\n\nОжидайте — модератор скоро возьмёт его, ответ придёт сюда.", reply_markup=BACK)
    elif kind in ("complaint", "appeal"):
        await m.answer(f"✅ <b>{k['poss']} {c} было зафиксировано!</b>\n\nВы можете дополнить своё обращение в главном меню, нажав на кнопку «📂 Мои обращения» и выбрав свою запись. "
                       "Мы можем написать вам, чтобы узнать больше информации или расспросить вас.", reply_markup=BACK)
    else:
        SESS[u.id] = dict(mode="addon", tid=t["id"])
        await m.answer(f"✅ <b>Анкета отправлена!</b> ({c})\n\n✍️ Можете дописать дополнение сюда:", reply_markup=kb([B(text="🏠 Вернуться в меню", callback_data="menu")]))


# ───────── группа: команды ─────────
def cmd_ok(m):
    return not C.COMMANDS_TOPIC_ID or m.message_thread_id == C.COMMANDS_TOPIC_ID

@dp.message(Command("mpmute"), G)
async def mpmute(m: Message, command: CommandObject):
    touch(m.from_user)
    if level(m.from_user.id) == 0 or not cmd_ok(m): return
    a = (command.args or "").split(maxsplit=2)
    d = re.fullmatch(r"(\d+)([smhdw])", a[1].lower()) if len(a) > 1 else None
    if len(a) < 3 or not d:
        await m.reply("Использование: <code>/mpmute @ник 30m причина</code>\nЕдиницы: s/m/h/d/w"); return
    u = find_user(m, a[0])
    if not u: await m.reply("❌ Игрок не найден (он должен был писать боту или в группу)."); return
    if level(u[0]): await m.reply("⛔ Нельзя замутить администратора."); return
    until = now() + int(d[1]) * dict(s=1, m=60, h=3600, d=86400, w=604800)[d[2]]
    db.ex("INSERT OR REPLACE INTO mutes VALUES(?,?,?)", (u[0], until, a[2][:300]))
    await m.reply(f"🔇 {mention(*u)} не сможет создавать обращения до <b>{fmt(until)}</b>.\n📝 Причина: {escape(a[2])}")
    await dm(u[0], f"🔇 Вам ограничено создание обращений до <b>{fmt(until)}</b>.\n📝 Причина: {escape(a[2])}")

@dp.message(Command("mpunmute"), G)
async def mpunmute(m: Message, command: CommandObject):
    if level(m.from_user.id) == 0 or not cmd_ok(m): return
    u = find_user(m, (command.args or "x").split()[0])
    if not u: await m.reply("❌ Игрок не найден."); return
    db.ex("DELETE FROM mutes WHERE user_id=?", (u[0],)); await m.reply(f"🔊 {mention(*u)} размучен.")

@dp.message(Command("setadmin"), G)
async def setadmin(m: Message, command: CommandObject):
    touch(m.from_user); actor = level(m.from_user.id)
    if actor < 2 or not cmd_ok(m): return
    a = (command.args or "").split()
    if len(a) != 2 or a[1] not in ("0", "1", "2"):
        await m.reply("Использование: <code>/setadmin @ник 1/2</code> (0 — снять)"); return
    u = find_user(m, a[0]); new = int(a[1])
    if not u: await m.reply("❌ Пользователь не найден (он должен был писать боту или в группу)."); return
    if u[0] in C.OWNERS: await m.reply("⛔ Владельцев менять нельзя."); return
    if actor == 2 and (new == 2 or level(u[0]) >= 2):
        await m.reply("⛔ Админ 2 уровня может назначать только админов 1 уровня."); return
    if new: db.ex("INSERT OR REPLACE INTO admins VALUES(?,?)", (u[0], new))
    else: db.ex("DELETE FROM admins WHERE user_id=?", (u[0],))
    await m.reply(f"✅ {mention(*u)}: " + (f"админ {new} уровня" if new else "админка снята"))


# ───────── группа: кнопки карточек ─────────
@dp.callback_query(GC, F.data.regexp(r"^(take|drop|close|ok|no):\d+$"))
async def group_cb(c: CallbackQuery):
    act, tid = c.data.split(":"); uid = c.from_user.id; touch(c.from_user)
    t = db.ticket(int(tid)); lvl = level(uid)
    if not t or t["status"] == "closed": await c.answer("Запись уже закрыта", show_alert=True); return
    if lvl == 0: await c.answer("⛔ У вас нет доступа", show_alert=True); return
    k = C.KINDS[t["kind"]]; me = disp(c.from_user); kw = dict(message_thread_id=t["topic_id"])

    if act == "take":
        if t["status"] != "open": await c.answer("Уже взято", show_alert=True); return
        if restricted(t) and lvl < 2:
            await c.answer("⛔ Эту запись может взять только админ с админ-панелью", show_alert=True); return
        db.ex("UPDATE tickets SET status='taken', taker_id=?, taker_name=? WHERE id=?", (uid, me, t["id"]))
        await refresh(t["id"])
        await bot.send_message(C.GROUP_ID, f"✅ {me} взял {k['acc']} на себя!", **kw)
        await dm(t["user_id"], f"👮 {k['poss']} <b>{code(t)}</b> взял модератор!\n\nСообщения от него будут приходить сюда.")
        await c.answer(); return

    if t["status"] != "taken": await c.answer("Сначала возьмите запись", show_alert=True); return

    if act == "drop":
        if uid != t["taker_id"] and lvl < 3: await c.answer("Отказаться может только взявший", show_alert=True); return
        db.ex("UPDATE tickets SET status='open', taker_id=NULL, taker_name=NULL WHERE id=?", (t["id"],))
        PEND.pop(t["taker_id"], None)
        await refresh(t["id"])
        await bot.send_message(C.GROUP_ID, f"↩️ {me} отказался от {k['gen']}. Теперь его может взять другой модератор.", **kw)
        await dm(t["user_id"], f"ℹ️ Модератор отказался от {k['gen']} <b>{code(t)}</b>. Скоро его возьмёт другой.")
        await c.answer(); return

    if uid != t["taker_id"]: await c.answer("Это не ваша запись", show_alert=True); return
    PEND[uid] = dict(tid=t["id"], act=act, reason=None, wait=act != "ok")
    if act == "ok":
        await send_confirm(t, PEND[uid])
    else:
        what = f"закрытия {k['gen']}" if act == "close" else f"отказа ({k['title']})"
        await bot.send_message(C.GROUP_ID, f"✍️ <b>Введите причину {what} {code(t)}</b>\n\n{me}, напишите её следующим сообщением в эту тему.", **kw)
    await c.answer()

async def send_confirm(t, p):
    k = C.KINDS[t["kind"]]; c = code(t)
    if p["act"] == "ok":
        txt = f"✅ {k['title']} {c} будет {end(k, 'одобрен')}.\n\n<b>Вы уверены?</b>"
        rows = [[B(text="✅ Да", callback_data=f"cf:yes:{t['id']}"), B(text="❌ Нет", callback_data=f"cf:no:{t['id']}")]]
    else:
        verb = end(k, "закрыт") if p["act"] == "close" else end(k, "отклонен")
        txt = f"📋 <b>{k['title']} {c} будет {verb} по причине:</b>\n\n«{escape(p['reason'])}»\n\n<b>Вы уверены?</b>"
        rows = [[B(text="✅ Да", callback_data=f"cf:yes:{t['id']}"), B(text="❌ Нет", callback_data=f"cf:no:{t['id']}")],
                [B(text="✏️ Редактировать причину", callback_data=f"cf:edit:{t['id']}")]]
    await bot.send_message(C.GROUP_ID, txt, message_thread_id=t["topic_id"], reply_markup=kb(*rows))

@dp.callback_query(GC, F.data.regexp(r"^cf:(yes|no|edit):\d+$"))
async def confirm_cb(c: CallbackQuery):
    _, a, tid = c.data.split(":"); uid = c.from_user.id
    t = db.ticket(int(tid)); p = PEND.get(uid)
    if not t or t["status"] != "taken" or t["taker_id"] != uid or not p or p["tid"] != t["id"]:
        await c.answer("Подтвердить может только модератор, ведущий запись", show_alert=True); return
    k = C.KINDS[t["kind"]]
    if a == "no":
        PEND.pop(uid, None); await c.message.delete(); await c.answer("Отменено"); return
    if a == "edit":
        p["wait"] = True; await c.message.edit_text("✍️ <b>Введите новую причину</b> следующим сообщением:"); await c.answer(); return
    res = dict(close="closed", ok="approved", no="denied")[p["act"]]; PEND.pop(uid, None)
    db.ex("UPDATE tickets SET status='closed', result=?, reason=?, closed_at=? WHERE id=?", (res, p["reason"], now(), t["id"]))
    db.ex("INSERT OR REPLACE INTO cooldowns VALUES(?,?,?)", (t["user_id"], t["kind"], now() + C.COOLDOWN_SEC))
    SESS.pop(t["user_id"], None)
    word = dict(closed=end(k, "закрыт"), approved=end(k, "одобрен"), denied=end(k, "отклонен"))[res]
    await c.message.edit_text(f"{'✅' if res != 'denied' else '❌'} {k['title']} <b>{code(t)}</b> {word}.")
    msg = f"{'✅' if res == 'approved' else '🔒' if res == 'closed' else '❌'} {k['poss']} <b>{code(t)}</b> {word}."
    if p["reason"]: msg += f"\n📝 <b>Причина:</b> {escape(p['reason'])}"
    await dm(t["user_id"], msg + "\n\n⏱ Новую запись такого типа можно создать через 10 минут.", BACK)
    await refresh(t["id"]); await c.answer()


# ───────── группа: сообщения в темах ─────────
@dp.message(G, F.message_thread_id)
async def group_msg(m: Message):
    if not m.from_user or m.from_user.is_bot: return
    touch(m.from_user); uid = m.from_user.id; text = (m.text or "").strip()
    p = PEND.get(uid)
    if p and p["wait"] and text and not text.startswith("+"):
        t = db.ticket(p["tid"])
        if t and t["topic_id"] == m.message_thread_id:
            p["reason"] = text[:500]; p["wait"] = False
            await send_confirm(t, p); return
    if text.startswith("+") and len(text) > 1:
        r = db.one("SELECT id FROM tickets WHERE topic_id=? AND status='taken' AND taker_id=?", (m.message_thread_id, uid))
        if not r: return
        t = db.ticket(r["id"])
        try:
            await bot.send_message(t["user_id"], f"👮 <b>Модератор - {code(t)}</b>\n\n{escape(text[1:].strip())}",
                                   reply_markup=kb([B(text="💬 Ответить", callback_data=f"reply:{t['id']}")]))
            try: await m.react([ReactionTypeEmoji(emoji="👍")])
            except TelegramBadRequest: pass
        except (TelegramForbiddenError, TelegramBadRequest):
            await m.reply("⚠️ Не удалось доставить: игрок заблокировал бота.")


async def main():
    me = await bot.get_me(); log.info("Бот @%s запущен", me.username)
    try:
        mem = await bot.get_chat_member(C.GROUP_ID, me.id)
        if mem.status not in ("administrator", "creator"):
            log.warning("⚠️ Бот не админ в группе — выдай права (управление темами, удаление сообщений)!")
    except Exception as e:
        log.warning("Нет доступа к группе %s: %s", C.GROUP_ID, e)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
