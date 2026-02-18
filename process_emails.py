import imaplib
import email
from email.header import decode_header
import os
import json
import re
from datetime import datetime
from openai import OpenAI
from bs4 import BeautifulSoup

# INSTELLINGEN
IMAP_SERVER = "imap.gmail.com"
EMAIL_USER = os.environ.get("GMAIL_USER")
EMAIL_PASS = os.environ.get("GMAIL_PASSWORD")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY")

# JSON BESTAND BESTAAT AL OF WORDT AANGEMAAKT
JSON_FILE = "press.json"

def clean_text(text):
    return re.sub(r'\s+', ' ', text).strip()

def extract_email_body(msg):
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdispo = str(part.get('Content-Disposition'))
            
            if ctype == 'text/plain' and 'attachment' not in cdispo:
                try:
                    body = part.get_payload(decode=True).decode('utf-8')
                except UnicodeDecodeError:
                    # Fallback naar latin-1 als utf-8 faalt
                    body = part.get_payload(decode=True).decode('latin-1', errors='ignore')
                return body
                
            elif ctype == 'text/html' and 'attachment' not in cdispo:
                try:
                    html = part.get_payload(decode=True).decode('utf-8')
                except UnicodeDecodeError:
                    html = part.get_payload(decode=True).decode('latin-1', errors='ignore')
                
                soup = BeautifulSoup(html, "html.parser")
                return soup.get_text()
    else:
        # Geen multipart, gewoon platte tekst
        try:
            body = msg.get_payload(decode=True).decode('utf-8')
        except UnicodeDecodeError:
            body = msg.get_payload(decode=True).decode('latin-1', errors='ignore')
            
    return body
    
def analyze_with_ai(subject, body, client):
    prompt = f"""
    Analyseer deze e-mail (een persbericht over een TV-programma).
    
    ONDERWERP: {subject}
    INHOUD (ingekort): {body[:3000]}
    
    Geef een JSON object terug met deze velden:
    - "titel": De exacte titel van het programma (zonder 'Seizoen X' of 'vanavond').
    - "zender": De zender (VTM, VRT 1, Play, Canvas, etc.).
    - "datum": De uitzenddatum in YYYY-MM-DD formaat (als het vandaag is, gebruik de datum van vandaag).
    - "tijd": Uitzenduur (HH:MM).
    - "samenvatting": Een wervende zin van max 30 woorden.
    - "seizoen_start": true als dit de start van een nieuw seizoen is, anders false.
    
    Geef ALLEEN JSON terug.
    """
    
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0
        )
        content = response.choices[0].message.content
        # Strip markdown code blocks if present
        content = content.replace("```json", "").replace("```", "")
        return json.loads(content)
    except Exception as e:
        print(f"AI Error: {e}")
        return None

def main():
    if not EMAIL_USER or not EMAIL_PASS:
        print("Geen credentials gevonden via environment variables.")
        return

    # 1. Verbinden met Gmail
    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(EMAIL_USER, EMAIL_PASS)
    mail.select("inbox")

    # 2. Zoek ongelezen mails
    status, messages = mail.search(None, 'UNSEEN')
    email_ids = messages[0].split()

    if not email_ids:
        print("Geen nieuwe emails.")
        return

    client = OpenAI(api_key=OPENAI_KEY)
    
    # Laad bestaande JSON
    existing_data = []
    if os.path.exists(JSON_FILE):
        with open(JSON_FILE, 'r', encoding='utf-8') as f:
            try:
                existing_data = json.load(f)
            except:
                existing_data = []

    new_entries = []

    for e_id in email_ids:
        _, msg_data = mail.fetch(e_id, '(RFC822)')
        for response_part in msg_data:
            if isinstance(response_part, tuple):
                msg = email.message_from_bytes(response_part[1])
                subject, encoding = decode_header(msg["Subject"])[0]
                if isinstance(subject, bytes):
                    subject = subject.decode(encoding if encoding else "utf-8")
                
                print(f"Verwerken: {subject}")
                
                body = extract_email_body(msg)
                
                # AI Analyse
                data = analyze_with_ai(subject, body, client)
                
                if data:
                    # Voeg unieke ID toe
                    data['id'] = f"{data['zender']}-{data['titel']}-{data['datum']}".replace(" ", "-").lower()
                    data['scraped_at'] = datetime.now().isoformat()
                    new_entries.append(data)

    mail.close()
    mail.logout()

    # 3. Opslaan (Nieuwste bovenaan)
    if new_entries:
        # Voeg toe aan bestaande data, vermijd dubbels op basis van ID
        existing_ids = {item['id'] for item in existing_data}
        for entry in new_entries:
            if entry['id'] not in existing_ids:
                existing_data.insert(0, entry) # Nieuwste bovenaan
        
        # Beperk tot laatste 50 items om bestand klein te houden
        existing_data = existing_data[:50]

        with open(JSON_FILE, 'w', encoding='utf-8') as f:
            json.dump(existing_data, f, indent=2, ensure_ascii=False)
        print(f"✅ {len(new_entries)} nieuwe items toegevoegd aan press.json")
    else:
        print("Geen relevante data gevonden in emails.")

if __name__ == "__main__":
    main()
