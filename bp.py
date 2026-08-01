#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bp.py — pipeline di pubblicazione automatica di Breaking Past su Instagram.

Comandi:
    prepare        PNG -> JPEG, validazione specifiche Instagram, alt text, caption, queue.json
    status         stato della coda (cosa e' pronto, cosa e' gia' uscito, cosa manca)
    preflight      controlli pre-volo: token, permessi, URL pubblici, quota, prossima edizione
    publish        pubblica l'edizione del giorno alle 20:30:00 Europe/Rome (idempotente)
    refresh-token  rinnova il long-lived token e lo riscrive nel secret del repo
    quota          quota di pubblicazione residua nelle 24 ore

Decisioni di progetto cablate qui dentro:
  - Instagram API with Instagram Login (graph.instagram.com), app in modalita' sviluppo.
  - Le immagini DEVONO essere JPEG e servite da URL pubblici (GitHub Pages):
    e' Meta a scaricarle dai propri server, non noi a caricarle.
  - Le caption sono ESATTAMENTE quelle di caption.txt, hashtag compresi.
  - L'alt text viene letto da alt_text.txt e caricato slide per slide.
  - Il cron di GitHub Actions slitta: il job parte in anticipo e questo script
    dorme fino alle 20:30:00 esatte calcolate da Python (ora legale inclusa).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------
# Costanti di progetto
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
SOURCE_DIR = ROOT / "episodi"          # cartelle Episodio_NNN_... come in 07_Output
DOCS_DIR = ROOT / "docs"               # radice di GitHub Pages
MEDIA_DIR = DOCS_DIR / "media"         # JPEG serviti pubblicamente
QUEUE_PATH = ROOT / "queue.json"

TZ = ZoneInfo("Europe/Rome")
PUBLISH_HOUR = int(os.environ.get("PUBLISH_HOUR", "20"))
PUBLISH_MINUTE = int(os.environ.get("PUBLISH_MINUTE", "30"))

GRAPH_HOST = "https://graph.instagram.com"
GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v24.0")

# Specifiche Instagram per le immagini del feed (carosello)
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "90"))
MIN_WIDTH = 320
MAX_WIDTH = 1440
MIN_RATIO = 0.8            # 4:5
MAX_RATIO = 1.91           # 1.91:1
MAX_BYTES = 8 * 1024 * 1024
MAX_CAPTION_CHARS = 2200
MAX_HASHTAGS = 30
MAX_ALT_CHARS = 1000
CAROUSEL_MIN = 2
CAROUSEL_MAX = 10

# Attesa massima per il passaggio di un container a FINISHED
CONTAINER_TIMEOUT_S = int(os.environ.get("CONTAINER_TIMEOUT_S", "300"))
CONTAINER_POLL_S = int(os.environ.get("CONTAINER_POLL_S", "5"))

# Tolleranza: se il job parte DOPO le 20:30 di quanto sotto, pubblica comunque
# (rete di sicurezza). Oltre, si ferma e segnala.
#
# Misurato sul campo il 31/07/2026: il cron di GitHub Actions e' partito con
# 3h53m di ritardo. Con la vecchia soglia di 90 minuti il job si rifiutava di
# pubblicare e l'edizione saltava. Meglio un'edizione in ritardo che nessuna
# edizione: 240 minuti coprono lo slittamento peggiore osservato.
LATE_TOLERANCE_MIN = int(os.environ.get("LATE_TOLERANCE_MIN", "240"))

# Un job GitHub non puo' durare piu' di 6 ore, quindi non si puo' partire la
# mattina e dormire fino a sera. I tentativi sono percio' distribuiti nel
# pomeriggio: quello che si sveglia troppo presto rinuncia subito e lascia il
# turno al successivo. Cosi' gli orari anticipati fanno da rete quando il cron
# slitta, senza sprecare un job da sette ore quando invece e' puntuale.
EARLY_SKIP_MIN = int(os.environ.get("EARLY_SKIP_MIN", "240"))

SLIDE_RE = re.compile(r"^\s*[—\-–]{0,2}\s*SLIDE\s+(\d+)\s*[—\-–]{0,2}\s*$", re.IGNORECASE)


