"""
Restaurant reservation reminder + archiver.

Runs on a cron every 30 min (recommended: `13,43 * * * *` in UTC).
ALL times are America/New_York and DST-aware: an "8:00 AM" opening stays
8:00 AM local whether it's EDT or EST. The cron stays fixed in UTC; this
script does the timezone math, so the workflow never needs editing.

Auth and the GitHub Actions workflow are UNCHANGED from the original:
OAuth token + Gmail address still come from env vars.

Reservations tab layout (row 1 = header):
  A Restaurant  B Reservation Date  C Booking Opening (date)  D Booking Opening Hour
  E Lead Days (sheet formula =IF(AND($B2<>"",$C2<>""),$B2-$C2,""))
  F Booking URL  G Party Size  H Occasion  I Notes
  J Sent_HeadsUp  K Sent_ActNow   <- script-managed; leave blank (you can hide them)

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
# Auth (unchanged from the original)
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
    """Sheets serial number -> date; falls back to common text formats."""
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
    """Sheets time serial (fraction of a day) -> time; falls back to text."""
    if v in (None, ''):
        return None
    if isinstance(v, (int, float)):
        secs = round((float(v) % 1) * 86400)          # fractional part = wall-clock
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
    Given the booking-open datetime (NY), return (heads_up_dt, act_now_dt).

    act_now : ALWAYS open_dt - 2h  -> reproduces every edge case automatically
              (e.g. 12:30 AM open -> 10:30 PM the day before).
    heads_up: opens 8:00 PM-11:59 PM  -> 9:00 AM the same day   (Case 1)
              opens 12:00 AM-7:59 PM  -> 6:00 PM the day before (Cases 2 & 3)
    """
    act_now = open_dt - timedelta(hours=2)
    if open_dt.hour >= 20:                                  # 8:00 PM - 11:59 PM
        heads_up = at_ny(open_dt.date(), 9, 0)
    else:                                                   # 12:00 AM - 7:59 PM
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
    logger.info(f"Email sent: {subject}")


def email_body(row, open_dt, kind):
    res_date = parse_date(row[C_RES_DATE])
    res_date_str = res_date.strftime('%m-%d-%Y') if res_date else '?'
    occasion = row[C_OCCASION]
    notes = row[C_NOTES]
    intro = ("🚨 Booking opens in ~2 hours — be ready to book!" if kind == 'actnow'
             else "📅 Heads-up: an upcoming booking window.")
    occasion_html = f"<p><b>Occasion:</b> {occasion}</p>" if occasion else ""
    notes_html = f"<p><b>Notes:</b> {notes}</p>" if notes else ""
    return f"""
    <html><body style="font-family:Arial,sans-serif;max-width:600px;">
      <h2>🍽️ {row[C_RESTAURANT]}</h2>
      <p style="font-size:16px;">{intro}</p><hr/>
      <p><b>Reservation date:</b> {res_date_str}</p>
      <p><b>Party size:</b> {row[C_PARTY] or '?'}</p>
      {occasion_html}
      <p><b>Booking opens:</b> {open_dt.strftime('%m-%d-%Y at %I:%M %p')} (NY)</p>
      <p><b>Lead days:</b> {row[C_LEAD]}</p>
      {notes_html}<br/>
      <a href="{row[C_URL]}" style="background:#000;color:#fff;padding:12px 24px;
         text-decoration:none;border-radius:6px;font-size:15px;">
         Book Now → {row[C_RESTAURANT]}</a>
      <br/><br/>
      <p style="color:#999;font-size:12px;">Auto-reminder from your reservation tracker.</p>
    </body></html>"""


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
    # pad every row to 11 columns so trailing blanks (J/K) are always indexable
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
    """sheet_rows: 1-based row numbers. Delete descending so indices don't shift."""
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
    """One row per restaurant: update if the name exists, else append."""
    rows = sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=DB_RANGE).execute().get('values', [])
    new_row = [name, lead_days, opening_time, url]
    for i, row in enumerate(rows[1:], start=2):        # skip header; i = sheet row
        if row and row[0].strip().lower() == name.strip().lower():
            sheets.spreadsheets().values().update(
                spreadsheetId=SHEET_ID, range=f'{DB_TAB}!A{i}:D{i}',
                valueInputOption='RAW', body={'values': [new_row]}).execute()
            logger.info(f"DB updated: {name}")
            return
    sheets.spreadsheets().values().append(
        spreadsheetId=SHEET_ID, range=DB_RANGE,
        valueInputOption='RAW', body={'values': [new_row]}).execute()
    logger.info(f"DB added: {name}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def run():
    gmail, sheets = get_services()
    now = datetime.now(NY)
    rows = read_reservations(sheets)
    res_sheet_id = get_sheet_id(sheets, RES_TAB)

    sent_updates, to_delete, to_archive = [], [], []

    for i, row in enumerate(rows):
        sheet_row = i + 2                       # A2:K -> first data row is sheet row 2
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
            logger.warning(f"Skipping row {sheet_row} ({name}): {e}")
            continue

        # 1) Moot (opening date+hour has passed) -> archive to DB, then delete row.
        if now > open_dt:
            to_archive.append((name, row[C_LEAD], open_time.strftime('%I:%M %p'), row[C_URL]))
            to_delete.append(sheet_row)
            logger.info(f"Moot, archiving: {name}")
            continue

        # 2) Reminders — fire when due and not already sent (dedup via J/K).
        heads_up_dt, act_now_dt = compute_reminders(open_dt)
        new_hu, new_an = row[C_SENT_HEADSUP], row[C_SENT_ACTNOW]
        changed = False

        if not str(row[C_SENT_HEADSUP]).strip() and now >= heads_up_dt:
            send_email(
                gmail,
                f"📅 Upcoming: book {name} — opens {open_dt.strftime('%m-%d-%Y %I:%M %p')}",
                email_body(row, open_dt, 'headsup'))
            new_hu = now.strftime('%m-%d-%Y %H:%M')
            changed = True

        if not str(row[C_SENT_ACTNOW]).strip() and now >= act_now_dt:
            send_email(
                gmail,
                f"🚨 ~2 hrs: book {name} at {open_dt.strftime('%I:%M %p')}",
                email_body(row, open_dt, 'actnow'))
            new_an = now.strftime('%m-%d-%Y %H:%M')
            changed = True

        if changed:
            sent_updates.append((sheet_row, new_hu, new_an))

    # Apply value writes BEFORE deletions so row indices stay valid.
    write_sent_flags(sheets, sent_updates)
    for name, lead, opening_time, url in to_archive:
        upsert_db(sheets, name, lead, opening_time, url)
    delete_rows(sheets, res_sheet_id, to_delete)
    logger.info("Run complete.")


if __name__ == '__main__':
    run()
