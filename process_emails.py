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

# JSON BESTAND
JSON_FILE = "press.json"

# KEYWORDS OM OP TE FILTEREN (Case insensitive)
# Alleen mails met deze woorden in het onderwerp worden verwerkt
RELEVANT_KEYWORDS = ["vtm", "vrt", "play", "persbericht", "telefacts", "programma", "uitzending", "start", "seizoen", "aflevering", "nieuws", "tv"]

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
        try:
            body = msg.get_payload(decode=True).decode('utf-8')
        except UnicodeDecodeError:
            body = msg.get_payload(decode=True).decode('latin-1', errors='ignore')
            
    return body

def analyze_with_ai(subject, body, client):
    # Huidige datum voor context (zodat AI weet wat "morgen" of "dinsdag 17 feb" betekent)
    today = datetime.now().strftime("%Y-%m-%d")
    
    prompt = f"""
    Je bent een assistent die TV-persberichten analyseert.
    VANDAAG IS HET: {today}
    
    ANALYSEER DEZE E-MAIL:
    Onderwerp: {subject}
    Inhoud: {body[:3500]}
    
    TAAK:
    1. Is dit een persbericht over een specifiek TV-programma of uitzending?
    2. Zo NEE (bijv. Google Security Alert, Twitter notificatie, Spam, Reclame): Geef JSON {{ "ignore": true }}
    3. Zo JA: Extraheer de volgende data in JSON:
    
    - "titel": De exacte titel van het programma.
    - "zender": De zender (VTM, VRT 1, Play4, Canvas, etc.).
    - "datum": De uitzenddatum in YYYY-MM-DD formaat. (Let op: "dinsdag 17 februari" moet je omzetten naar het juiste jaar, waarschijnlijk 2025 of 2026. Kijk naar de context of header datum).
    - "tijd": Uitzenduur (HH:MM).
    - "samenvatting": Een wervende samenvatting van max 2 zinnen.
    - "seizoen_start": true als dit de start van een nieuw seizoen is.
    
    Geef ALLEEN JSON terug.
    """
    
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0
        )
        content = response.choices[0].message.content
        content = content.replace("```json", "").replace("```", "").strip()
        data = json.loads(content)
        
        # Filter junk eruit
        if data.get("ignore") == True:
            return None
            
        return data
    except Exception as e:
        print(f"AI Error: {e}")
        return None

def is_relevant(subject):
    if not subject: return False
    sub_lower = subject.lower()
    return any(keyword in sub_lower for keyword in RELEVANT_KEYWORDS)

def main():
    if not EMAIL_USER or not EMAIL_PASS:
        print("Geen credentials.")
        return

    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(EMAIL_USER, EMAIL_PASS)
    mail.select("inbox")

    # Haal ALTIJD de lijst op, ook gelezen mails, want we kijken naar de laatste X
    status, messages = mail.search(None, 'ALL')
    email_ids = messages[0].split()

    if not email_ids:
        print("Inbox is leeg.")
        return

    # Pak de laatste 20 mails (om zeker te zijn dat we de juiste vinden tussen de spam)
    print(f"Totaal {len(email_ids)} mails. We scannen de laatste 20 op relevantie.")
    recent_ids = email_ids[-20:]

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

    # We draaien de lijst om: nieuwste eerst
    for e_id in reversed(recent_ids):
        _, msg_data = mail.fetch(e_id, '(RFC822)')
        for response_part in msg_data:
            if isinstance(response_part, tuple):
                msg = email.message_from_bytes(response_part[1])
                subject, encoding = decode_header(msg["Subject"])[0]
                if isinstance(subject, bytes):
                    subject = subject.decode(encoding if encoding else "utf-8")
                
                # STRENGE FILTER: Alleen verwerken als het op een persbericht lijkt
                if is_relevant(subject):
                    print(f"🔍 ANALYSEREN: {subject}")
                    body = extract_email_body(msg)
                    data = analyze_with_ai(subject, body, client)
                    
                    if data:
                        # Maak ID
                        clean_titel = re.sub(r'[^a-zA-Z0-9]', '', data['titel']).lower()
                        data['id'] = f"{data['zender'].lower()}-{clean_titel}-{data['datum']}"
                        data['scraped_at'] = datetime.now().isoformat()
                        
                        print(f"   ✅ GEVONDEN: {data['titel']} op {data['datum']}")
                        new_entries.append(data)
                    else:
                        print("   ❌ AI wees dit af (geen TV programma).")
                else:
                    # Print alleen de eerste 50 chars om log schoon te houden
                    print(f"⏭️  Overslaan (irrelevant): {subject[:50]}...")

    mail.close()
    mail.logout()

    # Opslaan
    if new_entries:
        # Voeg toe, voorkom dubbels
        existing_ids = {item['id'] for item in existing_data}
        for entry in new_entries:
            if entry['id'] not in existing_ids:
                existing_data.insert(0, entry)
        
        existing_data = existing_data[:50]

        with open(JSON_FILE, 'w', encoding='utf-8') as f:
            json.dump(existing_data, f, indent=2, ensure_ascii=False)
        print(f"💾 press.json bijgewerkt met {len(new_entries)} nieuwe items.")
    else:
        print("Geen nieuwe relevante persberichten gevonden.")

if __name__ == "__main__":
    main()
