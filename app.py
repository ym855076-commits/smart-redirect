import os, io, re, json, time, secrets, hmac
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests
try:
    import segno
except ImportError:
    segno = None
from flask import Flask, request, redirect, session, make_response, render_template_string

app = Flask(__name__)

# ───────── Настройки (задаются в Vercel → Settings → Environment Variables) ─────────
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
app.secret_key = os.environ.get("SECRET_KEY") or ADMIN_PASSWORD or secrets.token_hex(16)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=bool(os.environ.get("VERCEL")))
CONTACT_TG = os.environ.get("CONTACT_TG", "your_telegram").lstrip("@")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")   # токен бота от @BotFather
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")       # твой chat id (узнать у @userinfobot)

# Тарифы — меняй как хочешь (это стартовые цифры, подгони под рынок)
PLANS = [
    {"name": "Start",    "links": 5,   "tmt": 50,  "usd": 5},
    {"name": "Business", "links": 20,  "tmt": 120, "usd": 12},
    {"name": "Agency",   "links": 100, "tmt": 300, "usd": 30},
]

# ───────── Хранилище: Upstash Redis (REST) или память, если базы нет ─────────
REDIS_URL = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL")
REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("KV_REST_API_TOKEN")
USE_REDIS = bool(REDIS_URL and REDIS_TOKEN)
_mem = {}


def _mem_exec(c):
    op, key, *a = c
    if op == "DEL":
        return 1 if _mem.pop(key, None) is not None else 0
    h = _mem.setdefault(key, {})
    if op == "HGET":
        return h.get(a[0])
    if op == "HGETALL":
        return [x for kv in h.items() for x in kv]
    if op == "HSET":
        h[a[0]] = a[1]
        return 1
    if op == "HDEL":
        return 1 if h.pop(a[0], None) is not None else 0
    if op == "HINCRBY":
        h[a[0]] = str(int(h.get(a[0], 0)) + int(a[1]))
        return int(h[a[0]])


def run(cmds):
    if not USE_REDIS:
        return [_mem_exec(c) for c in cmds]
    r = requests.post(REDIS_URL.rstrip("/") + "/pipeline",
                      headers={"Authorization": "Bearer " + REDIS_TOKEN},
                      json=cmds, timeout=3)
    r.raise_for_status()
    return [x.get("result") for x in r.json()]


def pairs(flat):
    return dict(zip(flat[::2], flat[1::2])) if flat else {}


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_link(code):
    raw = run([["HGET", "sr:links", code]])[0]
    return json.loads(raw) if raw else None


def all_links():
    return {k: json.loads(v) for k, v in pairs(run([["HGETALL", "sr:links"]])[0]).items()}


BOT_RE = re.compile(r"bot|crawl|spider|slurp|preview|facebookexternalhit|whatsapp|curl|wget|python-requests|httpclient|headless|monitor|uptime", re.I)


def is_bot(ua):
    return (not ua) or bool(BOT_RE.search(ua))


def device_of(ua):
    u = ua.lower()
    if "iphone" in u or "ipad" in u:
        return "iOS"
    if "android" in u:
        return "Android"
    if "windows" in u:
        return "Windows"
    if "mac os" in u or "macintosh" in u:
        return "Mac"
    if "linux" in u:
        return "Linux"
    return "Other"


def base_url():
    local = "localhost" in request.host or request.host.startswith("127.")
    return ("http://" if local else "https://") + request.host + "/"


def alert_dead(code, link):
    """Сообщение в Telegram, если основная ссылка не открывается (не чаще раза в час)."""
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        return
    try:
        last = run([["HGET", "sr:alert", code]])[0]
        if last and time.time() - float(last) < 3600:
            return
        run([["HSET", "sr:alert", code, str(time.time())]])
        text = "⚠️ /%s (%s) не открывается:\n%s\nПосетители идут на резерв." % (code, link.get("client", ""), link["target"])
        requests.post("https://api.telegram.org/bot%s/sendMessage" % TG_BOT_TOKEN,
                      json={"chat_id": TG_CHAT_ID, "text": text}, timeout=2)
    except Exception:
        pass


