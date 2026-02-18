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

# TOEGESTANE DOMEINEN (Whitelist)
# Als een mail van een van deze domeinen komt, wordt hij ALTIJD geanalyseerd
TRUSTED_DOMAINS = ["playmedia.be", "dpgmedia.be", "vrt.be", "sbsbelgium.be", "standaard.be"]

# KEYWORDS (Fallback)
# Als de afzender niet in de whitelist staat, kijken we of het onderwerp deze woorden bevat
RELEVANT_KEYWORDS = ["persbericht", "telefacts", "programma", "uitzending", "start", "seizoen", "aflevering", "nieuws", "tv", "vtm", "play", "vrt"]

def clean_text(text):
    return re.sub(r'\s+', ' ', text).strip()

def decode_mime_header(header_value):
    if not header_value:
        return ""
    decoded_list = decode_header(header_value)
    result = ""
    for content, encoding in decoded_list:
        if isinstance(content, bytes):
            result += content.decode(encoding if encoding else "utf-8", errors="ignore")
        else:
            result += str(content)
    return result

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
                return body # Geef direct plain text terug als gevonden
                
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

def analyze_with_ai(subject, sender, body, email_date, client):
    # Huidige datum voor context
    today = datetime.now().strftime("%Y-%m-%d")
    
    prompt = f"""
    Je bent een data-processor. Je krijgt een ruwe e-mailtekst van een persbericht.
    
    CONTEXT:
    - Vandaag: {today}
    - Datum mail: {email_date}
    - Afzender: {sender}
    - Onderwerp: {subject}
    
    INHOUD EMAIL:
    {body[:7000]} 
    
    JOUW TAAK:
    Zet deze ongestructureerde e-mail om naar strakke JSON.
    
    ⚠️ NEGEER (Return {{ "ignore": true }}):
    - Google Security Alerts, Twitter notificaties, Spam, Reclame.
    
    ALS RELEVANT, GEEF JSON MET DEZE VELDEN:
    {{
      "titel": "Titel van het programma/nieuws",
      "zender": "Zender (VTM, VRT, Play, etc.)",
      "datum": "Uitzenddatum (YYYY-MM-DD)",
      "tijd": "Uitzenduur (HH:MM) of null",
      "seizoen_start": true/false,
      "volledige_tekst": "De COMPLETE tekst van het persbericht. Instructies: 1. Kort NIETS in. 2. Behoud alle details, quotes en alinea's. 3. Verwijder WEL de 'ruis' eromheen (zoals: 'Bekijk de trailer hier', 'Niet voor publicatie', 'Persverantwoordelijke: Jan', 'Uitschrijven', 'Verzonden vanaf iPhone'). 4. Zorg dat het leest als een schoon artikel. Gebruik \\n voor nieuwe alinea's."
    }}
    
    GEEF ALLEEN JSON.
    """
    
    try:
        # We gebruiken hier meer tokens voor de output omdat de tekst lang kan zijn
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=2500 
        )
        content = response.choices[0].message.content
        content = content.replace("```json", "").replace("```", "").strip()
        data = json.loads(content)
        
        if data.get("ignore") == True:
            return None
            
        return data
    except Exception as e:
        print(f"AI Error: {e}")
        return None

def is_relevant(subject, sender):
    # 1. Check Afzender Domein (Zeer betrouwbaar)
    sender_lower = sender.lower()
    for domain in TRUSTED_DOMAINS:
        if domain in sender_lower:
            return True
            
    # 2. Check Onderwerp Keywords (Fallback)
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

    # Haal alles op, we filteren de laatste 20 in Python
    status, messages = mail.search(None, 'ALL')
    email_ids = messages[0].split()

    if not email_ids:
        print("Inbox is leeg.")
        return

    # Pak de laatste 20 mails
    print(f"Totaal {len(email_ids)} mails. We scannen de laatste 20.")
    recent_ids = email_ids[-20:]

    client = OpenAI(api_key=OPENAI_KEY)
    
    # Laad bestaande JSON (om dubbels te voorkomen)
    existing_data = []
    # Als press.json corrupt is of vol junk zit, begin opnieuw
    if os.path.exists(JSON_FILE):
        try:
            with open(JSON_FILE, 'r', encoding='utf-8') as f:
                existing_data = json.load(f)
        except:
            existing_data = []

    new_entries = []

    # Loop omgekeerd (nieuwste eerst)
    for e_id in reversed(recent_ids):
        _, msg_data = mail.fetch(e_id, '(RFC822)')
        for response_part in msg_data:
            if isinstance(response_part, tuple):
                msg = email.message_from_bytes(response_part[1])
                
                # Decode headers
                subject = decode_mime_header(msg["Subject"])
                sender = decode_mime_header(msg["From"])
                date_str = decode_mime_header(msg["Date"])
                
                # Check relevantie (Subject OF Sender)
                if is_relevant(subject, sender):
                    print(f"🔍 ANALYSEREN: '{subject}' van '{sender}'")
                    
                    body = extract_email_body(msg)
                    data = analyze_with_ai(subject, sender, body, date_str, client)
                    
                    if data:
                        # Maak unieke ID
                        clean_titel = re.sub(r'[^a-zA-Z0-9]', '', data['titel']).lower()
                        data['id'] = f"{data['zender'].lower()}-{clean_titel}-{data['datum']}"
                        data['scraped_at'] = datetime.now().isoformat()
                        
                        print(f"   ✅ GEVONDEN: {data['titel']} ({data['zender']})")
                        new_entries.append(data)
                    else:
                        print("   ❌ AI: Irrelevant (Spam/Notificatie).")
                else:
                    # Print kort voor debugging
                    print(f"⏭️  Overslaan: {subject[:40]}... [{sender}]")

    mail.close()
    mail.logout()

    # Opslaan
    if new_entries:
        # Voeg toe, voorkom dubbels op basis van ID
        existing_ids = {item['id'] for item in existing_data}
        added_count = 0
        
        for entry in new_entries:
            if entry['id'] not in existing_ids:
                existing_data.insert(0, entry) # Nieuwste bovenaan
                existing_ids.add(entry['id'])
                added_count += 1
        
        # Houd max 50 items bij
        existing_data = existing_data[:50]

        with open(JSON_FILE, 'w', encoding='utf-8') as f:
            json.dump(existing_data, f, indent=2, ensure_ascii=False)
        print(f"💾 press.json geüpdatet met {added_count} nieuwe items.")
    else:
        print("Geen nieuwe relevante data.")

if __name__ == "__main__":
    main()
