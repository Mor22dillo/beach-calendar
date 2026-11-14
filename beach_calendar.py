#!/usr/bin/env python3
"""
Sincronizza i tornei di beach volley nel Lazio (AIBVC + FIPAV) su un Google Calendar
e invia su Telegram un riepilogo dei tornei nuovi, modificati o rimossi.

Fonti (le stesse API usate dai due siti):
  - AIBVC: https://api.aibvc.app/api/tournaments  (L1, L2, CNT)
  - FIPAV: https://media.myvolley.net/public/json/ (Assoluto, Serie Beach, Società)

Due modalità:
  - "ics" (default, senza Google Cloud): scrive un file .ics (ICS_PATH) e uno stato (STATE_PATH);
    Google Calendar si iscrive al file pubblicato online ("Altri calendari → Da URL").
  - "google": scrive direttamente su un Google Calendar tramite service account.

Variabili d'ambiente:
  MODALITA                     "ics" o "google" (default: google se c'è GOOGLE_CALENDAR_ID, altrimenti ics)
  ICS_PATH / STATE_PATH        percorsi dei file in modalità ics (default docs/beach-roma.ics, data/stato.json)
  LINK_CALENDARIO              link opzionale da mettere in fondo ai messaggi Telegram
  GOOGLE_SERVICE_ACCOUNT_JSON  contenuto JSON della chiave del service account
  GOOGLE_CALENDAR_ID           ID del calendario (es. abc123@group.calendar.google.com)
  TELEGRAM_BOT_TOKEN           token del bot Telegram
  TELEGRAM_CHAT_ID             chat a cui inviare i messaggi
  FILTRO_ZONA                  "regione" (default: tutto il Lazio), "provincia" (provincia di Roma) o "comune" (solo Roma)
  DRY_RUN                      se "1" non scrive sul calendario e stampa il messaggio invece di inviarlo
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Rome")
USER_AGENT = "beach-calendar-roma/1.0 (+uso personale)"
PROP_KEY = "bvsync_key"      # chiave univoca del torneo, salvata nell'evento
PROP_HASH = "bvsync_hash"    # impronta dei dati, per capire se è cambiato qualcosa

# --- Configurazione categorie -------------------------------------------------
AIBVC_API = "https://api.aibvc.app/api/tournaments"
AIBVC_QUERIES = [
    # Tornei L1 e L2 del circuito AIBVC
    {"upcoming": "true", "tourType": "aibvc_tour", "level": "L1,L2"},
    # Campionato Nazionale a Tappe (CNT)
    {"upcoming": "true", "tourType": "cnt"},
]
AIBVC_LINK = "https://aibvc.app/tournament/{id}"

FIPAV_BASE = "https://media.myvolley.net/public/json/"
FIPAV_MENU = {"assoluto": "Assoluto", "serie-beach": "Serie Beach", "societa": "Società"}
FIPAV_LINK = "https://www.federvolley.it/campionati/beach-volley/eventi"

GENERE = {"maschile": "M", "femminile": "F", "misto": "Misto"}


@dataclass
class Torneo:
    key: str
    titolo: str
    inizio: dt.date | dt.datetime
    fine: dt.date | dt.datetime
    luogo: str
    descrizione: str
    link: str
    riga_telegram: str          # testo breve per la notifica
    fonte: str                  # "aibvc" / "fipav"
    impronta: dict = field(default_factory=dict)

    @property
    def tutto_il_giorno(self) -> bool:
        return not isinstance(self.inizio, dt.datetime)

    @property
    def hash(self) -> str:
        raw = json.dumps(self.impronta, sort_keys=True, default=str)
        return hashlib.sha1(raw.encode()).hexdigest()[:16]


# --- HTTP ---------------------------------------------------------------------
def get_json(url: str, params: dict | None = None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


# --- Filtro zona ---------------------------------------------------------------
# FILTRO_ZONA: "comune" = solo Roma, "provincia" = provincia di Roma, "regione" = tutto il Lazio
ZONA_LABEL = {"comune": "a Roma", "provincia": "in provincia di Roma", "regione": "nel Lazio"}


def is_roma_aibvc(t: dict, zona: str) -> bool:
    city = (t.get("city") or "").strip()
    if zona == "regione":
        z = t.get("zone")
        zone = (z.get("name") if isinstance(z, dict) else z) or ""
        return (t.get("region") or "").strip().lower() == "lazio" or zone.strip().upper() == "LAZIO"
    if re.match(r"^roma\b", city, re.I):
        return True
    if zona == "provincia":
        addr = ((t.get("venue") or {}).get("address") or "")
        return bool(re.search(r"\(\s*rm\s*\)", city + " " + addr, re.I))
    return False


def is_roma_fipav(e: dict, zona: str) -> bool:
    if zona == "regione":
        return (e.get("regione") or "").strip().upper() == "LAZIO"
    if zona == "provincia":
        return (e.get("provincia") or "").upper() == "ROMA"
    return (e.get("comune") or "").strip().lower() == "roma"


def fuori_roma(citta: str) -> str:
    """Nome della città da mostrare se il torneo non è a Roma (altrimenti stringa vuota)."""
    c = re.sub(r"\s*\(.*?\)\s*", "", citta or "").strip()
    return "" if not c or c.lower() == "roma" else c


# --- AIBVC --------------------------------------------------------------------
def fetch_aibvc(zona: str) -> list[Torneo]:
    visti: dict[str, dict] = {}
    for q in AIBVC_QUERIES:
        page = 1
        while True:
            params = dict(q, page=page, limit=100)
            res = get_json(AIBVC_API, params)
            data = res if isinstance(res, list) else res.get("data", [])
            for t in data:
                visti[t["_id"]] = t
            pag = res.get("pagination") if isinstance(res, dict) else None
            if not pag or page >= pag.get("pages", 1):
                break
            page += 1
    return [aibvc_to_torneo(t) for t in visti.values()
            if not t.get("hiddenFromList") and is_roma_aibvc(t, zona)]


def aibvc_to_torneo(t: dict) -> Torneo:
    cat = "CNT " + t.get("level", "") if t.get("tourType") == "cnt" else t.get("level", "")
    gen = GENERE.get((t.get("gender") or "").lower(), t.get("gender") or "")
    venue = t.get("venue") or {}
    sospeso = (t.get("suspension") or {}).get("isSuspended")
    nome = t.get("name", "Torneo")
    citta = fuori_roma(t.get("city"))
    titolo = f"{'❌ SOSPESO · ' if sospeso else ''}AIBVC {cat} {gen} · {nome}".replace("  ", " ")
    if citta:
        titolo += f" · {citta}"

    inizio = parse_iso(t["startDate"])
    fine = parse_iso(t.get("endDate") or t["startDate"])
    if fine <= inizio:
        fine = inizio + dt.timedelta(hours=8)

    luogo = ", ".join(x for x in [venue.get("name"), venue.get("address"), t.get("city")] if x)
    link = AIBVC_LINK.format(id=t["_id"])
    righe = [f"Circuito: AIBVC {cat}", f"Genere: {gen}"]
    if t.get("ageCategory") and t["ageCategory"] != "open":
        righe.append(f"Categoria età: {t['ageCategory']}")
    if t.get("entryFee") is not None:
        righe.append(f"Quota: {t['entryFee']} € / coppia")
    if t.get("registrationDeadline"):
        righe.append("Scadenza iscrizioni: " + fmt_dt(parse_iso(t["registrationDeadline"])))
    righe.append(f"Dettagli: {link}")

    return Torneo(
        key=f"aibvc:{t['_id']}",
        titolo=titolo,
        inizio=inizio, fine=fine, luogo=luogo,
        descrizione="\n".join(righe), link=link, fonte="aibvc",
        riga_telegram=(f"{fmt_giorno(inizio)} · <b>AIBVC {cat} {gen}</b> · {html.escape(nome)}"
                       + (f" · 📍{html.escape(citta)}" if citta else "")),
        impronta={"t": titolo, "s": inizio, "e": fine, "l": luogo},
    )


# --- FIPAV --------------------------------------------------------------------
def fetch_fipav(zona: str, oggi: dt.date) -> list[Torneo]:
    idx = get_json(FIPAV_BASE + "index.json")
    stagioni = [s["season"] for s in idx.get("seasons", [])
                if "beach_volley" in s.get("discipline", []) and s.get("a", "") >= oggi.isoformat()]

    eventi = []
    for stagione in stagioni:
        disc = get_json(f"{FIPAV_BASE}{stagione}/discipline.json")
        for p in disc.get("pages", []):
            j = get_json(FIPAV_BASE + p["path_discipline"])
            serie = (j.get("beach_volley") or {}).get("serie") or {}
            for per_genere in serie.values():
                for gruppo in per_genere.values():
                    eventi.extend(gruppo.get("codici", []))

    # Tieni solo categorie richieste, zona scelta, da oggi in poi
    eventi = [e for e in eventi
              if e.get("menu_eventi") in FIPAV_MENU
              and is_roma_fipav(e, zona)
              and (e.get("data_fine_iso") or e.get("data_inizio_iso") or "") >= oggi.isoformat()]

    # Raggruppa per tappa: stessa data, stesso club, stesso impianto, stessa sezione
    tappe: dict[str, list[dict]] = {}
    for e in eventi:
        k = "|".join(str(e.get(x)) for x in ("data_inizio_iso", "data_fine_iso", "club_id", "location_id", "menu_eventi"))
        tappe.setdefault(k, []).append(e)

    out = []
    for k, gruppo in tappe.items():
        out.append(fipav_to_torneo(gruppo))
    return out


def short_tipologia(t: str) -> str:
    return (t.replace("Campionato Italiano per società ", "")
             .replace("Campionato Italiano Assoluto - ", "Assoluto ")
             .replace("Campionato Italiano ", "")
             .strip())


def fipav_to_torneo(gruppo: list[dict]) -> Torneo:
    e0 = gruppo[0]
    sezione = FIPAV_MENU[e0["menu_eventi"]]
    inizio = dt.date.fromisoformat(e0["data_inizio_iso"])
    fine = dt.date.fromisoformat(e0.get("data_fine_iso") or e0["data_inizio_iso"]) + dt.timedelta(days=1)

    # categorie: "Gold M+F", "Master 35 F", ...
    cat: dict[str, set] = {}
    for e in gruppo:
        cat.setdefault(short_tipologia(e.get("tipologia", "")), set()).add((e.get("genere") or "?")[0].upper())
    def gen_label(s): return "M+F" if s == {"M", "F"} else "/".join(sorted(s))

    def etichetta(c, g):
        lab = gen_label(g)
        # "Master 35 F" + "F" -> "Master 35 F" (il genere è già nel nome)
        return c if re.search(rf"\b{re.escape(lab)}$", c) else f"{c} {lab}".strip()
    categorie = sorted(etichetta(c, g) for c, g in cat.items())

    club = (e0.get("club") or "").strip()
    comune = e0.get("comune") or ""
    titolo = f"FIPAV {sezione} · {', '.join(categorie)}"
    if len(titolo) > 90:
        titolo = f"FIPAV {sezione} · {len(categorie)} categorie"
    titolo += f" · {club}" if club else ""
    citta = fuori_roma(comune)
    if citta:
        titolo += f" · {citta}"

    righe = [f"Circuito: FIPAV – {sezione}", "Categorie: " + ", ".join(categorie)]
    if club:
        righe.append(f"Organizzatore: {club}")
    mp = [e.get("montepremi") for e in gruppo if e.get("montepremi_valore")]
    if mp:
        righe.append("Montepremi: " + ", ".join(sorted(set(mp))))
    righe.append("Codici: " + ", ".join(sorted(e.get("codice", "") for e in gruppo)))
    righe.append(f"Elenco eventi: {FIPAV_LINK}")

    key_src = f"{e0['data_inizio_iso']}|{e0.get('club_id')}|{e0.get('location_id')}|{e0['menu_eventi']}"
    return Torneo(
        key="fipav:" + hashlib.sha1(key_src.encode()).hexdigest()[:16],
        titolo=titolo,
        inizio=inizio, fine=fine,
        luogo=f"{club}, {comune}" if club else comune,
        descrizione="\n".join(righe), link=FIPAV_LINK, fonte="fipav",
        riga_telegram=(f"{fmt_giorno(inizio)} · <b>FIPAV {sezione}</b> · {html.escape(', '.join(categorie))}"
                       + (f" · {html.escape(club)}" if club else "")
                       + (f" · 📍{html.escape(citta)}" if citta else "")),
        impronta={"t": titolo, "s": inizio, "e": fine, "c": categorie},
    )


# --- Date ---------------------------------------------------------------------
GIORNI = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]


def parse_iso(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ)


def fmt_dt(d: dt.datetime) -> str:
    return d.astimezone(TZ).strftime("%d/%m/%Y %H:%M")


def fmt_giorno(d) -> str:
    if isinstance(d, dt.datetime):
        d = d.astimezone(TZ)
        return f"{GIORNI[d.weekday()]} {d:%d/%m} ore {d:%H:%M}"
    return f"{GIORNI[d.weekday()]} {d:%d/%m}"


def sort_key(t: Torneo):
    d = t.inizio
    return d if isinstance(d, dt.datetime) else dt.datetime.combine(d, dt.time(), TZ)


# --- Google Calendar ----------------------------------------------------------
class GoogleCalendar:
    def __init__(self, calendar_id: str, sa_json: str):
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        creds = service_account.Credentials.from_service_account_info(
            json.loads(sa_json), scopes=["https://www.googleapis.com/auth/calendar"])
        self.svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self.cal = calendar_id

    def list_managed(self, time_min: dt.datetime) -> dict[str, dict]:
        out, token = {}, None
        while True:
            res = self.svc.events().list(
                calendarId=self.cal, timeMin=time_min.isoformat(), singleEvents=True,
                maxResults=2500, pageToken=token, showDeleted=False,
            ).execute()
            for ev in res.get("items", []):
                key = ev.get("extendedProperties", {}).get("private", {}).get(PROP_KEY)
                if key:
                    out[key] = ev
            token = res.get("nextPageToken")
            if not token:
                return out

    def insert(self, body: dict):
        self.svc.events().insert(calendarId=self.cal, body=body).execute()

    def update(self, event_id: str, body: dict):
        self.svc.events().update(calendarId=self.cal, eventId=event_id, body=body).execute()

    def delete(self, event_id: str):
        self.svc.events().delete(calendarId=self.cal, eventId=event_id).execute()


def event_body(t: Torneo) -> dict:
    if t.tutto_il_giorno:
        start, end = {"date": t.inizio.isoformat()}, {"date": t.fine.isoformat()}
    else:
        start = {"dateTime": t.inizio.isoformat(), "timeZone": "Europe/Rome"}
        end = {"dateTime": t.fine.isoformat(), "timeZone": "Europe/Rome"}
    return {
        "summary": t.titolo,
        "location": t.luogo,
        "description": t.descrizione,
        "start": start, "end": end,
        "source": {"title": t.fonte.upper(), "url": t.link},
        "extendedProperties": {"private": {PROP_KEY: t.key, PROP_HASH: t.hash}},
        "transparency": "transparent",
    }


def event_start(ev: dict) -> dt.datetime:
    s = ev.get("start", {})
    if "dateTime" in s:
        return parse_iso(s["dateTime"])
    return dt.datetime.combine(dt.date.fromisoformat(s["date"]), dt.time(), TZ)


# --- Sincronizzazione ---------------------------------------------------------
def sync(cal, tornei: list[Torneo], fonti_ok: set[str], now: dt.datetime, dry_run: bool):
    esistenti = cal.list_managed(now - dt.timedelta(days=2)) if cal else {}
    nuovi, modificati, rimossi = [], [], []

    for t in tornei:
        ev = esistenti.get(t.key)
        if ev is None:
            nuovi.append(t)
            if not dry_run:
                cal.insert(event_body(t))
        elif ev["extendedProperties"]["private"].get(PROP_HASH) != t.hash:
            modificati.append(t)
            if not dry_run:
                cal.update(ev["id"], event_body(t))

    # Rimuove eventi futuri spariti dalla fonte (solo se la fonte ha risposto correttamente)
    chiavi = {t.key for t in tornei}
    for key, ev in esistenti.items():
        fonte = key.split(":", 1)[0]
        if key not in chiavi and fonte in fonti_ok and event_start(ev) > now + dt.timedelta(days=1):
            rimossi.append(ev)
            if not dry_run:
                cal.delete(ev["id"])
    return nuovi, modificati, rimossi


# --- Modalità file .ics (senza Google Cloud) ----------------------------------
def torneo_record(t: Torneo) -> dict:
    return {
        "summary": t.titolo, "location": t.luogo, "description": t.descrizione, "url": t.link,
        "all_day": t.tutto_il_giorno, "start": t.inizio.isoformat(), "end": t.fine.isoformat(),
        "hash": t.hash,
    }


def record_start(r: dict) -> dt.datetime:
    if r["all_day"]:
        return dt.datetime.combine(dt.date.fromisoformat(r["start"]), dt.time(), TZ)
    return dt.datetime.fromisoformat(r["start"])


def sync_state(state: dict, tornei: list[Torneo], fonti_ok: set[str], now: dt.datetime):
    """Confronta i tornei con lo stato salvato. Ritorna (nuovo_stato, nuovi, modificati, rimossi)."""
    eventi = dict(state.get("eventi", {}))
    nuovi, modificati, rimossi = [], [], []
    for t in tornei:
        old = eventi.get(t.key)
        if old is None:
            nuovi.append(t)
        elif old.get("hash") != t.hash:
            modificati.append(t)
        eventi[t.key] = torneo_record(t)

    chiavi = {t.key for t in tornei}
    for key in list(eventi):
        r = eventi[key]
        fonte = key.split(":", 1)[0]
        if key not in chiavi and fonte in fonti_ok and record_start(r) > now + dt.timedelta(days=1):
            rimossi.append(eventi.pop(key))
        elif record_start(r) < now - dt.timedelta(days=180):   # pulizia dello storico
            eventi.pop(key)
    return {"ultimo_controllo": now.isoformat(timespec="minutes"), "eventi": eventi}, nuovi, modificati, rimossi


def _ics_text(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_fold(line: str) -> str:
    # le righe .ics vanno spezzate a 75 byte, continuando con uno spazio
    out, cur = [], b""
    for ch in line:
        b = ch.encode()
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode())
            cur = b""
        cur += b
    out.append(cur.decode())
    return "\r\n ".join(out)


def build_ics(state: dict, nome: str) -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    righe = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//beach-calendar-roma//IT", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{_ics_text(nome)}", "X-WR-TIMEZONE:Europe/Rome",
             "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H"]
    for key, r in sorted(state["eventi"].items(), key=lambda kv: kv[1]["start"]):
        righe += ["BEGIN:VEVENT", f"UID:{key.replace(':', '-')}@beach-calendar-roma", f"DTSTAMP:{stamp}"]
        if r["all_day"]:
            righe += [f"DTSTART;VALUE=DATE:{r['start'].replace('-', '')}",
                      f"DTEND;VALUE=DATE:{r['end'].replace('-', '')}"]
        else:
            fmt = lambda s: dt.datetime.fromisoformat(s).astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            righe += [f"DTSTART:{fmt(r['start'])}", f"DTEND:{fmt(r['end'])}"]
        righe += [f"SUMMARY:{_ics_text(r['summary'])}", f"LOCATION:{_ics_text(r['location'])}",
                  f"DESCRIPTION:{_ics_text(r['description'])}", f"URL:{r['url']}",
                  "TRANSP:TRANSPARENT", "END:VEVENT"]
    righe.append("END:VCALENDAR")
    return "\r\n".join(_ics_fold(l) for l in righe) + "\r\n"


# --- Telegram -----------------------------------------------------------------
def build_messages(nuovi, modificati, rimossi, errori, cal_link: str | None, dove: str = "nel Lazio") -> list[str]:
    blocchi = []
    if nuovi:
        blocchi.append(f"🏐 <b>{len(nuovi)} nuov{'o torneo' if len(nuovi) == 1 else 'i tornei'} {dove}</b>")
        blocchi += ["• " + t.riga_telegram for t in sorted(nuovi, key=sort_key)]
    if modificati:
        blocchi.append(f"\n✏️ <b>Aggiornati ({len(modificati)})</b>")
        blocchi += ["• " + t.riga_telegram for t in sorted(modificati, key=sort_key)]
    if rimossi:
        blocchi.append(f"\n🗑 <b>Non più in elenco ({len(rimossi)})</b>")
        blocchi += ["• " + html.escape(ev.get("summary", "")) for ev in rimossi]
    if errori:
        blocchi.append("\n⚠️ <b>Problemi</b>")
        blocchi += ["• " + html.escape(e) for e in errori]
    if not blocchi:
        return []
    if cal_link and (nuovi or modificati):
        blocchi.append(f'\n<a href="{cal_link}">Apri il calendario</a>')

    # Telegram accetta max 4096 caratteri per messaggio
    msgs, cur = [], ""
    for b in blocchi:
        if len(cur) + len(b) + 1 > 3900:
            msgs.append(cur)
            cur = ""
        cur += b + "\n"
    if cur.strip():
        msgs.append(cur)
    return [m.strip() for m in msgs]


def send_telegram(token: str, chat_id: str, text: str):
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data,
                                 headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


# --- Main ---------------------------------------------------------------------
def main() -> int:
    dry_run = os.environ.get("DRY_RUN") == "1"
    zona = (os.environ.get("FILTRO_ZONA") or "regione").lower()
    if zona not in ZONA_LABEL:
        zona = "regione"
    now = dt.datetime.now(TZ)

    tornei, fonti_ok, errori = [], set(), []
    for nome, fn in (("aibvc", lambda: fetch_aibvc(zona)), ("fipav", lambda: fetch_fipav(zona, now.date()))):
        try:
            res = fn()
            tornei += res
            fonti_ok.add(nome)
            print(f"{nome.upper()}: {len(res)} tornei {ZONA_LABEL[zona]}")
        except Exception as ex:  # una fonte giù non deve bloccare l'altra
            errori.append(f"{nome.upper()} non raggiungibile: {ex}")
            print(f"ERRORE {nome}: {ex}", file=sys.stderr)

    cal_id = os.environ.get("GOOGLE_CALENDAR_ID")
    modalita = os.environ.get("MODALITA") or ("google" if cal_id else "ics")
    cal_link = None

    if modalita == "ics":
        # Nessun Google Cloud: si genera un file .ics a cui Google Calendar si iscrive da URL
        ics_path = os.environ.get("ICS_PATH", "docs/beach-roma.ics")
        state_path = os.environ.get("STATE_PATH", "data/stato.json")
        state = {}
        if os.path.exists(state_path):
            with open(state_path, encoding="utf-8") as f:
                state = json.load(f)
        state, nuovi, modificati, rimossi = sync_state(state, tornei, fonti_ok, now)
        if not dry_run:
            for p in (ics_path, state_path):
                os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
            with open(ics_path, "w", encoding="utf-8", newline="") as f:
                f.write(build_ics(state, os.environ.get("NOME_CALENDARIO", "Beach volley Roma")))
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
        cal_link = os.environ.get("LINK_CALENDARIO") or None
    else:
        sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        sa_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
        if not sa_json and sa_file and os.path.exists(sa_file):
            with open(sa_file, encoding="utf-8") as f:
                sa_json = f.read()
        cal = None
        if not dry_run or sa_json:
            if not sa_json:
                raise SystemExit("Manca la chiave Google: imposta GOOGLE_SERVICE_ACCOUNT_JSON o GOOGLE_SERVICE_ACCOUNT_FILE")
            cal = GoogleCalendar(cal_id, sa_json)
        nuovi, modificati, rimossi = sync(cal, tornei, fonti_ok, now, dry_run)
        if cal_id:
            cal_link = f"https://calendar.google.com/calendar/u/0/r?cid={urllib.parse.quote(cal_id)}"

    print(f"Modalità: {modalita} · Nuovi: {len(nuovi)} · Aggiornati: {len(modificati)} · Rimossi: {len(rimossi)}")
    msgs = build_messages(nuovi, modificati, rimossi, errori, cal_link, ZONA_LABEL[zona])
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    for m in msgs:
        if dry_run or not (token and chat):
            print("\n--- messaggio Telegram ---\n" + m)
        else:
            send_telegram(token, chat, m)

    # Se entrambe le fonti falliscono il job risulta in errore (GitHub ti avvisa via email)
    return 1 if not fonti_ok else 0


if __name__ == "__main__":
    sys.exit(main())