# --------------------------------------------------------------------------
# Utilita'
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[{datetime.now(TZ):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def die(msg: str, code: int = 1) -> None:
    print(f"ERRORE: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def env(name: str, required: bool = True, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if required and not v:
        die(f"variabile d'ambiente mancante: {name}")
    return v or ""


def truthy(v: str | None) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "si", "sì", "on"}


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return re.sub(r"-{2,}", "-", s)


def load_queue() -> dict:
    if not QUEUE_PATH.exists():
        return {"progetto": "BREAKING PAST", "aggiornato": None, "episodi": []}
    with QUEUE_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def save_queue(q: dict) -> None:
    q["aggiornato"] = datetime.now(TZ).isoformat(timespec="seconds")
    tmp = QUEUE_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(q, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(QUEUE_PATH)


def api_get(path: str, params: dict) -> dict:
    url = f"{GRAPH_HOST}/{GRAPH_VERSION}/{path.lstrip('/')}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="GET")
    return _call(req, url)


def api_post(path: str, params: dict) -> dict:
    url = f"{GRAPH_HOST}/{GRAPH_VERSION}/{path.lstrip('/')}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    return _call(req, url)


def _call(req: urllib.request.Request, url: str) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code} su {url.split('?')[0]}\n{body}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"rete non raggiungibile su {url.split('?')[0]}: {e.reason}") from None


def head_ok(url: str) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=45) as r:
            ct = r.headers.get("Content-Type", "")
            return (200 <= r.status < 300 and "image" in ct), f"{r.status} {ct}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def resolve_user_id(token: str, configured: str) -> str:
    """L'ID giusto e' quello che il token stesso dichiara.

    La console Meta mostra l'ID del profilo professionale (flusso Facebook Login),
    che NON coincide con l'ID applicativo restituito da /me sul flusso
    Instagram Login. Chiamare l'ID sbagliato fa fallire la pubblicazione.
    Qui chiediamo al token chi e', e usiamo quello.
    """
    try:
        me = api_get("me", {"fields": "id,username", "access_token": token})
        real = str(me.get("id") or "")
    except Exception as e:  # noqa: BLE001
        log(f"AVVISO: /me non raggiungibile ({e}). Uso IG_USER_ID come da configurazione.")
        return configured
    if not real:
        return configured
    if configured and configured != real:
        log(f"IG_USER_ID configurato ({configured}) diverso da quello del token ({real}): "
            f"uso quello del token, @{me.get('username')}")
    return real


# --------------------------------------------------------------------------
# Lettura dei sorgenti di un episodio
# --------------------------------------------------------------------------

def parse_alt_text(path: Path, n_slides: int) -> list[str]:
    """Spezza alt_text.txt nei blocchi '— SLIDE N —'. Ne pretende esattamente n_slides."""
    if not path.exists():
        raise ValueError(f"alt_text.txt mancante in {path.parent.name}")
    blocks: dict[int, list[str]] = {}
    current: int | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = SLIDE_RE.match(line)
        if m:
            current = int(m.group(1))
            blocks[current] = []
            continue
        if current is not None:
            blocks[current].append(line)
    if not blocks:
        raise ValueError(f"alt_text.txt senza marcatori '— SLIDE N —' in {path.parent.name}")
    missing = [i for i in range(1, n_slides + 1) if i not in blocks]
    if missing:
        raise ValueError(f"alt text mancante per le slide {missing} in {path.parent.name}")
    out = []
    for i in range(1, n_slides + 1):
        txt = "\n".join(blocks[i]).strip()
        if not txt:
            raise ValueError(f"alt text vuoto per la slide {i} in {path.parent.name}")
        if len(txt) > MAX_ALT_CHARS:
            raise ValueError(
                f"alt text slide {i} di {path.parent.name}: {len(txt)} caratteri, "
                f"il limite Instagram e' {MAX_ALT_CHARS}"
            )
        out.append(txt)
    return out


def read_caption(path: Path) -> str:
    if not path.exists():
        raise ValueError(f"caption.txt mancante in {path.parent.name}")
    cap = path.read_text(encoding="utf-8").strip()
    if not cap:
        raise ValueError(f"caption.txt vuoto in {path.parent.name}")
    if len(cap) > MAX_CAPTION_CHARS:
        raise ValueError(
            f"caption di {path.parent.name}: {len(cap)} caratteri, limite {MAX_CAPTION_CHARS}"
        )
    n_tags = len(re.findall(r"(?<!\w)#\w+", cap))
    if n_tags > MAX_HASHTAGS:
        raise ValueError(f"caption di {path.parent.name}: {n_tags} hashtag, limite {MAX_HASHTAGS}")
    return cap


