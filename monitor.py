"""Watches one or more web pages and sends a Telegram message when the visible text changes.

Configuration (environment variables):
  TARGET_URL          one or more URLs, separated by spaces or new lines   (required)
  TELEGRAM_BOT_TOKEN  token from @BotFather                                (required for alerts)
  TELEGRAM_CHAT_ID    your chat id                                         (required for alerts)
  RENDER_URL          optional: URLs of JavaScript-built pages, loaded in headless Chrome
  RENDER_SELECTOR     optional: part of those pages to watch (default "main")
  CSS_SELECTOR        optional: only watch this part of the page, e.g. "main" or "#sessions"
  INTERVAL            seconds between checks (default 45)
  RUN_FOR             seconds to keep looping before exiting (default 270)
"""
import difflib
import hashlib
import os
import pathlib
import sys
import time

import requests
from bs4 import BeautifulSoup

URLS = os.environ.get("TARGET_URL", "").split()
RENDER_URLS = os.environ.get("RENDER_URL", "").split()   # pages built by JavaScript
SELECTOR = os.environ.get("CSS_SELECTOR", "").strip()
RENDER_SELECTOR = os.environ.get("RENDER_SELECTOR", "main").strip()
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
INTERVAL = int(os.environ.get("INTERVAL") or 45)
RUN_FOR = int(os.environ.get("RUN_FOR") or 270)
STATE_DIR = pathlib.Path("state")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Cache-Control": "no-cache",
}


def notify(text):
    print(text, flush=True)
    if not (TOKEN and CHAT_ID):
        print("(Telegram not configured, message only printed)", flush=True)
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text[:4000], "disable_web_page_preview": "true"},
            timeout=20,
        )
        r.raise_for_status()
    except Exception as e:  # never let a failed alert kill the monitor
        print(f"Telegram send failed: {e}", flush=True)


_browser = None


def render_html(url):
    """Load a JavaScript-built page in headless Chrome and return its HTML once content has appeared."""
    global _browser
    if _browser is None:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        try:
            _browser = pw.chromium.launch(channel="chrome")   # Chrome already installed (GitHub runners, most Macs)
        except Exception:
            _browser = pw.chromium.launch()                   # needs: playwright install chromium
    page = _browser.new_page(user_agent=HEADERS["User-Agent"])
    try:
        page.goto(url, wait_until="networkidle", timeout=45000)
        # wait until the main area actually shows text, so a loading spinner is never mistaken for a change
        page.wait_for_function(
            "() => { const m = document.querySelector('main') || document.body;"
            " return m && m.innerText.trim().length > 20; }",
            timeout=20000,
        )
        page.wait_for_timeout(1500)
        return page.content()
    finally:
        page.close()


def fetch_text(url, selector=SELECTOR, render=False):
    """Return the visible text of the page (or of the selector), one item per line."""
    if render:
        html = render_html(url)
    else:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        html = r.text
    soup = BeautifulSoup(html, "html.parser")
    SELECTOR = selector
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    if SELECTOR:
        nodes = soup.select(SELECTOR)
        if not nodes:
            raise ValueError(f"CSS_SELECTOR '{SELECTOR}' matched nothing")
        raw = "\n".join(n.get_text("\n") for n in nodes)
    else:
        raw = soup.get_text("\n")
    lines = [" ".join(line.split()) for line in raw.splitlines()]
    return "\n".join(line for line in lines if line)


def state_file(url):
    return STATE_DIR / (hashlib.sha1(url.encode()).hexdigest()[:12] + ".txt")


def summarize(old, new):
    added, removed = [], []
    for line in difflib.ndiff(old.splitlines(), new.splitlines()):
        if line.startswith("+ "):
            added.append(line[2:])
        elif line.startswith("- "):
            removed.append(line[2:])
    parts = []
    if added:
        parts.append("NEW:\n" + "\n".join(added[:25]))
    if removed:
        parts.append("REMOVED:\n" + "\n".join(removed[:15]))
    return "\n\n".join(parts)


def check(url, render=False):
    new = fetch_text(url, RENDER_SELECTOR if render else SELECTOR, render)
    path = state_file(url)
    if not path.exists():
        path.write_text(new, encoding="utf-8")
        notify(f"Monitoring started for {url}\n({len(new.splitlines())} lines of text recorded)")
        return
    old = path.read_text(encoding="utf-8")
    if new != old:
        path.write_text(new, encoding="utf-8")
        notify(f"PAGE CHANGED - check now!\n{url}\n\n{summarize(old, new)}")


def main():
    if not (URLS or RENDER_URLS):
        sys.exit("TARGET_URL / RENDER_URL is not set")
    STATE_DIR.mkdir(exist_ok=True)
    deadline = time.time() + RUN_FOR
    targets = [(u, False) for u in URLS] + [(u, True) for u in RENDER_URLS]
    while True:
        for url, render in targets:
            try:
                check(url, render)
                print(f"{time.strftime('%H:%M:%S')} ok {url}", flush=True)
            except Exception as e:  # site hiccups should not raise false alarms
                print(f"{time.strftime('%H:%M:%S')} error {url}: {e}", flush=True)
        if time.time() + INTERVAL > deadline:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
