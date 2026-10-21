"""
Мониторинг афиши Белорусского театра кукол (puppet-minsk.by) -> Telegram.

Что делает:
  1. Берёт афишу, собирает страницы спектаклей.
  2. На каждой странице спектакля находит сеансы (дата + ссылка на кассу tce.by).
  3. Шлёт в Telegram, когда:
       - появился НОВЫЙ сеанс (новая дата в продаже);
       - (опционально, CHECK_SEATS=1) сеанс снова стал доступен после «распродано».

Установка:
  pip install requests beautifulsoup4
  # только для CHECK_SEATS=1:
  pip install playwright && playwright install chromium

Переменные окружения:
  TG_TOKEN     токен бота от @BotFather
  TG_CHAT_ID   твой chat id (напиши боту /start, затем открой
               https://api.telegram.org/bot<TOKEN>/getUpdates)
  WATCH        необязательно: названия через запятую, напр. "Киви,Бабочки"
               (пусто = все спектакли)
  CHECK_SEATS  1 = заходить на tce.by через браузер и проверять наличие мест
"""
import json
import os
import re
import sys
import time
import html
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

AFISHA = "https://puppet-minsk.by/afisha"
STATE = Path(__file__).with_name("state.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
WATCH = [w.strip().lower() for w in os.environ.get("WATCH", "").split(",") if w.strip()]
CHECK_SEATS = os.environ.get("CHECK_SEATS") == "1"
HEARTBEAT_DAYS = 3  # раз в сколько дней слать «бот жив»

# Строка вида «22 Октября, Четверг, 19:00»
DATE_RE = re.compile(r"\d{1,2}\s+[А-Яа-яЁё]+,\s*[А-Яа-яЁё]+,\s*\d{1,2}:\d{2}")
# Маркеры «билетов нет» на странице кассы — ПОДСТРОЙ после первого запуска
SOLD_OUT = ("нет в продаже", "билетов нет", "проданы", "распродано", "sold out")


def tg(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        print("[TG не настроен]\n" + text)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=20,
    )
    r.raise_for_status()


def get(url: str) -> BeautifulSoup:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def show_pages() -> list[str]:
    soup = get(AFISHA)
    urls = set()
    for a in soup.find_all("a", href=True):
        if "/item/" in a["href"]:
            urls.add(urljoin(AFISHA, a["href"].split("#")[0]))
    return sorted(urls)


def sessions(show_url: str) -> tuple[str, list[dict]]:
    """Возвращает (название, [сеансы]) со страницы спектакля."""
    soup = get(show_url)
    title = (soup.title.string or "").strip() if soup.title else show_url
    out = []
    for a in soup.find_all("a", href=True):
        if "tce.by/shows" not in a["href"]:
            continue
        # дата — ближайший предшествующий текст нужного формата
        node = a.find_previous(string=DATE_RE)
        when = DATE_RE.search(str(node)).group(0) if node else "?"
        sid = parse_qs(urlparse(a["href"]).query).get("data", [a["href"]])[0]
        out.append({"id": sid, "when": when, "url": a["href"]})
    return title, out


def seats_available(pw_page, url: str) -> tuple[bool, str]:
    """Открывает кассу через браузер (там Anubis с proof-of-work)."""
    pw_page.goto(url, wait_until="domcontentloaded", timeout=60000)
    pw_page.wait_for_function(
        "!document.title.toLowerCase().includes('not a bot')", timeout=60000)
    pw_page.wait_for_load_state("networkidle", timeout=60000)
    text = pw_page.inner_text("body").strip()
    low = text.lower()
    if any(m in low for m in SOLD_OUT):
        return False, text[:300]
    return True, text[:300]


def main() -> None:
    state = json.loads(STATE.read_text("utf-8")) if STATE.exists() else None
    first_run = state is None
    state = state or {"seen": {}, "avail": {}}

    pages = show_pages()
    found = {}  # id -> данные сеанса
    for p in pages:
        try:
            title, ss = sessions(p)
        except Exception as e:  # noqa: BLE001
            print("skip", p, e, file=sys.stderr)
            continue
        if WATCH and not any(w in title.lower() for w in WATCH):
            continue
        for s in ss:
            s.update(title=title, page=p)
            found[s["id"]] = s

    new = [s for i, s in found.items() if i not in state["seen"]]

    # --- проверка мест (опционально) ---
    became_avail = []
    if CHECK_SEATS:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            for i, s in found.items():
                try:
                    ok, snippet = seats_available(page, s["url"])
                except Exception as e:  # noqa: BLE001
                    print("seats err", i, e, file=sys.stderr)
                    continue
                s["snippet"] = snippet
                was = state["avail"].get(i)
                state["avail"][i] = ok
                if ok and was is False:  # было «нет», стало «есть»
                    became_avail.append(s)
            browser.close()

    def fmt(s: dict, head: str) -> str:
        return (f"{head}\n<b>{html.escape(s['title'])}</b>\n{html.escape(s['when'])}\n"
                f"<a href=\"{html.escape(s['url'])}\">Купить</a>")

    if first_run:
        print(f"Первый запуск: запомнил {len(found)} сеансов, уведомления не шлю.")
        tg(f"Бот запущен. Отслеживаю сеансов: {len(found)}.")
    else:
        for s in new:
            tg(fmt(s, "🎟 Новый сеанс в продаже"))
        for s in became_avail:
            tg(fmt(s, "✅ Появились билеты"))

    now = time.time()
    last = state.get("heartbeat")
    if last is None:
        state["heartbeat"] = now
    elif now - last > HEARTBEAT_DAYS * 86400:
        tg(f"💚 Бот работает. Отслеживаю сеансов: {len(found)}.")
        state["heartbeat"] = now

    for i, s in found.items():
        state["seen"][i] = {"title": s["title"], "when": s["when"]}
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")


if __name__ == "__main__":
    main()