def split_hashtags(caption: str) -> tuple[str, str]:
    """Separa la coda di hashtag dal corpo della caption. Usato solo se
    HASHTAGS_IN_FIRST_COMMENT=true. Di default l'interruttore e' spento e la
    caption resta esattamente quella di caption.txt."""
    lines = caption.rstrip().split("\n")
    tail = []
    while lines and (not lines[-1].strip() or re.fullmatch(r"(\s*#\w+)+\s*", lines[-1])):
        tail.insert(0, lines.pop())
    body = "\n".join(lines).rstrip()
    tags = "\n".join(t for t in tail if t.strip()).strip()
    return (body, tags) if tags else (caption, "")


def episode_dirs() -> list[Path]:
    if not SOURCE_DIR.exists():
        die(f"cartella sorgente assente: {SOURCE_DIR}")
    return sorted(p for p in SOURCE_DIR.iterdir() if p.is_dir() and p.name.startswith("Episodio_"))


def parse_dir_name(d: Path) -> tuple[str, str, str]:
    """Episodio_001_2026-08-01_Il_debutto_di_MTV -> ('001', '2026-08-01', 'Il debutto di MTV')"""
    m = re.match(r"Episodio_(\d{3})_(\d{4}-\d{2}-\d{2})_(.+)$", d.name)
    if not m:
        raise ValueError(f"nome cartella non conforme: {d.name}")
    return m.group(1), m.group(2), m.group(3).replace("_", " ")


# --------------------------------------------------------------------------
# prepare
# --------------------------------------------------------------------------

def cmd_prepare(args) -> int:
    try:
        from PIL import Image
    except ImportError:
        die("Pillow non installato. Esegui: pip install -r requirements.txt")

    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / ".nojekyll").touch()

    queue = load_queue()
    by_slug = {e["slug"]: e for e in queue.get("episodi", [])}
    base_url = os.environ.get("PAGES_BASE_URL", "").rstrip("/")
    if not base_url:
        log("AVVISO: PAGES_BASE_URL non impostata — gli URL in queue.json restano relativi.")

    nuovi = aggiornati = invariati = 0
    errori: list[str] = []

    for d in episode_dirs():
        try:
            num, data_pub, titolo = parse_dir_name(d)
            slug = f"{num}-{slugify(titolo)}"
            pngs = sorted(d.glob("*_slide_*.png"), key=lambda p: int(re.search(r"_slide_(\d+)", p.name).group(1)))
            if not pngs:
                raise ValueError(f"nessun PNG in {d.name}")
            if not (CAROUSEL_MIN <= len(pngs) <= CAROUSEL_MAX):
                raise ValueError(f"{len(pngs)} slide in {d.name}: il carosello ne ammette {CAROUSEL_MIN}-{CAROUSEL_MAX}")

            caption = read_caption(d / "caption.txt")
            alts = parse_alt_text(d / "alt_text.txt", len(pngs))

            out_dir = MEDIA_DIR / slug
            out_dir.mkdir(parents=True, exist_ok=True)

            slides = []
            for i, png in enumerate(pngs, start=1):
                jpg = out_dir / f"slide_{i}.jpg"
                with Image.open(png) as im:
                    if im.size[0] < MIN_WIDTH:
                        raise ValueError(f"{png.name}: larghezza {im.size[0]}px sotto il minimo {MIN_WIDTH}")
                    ratio = im.size[0] / im.size[1]
                    if not (MIN_RATIO - 1e-6 <= ratio <= MAX_RATIO + 1e-6):
                        raise ValueError(
                            f"{png.name}: rapporto {ratio:.3f} fuori dall'intervallo Instagram "
                            f"{MIN_RATIO}–{MAX_RATIO}"
                        )
                    rgb = im.convert("RGB")
                    if rgb.size[0] > MAX_WIDTH:
                        h = round(rgb.size[1] * MAX_WIDTH / rgb.size[0])
                        rgb = rgb.resize((MAX_WIDTH, h), Image.LANCZOS)
                    rgb.save(jpg, "JPEG", quality=JPEG_QUALITY, subsampling=0,
                             optimize=True, progressive=False)
                    w, h = rgb.size
                size = jpg.stat().st_size
                if size > MAX_BYTES:
                    raise ValueError(f"{jpg.name}: {size/1e6:.1f} MB, oltre il limite di 8 MB")
                rel = f"media/{slug}/{jpg.name}"
                slides.append({
                    "n": i,
                    "file": rel,
                    "url": f"{base_url}/{rel}" if base_url else rel,
                    "alt_text": alts[i - 1],
                    "w": w, "h": h, "bytes": size,
                })

            entry = {
                "n": num,
                "slug": slug,
                "titolo": titolo,
                "cartella": d.name,
                "data_pubblicazione": data_pub,
                "ora_pubblicazione": f"{PUBLISH_HOUR:02d}:{PUBLISH_MINUTE:02d}",
                "timezone": "Europe/Rome",
                "caption": caption,
                "slides": slides,
                "posted": False,
                "posted_at": None,
                "media_id": None,
                "permalink": None,
            }

            prev = by_slug.get(slug)
            if prev:
                # l'idempotenza vince su tutto: quello che e' uscito non si tocca
                entry["posted"] = prev.get("posted", False)
                entry["posted_at"] = prev.get("posted_at")
                entry["media_id"] = prev.get("media_id")
                entry["permalink"] = prev.get("permalink")
                if prev == entry:
                    invariati += 1
                else:
                    aggiornati += 1
                by_slug[slug] = entry
            else:
                nuovi += 1
                by_slug[slug] = entry

            log(f"OK  {slug}: {len(slides)} JPEG · caption {len(caption)} car. · alt text {len(alts)}/{len(alts)}")

        except Exception as e:  # noqa: BLE001
            errori.append(f"{d.name}: {e}")
            log(f"KO  {d.name}: {e}")

    queue["episodi"] = sorted(by_slug.values(), key=lambda e: (e["data_pubblicazione"], e["n"]))
    save_queue(queue)
    write_index_html(queue, base_url)

    log(f"prepare: {nuovi} nuovi · {aggiornati} aggiornati · {invariati} invariati · {len(errori)} errori")
    if errori:
        for e in errori:
            print(f"  - {e}", file=sys.stderr)
        return 1
    return 0


