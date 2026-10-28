from flask import Flask, redirect
import requests

app = Flask(__name__)

# Ваши ссылки и резервные адреса (fallback)
LINKS = {
    "offer1": {
        "target": "https://t.me/your_channel",
        "fallback": "https://google.com",
    },
    "sale": {"target": "https://google.com", "fallback": "https://wikipedia.org"},
}


# Умный редирект с проверкой здоровья ссылок
@app.route("/<short_code>")
def smart_redirect(short_code):
  if short_code in LINKS:
    link_data = LINKS[short_code]
    target_url = link_data["target"]
    fallback_url = link_data["fallback"]

    try:
      response = requests.get(target_url, timeout=3)
      if response.status_code < 400:
        return redirect(target_url)
      else:
        return redirect(fallback_url)
    except:
      return redirect(fallback_url)
  else:
    return "Ссылка не найдена!", 404


# Красивая посадочная страница
@app.route("/")
def home():
  return """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Smart-Redirect — Защита и мониторинг ссылок</title>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
        <style>
            :root {
                --bg-color: #0F172A;
                --card-bg: #1E293B;
                --accent: #10B981;
                --text-main: #F8FAFC;
                --text-muted: #94A3B8;
                --border: #334155;
            }
            * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Inter', sans-serif; }
            body { background-color: var(--bg-color); color: var(--text-main); padding: 16px; display: flex; justify-content: center; min-height: 100vh; }
            .container { width: 100%; max-width: 480px; display: flex; flex-direction: column; gap: 24px; }
            header { text-align: center; padding: 24px 0 8px 0; }
            h1 { font-size: 28px; font-weight: 700; margin-bottom: 8px; letter-spacing: -0.5px; }
            p.subtitle { font-size: 16px; color: var(--text-muted); line-height: 1.5; }
            .card { background-color: var(--card-bg); border-radius: 16px; padding: 24px; box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.3); border: 1px solid var(--border); display: flex; flex-direction: column; gap: 16px; }
            .status-badge { display: inline-flex; align-items: center; gap: 8px; background: rgba(16, 185, 129, 0.1); color: var(--accent); padding: 6px 12px; border-radius: 20px; font-size: 13px; font-weight: 600; align-self: flex-start; }
            .status-dot { width: 8px; height: 8px; background-color: var(--accent); border-radius: 50%; box-shadow: 0 0 8px var(--accent); }
            .link-row { display: flex; justify-content: space-between; align-items: center; padding: 12px 0; border-bottom: 1px solid var(--border); }
            .link-row:last-child { border-bottom: none; padding-bottom: 0; }
            .link-info { display: flex; flex-direction: column; gap: 4px; }
            .link-title { font-size: 16px; font-weight: 600; }
            .link-url { font-size: 13px; color: var(--text-muted); }
            .btn { display: flex; align-items: center; justify-content: center; width: 100%; height: 48px; background-color: var(--accent); color: #0F172A; font-size: 16px; font-weight: 600; border-radius: 16px; border: none; cursor: pointer; text-decoration: none; transition: opacity 0.2s ease; box-shadow: 0 4px 12px rgba(16, 185, 129, 0.2); }
            .btn:hover { opacity: 0.9; }
            footer { text-align: center; font-size: 13px; color: var(--text-muted); padding: 16px 0; }
        </style>
    </head>
    <body>
        <div class="container">
            <header>
                <h1>Smart-Redirect</h1>
                <p class="subtitle">Автоматический мониторинг здоровья ссылок и защита трафика.</p>
            </header>
            <div class="card">
                <div class="status-badge">
                    <div class="status-dot"></div>
                    Система активна
                </div>
                <div class="link-row">
                    <div class="link-info">
                        <span class="link-title">/offer1</span>
                        <span class="link-url">Telegram канал (с резервом)</span>
                    </div>
                </div>
                <div class="link-row">
                    <div class="link-info">
                        <span class="link-title">/sale</span>
                        <span class="link-url">Акционная страница</span>
                    </div>
                </div>
                <a href="/offer1" class="btn">Проверить редирект /offer1</a>
            </div>
            <footer>
                Мобильный микро-SaaS проект
            </footer>
        </div>
    </body>
    </html>
    """


if __name__ == "__main__":
  app.run(host="0.0.0.0", port=5000, debug=True)
