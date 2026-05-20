"""
Restaurant reservation reminder + archiver.

Runs hourly (cron `13 * * * *` UTC). ALL times are America/New_York and
DST-aware: an "8:00 AM" opening stays 8:00 AM local in either EDT or EST.

Email grouping:
  - Heads-up: ONE digest per run listing every restaurant whose heads-up is due.
  - Act-now : ONE email per distinct scheduled act-now time. Restaurants that
              open at the exact same time share an email; everything else is
              sent separately.

Auth and the GitHub Actions workflow are unchanged (OAuth token via env vars).

Reservations tab (row 1 = header):
  A Restaurant  B Reservation Date  C Booking Opening  D Booking Opening Hour
  E Lead Days (formula)  F Booking URL  G Party Size  H Occasion  I Notes
  J Sent_HeadsUp  K Sent_ActNow   <- script-managed; leave blank
Restaurant DB tab: A Restaurant  B Lead Days  C Opening Time  D Booking URL
"""

import os
import json
import base64
import logging
from datetime import datetime, timedelta, time, date
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")
SHEETS_EPOCH = datetime(1899, 12, 30)  # Google Sheets serial-date origin

SCOPES = [
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/spreadsheets',
]
SHEET_ID = '1W0Zcxzml7FXTGT-CG8Fhi0vwwZnlCNVmkb8e61Kga6E'
RES_TAB = 'Reservations'
DB_TAB = 'Restaurant DB'
RES_RANGE = f'{RES_TAB}!A2:K'   # data only; row 1 is the header
DB_RANGE = f'{DB_TAB}!A:D'
YOUR_EMAIL = os.environ['GMAIL_ADDRESS']
TOKEN_JSON = os.environ['GMAIL_TOKEN_JSON']

# 0-based column indices within A:K
(C_RESTAURANT, C_RES_DATE, C_OPEN_DATE, C_OPEN_HOUR, C_LEAD,
 C_URL, C_PARTY, C_OCCASION, C_NOTES, C_SENT_HEADSUP, C_SENT_ACTNOW) = range(11)


