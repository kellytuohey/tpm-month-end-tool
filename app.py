import os
import json
import base64
import datetime
import threading
import webbrowser
import io
import urllib.request
from html.parser import HTMLParser

from flask import Flask, request, jsonify, render_template, send_from_directory
from dotenv import load_dotenv
import anthropic
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

load_dotenv()

app = Flask(__name__)

GOOGLE_CREDS_FILE = 'client_secret_376562692756-lnl6ptgugqugku5c0n4ds6d05no2s1m9.apps.googleusercontent.com.json'
GOOGLE_TOKEN_FILE = 'google_token.json'
GOOGLE_SCOPES = [
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/gmail.compose',
    'https://www.googleapis.com/auth/gmail.settings.basic'
]

with open('client_config.json') as f:
    CONFIG = json.load(f)


# ── Fiscal year helper ───────────────────────────────────────────────────────

def get_fiscal_year(year_end_month):
    today = datetime.date.today()
    return today.year if today.month <= year_end_month else today.year + 1


# ── Google auth ──────────────────────────────────────────────────────────────

def get_google_creds():
    creds = None
    if os.path.exists(GOOGLE_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_FILE, GOOGLE_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_CREDS_FILE, GOOGLE_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(GOOGLE_TOKEN_FILE, 'w') as f:
            f.write(creds.to_json())
    return creds


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route('/favicon.ico')
def favicon():
    return send_from_directory('static', 'favicon.png', mimetype='image/png')

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/clients')
def get_clients():
    clients = []
    for c in CONFIG['clients']:
        year_end_month = c.get('year_end_month', CONFIG.get('default_year_end_month', 12))
        clients.append({
            'name': c['name'],
            'drive_folder': c.get('drive_folder', c['name']),
            'reporting_folder': c.get('reporting_folder', 'Monthly Reporting'),
            'email': c['email'],
            'year_end_month': year_end_month,
            'frequency': c.get('frequency', 'monthly'),
            'fiscal_year': get_fiscal_year(year_end_month)
        })
    return jsonify({'clients': clients})


@app.route('/api/generate', methods=['POST'])
def generate():
    try:
        client_name  = request.form.get('client_name')
        client_email = request.form.get('client_email')
        cap_link      = request.form.get('cap_link')
        transcript    = request.form.get('transcript')
        personal_note = request.form.get('personal_note', '').strip()
        fiscal_year  = request.form.get('fiscal_year')
        report_month = request.form.get('report_month')
        drive_folder      = request.form.get('drive_folder')
        reporting_folder  = request.form.get('reporting_folder', 'Monthly Reporting')
        pdf_files    = request.files.getlist('pdf')
        force_create  = request.form.get('force_create') == 'true'
        file_action   = request.form.get('file_action', '')  # 'overwrite', 'add', or ''

        missing = [f for f, v in {
            'client': client_name, 'email': client_email,
            'cap link': cap_link, 'transcript': transcript,
            'fiscal year': fiscal_year,
        }.items() if not v]
        if not pdf_files:
            missing.append('PDF')
        if missing:
            return jsonify({'error': f'Missing: {", ".join(missing)}'}), 400

        creds = get_google_creds()

        # 1. Upload PDFs to Drive
        drive_service = build('drive', 'v3', credentials=creds)

        # Check client folder exists before doing anything
        folder_name = drive_folder or client_name
        folder_check = check_client_folder(drive_service, folder_name)
        if not folder_check['exists'] and not force_create:
            return jsonify({
                'folder_missing': True,
                'message': f'Client folder not found in your Google Drive. Create "{folder_name}" folder now?'
            }), 200

        # Check for duplicate filenames before uploading
        if not file_action:
            duplicates = check_duplicate_files(drive_service, [f.filename for f in pdf_files], folder_name, fiscal_year, reporting_folder)
            if duplicates:
                names = ', '.join(f'"{n}"' for n in duplicates)
                return jsonify({
                    'duplicate_files': True,
                    'message': f'{names} already exist in this folder. Overwrite or add as a new copy?'
                }), 200

        drive_links = [upload_to_drive(drive_service, f, folder_name, fiscal_year, reporting_folder, overwrite=(file_action == 'overwrite')) for f in pdf_files]

        # 2. Extract callouts via Claude
        callouts = extract_callouts(transcript, client_name)

        # 3. Create Gmail draft
        gmail_service = build('gmail', 'v1', credentials=creds)
        first_name = client_name.split()[0]
        create_gmail_draft(gmail_service, client_email, first_name, cap_link, drive_links, callouts, report_month, personal_note)

        return jsonify({
            'success': True,
            'drive_links': drive_links,
            'callouts': callouts,
            'message': 'Done! Draft is waiting in your Gmail.'
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Drive upload ─────────────────────────────────────────────────────────────

def find_folder(service, name, parent_id):
    q = (
        f"name='{name}' and mimeType='application/vnd.google-apps.folder' "
        f"and trashed=false and '{parent_id}' in parents"
    )
    results = service.files().list(q=q, fields='files(id)').execute()
    files = results.get('files', [])
    return files[0]['id'] if files else None

def find_or_create_folder(service, name, parent_id):
    folder_id = find_folder(service, name, parent_id)
    if folder_id:
        return folder_id
    meta = {'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}
    return service.files().create(body=meta, fields='id').execute()['id']

def check_client_folder(service, client_name):
    clients_id = find_folder(service, 'Clients', 'root')
    if not clients_id:
        return {'exists': False}
    client_id = find_folder(service, client_name, clients_id)
    return {'exists': client_id is not None}

def get_reporting_folder_id(service, client_name, fiscal_year, reporting_folder):
    clients_id   = find_or_create_folder(service, 'Clients', 'root')
    client_id    = find_or_create_folder(service, client_name, clients_id)
    year_id      = find_or_create_folder(service, str(fiscal_year), client_id)
    return find_or_create_folder(service, reporting_folder, year_id)

def check_duplicate_files(service, filenames, client_name, fiscal_year, reporting_folder):
    try:
        reporting_id = get_reporting_folder_id(service, client_name, fiscal_year, reporting_folder)
        duplicates = []
        for name in filenames:
            safe = name.replace("'", "\\'")
            q = f"name='{safe}' and '{reporting_id}' in parents and trashed=false"
            results = service.files().list(q=q, fields='files(id)').execute()
            if results.get('files'):
                duplicates.append(name)
        return duplicates
    except Exception:
        return []

def upload_to_drive(service, pdf_file, client_name, fiscal_year, reporting_folder='Monthly Reporting', overwrite=False):
    reporting_id = get_reporting_folder_id(service, client_name, fiscal_year, reporting_folder)

    if overwrite:
        safe = pdf_file.filename.replace("'", "\\'")
        q = f"name='{safe}' and '{reporting_id}' in parents and trashed=false"
        existing = service.files().list(q=q, fields='files(id)').execute().get('files', [])
        for f in existing:
            service.files().delete(fileId=f['id']).execute()

    pdf_bytes = pdf_file.read()
    media = MediaIoBaseUpload(io.BytesIO(pdf_bytes), mimetype='application/pdf')
    file_meta = {'name': pdf_file.filename, 'parents': [reporting_id]}
    uploaded = service.files().create(body=file_meta, media_body=media, fields='id,webViewLink').execute()
    return uploaded.get('webViewLink', '')


# ── Cap.so thumbnail fetcher ─────────────────────────────────────────────────

class _OGParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.og_image = None
    def handle_starttag(self, tag, attrs):
        if tag == 'meta' and not self.og_image:
            d = dict(attrs)
            if d.get('property') == 'og:image':
                self.og_image = d.get('content')

def get_og_image(url):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as r:
            html = r.read().decode('utf-8', errors='ignore')
        p = _OGParser()
        p.feed(html)
        return p.og_image
    except Exception:
        return None


# ── Claude callout extraction ─────────────────────────────────────────────────

def extract_callouts(transcript, client_name):
    ai = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
    msg = ai.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=400,
        messages=[{
            'role': 'user',
            'content': f"""You're helping a bookkeeper named Kelly write a monthly financial summary email to a client.

Extract 2-3 key financial highlights from this transcript for {client_name}. These should be the most important things the client needs to know about their numbers this month — written in plain English a business owner would immediately understand. No jargon, no accounting terms, no bullet symbols. Just the highlight text, one per line.

Transcript:
{transcript}

Return only the 2-3 highlights, one per line, nothing else."""
        }]
    )
    return [line.strip() for line in msg.content[0].text.strip().split('\n') if line.strip()]


# ── Gmail draft ───────────────────────────────────────────────────────────────

def create_gmail_draft(service, to_email, first_name, cap_link, drive_links, callouts, report_month, personal_note=''):
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    if not report_month:
        last = datetime.date.today().replace(day=1) - datetime.timedelta(days=1)
        report_month = last.strftime('%B %Y')

    # Fetch Gmail signature
    signature_html = ''
    try:
        send_as = service.users().settings().sendAs().list(userId='me').execute()
        for alias in send_as.get('sendAs', []):
            if alias.get('isDefault'):
                signature_html = alias.get('signature', '')
                break
    except Exception:
        pass

    callout_items = ''.join(f'<li>{c}</li>' for c in callouts)

    if len(drive_links) == 1:
        report_links_html = f'<a href="{drive_links[0]}">View your report</a>'
    else:
        items = ''.join(f'<li><a href="{l}">View report {i+1}</a></li>' for i, l in enumerate(drive_links))
        report_links_html = f'<ul style="padding-left:20px;margin:8px 0;">{items}</ul>'

    sig_block = f'<br><br>{signature_html}' if signature_html else '<p>Kelly</p>'

    note_line = f'  <p>{personal_note}</p>\n' if personal_note else ''

    # Try to fetch og:image from Cap.so link
    og_image = get_og_image(cap_link)
    if og_image:
        walkthrough_html = (
            f'<p><a href="{cap_link}" style="display:inline-block;">'
            f'<img src="{og_image}" alt="Watch the full walkthrough" '
            f'width="480" style="max-width:100%;border-radius:6px;display:block;" />'
            f'</a></p>'
        )
    else:
        walkthrough_html = f'<p><a href="{cap_link}">Watch the full walkthrough</a></p>'

    html = f"""
<div style="font-family:Georgia,serif;font-size:15px;line-height:1.7;color:#1a1a1a;max-width:560px;">
  <p>Hi {first_name},</p>
{note_line}  <p>Your {report_month} report is ready!</p>
  <p>A few things worth noting this month:</p>
  <ul style="padding-left:20px;margin:8px 0;">
    {callout_items}
  </ul>
  {walkthrough_html}
  <p>{report_links_html}</p>
  <p>As always, reach out if anything comes up!</p>
  {sig_block}
</div>
"""

    msg = MIMEMultipart('alternative')
    msg['To'] = to_email
    msg['Subject'] = f"{first_name} - {report_month} Financial Report"
    msg.attach(MIMEText(html, 'html', 'utf-8'))

    encoded = base64.urlsafe_b64encode(msg.as_bytes()).decode('utf-8')
    service.users().drafts().create(userId='me', body={'message': {'raw': encoded}}).execute()


# ── Start ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print("\nChecking Google authorization...")
    get_google_creds()
    print("Google authorized.\n")

    def open_browser():
        import time
        time.sleep(1.5)
        webbrowser.open('http://localhost:8080')

    threading.Thread(target=open_browser, daemon=True).start()
    print("Starting app at http://localhost:8080\n")
    app.run(host='0.0.0.0', port=8080, debug=False)
