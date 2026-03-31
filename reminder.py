import json
import os
import base64
import logging
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

SCOPES = [
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/spreadsheets'
]
SHEET_ID = '1W0Zcxzml7FXTGT-CG8Fhi0vwwZnlCNVmkb8e61Kga6E'
RESERVATIONS_RANGE = 'Reservations!A:G'
DB_RANGE = 'Restaurant DB!A:D'
YOUR_EMAIL = os.environ['GMAIL_ADDRESS']
TOKEN_JSON = os.environ['GMAIL_TOKEN_JSON']


def get_services():
    creds = Credentials.from_authorized_user_info(
        json.loads(TOKEN_JSON), SCOPES
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    gmail = build('gmail', 'v1', credentials=creds)
    sheets = build('sheets', 'v4', credentials=creds)
    return gmail, sheets


def send_email(gmail, subject, body):
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = YOUR_EMAIL
    msg['To'] = YOUR_EMAIL
    msg.attach(MIMEText(body, 'html'))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail.users().messages().send(
        userId='me', body={'raw': raw}
    ).execute()
    logger.info(f"Email sent: {subject}")


def build_email_body(entry, reminder_type):
    try:
        booking_opens = datetime.strptime(entry['Booking Opening'], '%m-%d-%Y %H:%M')
    except ValueError:
        booking_opens = datetime.strptime(entry['Booking Opening'], '%Y-%m-%d %H:%M')
    reservation_date = datetime.strptime(entry['Reservation Date'], '%m-%d-%Y')
    time_str = booking_opens.strftime('%I:%M %p')
    res_date_str = reservation_date.strftime('%A, %B %d, %Y')
    opens_date_str = booking_opens.strftime('%A, %B %d')
    occasion_html = f"<p><b>Occasion:</b> {entry['Occasion']}</p>" if entry.get('Occasion') else ""
    notes_html = f"<p><b>Notes:</b> {entry['Notes']}</p>" if entry.get('Notes') else ""

    return f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 600px;">
        <h2>🍽️ Reservation Reminder: {entry['Restaurant']}</h2>
        <p style="font-size: 16px;">{reminder_type}</p>
        <hr/>
        <p><b>Restaurant:</b> {entry['Restaurant']}</p>
        <p><b>Reservation Date:</b> {res_date_str}</p>
        <p><b>Party Size:</b> {entry.get('Party Size', '?')}</p>
        <p><b>Occasion:</b> {occasion_html}</p>
        <p><b>Booking Opens:</b> {opens_date_str} at <b>{time_str}</b></p>
        {notes_html}
        <br/>
        <a href="{entry['Booking URL']}" 
           style="background:#000;color:#fff;padding:12px 24px;
                  text-decoration:none;border-radius:6px;font-size:15px;">
            Book Now → {entry['Restaurant']}
        </a>
        <br/><br/>
        <p style="color:#999;font-size:12px;">Auto-reminder from your reservation tracker.</p>
    </body></html>
    """


def read_sheet(sheets):
    result = sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=RESERVATIONS_RANGE
    ).execute()
    rows = result.get('values', [])
    if len(rows) <= 1:
        return []  # only header or empty

    headers = rows[0]
    return [dict(zip(headers, row)) for row in rows[1:]]


def delete_rows(sheets, indices):
    """Delete rows by index (0-based, excluding header)"""
    # Sort descending so row deletion doesn't shift indices
    requests = []
    for i in sorted(indices, reverse=True):
        requests.append({
            "deleteDimension": {
                "range": {
                    "sheetId": 0,
                    "dimension": "ROWS",
                    "startIndex": i + 1,  # +1 to account for header
                    "endIndex": i + 2
                }
            }
        })
    if requests:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"requests": requests}
        ).execute()
        logger.info(f"Deleted {len(requests)} expired rows.")

def upsert_restaurant_db(sheets, entry, reservation_date, booking_opens):
    lead_days = (reservation_date - booking_opens.date()).days

    # Read existing DB
    result = sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=DB_RANGE
    ).execute()
    rows = result.get('values', [])

    # Find if restaurant already exists (skip header)
    restaurant_name = entry['Restaurant']
    existing_row = None
    for i, row in enumerate(rows[1:], start=1):
        if row and row[0].lower() == restaurant_name.lower():
            existing_row = i
            break

    opening_time = booking_opens.strftime('%H:%M')
    new_row = [restaurant_name, lead_days, opening_time, entry.get('Booking URL', '')]

    if existing_row:
        # Update existing row
        sheets.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f'Restaurant DB!A{existing_row + 1}:D{existing_row + 1}',
            valueInputOption='RAW',
            body={'values': [new_row]}
        ).execute()
        logger.info(f"Updated Restaurant DB: {restaurant_name}")
    else:
        # Append new row
        sheets.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range=DB_RANGE,
            valueInputOption='RAW',
            body={'values': [new_row]}
        ).execute()
        logger.info(f"Added to Restaurant DB: {restaurant_name}")

def run():
    today = datetime.now().date()
    tomorrow = today + timedelta(days=1)
    gmail, sheets = get_services()

    reservations = read_sheet(sheets)
    rows_to_delete = []

    for i, entry in enumerate(reservations):
        try:
            reservation_date = datetime.strptime(
                entry['Reservation Date'], '%m-%d-%Y'
            ).date()
            try:
                booking_opens = datetime.strptime(entry['Booking Opening'], '%m-%d-%Y %H:%M')
            except ValueError:
                booking_opens = datetime.strptime(entry['Booking Opening'], '%Y-%m-%d %H:%M')
            booking_opens_date = booking_opens.date()
        except (KeyError, ValueError) as e:
            logger.warning(f"Skipping malformed row {i}: {e}")
            continue

       # Always upsert to Restaurant DB
        upsert_restaurant_db(sheets, entry, reservation_date, booking_opens)

        # Auto-remove expired
        if reservation_date < today:
            logger.info(f"Marking for removal: {entry['Restaurant']}")
            rows_to_delete.append(i)
            continue

        # Day-before reminder
        if booking_opens_date == tomorrow:
            send_email(
                gmail,
                subject=f"⏰ Tomorrow: Book {entry['Restaurant']} at {booking_opens.strftime('%I:%M %p')}",
                body=build_email_body(entry, "📅 Booking opens <b>tomorrow</b> — be ready!")
            )

        # Morning-of reminder
        elif booking_opens_date == today:
            send_email(
                gmail,
                subject=f"🚨 TODAY: Book {entry['Restaurant']} at {booking_opens.strftime('%I:%M %p')}",
                body=build_email_body(entry, "🚨 Booking opens <b>today</b> — don't miss it!")
            )

    delete_rows(sheets, rows_to_delete)
    logger.info("Run complete.")


if __name__ == '__main__':
    run()