def write_index_html(queue: dict, base_url: str) -> None:
    """Indice statico su Pages: serve a Meta come prova che i file esistono e a noi come cruscotto."""
    rows = []
    for e in queue.get("episodi", []):
        stato = "pubblicata" if e.get("posted") else "in coda"
        thumbs = "".join(
            f'<a href="{s["file"]}"><img src="{s["file"]}" alt="{s["n"]}" loading="lazy"></a>'
            for s in e.get("slides", [])
        )
        rows.append(
            f'<article><h2>{e["n"]} · {e["titolo"]}</h2>'
            f'<p class="meta">{e["data_pubblicazione"]} · {e["ora_pubblicazione"]} '
            f'Europe/Rome · <span class="s {"ok" if e.get("posted") else "wait"}">{stato}</span></p>'
            f'<div class="thumbs">{thumbs}</div></article>'
        )
    html = f"""<!doctype html>
<html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>Breaking Past — media pubblici</title>
<style>
:root{{--nero:#121216;--carta:#F4F1E8;--rosso:#C8102E;--blu:#155E8B;--meta:#5F5F66;--linea:#D9D3C4}}
*{{box-sizing:border-box}}
body{{margin:0;padding:32px 20px 64px;background:var(--carta);color:var(--nero);
font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}}
header{{max-width:1080px;margin:0 auto 32px}}
h1{{margin:0;font-size:28px;letter-spacing:-.02em}}
h1 span{{background:var(--nero);color:#fff;padding:2px 8px}}
h1 em{{background:var(--rosso);color:#fff;padding:2px 8px;font-style:normal}}
.sub{{color:var(--meta);font-size:14px;margin-top:8px}}
main{{max-width:1080px;margin:0 auto}}
article{{border-top:1px solid var(--linea);padding:20px 0}}
h2{{margin:0 0 4px;font-size:18px}}
.meta{{margin:0 0 12px;color:var(--meta);font-size:13px}}
.s{{font-weight:600}} .s.ok{{color:var(--blu)}} .s.wait{{color:var(--meta)}}
.thumbs{{display:flex;gap:8px;flex-wrap:wrap}}
.thumbs img{{width:84px;height:105px;object-fit:cover;border:1px solid var(--linea);display:block}}
footer{{max-width:1080px;margin:40px auto 0;color:var(--meta);font-size:13px;
border-top:1px solid var(--linea);padding-top:16px}}
</style></head><body>
<header><h1><span>BREAKING</span><em>PAST</em></h1>
<p class="sub">Media pubblici serviti a Instagram. Questa pagina esiste perche' e' Meta a
scaricare le immagini dai propri server: non e' un sito destinato al pubblico.</p></header>
<main>{"".join(rows) or "<p>Coda vuota: esegui <code>python bp.py prepare</code>.</p>"}</main>
<footer>Aggiornato: {queue.get("aggiornato") or "—"} · {len(queue.get("episodi", []))} edizioni in coda ·
<a href="privacy.html">Informativa sulla privacy</a></footer>
</body></html>
"""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "index.html").write_text(html, encoding="utf-8")


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

