import datetime
import email
import html
import imaplib
import os
import re
from email.header import decode_header, make_header
from html.parser import HTMLParser

import requests

# Form field name in the email -> label shown in Telegram.
FIELDS = {
    "name": "👤 Ім'я",
    "phone": "📞 Телефон",
    "type": "📍 Тип",
    "date": "📅 Дата",
    "timeslot": "🕐 Час",
    "message": "💬 Повідомлення",
}
# Shown in monospace: one tap on it in Telegram copies the value.
COPYABLE = {"phone"}

MAX_VALUE_LEN = 1000


def decode(value):
    return str(make_header(decode_header(value or "")))


class _HtmlToText(HTMLParser):
    BLOCK_END = {"p", "div", "tr", "td", "th", "li", "h1", "h2", "h3"}

    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.BLOCK_END:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def get_text(msg):
    """Prefer text/plain, fall back to text/html converted to plain text."""
    html_body = ""
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        if ctype == "text/plain":
            return text
        html_body = html_body or text
    if html_body:
        parser = _HtmlToText()
        parser.feed(html_body)
        return "".join(parser.parts)
    return ""


def parse_submission(text):
    """Parse a Formspree notification. Returns (site, values, footer) or None."""
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]

    site = None
    m = re.search(r"submitted (?:a|your) form on\s+\[?(?:https?://)?([\w.-]+)", text)
    if m:
        site = m.group(1).rstrip(".")

    values = {}
    footer = None
    key = None
    for line in lines:
        # Field names come as "name" in HTML mails and as "name:" in the plain-text part.
        field = line.rstrip(":").strip().lower()
        if field in FIELDS:
            key = field
            values.setdefault(key, [])
        elif line.startswith("Submitted "):
            footer = line[len("Submitted "):]
            key = None  # everything after this is Formspree boilerplate
        elif key:
            # Strip markdown links like [0663540849](tel:+380663540849).
            values[key].append(re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line))

    if not values:
        return None
    return site, {k: "\n".join(v) for k, v in values.items()}, footer


def format_submission(site, values, footer):
    esc = html.escape
    out = ["📝 <b>Новий запис з сайту</b>"]
    if site:
        out.append(f"<i>{esc(site)}</i>")
    out.append("")
    for key, label in FIELDS.items():
        if key not in values:
            continue
        value = values[key][:MAX_VALUE_LEN] or "—"
        rendered = esc(value)
        if key in COPYABLE:
            rendered = f"<code>{rendered}</code>"
        out.append(f"<b>{label}:</b> {rendered}")
    if footer:
        out += ["", f"🕒 {esc(footer)}"]
    return "\n".join(out)


def copy_button(phone):
    """Inline keyboard with a button that copies the phone number on tap."""
    number = re.sub(r"[^\d+]", "", phone or "")
    if not number:
        return None
    return {"inline_keyboard": [[{"text": f"📋 Скопіювати {number}", "copy_text": {"text": number}}]]}


def send_telegram(token, chat_id, text, html_mode, reply_markup=None):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text[:4000]}
    if html_mode:
        payload["parse_mode"] = "HTML"
    if reply_markup:
        payload["reply_markup"] = reply_markup

    r = requests.post(url, json=payload, timeout=15)
    if r.status_code == 400 and reply_markup:
        # The button is a nice-to-have: if Telegram rejects it, still deliver the message.
        print(f"Telegram rejected the button: {r.text[:200]}")
        payload.pop("reply_markup")
        r = requests.post(url, json=payload, timeout=15)
    r.raise_for_status()


def main():
    filter_from = os.environ["FILTER_FROM"]  # domain or address, e.g. formspree.io
    tg_token = os.environ["TG_TOKEN"]
    tg_chat_id = os.environ["TG_CHAT_ID"]

    # Only look at mail from the last two days, so the first run does not dump old unread mail.
    since = (datetime.date.today() - datetime.timedelta(days=2)).strftime("%d-%b-%Y")

    imap = imaplib.IMAP4_SSL("imap.ukr.net", 993)
    imap.login(os.environ["UKR_USER"], os.environ["UKR_APP_PASSWORD"])
    imap.select("INBOX")

    quoted = '"' + filter_from + '"'
    status, data = imap.search(None, "UNSEEN", "FROM", quoted, "SINCE", since)
    if status != "OK":
        raise RuntimeError(f"IMAP search failed: {status} {data}")

    # Diagnostics: counts only. Never print senders or subjects, the log is public.
    def count(*criteria):
        st, d = imap.search(None, *criteria)
        return len(d[0].split()) if st == "OK" and d and d[0] else -1

    ids = data[0].split() if data and data[0] else []
    print(f"filter length={len(filter_from)} since={since}")
    print(f"INBOX total={count('ALL')} unseen={count('UNSEEN')}")
    print(f"from-match any state={count('FROM', quoted)}")
    print(f"from-match unseen={count('UNSEEN', 'FROM', quoted)}")
    print(f"to forward (unseen + from + since)={len(ids)}")

    for num in ids:
        _, md = imap.fetch(num, "(BODY.PEEK[])")
        msg = email.message_from_bytes(md[0][1])
        body = get_text(msg)

        parsed = parse_submission(body)
        if parsed:
            _, values, _ = parsed
            send_telegram(
                tg_token,
                tg_chat_id,
                format_submission(*parsed),
                html_mode=True,
                reply_markup=copy_button(values.get("phone")),
            )
        else:
            # Not a recognised form notification: send it as plain text.
            raw = f"📩 {decode(msg.get('From'))}\n{decode(msg.get('Subject'))}\n\n{body[:1500]}"
            send_telegram(tg_token, tg_chat_id, raw, html_mode=False)

        # Mark as read only after a successful send, so a failed send is retried on the next run.
        imap.store(num, "+FLAGS", "\\Seen")

    imap.logout()


if __name__ == "__main__":
    main()
