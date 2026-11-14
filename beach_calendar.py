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
FIPAV_DETTAGLIO = FIPAV_LINK + "#{stagione}/{codice}"   # scheda della singola gara

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
    info: dict = field(default_factory=dict)   # dati strutturati per la pagina riepilogo

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
    righe = [f"🔗 Dettaglio torneo: {link}", "", f"Circuito: AIBVC {cat}", f"Genere: {gen}"]
    if t.get("ageCategory") and t["ageCategory"] != "open":
        righe.append(f"Categoria età: {t['ageCategory']}")
    if t.get("entryFee") is not None:
        righe.append(f"Quota: {t['entryFee']} € / coppia")
    if t.get("registrationDeadline"):
        righe.append("Scadenza iscrizioni: " + fmt_dt(parse_iso(t["registrationDeadline"])))

    return Torneo(
        key=f"aibvc:{t['_id']}",
        titolo=titolo,
        inizio=inizio, fine=fine, luogo=luogo,
        descrizione="\n".join(righe), link=link, fonte="aibvc",
        riga_telegram=(f"{fmt_giorno(inizio)} · <b>AIBVC {cat} {gen}</b> · {html.escape(nome)}"
                       + (f" · 📍{html.escape(citta)}" if citta else "")),
        impronta={"t": titolo, "s": inizio, "e": fine, "l": luogo},
        info={
            "fonte": "AIBVC", "circuito": "CNT" if t.get("tourType") == "cnt" else t.get("level", ""),
            "nome": nome, "categorie": [f"{cat}".strip()], "generi": [gen] if gen else [],
            "citta": re.sub(r"\s*\(.*?\)\s*", "", t.get("city") or "").strip(),
            "impianto": venue.get("name") or "", "indirizzo": venue.get("address") or "",
            "quota": t.get("entryFee"), "sospeso": bool(sospeso),
            "scadenza": parse_iso(t["registrationDeadline"]).isoformat() if t.get("registrationDeadline") else None,
            "links": [{"label": "Dettaglio torneo", "url": link, "genere": gen}],
        },
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
                    for e in gruppo.get("codici", []):
                        eventi.append(dict(e, _stagione=stagione))

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

    def link_gara(e):
        return FIPAV_DETTAGLIO.format(stagione=e.get("_stagione", ""), codice=e.get("codice", ""))
    gare = sorted(gruppo, key=lambda e: (short_tipologia(e.get("tipologia", "")), e.get("genere", "")))
    link = link_gara(gare[0])

    righe = ["🔗 Dettaglio tappa:"]
    for e in gare:
        righe.append(f"• {etichetta(short_tipologia(e.get('tipologia', '')), {(e.get('genere') or '?')[0].upper()})}: {link_gara(e)}")
    righe += ["", f"Circuito: FIPAV – {sezione}", "Categorie: " + ", ".join(categorie)]
    if club:
        righe.append(f"Organizzatore: {club}")
    if comune:
        righe.append(f"Località: {comune}")
    mp = [e.get("montepremi") for e in gruppo if e.get("montepremi_valore")]
    if mp:
        righe.append("Montepremi: " + ", ".join(sorted(set(mp))))

    key_src = f"{e0['data_inizio_iso']}|{e0.get('club_id')}|{e0.get('location_id')}|{e0['menu_eventi']}"
    return Torneo(
        key="fipav:" + hashlib.sha1(key_src.encode()).hexdigest()[:16],
        titolo=titolo,
        inizio=inizio, fine=fine,
        luogo=f"{club}, {comune}" if club else comune,
        descrizione="\n".join(righe), link=link, fonte="fipav",
        riga_telegram=(f"{fmt_giorno(inizio)} · <b>FIPAV {sezione}</b> · {html.escape(', '.join(categorie))}"
                       + (f" · {html.escape(club)}" if club else "")
                       + (f" · 📍{html.escape(citta)}" if citta else "")),
        impronta={"t": titolo, "s": inizio, "e": fine, "c": categorie},
        info={
            "fonte": "FIPAV", "circuito": sezione, "nome": club or sezione, "categorie": categorie,
            "generi": sorted({(e.get("genere") or "?")[0].upper() for e in gruppo}),
            "citta": comune, "provincia": (e0.get("provincia") or "").title(),
            "impianto": "", "indirizzo": "", "organizzatore": club,
            "links": [{"label": etichetta(short_tipologia(e.get("tipologia", "")), {(e.get("genere") or "?")[0].upper()}),
                       "url": link_gara(e), "genere": (e.get("genere") or "?")[0].upper()} for e in gare],
        },
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
        "hash": t.hash, "info": t.info,
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


# --- Pagina riepilogo con filtri (docs/index.html) ---------------------------
def build_pagina(state: dict, zona: str, now: dt.datetime) -> str:
    titolo = {"comune": "a Roma", "provincia": "in provincia di Roma", "regione": "nel Lazio"}.get(zona, "")
    dati = [r for r in state.get("eventi", {}).values() if r.get("info")]
    js = json.dumps(dati, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return (PAGINA_HTML.replace("__ZONA_TITOLO__", titolo)
            .replace("__AGGIORNATO__", now.strftime("%d/%m/%Y alle %H:%M"))
            .replace("__REPO__", os.environ.get("GITHUB_REPOSITORY", ""))
            .replace("__DATI__", js))


PAGINA_HTML = r"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tornei beach volley __ZONA_TITOLO__</title>
<style>
  :root {
    --sand: #f6f1e7; --card: #ffffff; --ink: #1d2a33; --muted: #6b7780; --line: #e6dfd1;
    --sea: #0e6f8f; --sea-soft: #e3f1f5;
    --aibvc: #0e6f8f; --fipav: #c4532b; --fipav-soft: #fbe9e2;
    --radius: 14px;
    --f: #d6337f; --f-soft: #fde3ef; --f-line: #f4bcd6; --f-bg: #fff5f9;
    --m: #1f66c9; --m-soft: #e1ecfb; --m-line: #b8d0f2; --m-bg: #f4f8fe;
  }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         background: var(--sand); color: var(--ink); font-size: 15px; line-height: 1.4; }
  header { background: linear-gradient(135deg, #0e6f8f, #13506a); color: #fff; padding: 22px 18px 18px; }
  header .wrap, main { max-width: 980px; margin: 0 auto; }
  h1 { margin: 0; font-size: 22px; letter-spacing: -.01em; }
  .sub { opacity: .85; font-size: 13px; margin-top: 4px; }
  main { padding: 14px 14px 60px; }

  .filters { background: var(--card); border: 1px solid var(--line); border-radius: var(--radius);
             padding: 14px; margin-top: -10px; position: relative; box-shadow: 0 4px 16px rgba(0,0,0,.05); }
  .row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 10px; }
  .row:last-child { margin-bottom: 0; }
  .lbl { font-size: 12px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; min-width: 72px; }
  input[type=search], input[type=date], select {
    font: inherit; padding: 8px 10px; border: 1px solid var(--line); border-radius: 10px; background: #fff; color: var(--ink); }
  input[type=search] { flex: 1; min-width: 200px; }
  .chip { font: inherit; font-size: 13px; padding: 6px 12px; border-radius: 999px; border: 1px solid var(--line);
          background: #fff; color: var(--ink); cursor: pointer; user-select: none; }
  .chip[aria-pressed=true] { background: var(--sea); border-color: var(--sea); color: #fff; }
  .chip.fipav[aria-pressed=true] { background: #37474f; border-color: #37474f; }
  .reset { margin-left: auto; background: none; border: none; color: var(--sea); font: inherit; font-size: 13px; cursor: pointer; text-decoration: underline; }
  .count { margin: 16px 2px 6px; color: var(--muted); font-size: 13px; }

  .month { margin: 22px 2px 8px; font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
  .card { display: flex; gap: 14px; background: var(--card); border: 1px solid var(--line); border-radius: var(--radius);
          padding: 12px 14px 12px 20px; margin-bottom: 8px; position: relative; overflow: hidden; }
  .card::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 6px; background: var(--line); }
  .card.gF::before { background: var(--f); }
  .card.gM::before { background: var(--m); }
  .card.gMF::before { background: linear-gradient(var(--m) 50%, var(--f) 50%); }
  .badge.g-F { background: var(--f-soft); color: var(--f); }
  .badge.g-M { background: var(--m-soft); color: var(--m); }
  .chip.g-F[aria-pressed=true] { background: var(--f); border-color: var(--f); }
  .chip.g-M[aria-pressed=true] { background: var(--m); border-color: var(--m); }
  .legend { display: flex; gap: 14px; font-size: 12px; color: var(--muted); margin-left: 6px; }
  .legend i { display: inline-block; width: 10px; height: 10px; border-radius: 3px; margin-right: 5px; vertical-align: -1px; }
  .card.past { opacity: .55; }
  .date { flex: 0 0 54px; text-align: center; border-right: 1px solid var(--line); padding-right: 12px; }
  .date .d { font-size: 24px; font-weight: 700; line-height: 1; }
  .date .w { font-size: 12px; color: var(--muted); text-transform: uppercase; margin-top: 3px; }
  .date .t { font-size: 12px; color: var(--sea); font-weight: 600; margin-top: 4px; }
  .body { flex: 1; min-width: 0; }
  .top { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-bottom: 4px; }
  .badge { font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 6px; background: #eceff1; color: #37474f; }
  .badge.fipav { background: #37474f; color: #fff; }
  .badge.warn { background: #fde2e2; color: #b42318; }
  .name { font-weight: 650; font-size: 16px; }
  .cats { font-size: 13px; color: var(--ink); margin-top: 2px; }
  .where { font-size: 13px; color: var(--muted); margin-top: 2px; }
  .links { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  .links a { font-size: 12px; text-decoration: none; color: var(--sea); border: 1px solid #b9dbe5; border-radius: 8px; padding: 4px 9px; background: #f6fbfc; }
  .links a.g-F { color: var(--f); border-color: var(--f-line); background: var(--f-bg); }
  .links a.g-M { color: var(--m); border-color: var(--m-line); background: var(--m-bg); }
  .links a:hover { filter: brightness(.95); }
  header .wrap { display: flex; gap: 12px; align-items: flex-start; justify-content: space-between; }
  .agg { flex: none; font: inherit; font-size: 13px; font-weight: 600; color: #fff; background: rgba(255,255,255,.16);
         border: 1px solid rgba(255,255,255,.35); border-radius: 10px; padding: 8px 12px; cursor: pointer; white-space: nowrap; }
  .agg:disabled { opacity: .6; cursor: default; }
  .panel { background: #fff; color: var(--ink); border-radius: 12px; padding: 14px; margin-top: 12px; font-size: 14px; display: none; }
  .panel.on { display: block; }
  .panel p { margin: 0 0 10px; }
  .panel input { width: 100%; font: inherit; padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; margin-bottom: 8px; }
  .panel .btns { display: flex; flex-wrap: wrap; gap: 8px; }
  .panel button, .panel a.btn { font: inherit; font-size: 13px; padding: 7px 12px; border-radius: 8px; border: 1px solid var(--sea);
         background: var(--sea); color: #fff; cursor: pointer; text-decoration: none; }
  .panel .ghost { background: #fff; color: var(--sea); }
  .panel small { color: var(--muted); display: block; margin-top: 8px; }
  .stato { font-size: 13px; margin-top: 8px; opacity: .95; min-height: 1em; }
  .empty { text-align: center; color: var(--muted); padding: 40px 10px; }
  footer { text-align: center; color: var(--muted); font-size: 12px; margin-top: 30px; }
  footer a { color: var(--muted); }
  @media (max-width: 560px) {
    .lbl { min-width: 100%; }
    .date { flex-basis: 46px; padding-right: 10px; }
    .name { font-size: 15px; }
  }
</style>
</head>
<body>
<header><div style="max-width:980px;margin:0 auto">
  <div class="wrap">
    <div>
      <h1>🏐 Tornei beach volley __ZONA_TITOLO__</h1>
      <div class="sub">AIBVC (L1, L2, CNT) e FIPAV (Assoluto, Serie Beach, Società) · aggiornato il __AGGIORNATO__</div>
    </div>
    <button class="agg" id="agg">🔄 Aggiorna ora</button>
  </div>
  <div class="stato" id="stato"></div>
  <div class="panel" id="panel">
    <p><b>Aggiornamento manuale</b></p>
    <p>Per avviarlo da qui con un tocco, incolla una volta il tuo <i>token GitHub</i> (resta salvato solo su questo dispositivo).
       In alternativa apri GitHub e premi <b>Run workflow</b>.</p>
    <input type="password" id="tok" placeholder="github_pat_…" autocomplete="off">
    <div class="btns">
      <button id="salvaTok">Salva e aggiorna</button>
      <a class="btn ghost" id="apriGh" target="_blank" rel="noopener">Apri GitHub</a>
      <button class="ghost" id="chiudi">Chiudi</button>
    </div>
    <small id="tokInfo"></small>
  </div>
</div></header>

<main>
  <section class="filters" aria-label="Filtri">
    <div class="row">
      <input type="search" id="q" placeholder="Cerca impianto, città, organizzatore, categoria…" aria-label="Cerca">
    </div>
    <div class="row" id="periodo">
      <span class="lbl">Periodo</span>
      <button class="chip" data-p="7">7 giorni</button>
      <button class="chip" data-p="30">30 giorni</button>
      <button class="chip" data-p="90">3 mesi</button>
      <button class="chip" data-p="all">Tutti i prossimi</button>
      <button class="chip" data-p="weekend">Prossimo weekend</button>
      <span style="display:flex;gap:6px;align-items:center">
        <input type="date" id="dal" aria-label="Dal"> – <input type="date" id="al" aria-label="Al">
      </span>
    </div>
    <div class="row" id="circuiti"><span class="lbl">Circuito</span></div>
    <div class="row" id="generi"><span class="lbl">Genere</span></div>
    <div class="row">
      <span class="lbl">Città</span>
      <select id="citta"><option value="">Tutte</option></select>
      <button class="reset" id="reset">Azzera filtri</button>
    </div>
  </section>

  <div class="count" id="count"></div>
  <div id="list"></div>
  <footer>Fonti: <a href="https://aibvc.app/" target="_blank" rel="noopener">aibvc.app</a> ·
    <a href="https://www.federvolley.it/campionati/beach-volley/eventi" target="_blank" rel="noopener">federvolley.it</a></footer>
</main>

<script>
const DATI = __DATI__;
const OGGI = new Date(); OGGI.setHours(0,0,0,0);
const GIORNI = ["dom","lun","mar","mer","gio","ven","sab"];
const MESI = ["gennaio","febbraio","marzo","aprile","maggio","giugno","luglio","agosto","settembre","ottobre","novembre","dicembre"];
const CIRCUITI = [
  ["AIBVC","L1","AIBVC L1"],["AIBVC","L2","AIBVC L2"],["AIBVC","CNT","AIBVC CNT"],
  ["FIPAV","Assoluto","FIPAV Assoluto"],["FIPAV","Serie Beach","FIPAV Serie Beach"],["FIPAV","Società","FIPAV Società"]];
const GENERI = [["M","Maschile"],["F","Femminile"]];

// Normalizza i dati
const T = DATI.map(r => {
  const i = r.info || {};
  const s = r.all_day ? new Date(r.start + "T00:00:00") : new Date(r.start);
  const eExcl = r.all_day ? new Date(r.end + "T00:00:00") : new Date(r.end);
  const e = r.all_day ? new Date(eExcl.getTime() - 86400000) : eExcl;
  const cKey = (i.fonte || "") + "|" + (i.circuito || "");
  const testo = [i.nome, i.citta, i.impianto, i.indirizzo, i.organizzatore, (i.categorie||[]).join(" "), i.fonte, i.circuito, r.summary]
      .filter(Boolean).join(" ").toLowerCase();
  return { r, i, s, e, cKey, testo };
}).sort((a,b) => a.s - b.s);

// --- stato filtri (ricordato nel browser, se possibile)
const DEF = { q:"", p:"all", dal:"", al:"", circ:[], gen:[], citta:"" };
let F = { ...DEF };
try { F = { ...DEF, ...JSON.parse(localStorage.getItem("bv-filtri") || "{}") }; } catch (e) {}
const salva = () => { try { localStorage.setItem("bv-filtri", JSON.stringify(F)); } catch (e) {} };

// --- costruzione controlli
const $ = id => document.getElementById(id);
function chip(parent, label, pressed, onClick, extra) {
  const b = document.createElement("button");
  b.className = "chip" + (extra ? " " + extra : ""); b.textContent = label;
  b.setAttribute("aria-pressed", pressed ? "true" : "false");
  b.onclick = () => { onClick(); render(); };
  parent.appendChild(b); return b;
}
function buildChips() {
  const c = $("circuiti"); c.querySelectorAll(".chip").forEach(x => x.remove());
  const presenti = new Set(T.map(t => t.cKey));
  CIRCUITI.filter(([f,k]) => presenti.has(f+"|"+k)).forEach(([f,k,l]) => {
    const key = f+"|"+k;
    chip(c, l, F.circ.includes(key), () => {
      F.circ = F.circ.includes(key) ? F.circ.filter(x => x !== key) : [...F.circ, key];
    }, f === "FIPAV" ? "fipav" : "");
  });
  const g = $("generi"); g.querySelectorAll(".chip").forEach(x => x.remove());
  GENERI.forEach(([k,l]) => chip(g, l, F.gen.includes(k), () => {
    F.gen = F.gen.includes(k) ? F.gen.filter(x => x !== k) : [...F.gen, k];
  }, "g-" + k));
  if (!g.querySelector(".legend")) g.insertAdjacentHTML("beforeend",
    '<span class="legend"><span><i style="background:var(--m)"></i>maschile</span><span><i style="background:var(--f)"></i>femminile</span></span>');
  else g.appendChild(g.querySelector(".legend"));
  document.querySelectorAll("#periodo .chip").forEach(b =>
    b.setAttribute("aria-pressed", (!F.dal && !F.al && F.p === b.dataset.p) ? "true" : "false"));
}
const citta = [...new Set(T.map(t => t.i.citta).filter(Boolean))].sort((a,b) => a.localeCompare(b, "it"));
citta.forEach(c => { const o = document.createElement("option"); o.value = o.textContent = c; $("citta").appendChild(o); });

document.querySelectorAll("#periodo .chip").forEach(b => b.onclick = () => {
  F.p = b.dataset.p; F.dal = ""; F.al = ""; $("dal").value = ""; $("al").value = ""; render();
});
$("q").oninput = e => { F.q = e.target.value; render(); };
$("dal").onchange = e => { F.dal = e.target.value; render(); };
$("al").onchange = e => { F.al = e.target.value; render(); };
$("citta").onchange = e => { F.citta = e.target.value; render(); };
$("reset").onclick = () => { F = { ...DEF, circ: [], gen: [] }; syncInputs(); render(); };
function syncInputs() { $("q").value = F.q; $("dal").value = F.dal; $("al").value = F.al;
  $("citta").value = citta.includes(F.citta) ? F.citta : ""; }

// --- periodo
function intervallo() {
  if (F.dal || F.al) {
    return [F.dal ? new Date(F.dal + "T00:00:00") : new Date(0), F.al ? new Date(F.al + "T23:59:59") : new Date(8.64e15)];
  }
  const da = new Date(OGGI);
  if (F.p === "weekend") {
    const d = OGGI.getDay();                       // 0 = domenica, 6 = sabato
    const inizio = new Date(OGGI);
    if (d !== 0 && d !== 6) inizio.setDate(OGGI.getDate() + (6 - d));   // prossimo sabato
    const fine = new Date(inizio);
    if (inizio.getDay() === 6) fine.setDate(inizio.getDate() + 1);       // fino a domenica
    fine.setHours(23,59,59);
    return [inizio, fine];
  }
  if (F.p === "all") return [da, new Date(8.64e15)];
  const a = new Date(OGGI); a.setDate(a.getDate() + Number(F.p)); a.setHours(23,59,59);
  return [da, a];
}

// --- render
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;" }[c]));
const hhmm = d => d.toLocaleTimeString("it-IT", { hour: "2-digit", minute: "2-digit" });
function render() {
  salva(); buildChips();
  const [da, a] = intervallo();
  const q = F.q.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const out = T.filter(t =>
    t.e >= da && t.s <= a &&
    (!F.circ.length || F.circ.includes(t.cKey)) &&
    (!F.gen.length || (t.i.generi || []).some(g => F.gen.includes(g))) &&
    (!F.citta || t.i.citta === F.citta) &&
    q.every(w => t.testo.includes(w)));

  $("count").textContent = out.length === 1 ? "1 torneo" : out.length + " tornei";
  const list = $("list"); list.innerHTML = "";
  if (!out.length) { list.innerHTML = '<div class="empty">Nessun torneo con questi filtri.</div>'; return; }

  let mese = "";
  for (const t of out) {
    const m = MESI[t.s.getMonth()] + " " + t.s.getFullYear();
    if (m !== mese) { mese = m; list.insertAdjacentHTML("beforeend", `<div class="month">${m}</div>`); }
    const i = t.i, f = i.fonte === "FIPAV";
    const multi = t.e.toDateString() !== t.s.toDateString();
    const dove = [i.impianto, i.indirizzo, i.citta && (i.provincia && i.provincia !== i.citta ? `${i.citta} (${i.provincia})` : i.citta)]
                 .filter(Boolean).join(" · ");
    const links = (i.links || [{ label: "Dettaglio", url: t.r.url }])
                 .map(l => `<a class="${l.genere ? "g-" + esc(l.genere) : ""}" href="${esc(l.url)}" target="_blank" rel="noopener">🔗 ${esc(l.label)}</a>`).join("");
    const gs = (i.generi || []).filter(g => g === "M" || g === "F").sort().reverse();   // M prima di F
    const gCls = gs.length === 2 ? "gMF" : gs.length === 1 ? "g" + gs[0] : "";
    const extra = [];
    if (i.quota != null) extra.push(`${i.quota} € / coppia`);
    if (i.scadenza) { const sc = new Date(i.scadenza);
      extra.push(`iscrizioni entro ${sc.getDate()}/${sc.getMonth()+1} ore ${hhmm(sc)}`); }
    list.insertAdjacentHTML("beforeend", `
      <article class="card ${f ? "f" : ""} ${gCls} ${t.e < OGGI ? "past" : ""}">
        <div class="date">
          <div class="d">${t.s.getDate()}</div>
          <div class="w">${GIORNI[t.s.getDay()]}${multi ? "–" + GIORNI[t.e.getDay()] : ""}</div>
          ${t.r.all_day ? "" : `<div class="t">${hhmm(t.s)}</div>`}
        </div>
        <div class="body">
          <div class="top">
            <span class="badge ${f ? "fipav" : ""}">${esc(i.fonte || "")} ${esc(i.circuito || "")}</span>
            ${gs.map(g => `<span class="badge g-${g}">${g === "F" ? "Femminile" : "Maschile"}</span>`).join("")}
            ${i.sospeso ? '<span class="badge warn">SOSPESO</span>' : ""}
          </div>
          <div class="name">${esc(i.nome || t.r.summary)}</div>
          ${f ? `<div class="cats">${esc((i.categorie || []).join(", "))}</div>` : ""}
          <div class="where">📍 ${esc(dove || t.r.location)}</div>
          ${extra.length ? `<div class="where">${esc(extra.join(" · "))}</div>` : ""}
          <div class="links">${links}</div>
        </div>
      </article>`);
  }
}
syncInputs(); render();

// --- Aggiornamento manuale (avvia il workflow GitHub "sync.yml") ----------------
(function () {
  let repo = "__REPO__";
  if (!repo && location.hostname.endsWith(".github.io"))
    repo = location.hostname.split(".")[0] + "/" + (location.pathname.split("/")[1] || "");
  const API = "https://api.github.com/repos/" + repo + "/actions";
  const WF = "sync.yml";
  const ghLink = "https://github.com/" + repo + "/actions/workflows/" + WF;
  const btn = $("agg"), stato = $("stato"), panel = $("panel");
  $("apriGh").href = ghLink;
  if (!repo) { btn.style.display = "none"; return; }

  const getTok = () => { try { return localStorage.getItem("bv-token") || ""; } catch (e) { return ""; } };
  const setTok = t => { try { t ? localStorage.setItem("bv-token", t) : localStorage.removeItem("bv-token"); } catch (e) {} };
  const say = t => { stato.textContent = t; };
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  function apriPannello(msg) {
    $("tokInfo").innerHTML = msg || (getTok() ? 'Token salvato su questo dispositivo. <a href="#" id="dimentica">Rimuovilo</a>' : "");
    const d = document.getElementById("dimentica");
    if (d) d.onclick = e => { e.preventDefault(); setTok(""); apriPannello("Token rimosso."); };
    panel.classList.add("on");
  }
  $("chiudi").onclick = () => panel.classList.remove("on");
  $("salvaTok").onclick = () => {
    const t = $("tok").value.trim();
    if (!t) { apriPannello("Incolla il token oppure usa “Apri GitHub”."); return; }
    setTok(t); $("tok").value = ""; panel.classList.remove("on"); avvia();
  };
  btn.onclick = () => getTok() ? avvia() : apriPannello();

  async function avvia() {
    const tok = getTok();
    const h = { "Accept": "application/vnd.github+json", "Authorization": "Bearer " + tok, "X-GitHub-Api-Version": "2022-11-28" };
    btn.disabled = true; say("Avvio dell'aggiornamento…");
    const t0 = new Date(Date.now() - 60000).toISOString();
    try {
      const r = await fetch(`${API}/workflows/${WF}/dispatches`, { method: "POST", headers: h, body: JSON.stringify({ ref: "main" }) });
      if (r.status === 401 || r.status === 403 || r.status === 404) {
        btn.disabled = false; say(""); setTok("");
        apriPannello("Il token non è valido o non ha il permesso “Actions: Read and write” su questo repository. Incollane uno nuovo oppure usa “Apri GitHub”.");
        return;
      }
      if (!r.ok) throw new Error("risposta " + r.status);
      say("Aggiornamento avviato: controllo dei siti AIBVC e FIPAV in corso (circa 1 minuto)…");
      // segue l'esecuzione fino alla fine
      let run = null;
      for (let i = 0; i < 40; i++) {
        await sleep(i ? 8000 : 5000);
        const q = await fetch(`${API}/workflows/${WF}/runs?event=workflow_dispatch&per_page=5&created=%3E%3D${encodeURIComponent(t0)}`, { headers: h });
        if (!q.ok) continue;
        run = ((await q.json()).workflow_runs || [])[0];
        if (run && run.status === "completed") break;
      }
      if (!run || run.status !== "completed") { say("L'aggiornamento è ancora in corso. Ricarica la pagina tra qualche minuto."); btn.disabled = false; return; }
      if (run.conclusion !== "success") {
        say("L'aggiornamento non è riuscito. "); const a = document.createElement("a");
        a.href = run.html_url; a.target = "_blank"; a.textContent = "Vedi i dettagli su GitHub"; a.style.color = "#fff";
        stato.appendChild(a); btn.disabled = false; return;
      }
      // attende che GitHub Pages pubblichi la nuova versione della pagina
      say("Fatto! Le eventuali novità arrivano su Telegram. Pubblico la pagina aggiornata…");
      const vecchia = "__AGGIORNATO__";
      for (let i = 0; i < 24; i++) {
        await sleep(10000);
        try {
          const txt = await (await fetch(location.pathname + "?v=" + Date.now(), { cache: "no-store" })).text();
          const m = txt.match(/aggiornato il ([^<]+)<\/div>/);
          if (m && m[1].trim() !== vecchia) { location.replace(location.pathname + "?v=" + Date.now()); return; }
        } catch (e) {}
      }
      say("Aggiornamento completato. Se la pagina non è cambiata, ricaricala tra un minuto.");
      btn.disabled = false;
    } catch (e) {
      say("Impossibile avviare l'aggiornamento (" + e.message + "). Prova da GitHub.");
      apriPannello(); btn.disabled = false;
    }
  }
})();
</script>
</body>
</html>
"""


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
            pagina = os.path.join(os.path.dirname(ics_path) or ".", "index.html")
            with open(pagina, "w", encoding="utf-8") as f:
                f.write(build_pagina(state, zona, now))
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