def cmd_status(args) -> int:
    q = load_queue()
    eps = q.get("episodi", [])
    if not eps:
        log("coda vuota: esegui prima `python bp.py prepare`")
        return 1
    oggi = datetime.now(TZ).date()
    pub = [e for e in eps if e.get("posted")]
    print(f"\nBREAKING PAST — coda di pubblicazione ({len(eps)} edizioni)")
    print(f"aggiornata: {q.get('aggiornato') or '—'}\n")
    print(f"{'N':>4}  {'DATA':<11} {'STATO':<12} {'SLIDE':>5}  TITOLO")
    print("-" * 78)
    for e in eps:
        d = date.fromisoformat(e["data_pubblicazione"])
        if e.get("posted"):
            stato = "pubblicata"
        elif d < oggi:
            stato = "SALTATA!"
        elif d == oggi:
            stato = "OGGI"
        else:
            stato = f"tra {(d - oggi).days} gg"
        print(f"{e['n']:>4}  {e['data_pubblicazione']:<11} {stato:<12} {len(e.get('slides', [])):>5}  {e['titolo']}")
    saltate = [e for e in eps if not e.get("posted") and date.fromisoformat(e["data_pubblicazione"]) < oggi]
    print("-" * 78)
    print(f"pubblicate: {len(pub)} · in coda: {len(eps) - len(pub)} · saltate: {len(saltate)}")
    if eps:
        ultima = max(date.fromisoformat(e["data_pubblicazione"]) for e in eps)
        residui = (ultima - oggi).days
        print(f"copertura: fino al {ultima.isoformat()} ({residui} giorni di autonomia)\n")
    return 1 if saltate else 0


# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------

def find_episode(queue: dict, giorno: date) -> dict | None:
    for e in queue.get("episodi", []):
        if e["data_pubblicazione"] == giorno.isoformat():
            return e
    return None