# --------------------------------------------------------------------------- #
# Auth (unchanged)
# --------------------------------------------------------------------------- #
def get_services():
    creds = Credentials.from_authorized_user_info(json.loads(TOKEN_JSON), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    gmail = build('gmail', 'v1', credentials=creds)
    sheets = build('sheets', 'v4', credentials=creds)
    return gmail, sheets


# --------------------------------------------------------------------------- #
# Value parsing — read raw serials so the display format never matters
# --------------------------------------------------------------------------- #
def parse_date(v):
    if v in (None, ''):
        return None
    if isinstance(v, (int, float)):
        return (SHEETS_EPOCH + timedelta(days=float(v))).date()
    for fmt in ('%m-%d-%Y', '%Y-%m-%d', '%m/%d/%Y'):
        try:
            return datetime.strptime(str(v).strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognized date: {v!r}")


def parse_time(v):
    if v in (None, ''):
        return None
    if isinstance(v, (int, float)):
        secs = round((float(v) % 1) * 86400)
        return time((secs // 3600) % 24, (secs % 3600) // 60)
    for fmt in ('%I:%M %p', '%I:%M:%S %p', '%H:%M', '%H:%M:%S'):
        try:
            return datetime.strptime(str(v).strip(), fmt).time()
        except ValueError:
            continue
    raise ValueError(f"unrecognized time: {v!r}")


def at_ny(d: date, hour: int, minute: int) -> datetime:
    return datetime.combine(d, time(hour, minute), tzinfo=NY)


# --------------------------------------------------------------------------- #
# Reminder schedule
# --------------------------------------------------------------------------- #
def compute_reminders(open_dt: datetime):
    """
    act_now : ALWAYS open_dt - 2h.
    heads_up: opens 8:00 PM-11:59 PM -> 9:00 AM same day;
              opens 12:00 AM-7:59 PM -> 6:00 PM day before.
    """
    act_now = open_dt - timedelta(hours=2)
    if open_dt.hour >= 20:
        heads_up = at_ny(open_dt.date(), 9, 0)
    else:
        heads_up = at_ny(open_dt.date() - timedelta(days=1), 18, 0)
    return heads_up, act_now


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
def send_email(gmail, subject, body):
    msg = MIMEMultipart('alternative')
    msg['Subject'], msg['From'], msg['To'] = subject, YOUR_EMAIL, YOUR_EMAIL
    msg.attach(MIMEText(body, 'html'))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail.users().messages().send(userId='me', body={'raw': raw}).execute()
    logger.info("Email sent.")


def _restaurant_block(row, open_dt):
    """One restaurant's HTML, reused by both the digest and act-now emails."""
    res_date = parse_date(row[C_RES_DATE])
    res_date_str = res_date.strftime('%m-%d-%Y') if res_date else '?'
    occasion = row[C_OCCASION]
    notes = row[C_NOTES]
    occasion_html = f"<p style='margin:2px 0;'><b>Occasion:</b> {occasion}</p>" if occasion else ""
    notes_html = f"<p style='margin:2px 0;'><b>Notes:</b> {notes}</p>" if notes else ""
    return f"""
      <div style="margin-bottom:18px;">
        <h3 style="margin:0 0 6px;">🍽️ {row[C_RESTAURANT]}</h3>
        <p style="margin:2px 0;"><b>Booking opens:</b> {open_dt.strftime('%m-%d-%Y at %I:%M %p')} (NY)</p>
        <p style="margin:2px 0;"><b>Reservation date:</b> {res_date_str}</p>
        <p style="margin:2px 0;"><b>Party size:</b> {row[C_PARTY] or '?'}</p>
        {occasion_html}{notes_html}
        <a href="{row[C_URL]}" style="display:inline-block;margin-top:8px;background:#000;
           color:#fff;padding:10px 20px;text-decoration:none;border-radius:6px;font-size:14px;">
           Book Now &rarr; {row[C_RESTAURANT]}</a>
      </div>"""


def _wrap(intro, blocks_html):
    return f"""
    <html><body style="font-family:Arial,sans-serif;max-width:600px;">
      <p style="font-size:16px;">{intro}</p><hr/>
      {blocks_html}
      <p style="color:#999;font-size:12px;">Auto-reminder from your reservation tracker.</p>
    </body></html>"""


def send_headsup_digest(gmail, items):
    """items: list of {'row','open_dt',...}. One email listing all of them."""
    blocks = "".join(_restaurant_block(it['row'], it['open_dt']) for it in items)
    body = _wrap("📅 Heads-up — upcoming booking windows:", blocks)
    if len(items) == 1:
        name = items[0]['row'][C_RESTAURANT]
        t = items[0]['open_dt'].strftime('%m-%d-%Y %I:%M %p')
        subject = f"📅 Upcoming: book {name} — opens {t}"
    else:
        subject = f"📅 {len(items)} upcoming booking windows"
    send_email(gmail, subject, body)


def send_actnow(gmail, items):
    """items: list sharing the SAME scheduled act-now time (usually one)."""
    blocks = "".join(_restaurant_block(it['row'], it['open_dt']) for it in items)
    body = _wrap("🚨 Booking opens in ~2 hours — be ready to book!", blocks)
    names = [it['row'][C_RESTAURANT] for it in items]
    open_time = items[0]['open_dt'].strftime('%I:%M %p')
    if len(items) == 1:
        subject = f"🚨 ~2 hrs: book {names[0]} at {open_time}"
    elif len(items) == 2:
        subject = f"🚨 ~2 hrs: book {names[0]} & {names[1]} at {open_time}"
    else:
        subject = f"🚨 ~2 hrs: book {len(items)} spots at {open_time}"
    send_email(gmail, subject, body)


# --------------------------------------------------------------------------- #
# Sheet I/O
# --------------------------------------------------------------------------- #
def get_sheet_id(sheets, title):
    meta = sheets.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    for s in meta['sheets']:
        if s['properties']['title'] == title:
            return s['properties']['sheetId']
    raise ValueError(f"tab not found: {title}")


def read_reservations(sheets):
    res = sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=RES_RANGE,
        valueRenderOption='UNFORMATTED_VALUE',
        dateTimeRenderOption='SERIAL_NUMBER',
    ).execute()
    return [r + [''] * (11 - len(r)) for r in res.get('values', [])]


def write_sent_flags(sheets, updates):
    """updates: list of (sheet_row, headsup_value, actnow_value)."""
    if not updates:
        return
    data = [{'range': f'{RES_TAB}!J{r}:K{r}', 'values': [[hu, an]]}
            for r, hu, an in updates]
    sheets.spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={'valueInputOption': 'RAW', 'data': data},
    ).execute()
    logger.info(f"Marked {len(updates)} row(s) as reminded.")


def delete_rows(sheets, sheet_id, sheet_rows):
    if not sheet_rows:
        return
    reqs = [{'deleteDimension': {'range': {
                'sheetId': sheet_id, 'dimension': 'ROWS',
                'startIndex': r - 1, 'endIndex': r}}}
            for r in sorted(sheet_rows, reverse=True)]
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID, body={'requests': reqs}).execute()
    logger.info(f"Deleted {len(reqs)} expired row(s).")


def upsert_db(sheets, name, lead_days, opening_time, url):
    rows = sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=DB_RANGE).execute().get('values', [])
    new_row = [name, lead_days, opening_time, url]
    for i, row in enumerate(rows[1:], start=2):
        if row and row[0].strip().lower() == name.strip().lower():
            sheets.spreadsheets().values().update(
                spreadsheetId=SHEET_ID, range=f'{DB_TAB}!A{i}:D{i}',
                valueInputOption='RAW', body={'values': [new_row]}).execute()
            logger.info("DB row updated.")
            return
    sheets.spreadsheets().values().append(
        spreadsheetId=SHEET_ID, range=DB_RANGE,
        valueInputOption='RAW', body={'values': [new_row]}).execute()
    logger.info("DB row added.")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def run():
    gmail, sheets = get_services()
    now = datetime.now(NY)
    rows = read_reservations(sheets)
    res_sheet_id = get_sheet_id(sheets, RES_TAB)

    due_headsup = []      # [{'row','open_dt','sheet_row'}, ...]
    due_actnow = {}       # act_now_dt -> [{'row','open_dt','sheet_row'}, ...]
    to_delete, to_archive = [], []

    # Pass 1: classify every row.
    for i, row in enumerate(rows):
        sheet_row = i + 2
        name = str(row[C_RESTAURANT]).strip()
        if not name:
            continue
        try:
            open_date = parse_date(row[C_OPEN_DATE])
            open_time = parse_time(row[C_OPEN_HOUR])
            if open_date is None or open_time is None:
                raise ValueError("missing open date or hour")
            open_dt = datetime.combine(open_date, open_time, tzinfo=NY)
        except (ValueError, KeyError) as e:
            logger.warning(f"Skipping row {sheet_row}: {e}")
            continue

        if now > open_dt:                       # moot -> archive + delete
            to_archive.append((name, row[C_LEAD], open_time.strftime('%I:%M %p'), row[C_URL]))
            to_delete.append(sheet_row)
            logger.info(f"Moot, archiving row {sheet_row}.")
            continue

        heads_up_dt, act_now_dt = compute_reminders(open_dt)
        rec = {'row': row, 'open_dt': open_dt, 'sheet_row': sheet_row}
        if not str(row[C_SENT_HEADSUP]).strip() and now >= heads_up_dt:
            due_headsup.append(rec)
        if not str(row[C_SENT_ACTNOW]).strip() and now >= act_now_dt:
            due_actnow.setdefault(act_now_dt, []).append(rec)

    # Pass 2: send. Only stamp a row's flag if its email actually went out.
    stamp = now.strftime('%m-%d-%Y %H:%M')
    sent_hu_rows, sent_an_rows = set(), set()

    if due_headsup:                              # one digest for all heads-ups
        try:
            send_headsup_digest(gmail, due_headsup)
            sent_hu_rows = {it['sheet_row'] for it in due_headsup}
        except Exception as e:
            logger.error(f"Heads-up digest failed, will retry next run: {e}")

    for act_time, items in sorted(due_actnow.items()):   # one email per distinct time
        try:
            send_actnow(gmail, items)
            sent_an_rows |= {it['sheet_row'] for it in items}
        except Exception as e:
            logger.error(f"Act-now email failed for {act_time}, will retry: {e}")

    # Pass 3: write flags (preserving the column we didn't just set), then clean up.
    existing = {i + 2: (rows[i][C_SENT_HEADSUP], rows[i][C_SENT_ACTNOW])
                for i in range(len(rows))}
    updates = []
    for r in (sent_hu_rows | sent_an_rows):
        hu, an = existing.get(r, ('', ''))
        if r in sent_hu_rows:
            hu = stamp
        if r in sent_an_rows:
            an = stamp
        updates.append((r, hu, an))
    write_sent_flags(sheets, updates)

    for name, lead, opening_time, url in to_archive:
        upsert_db(sheets, name, lead, opening_time, url)
    delete_rows(sheets, res_sheet_id, to_delete)
    logger.info("Run complete.")


if __name__ == '__main__':
    run()
