# === Imports ===
import os
import base64
import json
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS
from googleapiclient.discovery import build
from google.oauth2 import service_account

# === App Initialization ===
app = Flask(__name__)
app.secret_key = 'your_secret_key'
CORS(app, supports_credentials=True)

# === Constants ===
SERVICE_ACCOUNT_FILE = 'workspace_service_account.json'  # Domain-wide delegated SA
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

# === Helper: Workspace delegated credentials ===
def get_workspace_credentials(user_email: str):
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    return creds.with_subject(user_email)

# === Helper: robust Gmail count with pagination ===
def count_messages(service, user_id: str, query: str) -> int:
    total = 0
    page_token = None
    while True:
        # Execute the API call to list messages
        resp = service.users().messages().list(
            userId=user_id,            
            q=query,
            pageToken=page_token,
            includeSpamTrash=False  # Exclude spam/trash messages
        ).execute()

        # Count messages, ensure uniqueness (no duplicates)
        count = len(resp.get('messages', []))
        total += count
        print(f"Fetched {count} messages in this page. Total so far: {total}")  # Output to terminal
        
        page_token = resp.get('nextPageToken')

        # Break the loop if no more pages are available
        if not page_token:
            break

    return total

# === Helper: build inclusive date range for Gmail ===
def build_date_query(start_yyyy_mm_dd: str, end_yyyy_mm_dd: str) -> str:
    try:
        start_dt = datetime.strptime(start_yyyy_mm_dd, "%Y-%m-%d").date()
        end_dt = datetime.strptime(end_yyyy_mm_dd, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("Dates must be in YYYY-MM-DD format")

    if end_dt < start_dt:
        raise ValueError("End date must be on or after start date")

    before_exclusive = end_dt + timedelta(days=1)
    # Use YYYY/MM/DD as Gmail expects; `before:` is exclusive
    return f"after:{start_dt.strftime('%Y/%m/%d')} before:{before_exclusive.strftime('%Y/%m/%d')}"

def get_latest_history_id(service, user_id):
    history = service.users().history().list(userId=user_id, startHistoryId='1').execute()
    latest_history_id = history.get('historyId', None)
    print(f"Latest history ID: {latest_history_id}")  # Output to terminal
    return latest_history_id

def check_inbox_sync(service, user_id, last_known_history_id):
    history = service.users().history().list(userId=user_id, startHistoryId=last_known_history_id).execute()
    print(f"History check: {history}")  # Output to terminal
    if 'history' in history:
        # If there is a history record, it indicates new changes since last check
        return True
    else:
        # No new changes, potentially still syncing
        return False

def check_message_count(service, user_id):
    response = service.users().messages().list(userId=user_id, labelIds=['INBOX']).execute()
    message_count = len(response.get('messages', []))
    print(f"Message count in inbox: {message_count}")  # Output to terminal
    return message_count

def count_messages_by_thread(service, user_id, query):
    total = 0
    page_token = None
    while True:
        response = service.users().threads().list(
            userId=user_id, q=query, pageToken=page_token
        ).execute()
        count = len(response.get('threads', []))
        total += count
        print(f"Fetched {count} threads in this page. Total so far: {total}")  # Output to terminal
        page_token = response.get('nextPageToken')
        if not page_token:
            break
    return total

# === Helper: Fetch sent email details ===
def get_sent_email_details(service, user_id: str, query: str):
    email_details = []
    page_token = None
    while True:
        # Fetch sent emails matching the query
        resp = service.users().messages().list(
            userId=user_id,
            q=query,
            pageToken=page_token,
            includeSpamTrash=False  # Exclude spam/trash messages
        ).execute()

        for msg in resp.get('messages', []):
            message = service.users().messages().get(userId=user_id, id=msg['id']).execute()
            
            # Extract necessary details
            email_data = {}
            headers = message['payload']['headers']
            for header in headers:
                if header['name'] == 'To':
                    email_data['to'] = header['value']
                elif header['name'] == 'Subject':
                    email_data['subject'] = header['value']
                elif header['name'] == 'Date':
                    email_data['date'] = header['value']

            # Decode message body (base64url to plaintext)
            if 'parts' in message['payload']:
                for part in message['payload']['parts']:
                    if part['mimeType'] == 'text/plain':
                        data = part['body']['data']
                        decoded_message = base64.urlsafe_b64decode(data).decode('utf-8')  # Decode base64url to UTF-8 string
                        email_data['message'] = decoded_message
                        break

            email_details.append(email_data)

        page_token = resp.get('nextPageToken')
        if not page_token:
            break

    return email_details

@app.route('/')
def home():
    return 'Welcome to Email Monitor API (Google Workspace version)'

@app.route('/dashboard')
def dashboard():
    email = request.args.get('email')
    start_date = request.args.get('start')
    end_date = request.args.get('end')

    if not email or not start_date or not end_date:
        return jsonify({'error': 'Missing email or date parameters'}), 400

    try:
        # Delegated creds for the Workspace user
        creds = get_workspace_credentials(email)
        service = build('gmail', 'v1', credentials=creds, cache_discovery=False)

        # Inclusive date range query
        date_q = build_date_query(start_date, end_date)

        # SENT: rely on label for accuracy
        sent_q = f"{date_q} label:sent"

        # RECEIVED: filter only inbox messages and exclude sent, drafts, spam, trash
        # We use 'in:inbox' to restrict only to Inbox messages
        received_q = (
            f"{date_q} label:inbox -label:sent -label:drafts -label:spam -label:trash -from:me"
        )

        sent_count = count_messages(service, 'me', sent_q)
        received_count = count_messages(service, 'me', received_q)

        # Output sent and received counts to the terminal
        print(f"Sent count: {sent_count}")
        print(f"Received count: {received_count}")

        return jsonify({
            'sent': sent_count,
            'received': received_count,
            'date_range': f"{start_date} to {end_date}"
        })

    except ValueError as ve:
        return jsonify({'error': str(ve)}), 400
    except Exception as e:
        # Surface a bit more context for debugging (safe message)
        return jsonify({'error': f'Failed to fetch Gmail data: {type(e).__name__}'}), 500
    
@app.route('/sent_details')
def sent_details():
    email = request.args.get('email')
    start_date = request.args.get('start')
    end_date = request.args.get('end')

    if not email or not start_date or not end_date:
        return jsonify({'error': 'Missing email or date parameters'}), 400

    try:
        # Delegated credentials for the Workspace user
        creds = get_workspace_credentials(email)
        service = build('gmail', 'v1', credentials=creds, cache_discovery=False)

        # Build the date range query
        date_q = build_date_query(start_date, end_date)

        # SENT: Query to fetch sent emails
        sent_q = f"{date_q} label:sent"
        
        # Get the sent email details
        email_details = get_sent_email_details(service, 'me', sent_q)

        return jsonify({
            'email_details': email_details
        })

    except ValueError as ve:
        return jsonify({'error': str(ve)}), 400
    except Exception as e:
        return jsonify({'error': f'Failed to fetch Gmail data: {type(e).__name__}'}), 500
    

# Route to save email IDs to a .txt file
@app.route('/save_emails_to_file', methods=['POST'])
def save_emails_to_file():
    # Get the emails from the request body
    data = request.get_json()
    emails = data.get('emails', [])

    # Format emails as key-value pairs
    email_data = '\n'.join([f'email = "{email}"' for email in emails])

    # Write to a .txt file
    try:
        with open('emails.txt', 'w') as file:
            file.write(email_data)
        return jsonify({'message': 'Emails saved to file successfully'})
    except Exception as e:
        return jsonify({'error': f'Failed to save emails: {str(e)}'}), 500

if __name__ == '__main__':
    app.run(debug=True)