def cmd_preflight(args) -> int:
    problemi: list[str] = []
    avvisi: list[str] = []

    user_id = os.environ.get("IG_USER_ID", "")
    token = os.environ.get("IG_ACCESS_TOKEN", "")
    base_url = os.environ.get("PAGES_BASE_URL", "").rstrip("/")

    print("\nPREFLIGHT — Breaking Past\n" + "=" * 60)

    for name, val in (("IG_USER_ID", user_id), ("IG_ACCESS_TOKEN", token), ("PAGES_BASE_URL", base_url)):
        print(f"  {'OK ' if val else 'KO '} {name}: {'impostata' if val else 'MANCANTE'}")
        if not val:
            problemi.append(f"{name} non impostata")
    if problemi:
        print("\n" + "\n".join(f"  - {p}" for p in problemi))
        return 1

    # token e account
    try:
        me = api_get("me", {"fields": "id,username,account_type", "access_token": token})
        print(f"  OK  account: @{me.get('username')} (id {me.get('id')}, {me.get('account_type')})")
        if str(me.get("id")) != str(user_id):
            print(f"  --  IG_USER_ID configurato ({user_id}) diverso da quello del token: "
                  f"lo script usera' quello del token ({me.get('id')})")
            user_id = str(me.get("id"))
    except Exception as e:  # noqa: BLE001
        print(f"  KO  token non valido: {e}")
        problemi.append("token non valido o scaduto")

    # quota
    try:
        lim = api_get(f"{user_id}/content_publishing_limit",
                      {"fields": "config,quota_usage", "access_token": token})
        d0 = (lim.get("data") or [{}])[0]
        usato = d0.get("quota_usage", 0)
        tot = (d0.get("config") or {}).get("quota_total", 50)
        print(f"  OK  quota 24h: {usato}/{tot} pubblicazioni usate")
        if tot - usato < 1:
            problemi.append("quota di pubblicazione esaurita")
    except Exception as e:  # noqa: BLE001
        print(f"  --  quota non leggibile: {e}")
        avvisi.append("quota non leggibile")

    # coda
    q = load_queue()
    oggi = date.fromisoformat(args.date) if getattr(args, "date", None) else datetime.now(TZ).date()
    ep = find_episode(q, oggi)
    if not ep:
        print(f"  KO  nessuna edizione in coda per {oggi}")
        problemi.append(f"nessuna edizione per {oggi}")
    else:
        print(f"  OK  edizione del {oggi}: {ep['n']} — {ep['titolo']}")
        if ep.get("posted"):
            print(f"  --  gia' pubblicata alle {ep.get('posted_at')} (media {ep.get('media_id')})")
        # URL pubblici raggiungibili
        ko = []
        for s in ep["slides"]:
            url = s["url"] if s["url"].startswith("http") else f"{base_url}/{s['file']}"
            ok, det = head_ok(url)
            if not ok:
                ko.append(f"slide {s['n']}: {det}")
        if ko:
            print(f"  KO  {len(ko)}/{len(ep['slides'])} immagini non raggiungibili su Pages")
            for k in ko[:5]:
                print(f"        {k}")
            problemi.append("immagini non raggiungibili su GitHub Pages")
        else:
            print(f"  OK  {len(ep['slides'])} immagini raggiungibili su Pages (JPEG, HTTP 200)")
        print(f"  OK  caption: {len(ep['caption'])}/{MAX_CAPTION_CHARS} caratteri")
        print(f"  OK  alt text: {sum(1 for s in ep['slides'] if s.get('alt_text'))}/{len(ep['slides'])} slide")

    # edizioni future
    future = [e for e in q.get("episodi", []) if date.fromisoformat(e["data_pubblicazione"]) > oggi and not e.get("posted")]
    print(f"  {'OK ' if len(future) >= 3 else '-- '} autonomia: {len(future)} edizioni ancora in coda")
    if len(future) < 3:
        avvisi.append("meno di 3 edizioni in coda: prepara il batch successivo")

    print("=" * 60)
    for a in avvisi:
        print(f"  avviso: {a}")
    for p in problemi:
        print(f"  BLOCCO: {p}")
    print()
    return 1 if problemi else 0


# --------------------------------------------------------------------------
# publish
# --------------------------------------------------------------------------

def attendi_le_2030(dry_run: bool = False) -> bool:
    """Il cron di Actions slitta: qui l'orario lo decide Python, ora legale inclusa.
    Ritorna False se e' troppo presto o troppo tardi per pubblicare."""
    now = datetime.now(TZ)
    target = now.replace(hour=PUBLISH_HOUR, minute=PUBLISH_MINUTE, second=0, microsecond=0)
    anticipo = (target - now).total_seconds() / 60
    if anticipo > EARLY_SKIP_MIN and not dry_run:
        log(f"mancano {anticipo:.0f} minuti alle {PUBLISH_HOUR:02d}:{PUBLISH_MINUTE:02d}: "
            f"troppo presto per restare in attesa (limite {EARLY_SKIP_MIN} min). "
            f"Lascio il turno al tentativo successivo.")
        return False
    if now > target:
        ritardo = (now - target).total_seconds() / 60
        if ritardo > LATE_TOLERANCE_MIN:
            log(f"siamo {ritardo:.0f} minuti oltre le {PUBLISH_HOUR:02d}:{PUBLISH_MINUTE:02d}: "
                f"oltre la tolleranza di {LATE_TOLERANCE_MIN} minuti, non pubblico.")
            return False
        log(f"partenza in ritardo di {ritardo:.0f} minuti — dentro la tolleranza, pubblico subito.")
        return True
    attesa = (target - now).total_seconds()
    log(f"ora attuale {now:%H:%M:%S} · pubblicazione alle {target:%H:%M:%S} "
        f"({attesa/60:.1f} minuti di attesa)")
    if dry_run:
        log("DRY_RUN: salto l'attesa.")
        return True
    while True:
        resto = (target - datetime.now(TZ)).total_seconds()
        if resto <= 0:
            break
        time.sleep(min(resto, 30))
    log(f"sono le {datetime.now(TZ):%H:%M:%S}: si pubblica.")
    return True


