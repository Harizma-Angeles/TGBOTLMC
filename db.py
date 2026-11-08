import sqlite3, json, os
from config import DB_PATH

os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
con = sqlite3.connect(DB_PATH, check_same_thread=False)
con.row_factory = sqlite3.Row
con.executescript("""
CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT);
CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY, level INTEGER);
CREATE TABLE IF NOT EXISTS mutes(user_id INTEGER PRIMARY KEY, until INTEGER, reason TEXT);
CREATE TABLE IF NOT EXISTS cooldowns(user_id INTEGER, kind TEXT, until INTEGER, PRIMARY KEY(user_id, kind));
CREATE TABLE IF NOT EXISTS tickets(
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, num INTEGER, user_id INTEGER,
  username TEXT, first_name TEXT, topic_id INTEGER, card_msg_id INTEGER,
  status TEXT DEFAULT 'open', taker_id INTEGER, taker_name TEXT,
  fields TEXT, ctype TEXT, result TEXT, reason TEXT, created INTEGER, closed_at INTEGER);
""")

def ex(sql, args=()):
    cur = con.execute(sql, args); con.commit(); return cur

def rows(sql, args=()):
    return [dict(r) for r in con.execute(sql, args).fetchall()]

def one(sql, args=()):
    r = con.execute(sql, args).fetchone(); return dict(r) if r else None

def ticket(tid):
    t = one("SELECT * FROM tickets WHERE id=?", (tid,))
    if t: t["fields"] = json.loads(t["fields"] or "[]")
    return t

def next_num(kind):
    return (one("SELECT COALESCE(MAX(num),0) m FROM tickets WHERE kind=?", (kind,))["m"]) + 1
