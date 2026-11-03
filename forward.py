import datetime
import email
import imaplib
import os
from email.header import decode_header, make_header

import requests

FILTER_FROM = os.environ["FILTER_FROM"]  # domain or address, e.g. site.com
TG_TOKEN = os.environ["TG_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]

# Only look at mail from the last two days, so the first run does not dump old unread mail.
since = (datetime.date.today() - datetime.timedelta(days=2)).strftime("%d-%b-%Y")


def decode(value):
    return str(make_header(decode_header(value or "")))


def get_text(msg):
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True)
            if payload:
                return payload.decode(part.get_content_charset() or "utf-8", "replace")
    return ""


def send_telegram(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT_ID, "text": text[:4000]},
        timeout=15,
    )
    r.raise_for_status()


def main():
    imap = imaplib.IMAP4_SSL("imap.ukr.net", 993)
    imap.login(os.environ["UKR_USER"], os.environ["UKR_APP_PASSWORD"])
    imap.select("INBOX")

    status, data = imap.search(None, "UNSEEN", "FROM", f'"{FILTER_FROM}"', "SINCE", since)
    if status != "OK":
        raise RuntimeError(f"IMAP search failed: {status} {data}")

    # Diagnostics: counts only. Never print senders or subjects, the log is public.
    def count(*criteria):
        st, d = imap.search(None, *criteria)
        return len(d[0].split()) if st == "OK" and d and d[0] else -1

    print(f"filter length={len(FILTER_FROM)} since={since}")
    print(f"INBOX total={count('ALL')} unseen={count('UNSEEN')}")
    print(f"from-match any state={count('FROM', chr(34) + FILTER_FROM + chr(34))}")
    print(f"from-match unseen={count('UNSEEN', 'FROM', chr(34) + FILTER_FROM + chr(34))}")
    print(f"to forward (unseen + from + since)={len(data[0].split()) if data and data[0] else 0}")

    for num in data[0].split():
        _, md = imap.fetch(num, "(BODY.PEEK[])")
        msg = email.message_from_bytes(md[0][1])
        text = f"📩 {decode(msg.get('From'))}\n{decode(msg.get('Subject'))}\n\n{get_text(msg)[:500]}"
        send_telegram(text)
        # Mark as read only after a successful send, so a failed send is retried on the next run.
        imap.store(num, "+FLAGS", "\\Seen")

    imap.logout()


if __name__ == "__main__":
    main()