def attendi_container(container_id: str, token: str, etichetta: str) -> None:
    scadenza = time.time() + CONTAINER_TIMEOUT_S
    while time.time() < scadenza:
        r = api_get(container_id, {"fields": "status_code,status", "access_token": token})
        code = r.get("status_code")
        if code == "FINISHED":
            return
        if code in {"ERROR", "EXPIRED"}:
            raise RuntimeError(f"{etichetta}: container in stato {code} — {r.get('status')}")
        time.sleep(CONTAINER_POLL_S)
    raise RuntimeError(f"{etichetta}: container non pronto entro {CONTAINER_TIMEOUT_S}s")


def cmd_publish(args) -> int:
    dry = truthy(os.environ.get("DRY_RUN")) or args.dry_run
    user_id = env("IG_USER_ID", required=not dry, default="0")
    token = env("IG_ACCESS_TOKEN", required=not dry, default="")
    base_url = env("PAGES_BASE_URL", required=True).rstrip("/")
    if token:
        user_id = resolve_user_id(token, user_id)

    q = load_queue()
    giorno = date.fromisoformat(args.date) if args.date else datetime.now(TZ).date()
    ep = find_episode(q, giorno)
    if not ep:
        log(f"nessuna edizione in coda per il {giorno}. Niente da fare.")
        return 0

    # Idempotenza: due job che partono insieme non producono due post.
    if ep.get("posted"):
        log(f"edizione {ep['n']} gia' pubblicata il {ep.get('posted_at')} "
            f"(media {ep.get('media_id')}). Esco senza fare nulla.")
        return 0

    log(f"edizione {ep['n']} — {ep['titolo']} ({len(ep['slides'])} slide)")

    if not attendi_le_2030(dry_run=dry):
        # Non e' un guasto: o e' troppo presto, o troppo tardi. In entrambi i
        # casi il job esce pulito, cosi' la cronologia resta leggibile e i
        # rossi segnalano solo i problemi veri.
        return 0

    # Ricontrollo l'idempotenza DOPO l'attesa: nel frattempo un altro job
    # potrebbe aver pubblicato.
    q = load_queue()
    ep = find_episode(q, giorno)
    if ep.get("posted"):
        log("pubblicata da un altro job durante l'attesa. Esco.")
        return 0

    caption = ep["caption"]
    primo_commento = ""
    if truthy(os.environ.get("HASHTAGS_IN_FIRST_COMMENT")):
        caption, primo_commento = split_hashtags(caption)
        log(f"hashtag spostati nel primo commento ({len(primo_commento)} caratteri)")

    if dry:
        log("DRY_RUN attivo: nessuna chiamata di pubblicazione.")
        for s in ep["slides"]:
            url = s["url"] if s["url"].startswith("http") else f"{base_url}/{s['file']}"
            ok, det = head_ok(url)
            log(f"  slide {s['n']}: {'OK' if ok else 'KO'} {det} — {url}")
        log(f"  caption: {len(caption)} caratteri")
        return 0

    # 1) un container figlio per slide, con il proprio alt_text
    figli: list[str] = []
    for s in ep["slides"]:
        url = s["url"] if s["url"].startswith("http") else f"{base_url}/{s['file']}"
        r = api_post(f"{user_id}/media", {
            "image_url": url,
            "is_carousel_item": "true",
            "alt_text": s["alt_text"],
            "access_token": token,
        })
        cid = r.get("id")
        if not cid:
            raise RuntimeError(f"slide {s['n']}: nessun id di container nella risposta ({r})")
        attendi_container(cid, token, f"slide {s['n']}")
        figli.append(cid)
        log(f"  slide {s['n']}/{len(ep['slides'])} pronta (container {cid})")

    # 2) container del carosello con la caption
    r = api_post(f"{user_id}/media", {
        "media_type": "CAROUSEL",
        "children": ",".join(figli),
        "caption": caption,
        "access_token": token,
    })
    carosello = r.get("id")
    if not carosello:
        raise RuntimeError(f"nessun id per il container del carosello ({r})")
    attendi_container(carosello, token, "carosello")
    log(f"  carosello pronto (container {carosello})")

    # 3) pubblicazione
    r = api_post(f"{user_id}/media_publish", {"creation_id": carosello, "access_token": token})
    media_id = r.get("id")
    if not media_id:
        raise RuntimeError(f"pubblicazione non riuscita ({r})")

    permalink = None
    try:
        permalink = api_get(media_id, {"fields": "permalink", "access_token": token}).get("permalink")
    except Exception:  # noqa: BLE001
        pass

    # 4) marcatura immediata: da qui in poi il job e' idempotente anche se crolla dopo
    ep["posted"] = True
    ep["posted_at"] = datetime.now(TZ).isoformat(timespec="seconds")
    ep["media_id"] = media_id
    ep["permalink"] = permalink
    save_queue(q)
    write_index_html(q, base_url)
    log(f"PUBBLICATA edizione {ep['n']} — media {media_id} — {permalink or 'permalink non disponibile'}")

    # 5) eventuale primo commento con gli hashtag
    if primo_commento:
        try:
            c = api_post(f"{media_id}/comments", {"message": primo_commento, "access_token": token})
            log(f"  hashtag pubblicati nel primo commento ({c.get('id')})")
        except Exception as e:  # noqa: BLE001
            log(f"  AVVISO: primo commento non riuscito ({e}). Il post e' comunque online.")

    return 0