def top(code, kind, n=5):
    d = pairs(run([["HGETALL", "sr:%s:%s" % (kind, code)]])[0])
    return sorted(((k, int(v)) for k, v in d.items()), key=lambda kv: -kv[1])[:n]


def record_hit(code, used_fallback, country="??", device="Other"):
    cmds = [["HINCRBY", "sr:clicks", code, 1], ["HINCRBY", "sr:day:" + today(), code, 1],
            ["HINCRBY", "sr:cc:" + code, country, 1], ["HINCRBY", "sr:dev:" + code, device, 1]]
    if used_fallback:
        cmds.append(["HINCRBY", "sr:fb", code, 1])
    try:
        run(cmds)
    except Exception:
        pass  # счётчик не должен ломать редирект


def week_stats(code):
    days = [(datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
    res = run([["HGET", "sr:day:" + d, code] for d in days])
    return [(d[5:], int(x or 0)) for d, x in zip(days, res)]


# ───────── Проверка ссылок ─────────
CACHE = {}  # url -> (до какого времени, жива ли)


def is_alive(url):
    host = urlparse(url).netloc.lower()
    if host in ("t.me", "telegram.me"):
        return True  # t.me всегда отвечает 200, проверить канал снаружи нельзя
    now = time.time()
    hit = CACHE.get(url)
    if hit and hit[0] > now:
        return hit[1]
    try:
        r = requests.get(url, timeout=2, stream=True, headers={"User-Agent": "Mozilla/5.0 Smart-Redirect"})
        ok = r.status_code < 400
        r.close()
    except requests.RequestException:
        ok = False
    CACHE[url] = (now + (120 if ok else 30), ok)
    return ok


# ───────── Редирект ─────────
RESERVED = {"admin", "s", "qr", "favicon.ico", "robots.txt", "static", "api"}


@app.route("/<code>")
def go(code):
    if code in RESERVED:
        return "Not found", 404
    try:
        link = get_link(code)
    except Exception:
        return "Сервис временно недоступен", 503
    if not link:
        return "Ссылка не найдена", 404
    ua = request.headers.get("User-Agent", "")
    alive = is_alive(link["target"])
    if not alive:
        alert_dead(code, link)
    dest = link["target"] if (alive or not link.get("fallback")) else link["fallback"]
    if not is_bot(ua):  # боты и превью мессенджеров не считаются
        record_hit(code, dest != link["target"], request.headers.get("x-vercel-ip-country", "??"), device_of(ua))
    resp = redirect(dest, 302)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ───────── QR-код ссылки ─────────
@app.route("/qr/<code>.svg")
def qr(code):
    if segno is None or not get_link(code):
        return "Not found", 404
    buf = io.BytesIO()
    segno.make(base_url() + code, error="m").save(buf, kind="svg", scale=8, border=3)
    resp = make_response(buf.getvalue())
    resp.mimetype = "image/svg+xml"
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


# ───────── Публичная статистика для клиента ─────────
@app.route("/s/<code>/<key>")
def client_stats(code, key):
    link = get_link(code)
    if not link or not hmac.compare_digest(link.get("key", ""), key):
        return "Not found", 404
    total, fb = run([["HGET", "sr:clicks", code], ["HGET", "sr:fb", code]])
    week = week_stats(code)
    return render_template_string(STATS_HTML, code=code, link=link, total=int(total or 0),
                                  fb=int(fb or 0), week=week, mx=max([n for _, n in week] + [1]), css=CSS,
                                  countries=top(code, "cc"), devices=top(code, "dev"))


# ───────── Админка ─────────
def authed():
    return bool(ADMIN_PASSWORD) and session.get("ok") is True


def csrf_ok():
    return hmac.compare_digest(request.form.get("csrf", ""), session.get("csrf", "x"))


@app.route("/admin")
def admin():
    if not ADMIN_PASSWORD:
        return "Задай ADMIN_PASSWORD в настройках Vercel и сделай Redeploy.", 503
    if not authed():
        return render_template_string(LOGIN_HTML, css=CSS, err=request.args.get("e"))
    session.setdefault("csrf", secrets.token_hex(16))
    links = all_links()
    clicks = pairs(run([["HGETALL", "sr:clicks"]])[0])
    fbs = pairs(run([["HGETALL", "sr:fb"]])[0])
    tday = pairs(run([["HGETALL", "sr:day:" + today()]])[0])
    rows = []
    for code, l in sorted(links.items(), key=lambda kv: kv[1].get("client", "")):
        rows.append(dict(code=code, l=l, total=clicks.get(code, 0), fb=fbs.get(code, 0), today=tday.get(code, 0),
                         stats=base_url() + "s/" + code + "/" + l.get("key", "")))
    return render_template_string(ADMIN_HTML, css=CSS, rows=rows, csrf=session["csrf"],
                                  db=USE_REDIS, msg=request.args.get("m"), host=base_url(), tg=bool(TG_BOT_TOKEN and TG_CHAT_ID), qr=segno is not None)


@app.route("/admin/login", methods=["POST"])
def login():
    if ADMIN_PASSWORD and hmac.compare_digest(request.form.get("pw", ""), ADMIN_PASSWORD):
        session["ok"] = True
        session["csrf"] = secrets.token_hex(16)
        return redirect("/admin")
    time.sleep(1)
    return redirect("/admin?e=1")


@app.route("/admin/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/admin")


CODE_RE = re.compile(r"^[A-Za-z0-9_-]{2,32}$")


def clean_url(u):
    u = (u or "").strip()
    p = urlparse(u)
    if p.scheme in ("http", "https") and p.netloc and p.netloc != request.host:
        return u
    return None


@app.route("/admin/save", methods=["POST"])
def save():
    if not (authed() and csrf_ok()):
        return redirect("/admin")
    code = request.form.get("code", "").strip()
    target = clean_url(request.form.get("target"))
    fb_raw = request.form.get("fallback", "").strip()
    fallback = clean_url(fb_raw) if fb_raw else ""
    if not CODE_RE.match(code) or code in RESERVED or not target or fallback is None:
        return redirect("/admin?m=Ошибка: проверь код (латиница, 2–32) и ссылки (https://...)")
    old = get_link(code) or {}
    data = {"target": target, "fallback": fallback, "client": request.form.get("client", "").strip()[:60],
            "key": old.get("key") or secrets.token_urlsafe(8), "created": old.get("created") or today()}
    run([["HSET", "sr:links", code, json.dumps(data, ensure_ascii=False)]])
    return redirect("/admin?m=Сохранено: /" + code)


@app.route("/admin/delete", methods=["POST"])
def delete():
    if authed() and csrf_ok():
        code = request.form.get("code", "")
        run([["HDEL", "sr:links", code], ["HDEL", "sr:clicks", code], ["HDEL", "sr:fb", code],
             ["HDEL", "sr:alert", code], ["DEL", "sr:cc:" + code], ["DEL", "sr:dev:" + code]])
    return redirect("/admin?m=Удалено")


# ───────── Лендинг для продаж (RU / EN) ─────────
TXT = {
    "ru": dict(h="Одна короткая ссылка. Меняй адрес — QR и баннеры остаются прежними.",
               sub="Smart-Redirect: ссылка на ваш сайт, магазин или Telegram, которую можно поменять в любой момент, с резервным адресом и статистикой кликов.",
               b=[("Не перепечатывай QR", "Ссылка на визитке, баннере или в рекламе остаётся той же. Адрес назначения меняем за минуту."),
                  ("Резервная страница", "Если сайт не открывается, посетитель автоматически попадает на запасной адрес."),
                  ("Статистика кликов", "Сколько человек перешло сегодня и за неделю. Отдельная страница для вас.")],
               pl="Тарифы", mo="в месяц", ln="ссылок", cur="TMT", cta="Написать в Telegram",
               pay="Оплата: перевод на карту (манаты) или USDT.", other="English"),
    "en": dict(h="One short link. Change the destination, keep every QR code and ad.",
               sub="Smart-Redirect: a link to your site, shop or Telegram that you can repoint anytime, with a backup address and click stats.",
               b=[("No more reprinting QR codes", "The link on your card, banner or ad never changes. We update the destination in a minute."),
                  ("Backup page", "If your site is down, visitors are sent to your fallback address automatically."),
                  ("Click stats", "See clicks today and over the last 7 days on your own private page.")],
               pl="Pricing", mo="per month", ln="links", cur="USD", cta="Message on Telegram",
               pay="Payment: USDT (Telegram Wallet).", other="Русский"),
}


@app.route("/")
def home():
    lang = "en" if request.args.get("lang") == "en" else "ru"
    return render_template_string(HOME_HTML, t=TXT[lang], lang=lang, plans=PLANS, tg=CONTACT_TG, css=CSS,
                                  other="ru" if lang == "en" else "en")


# ───────── Шаблоны ─────────
CSS = """
:root{--bg:#0F172A;--card:#1E293B;--acc:#10B981;--tx:#F8FAFC;--mu:#94A3B8;--bd:#334155}
*{box-sizing:border-box;margin:0;padding:0;font-family:Inter,system-ui,sans-serif}
body{background:var(--bg);color:var(--tx);padding:16px;display:flex;justify-content:center;min-height:100vh}
.c{width:100%;max-width:520px;display:flex;flex-direction:column;gap:16px}
h1{font-size:26px;line-height:1.25;letter-spacing:-.5px}h2{font-size:18px}
p,.mu{color:var(--mu);line-height:1.5;font-size:15px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:16px;padding:18px;display:flex;flex-direction:column;gap:10px}
.btn{display:flex;align-items:center;justify-content:center;min-height:46px;background:var(--acc);color:#0F172A;font-weight:600;font-size:16px;border-radius:14px;border:0;text-decoration:none;cursor:pointer;padding:0 16px;width:100%}
.btn.s{background:transparent;color:var(--mu);border:1px solid var(--bd);min-height:36px;font-size:13px;width:auto}
.btn.d{background:#7f1d1d;color:#fecaca;min-height:36px;font-size:13px;width:auto}
input{width:100%;min-height:44px;background:var(--bg);border:1px solid var(--bd);border-radius:12px;color:var(--tx);padding:0 12px;font-size:15px}
.price{font-size:24px;font-weight:700}.price small{font-size:13px;color:var(--mu);font-weight:400}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.sp{justify-content:space-between}
code{color:var(--acc);word-break:break-all;font-size:13px}.msg{color:var(--acc);font-size:14px}
.bar{height:8px;background:var(--acc);border-radius:4px}
"""

HOME_HTML = """<!DOCTYPE html><html lang="{{lang}}"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Smart-Redirect</title>
<style>{{css}}</style></head><body><div class="c">
<div class="row sp"><b>Smart-Redirect</b><a class="btn s" href="/?lang={{other}}">{{t.other}}</a></div>
<h1>{{t.h}}</h1><p>{{t.sub}}</p>
{% for title, text in t.b %}<div class="card"><h2>{{title}}</h2><p>{{text}}</p></div>{% endfor %}
<h2>{{t.pl}}</h2>
{% for p in plans %}<div class="card"><div class="row sp"><b>{{p.name}}</b><span class="mu">{{p.links}} {{t.ln}}</span></div>
<div class="price">{% if lang=='en' %}${{p.usd}}{% else %}{{p.tmt}} TMT{% endif %} <small>{{t.mo}}</small></div></div>{% endfor %}
<a class="btn" href="https://t.me/{{tg}}">{{t.cta}}</a><p>{{t.pay}}</p></div></body></html>"""

LOGIN_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin</title><style>{{css}}</style></head><body><div class="c"><h1>Admin</h1>
<form method="post" action="/admin/login" class="card"><input type="password" name="pw" placeholder="Пароль" autofocus>
{% if err %}<span class="mu">Неверный пароль</span>{% endif %}<button class="btn">Войти</button></form></div></body></html>"""

ADMIN_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin</title><style>{{css}}</style></head><body><div class="c">
<div class="row sp"><h1>Ссылки</h1><form method="post" action="/admin/logout"><button class="btn s">Выйти</button></form></div>
{% if not db %}<div class="card"><b>⚠ База не подключена</b><p>Клики и ссылки сотрутся при перезапуске. Подключи Upstash Redis в Vercel (Storage).</p></div>{% endif %}
{% if not tg %}<div class="card"><b>Уведомления выключены</b><p>Добавь TG_BOT_TOKEN и TG_CHAT_ID в Vercel, и бот напишет тебе, когда ссылка перестанет открываться.</p></div>{% endif %}
{% if msg %}<div class="msg">{{msg}}</div>{% endif %}
<form method="post" action="/admin/save" class="card"><input type="hidden" name="csrf" value="{{csrf}}">
<h2>Добавить / изменить</h2>
<input name="code" placeholder="Код (например cafe-menu)" required>
<input name="client" placeholder="Клиент (название)">
<input name="target" placeholder="Основная ссылка https://..." required>
<input name="fallback" placeholder="Резервная ссылка (необязательно)">
<p>Тот же код = изменить ссылку. Telegram-ссылки (t.me) автоматически не проверяются.</p>
<button class="btn">Сохранить</button></form>
{% for r in rows %}<div class="card"><div class="row sp"><b>/{{r.code}}</b><span class="mu">{{r.l.client}}</span></div>
<code>→ {{r.l.target}}</code>{% if r.l.fallback %}<code>⤷ {{r.l.fallback}}</code>{% endif %}
<p>Кликов: {{r.total}} · сегодня: {{r.today}} · на резерв: {{r.fb}}</p>
<code>{{host}}{{r.code}}</code><code>Статистика клиенту: {{r.stats}}</code>
{% if qr %}<a href="/qr/{{r.code}}.svg" target="_blank"><img src="/qr/{{r.code}}.svg" width="110" height="110" style="background:#fff;border-radius:8px" alt="QR"></a>{% endif %}
<form method="post" action="/admin/delete" onsubmit="return confirm('Удалить /{{r.code}}?')">
<input type="hidden" name="csrf" value="{{csrf}}"><input type="hidden" name="code" value="{{r.code}}"><button class="btn d">Удалить</button></form></div>
{% else %}<p>Пока нет ссылок.</p>{% endfor %}</div></body></html>"""

STATS_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Stats /{{code}}</title><style>{{css}}</style></head><body><div class="c">
<h1>/{{code}}</h1><p>{{link.client}}</p>
<div class="card"><div class="price">{{total}} <small>кликов всего</small></div><p>На резервную страницу: {{fb}}</p></div>
<div class="card"><h2>7 дней</h2>
{% for d, n in week %}<div class="row sp"><span class="mu">{{d}}</span><b>{{n}}</b></div>
<div class="bar" style="width:{{ (n / mx * 100) | int }}%"></div>{% endfor %}</div>
<div class="card"><h2>Страны</h2>{% for k, n in countries %}<div class="row sp"><span class="mu">{{k}}</span><b>{{n}}</b></div>{% else %}<p>Пока нет данных</p>{% endfor %}</div>
<div class="card"><h2>Устройства</h2>{% for k, n in devices %}<div class="row sp"><span class="mu">{{k}}</span><b>{{n}}</b></div>{% else %}<p>Пока нет данных</p>{% endfor %}</div>
</div></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