# --------------------------------------------------------------------------
# refresh-token
# --------------------------------------------------------------------------

def cmd_refresh_token(args) -> int:
    token = env("IG_ACCESS_TOKEN")
    r = api_get("refresh_access_token", {"grant_type": "ig_refresh_token", "access_token": token})
    nuovo = r.get("access_token")
    scade_tra = int(r.get("expires_in", 0))
    if not nuovo:
        die(f"rinnovo non riuscito: {r}")
    scadenza = datetime.now(TZ) + timedelta(seconds=scade_tra)
    log(f"token rinnovato · valido {scade_tra // 86400} giorni (fino al {scadenza:%Y-%m-%d})")

    if args.print_token:
        print(nuovo)
        return 0

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        log("GITHUB_REPOSITORY assente: non riscrivo il secret. Usa --print-token per ottenerlo.")
        return 0
    try:
        subprocess.run(
            ["gh", "secret", "set", "IG_ACCESS_TOKEN", "--repo", repo, "--body", nuovo],
            check=True, capture_output=True, text=True,
        )
        log(f"secret IG_ACCESS_TOKEN aggiornato su {repo}")
    except FileNotFoundError:
        die("gh CLI non disponibile: impossibile riscrivere il secret")
    except subprocess.CalledProcessError as e:
        die(f"gh secret set non riuscito: {e.stderr.strip()}")
    return 0


# --------------------------------------------------------------------------
# quota
# --------------------------------------------------------------------------

def cmd_quota(args) -> int:
    user_id = env("IG_USER_ID")
    token = env("IG_ACCESS_TOKEN")
    user_id = resolve_user_id(token, user_id)
    r = api_get(f"{user_id}/content_publishing_limit",
                {"fields": "config,quota_usage", "access_token": token})
    d0 = (r.get("data") or [{}])[0]
    usato = d0.get("quota_usage", 0)
    cfg = d0.get("config") or {}
    tot = cfg.get("quota_total", 50)
    print(f"quota 24h: {usato}/{tot} usate · {tot - usato} disponibili "
          f"(finestra {cfg.get('quota_duration', 86400)}s)")
    return 0


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(prog="bp.py", description="Pipeline di pubblicazione Breaking Past")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("prepare", help="PNG->JPEG, validazione, alt text, caption, queue.json")
    sp.set_defaults(func=cmd_prepare)

    sp = sub.add_parser("status", help="stato della coda")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("preflight", help="controlli pre-volo")
    sp.add_argument("--date", help="verifica l'edizione di questa data ISO invece di oggi")
    sp.set_defaults(func=cmd_preflight)

    sp = sub.add_parser("publish", help="pubblica l'edizione del giorno alle 20:30 Europe/Rome")
    sp.add_argument("--date", help="forza una data ISO (YYYY-MM-DD)")
    sp.add_argument("--dry-run", action="store_true", help="nessuna chiamata di pubblicazione")
    sp.set_defaults(func=cmd_publish)

    sp = sub.add_parser("refresh-token", help="rinnova il long-lived token e riscrive il secret")
    sp.add_argument("--print-token", action="store_true", help="stampa il token invece di salvarlo")
    sp.set_defaults(func=cmd_refresh_token)

    sp = sub.add_parser("quota", help="quota di pubblicazione residua")
    sp.set_defaults(func=cmd_quota)

    args = p.parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        log("interrotto")
        return 130
    except Exception as e:  # noqa: BLE001
        die(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
