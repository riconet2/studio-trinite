#!/usr/bin/env python3
"""
Générateur de agenda.json — Studio La Trinité-sur-Mer

Chaîne : sources -> récupération -> filtrage -> dédoublonnage -> agenda.json

- Une seule source pour l'instant : l'API DATAtourisme (REST v1).
- La clé API est lue UNIQUEMENT dans la variable d'environnement DATATOURISME_API_KEY.
  Elle n'est jamais écrite dans un fichier, un log ou agenda.json.
- Bibliothèque standard Python uniquement (rien à installer).
- Aucune information n'est inventée : un champ absent de la source reste vide (null).

Usage :
    python scripts/build_agenda.py              # génère agenda.json
    python scripts/build_agenda.py --dry-run    # calcule et affiche le rapport, n'écrit rien
    python scripts/build_agenda.py --probe      # affiche la STRUCTURE d'une réponse (diagnostic)
    python scripts/build_agenda.py --fixture f.json   # test hors ligne (pas d'appel réseau)
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import json
import math
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

API_BASE = "https://api.datatourisme.fr/v1"
ENV_KEY = "DATATOURISME_API_KEY"
SCHEMA_VERSION = 1
SOURCE_LABEL = "DATAtourisme"

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "agenda_config.json"
DEFAULT_OUTPUT = SCRIPT_DIR.parent / "agenda.json"

PROXIMITY_ORDER = {"immediate": 0, "near": 1, "daytrip": 2}

# Champs demandés à l'API (ordre = du plus complet au plus simple ; on prend le premier accepté).
FIELDS_FULL = (
    "uuid,label,type,lastUpdate,takesPlaceAt,isLocatedAt,"
    "!isLocatedAt.address.hasAddressCity.isPartOfDepartment,"
    "hasDescription,hasContact,!hasContact.address,hasBeenCreatedBy,!hasBeenCreatedBy.address"
)
FIELDS_SIMPLE = "uuid,label,type,lastUpdate,takesPlaceAt,isLocatedAt,hasDescription,hasContact,hasBeenCreatedBy"
FIELDS_MIN = "uuid,label,type,takesPlaceAt,isLocatedAt,hasDescription,hasContact"

DEPT_FILTER = "isLocatedAt.address.hasAddressCity.isPartOfDepartment.insee=56"


# --------------------------------------------------------------------------------------
# Sécurité : masquage de la clé dans tout ce qui est affiché
# --------------------------------------------------------------------------------------
_SECRETS: list[str] = []


def register_secret(value: str) -> None:
    if value and value not in _SECRETS:
        _SECRETS.append(value)


def redact(text: object) -> str:
    out = str(text)
    for s in _SECRETS:
        out = out.replace(s, "***")
    out = re.sub(r"(api_key=)[^&\s\"']+", r"\1***", out, flags=re.I)
    return out


def log(msg: str = "") -> None:
    print(redact(msg), flush=True)


# --------------------------------------------------------------------------------------
# Utilitaires texte
# --------------------------------------------------------------------------------------
def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def norm(s: object) -> str:
    """Minuscule, sans accents, sans apostrophes ; tirets et ponctuation -> espaces."""
    s = strip_accents(str(s or "").lower())
    s = s.replace("'", "").replace("\u2019", "").replace("\u02bc", "")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_commune(s: object) -> str:
    n = norm(s)
    n = re.sub(r"^(la|le|les|l) ", "", n)
    n = re.sub(r"\bst\b", "saint", n)
    n = re.sub(r"\bste\b", "sainte", n)
    return n


def kw_regex(kw: str) -> re.Pattern:
    """Mot-clé normalisé ; un '*' final = préfixe (randonn* -> randonnée, randonnées...)."""
    prefix = kw.endswith("*")
    core = norm(kw.rstrip("*"))
    pat = r"\b" + re.escape(core) + (r"\w*" if prefix else r"\b")
    return re.compile(pat)


def compile_kws(kws: list[str]) -> list[re.Pattern]:
    return [kw_regex(k) for k in kws]


def any_kw(patterns: list[re.Pattern], text_norm: str) -> bool:
    return any(p.search(text_norm) for p in patterns)


def count_kw(patterns: list[re.Pattern], text_norm: str) -> int:
    return sum(1 for p in patterns if p.search(text_norm))


def clean_html(s: str) -> str:
    s = re.sub(r"<\s*br\s*/?\s*>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def shorten(s: str, max_chars: int) -> str:
    s = clean_html(s)
    if len(s) <= max_chars:
        return s
    cut = s[:max_chars]
    # on coupe de préférence à la fin d'une phrase, sinon à un mot entier
    m = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if m >= int(max_chars * 0.5):
        return cut[: m + 1].strip()
    cut = cut.rsplit(" ", 1)[0] if " " in cut else cut
    return cut.rstrip(" ,;:-") + "…"


# --------------------------------------------------------------------------------------
# Lecture tolérante des objets DATAtourisme
# (la structure exacte peut varier : valeurs simples ou listes, textes multilingues, préfixes)
# --------------------------------------------------------------------------------------
LANG_KEYS = ("@fr", "fr", "fr-FR", "fr_FR", "fra", "@en", "en", "en-GB", "en-US")
VALUE_KEYS = ("@value", "value", "label", "name", "rdfs:label", "schema:name", "legalName", "schema:legalName")


def as_list(x: object) -> list:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def get(d: object, *names: str):
    """Première valeur non vide parmi plusieurs noms de clés (avec ou sans préfixe schema:/dc:...)."""
    if not isinstance(d, dict):
        return None
    for n in names:
        for k in (n, f"schema:{n}", f"dc:{n}", f"foaf:{n}", f"dt:{n}"):
            if k in d and d[k] not in (None, "", [], {}):
                return d[k]
    return None


def text_of(x: object) -> str:
    """Extrait un texte (français de préférence) d'une valeur simple, d'une liste ou d'un dict multilingue."""
    if x is None:
        return ""
    if isinstance(x, str):
        return x.strip()
    if isinstance(x, (int, float)):
        return str(x)
    if isinstance(x, list):
        for item in x:
            t = text_of(item)
            if t:
                return t
        return ""
    if isinstance(x, dict):
        for k in LANG_KEYS + VALUE_KEYS:
            if k in x:
                t = text_of(x[k])
                if t:
                    return t
    return ""


def parse_date(x: object) -> str | None:
    m = re.match(r"\s*(\d{4}-\d{2}-\d{2})", text_of(x))
    if not m:
        return None
    try:
        datetime.strptime(m.group(1), "%Y-%m-%d")
    except ValueError:
        return None
    return m.group(1)


def parse_time(x: object) -> str | None:
    t = text_of(x)
    m = re.search(r"(?:T|\s|^)(\d{1,2}):(\d{2})", t)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 23 or mm > 59 or (hh == 0 and mm == 0):  # 00:00 = "heure non précisée"
        return None
    return f"{hh:02d}:{mm:02d}"


def to_float(x: object) -> float | None:
    try:
        v = float(text_of(x).replace(",", "."))
    except ValueError:
        return None
    return v


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def type_names(poi: dict) -> list[str]:
    out = []
    for t in as_list(poi.get("type")) + as_list(poi.get("@type")):
        s = text_of(t)
        if s:
            out.append(re.split(r"[#:/]", s)[-1])
    return out


def periods_of(poi: dict) -> list[dict]:
    """Une entrée par occurrence : {start_date, end_date, start_time, end_time}."""
    out = []
    for p in as_list(get(poi, "takesPlaceAt")):
        if not isinstance(p, dict):
            continue
        sd = parse_date(get(p, "startDate"))
        if not sd:
            continue
        ed = parse_date(get(p, "endDate"))
        st = parse_time(get(p, "startTime")) or parse_time(get(p, "startDate"))
        et = parse_time(get(p, "endTime")) or parse_time(get(p, "endDate"))
        out.append({"start_date": sd, "end_date": ed, "start_time": st, "end_time": et})
    if not out:  # certaines sources mettent les dates directement sur le POI
        sd = parse_date(get(poi, "startDate"))
        if sd:
            out.append({"start_date": sd, "end_date": parse_date(get(poi, "endDate")),
                        "start_time": parse_time(get(poi, "startDate")),
                        "end_time": parse_time(get(poi, "endDate"))})
    return out


def place_info(poi: dict) -> dict:
    """Lieu (nom), commune, adresse, coordonnées."""
    place_name = street = city = ""
    lat = lon = None
    for loc in as_list(get(poi, "isLocatedAt")):
        if not isinstance(loc, dict):
            continue
        place_name = place_name or text_of(get(loc, "label", "name"))
        geo = get(loc, "geo")
        for g in as_list(geo):
            if isinstance(g, dict) and lat is None:
                lat = to_float(get(g, "latitude", "lat"))
                lon = to_float(get(g, "longitude", "lon", "lng"))
        for addr in as_list(get(loc, "address")):
            if not isinstance(addr, dict):
                continue
            street = street or text_of(get(addr, "streetAddress"))
            city = city or text_of(get(addr, "addressLocality")) or text_of(get(addr, "hasAddressCity"))
    return {"place": place_name, "street": street, "city": city, "lat": lat, "lon": lon}


def description_of(poi: dict, max_chars: int) -> str:
    best_short = best_long = ""
    for d in as_list(get(poi, "hasDescription")):
        if not isinstance(d, dict):
            continue
        best_short = best_short or text_of(get(d, "shortDescription"))
        best_long = best_long or text_of(get(d, "description"))
    return shorten(best_short or best_long, max_chars) if (best_short or best_long) else ""


def url_of(poi: dict) -> str:
    for c in as_list(get(poi, "hasContact")):
        if not isinstance(c, dict):
            continue
        for h in as_list(get(c, "homepage", "url")):
            u = text_of(h)
            if u.lower().startswith("www."):
                u = "https://" + u
            if re.match(r"^https?://[^\s]+$", u, flags=re.I):
                return u
    return ""


def producer_of(poi: dict) -> str:
    for c in as_list(get(poi, "hasBeenCreatedBy")):
        t = text_of(get(c, "legalName", "label", "name")) if isinstance(c, dict) else text_of(c)
        if t:
            return t
    return ""


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------
class Config:
    def __init__(self, raw: dict):
        self.raw = raw
        self.studio = raw["studio"]
        self.horizon_days = int(raw["horizon_days"])
        self.radius_km = int(raw["search_radius_km"])
        self.desc_max = int(raw["description_max_chars"])
        self.max_occ = int(raw["max_occurrences_per_poi"])
        self.long_days = int(raw["long_event_days"])
        self.keep_uncat = set(raw["keep_uncategorized_in"])
        self.daytrip_min = int(raw["daytrip_min_score"])
        self.daytrip_per_month = int(raw["daytrip_max_per_month"])
        self.base_score = raw["category_base_score"]
        self.type_hints = raw["type_hints"]
        self.exclude_types = set(raw["exclude_types"])
        self.official = [norm(x) for x in raw["source_rank_official"]]
        self.rx_exclude = compile_kws(raw["exclude_title_keywords"])
        self.rx_low = compile_kws(raw["low_interest_title_keywords"])
        self.rx_boost = compile_kws(raw["boost_keywords"])
        self.rx_cat = {slug: compile_kws(kws) for slug, kws in raw["categories"].items()}
        self.commune_tier = {}
        for tier, names in raw["communes"].items():
            for n in names:
                self.commune_tier[norm_commune(n)] = tier

    @staticmethod
    def load(path: Path) -> "Config":
        return Config(json.loads(path.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------------------
# Récupération (DATAtourisme)
# --------------------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status} — {message}")
        self.status = status


def api_get(path: str, params: dict, key: str, timeout: int = 60, retries: int = 3) -> dict:
    url = f"{API_BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "X-API-Key": key,  # la clé passe par l'en-tête, jamais dans l'URL
        "Accept": "application/json",
        "User-Agent": "studio-trinite-agenda/1.0",
    })
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read()[:300].decode("utf-8", "replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                wait = min(int(e.headers.get("Retry-After", 0) or 0), 60) or 3 * (attempt + 1)
                time.sleep(wait)
                continue
            raise ApiError(e.code, redact(body)) from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise ApiError(0, redact(e)) from None
    raise ApiError(0, "échec inconnu")


def build_params(cfg: Config, attempt: dict, page: int, today: date, page_size: int = 250) -> dict:
    params = {"page": page, "page_size": page_size, "lang": "fr", "fields": attempt["fields"]}
    filters = []
    if attempt["geo"]:
        s = cfg.studio
        params["geo_distance"] = f"{s['lat']},{s['lon']},{cfg.radius_km}km"
    else:
        filters.append(DEPT_FILTER)
    if attempt.get("date"):
        filters.append(f"takesPlaceAt.endDate[gte]={today.isoformat()}")
    if filters:
        params["filters"] = " AND ".join(filters)
    return params


def fetch_datatourisme(cfg: Config, key: str, today: date, probe: bool = False) -> tuple[list[dict], dict]:
    """Retourne (objets bruts, infos sur la méthode retenue). Lève ApiError si aucune méthode ne marche."""
    attempts = [
        {"name": "rayon + champs complets", "geo": True, "fields": FIELDS_FULL},
        {"name": "rayon + champs simples", "geo": True, "fields": FIELDS_SIMPLE},
        {"name": "rayon + champs minimaux", "geo": True, "fields": FIELDS_MIN},
        {"name": "département 56 + champs minimaux", "geo": False, "fields": FIELDS_MIN},
    ]
    chosen, first, errors = None, None, []
    for att in attempts:
        try:
            first = api_get("/entertainmentAndEvent",
                            build_params(cfg, att, 1, today, page_size=5 if probe else 250), key)
            if isinstance(first, dict) and isinstance(first.get("objects"), list):
                chosen = dict(att)
                break
            errors.append(f"{att['name']}: réponse sans 'objects'")
        except ApiError as e:
            errors.append(f"{att['name']}: {e}")
            if e.status in (401, 403):  # clé refusée : inutile d'insister
                raise ApiError(e.status, "clé API refusée (vérifier le secret DATATOURISME_API_KEY)") from None
    if not chosen:
        raise ApiError(0, "aucune variante de requête acceptée :\n  " + "\n  ".join(errors))

    if probe:
        return first["objects"], {"method": chosen["name"], "total": (first.get("meta") or {}).get("total")}

    total = int((first.get("meta") or {}).get("total") or len(first["objects"]))
    if total > 9000:  # l'accès par numéro de page est limité à 10 000 résultats
        att2 = dict(chosen, date=True, name=chosen["name"] + " + filtre de dates")
        try:
            test = api_get("/entertainmentAndEvent", build_params(cfg, att2, 1, today), key)
            t2 = int((test.get("meta") or {}).get("total") or 0)
            if isinstance(test.get("objects"), list) and 0 < t2 < total:
                chosen, first, total = att2, test, t2
        except ApiError as e:
            log(f"  ! filtre de dates refusé ({e.status}) — poursuite sans filtre")
    if total > 10000:
        log(f"  ! {total} résultats > 10 000 : une partie ne pourra pas être lue (limite API)")

    objects = list(first["objects"])
    pages = int((first.get("meta") or {}).get("total_pages") or 1)
    for page in range(2, min(pages, 40) + 1):
        time.sleep(0.3)
        data = api_get("/entertainmentAndEvent", build_params(cfg, chosen, page, today), key)
        objs = data.get("objects") or []
        if not objs:
            break
        objects.extend(objs)
    return objects, {"method": chosen["name"], "total": total, "pages": pages}


# --------------------------------------------------------------------------------------
# Normalisation, classification, filtrage
# --------------------------------------------------------------------------------------
def classify(title: str, desc: str, types: list[str], cfg: Config) -> str:
    t_norm, d_norm = norm(title), norm(desc[:400])
    scores: dict[str, int] = defaultdict(int)
    for slug, patterns in cfg.rx_cat.items():
        scores[slug] += 2 * count_kw(patterns, t_norm) + count_kw(patterns, d_norm)
    for tn in types:
        hint = cfg.type_hints.get(tn)
        if hint:
            scores[hint] += 2
    if scores.get("plein_air") and scores.get("nautique"):  # sport en mer -> nautique
        scores["nautique"] += 2
    best = max(scores.items(), key=lambda kv: kv[1], default=("autre", 0))
    return best[0] if best[1] > 0 else "autre"


def make_candidates(pois: list[dict], cfg: Config, today: date, stats: Counter, out_of_scope: Counter) -> list[dict]:
    limit = today + timedelta(days=cfg.horizon_days)
    cands = []
    for poi in pois:
        title = text_of(poi.get("label") or poi.get("rdfs:label"))
        uuid = text_of(poi.get("uuid")) or text_of(poi.get("@id")) or text_of(poi.get("uri"))
        types = type_names(poi)
        if not title:
            stats["sans titre"] += 1
            continue
        if any(t in cfg.exclude_types for t in types):
            stats["exclu : événement professionnel"] += 1
            continue
        title_norm = norm(title)
        if any_kw(cfg.rx_exclude, title_norm):
            stats["exclu : sans intérêt visiteur (mot-clé du titre)"] += 1
            continue
        info = place_info(poi)
        tier = cfg.commune_tier.get(norm_commune(info["city"]))
        if not info["city"]:
            stats["sans commune"] += 1
            continue
        if not tier:
            stats["hors périmètre (commune)"] += 1
            out_of_scope[info["city"]] += 1
            continue
        periods = periods_of(poi)
        if not periods:
            stats["sans date exploitable"] += 1
            continue
        desc = description_of(poi, cfg.desc_max)
        category = classify(title, desc, types, cfg)
        producer = producer_of(poi)
        last_update = parse_date(get(poi, "lastUpdate"))
        url = url_of(poi)
        rank = 1 if any(k in norm(producer) for k in cfg.official) else 2
        distance = None
        if info["lat"] is not None and info["lon"] is not None:
            distance = round(haversine_km(cfg.studio["lat"], cfg.studio["lon"], info["lat"], info["lon"]), 1)
        location = info["place"] or info["street"]
        kept_occ = 0
        for p in periods:
            sd, ed = p["start_date"], p["end_date"]
            if ed and ed < sd:
                ed = None
            last = ed or sd
            if last < today.isoformat() or sd > limit.isoformat():
                stats["hors fenêtre de dates"] += 1
                continue
            if kept_occ >= cfg.max_occ:  # plafond compté sur les dates utiles seulement
                stats["au-delà du plafond d'occurrences"] += 1
                continue
            kept_occ += 1
            start = f"{sd}T{p['start_time']}" if p["start_time"] else sd
            if ed and ed != sd:
                end = f"{ed}T{p['end_time']}" if p["end_time"] else ed
            elif p["end_time"] and p["start_time"] and p["end_time"] > p["start_time"]:
                end = f"{sd}T{p['end_time']}"
            else:
                end = None
            duration = (datetime.strptime(last, "%Y-%m-%d") - datetime.strptime(sd, "%Y-%m-%d")).days + 1
            cands.append({
                "id": "dt-" + hashlib.sha1(f"{uuid or title}|{start}".encode("utf-8")).hexdigest()[:10],
                "series_id": "dt-" + hashlib.sha1((uuid or title).encode("utf-8")).hexdigest()[:10],
                "title": title, "start": start, "end": end,
                "location": location or None, "city": info["city"],
                "description": desc or None, "url": url or None,
                "category": category, "proximity": tier,
                "source": SOURCE_LABEL, "source_name": producer or None,
                "distance_km": distance, "last_update": last_update,
                "_rank": rank, "_duration": duration, "_title_norm": title_norm,
            })
    return cands


def score_and_priority(c: dict, cfg: Config) -> None:
    text = c["_title_norm"] + " " + norm((c["description"] or "")[:300])
    score = cfg.base_score.get(c["category"], 0)
    if any_kw(cfg.rx_boost, text):
        score += 1
    if 2 <= c["_duration"] <= 7:
        score += 1
    if c["_duration"] > cfg.long_days or any_kw(cfg.rx_low, c["_title_norm"]):
        score = min(score, 1)
    tier_bonus = {"immediate": 1, "near": 0, "daytrip": -1}[c["proximity"]]
    eff = score + tier_bonus
    c["_score"] = score
    c["priority"] = 1 if eff >= 4 else 2 if eff >= 2 else 3


def apply_scope_rules(cands: list[dict], cfg: Config, stats: Counter) -> list[dict]:
    kept = []
    for c in cands:
        score_and_priority(c, cfg)
        if c["category"] == "autre" and c["proximity"] not in cfg.keep_uncat:
            stats["exclu : catégorie indéterminée hors La Trinité"] += 1
            continue
        if c["proximity"] == "daytrip" and c["_score"] < cfg.daytrip_min:
            stats["exclu : sortie à la journée peu marquante"] += 1
            continue
        kept.append(c)
    # sorties à la journée : plafond par mois (les mieux notées d'abord)
    by_month: dict[str, list[dict]] = defaultdict(list)
    rest = []
    for c in kept:
        (by_month[c["start"][:7]] if c["proximity"] == "daytrip" else rest).append(c)
    for _, items in by_month.items():
        items.sort(key=lambda c: (-c["_score"], c["start"]))
        rest.extend(items[: cfg.daytrip_per_month])
        stats["exclu : plafond mensuel sorties à la journée"] += max(0, len(items) - cfg.daytrip_per_month)
    return rest


# --------------------------------------------------------------------------------------
# Dédoublonnage
# --------------------------------------------------------------------------------------
_STOP = {"le", "la", "les", "l", "de", "du", "des", "d", "et", "a", "au", "aux", "en", "sur", "un", "une", "pour", "dans"}


def title_tokens(title_norm: str) -> list[str]:
    return [t for t in title_norm.split() if t not in _STOP and not re.fullmatch(r"20\d\d", t)]


def same_event(a: dict, b: dict) -> bool:
    ta, tb = title_tokens(a["_title_norm"]), title_tokens(b["_title_norm"])
    if not ta or not tb:
        return False
    # deux séances distinctes le même jour (horaires connus et éloignés) ne sont pas des doublons
    if "T" in a["start"] and "T" in b["start"]:
        ha, hb = int(a["start"][11:13]) * 60 + int(a["start"][14:16]), int(b["start"][11:13]) * 60 + int(b["start"][14:16])
        if abs(ha - hb) >= 90:
            return False
    sa, sb = " ".join(ta), " ".join(tb)
    if difflib.SequenceMatcher(None, sa, sb).ratio() >= 0.82:
        return True
    short, long_ = (set(ta), set(tb)) if len(ta) <= len(tb) else (set(tb), set(ta))
    return len(short) >= 2 and short <= long_


def dedupe(cands: list[dict], stats: Counter) -> list[dict]:
    """Garde la version de la source la plus officielle, complète ses champs vides avec les doublons."""
    ordered = sorted(cands, key=lambda c: (
        c["_rank"],
        -sum(1 for k in ("end", "location", "description", "url") if c[k]),
        c["start"], c["id"]))
    kept: list[dict] = []
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for c in ordered:
        key = (c["start"][:10], norm_commune(c["city"]))
        twin = next((k for k in buckets[key] if same_event(k, c)), None)
        if twin is None:
            buckets[key].append(c)
            kept.append(c)
            continue
        stats["doublon fusionné"] += 1
        for field in ("end", "location", "description", "url", "distance_km", "last_update"):
            if not twin[field] and c[field]:
                twin[field] = c[field]
    return kept


# --------------------------------------------------------------------------------------
# Sortie
# --------------------------------------------------------------------------------------
PUBLIC_FIELDS = ["id", "series_id", "title", "start", "end", "location", "city", "description", "url",
                 "category", "priority", "proximity", "source", "source_name", "distance_km", "last_update"]


def to_public(c: dict) -> dict:
    return {k: c.get(k) for k in PUBLIC_FIELDS}


def build_document(events: list[dict], cfg: Config) -> dict:
    events = sorted(events, key=lambda c: (c["start"], PROXIMITY_ORDER[c["proximity"]], c["priority"], c["title"]))
    # Date de dernière mise à jour des données : la plus récente des dates « lastUpdate »
    # fournies par les producteurs pour les événements retenus (null si la source n'en donne aucune).
    updates = [e["last_update"] for e in events if e.get("last_update")]
    return {
        "version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data_updated_at": max(updates) if updates else None,
        "studio": cfg.studio,
        "horizon_days": cfg.horizon_days,
        "attribution": "Données : DATAtourisme et ses producteurs (offices de tourisme, communes, etc.)",
        "counts": {
            "total": len(events),
            "by_proximity": dict(Counter(e["proximity"] for e in events)),
        },
        "events": [to_public(e) for e in events],
    }


def comparable(doc: dict | None):
    if not doc:
        return None
    return {"version": doc.get("version"), "events": doc.get("events")}


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_atomic(path: Path, doc: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------------------
# Diagnostic de structure (--probe) : montre les chemins et types, sans la clé
# --------------------------------------------------------------------------------------
def structure_lines(obj: object, prefix: str = "", depth: int = 0, out: list[str] | None = None) -> list[str]:
    out = [] if out is None else out
    if depth > 6 or len(out) > 400:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            structure_lines(v, f"{prefix}.{k}" if prefix else k, depth + 1, out)
    elif isinstance(obj, list):
        out.append(f"{prefix}[]  (liste de {len(obj)})")
        if obj:
            structure_lines(obj[0], prefix + "[]", depth + 1, out)
    else:
        out.append(f"{prefix} = {str(obj)[:70]!r}")
    return out


# --------------------------------------------------------------------------------------
# Programme principal
# --------------------------------------------------------------------------------------
def run(args: argparse.Namespace) -> int:
    cfg = Config.load(Path(args.config))
    today = date.fromisoformat(args.today) if args.today else datetime.now(ZoneInfo("Europe/Paris")).date()
    key = os.environ.get(ENV_KEY, "").strip()
    register_secret(key)

    if args.fixture:
        raw = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
        pois = raw["objects"] if isinstance(raw, dict) else raw
        info = {"method": "fixture (hors ligne)", "total": len(pois)}
    else:
        if not key:
            log(f"ERREUR : variable d'environnement {ENV_KEY} absente (voir le secret GitHub).")
            return 2
        try:
            pois, info = fetch_datatourisme(cfg, key, today, probe=args.probe)
        except ApiError as e:
            log(f"ERREUR récupération DATAtourisme : {e}")
            return 1

    log(f"DATAtourisme : {len(pois)} fiches lues (méthode : {info['method']}, total annoncé : {info.get('total')})")

    if args.probe:
        for i, poi in enumerate(pois[:2], 1):
            log(f"\n--- Structure de la fiche {i} ---")
            for line in structure_lines(poi):
                log("  " + line)
        return 0

    if not pois:
        log("ERREUR : aucune fiche reçue — agenda.json conservé tel quel.")
        return 1

    # Contrôle de plausibilité : si la structure a changé, on n'écrase rien.
    with_title = sum(1 for p in pois if text_of(p.get("label") or p.get("rdfs:label")))
    with_dates = sum(1 for p in pois if periods_of(p))
    with_city = sum(1 for p in pois if place_info(p)["city"])
    n = len(pois)
    log(f"Contrôle : titres {with_title}/{n}, dates {with_dates}/{n}, communes {with_city}/{n}")
    if n >= 5 and (with_title < 0.8 * n or with_dates < 0.3 * n or with_city < 0.5 * n):
        log("ERREUR : structure de réponse inattendue (titres/dates/communes manquants).")
        log("           agenda.json conservé. Relancer en mode --probe pour voir la structure.")
        return 3

    stats: Counter = Counter()
    out_of_scope: Counter = Counter()
    cands = make_candidates(pois, cfg, today, stats, out_of_scope)
    cands = apply_scope_rules(cands, cfg, stats)
    events = dedupe(cands, stats)
    doc = build_document(events, cfg)

    log("\nRapport :")
    for label, count in stats.most_common():
        log(f"  - {label} : {count}")
    log(f"  => {len(events)} événements retenus")
    log(f"  par proximité : {doc['counts']['by_proximity']}")
    log(f"  par catégorie : {dict(Counter(e['category'] for e in events))}")
    log(f"  par priorité  : {dict(sorted(Counter(e['priority'] for e in events).items()))}")
    log(f"  dernière mise à jour des données (producteurs) : {doc['data_updated_at']}")
    if events:
        cov = {k: sum(1 for e in events if e[k]) for k in ("url", "description", "location", "end")}
        log("  couverture des champs : " + ", ".join(f"{k} {v}/{len(events)}" for k, v in cov.items())
            + f", heure connue {sum(1 for e in events if 'T' in e['start'])}/{len(events)}")
    if out_of_scope:
        top = ", ".join(f"{c} ({n})" for c, n in out_of_scope.most_common(8))
        log(f"  communes hors périmètre les plus fréquentes : {top}")

    out_path = Path(args.output)
    previous = read_json(out_path)
    prev_count = len(previous.get("events", [])) if isinstance(previous, dict) else 0
    if not args.force and prev_count >= 20 and len(events) < 0.3 * prev_count:
        log(f"ERREUR : chute suspecte ({prev_count} -> {len(events)} événements) — agenda.json conservé. "
            "Relancer avec --force si c'est voulu.")
        return 4
    if not events and not args.allow_empty:
        log("ERREUR : 0 événement retenu — agenda.json conservé (--allow-empty pour forcer).")
        return 4

    if args.dry_run:
        log("\n(dry-run : rien n'a été écrit)")
        return 0
    if comparable(previous) == comparable(doc):
        log("\nAucun changement : agenda.json inchangé.")
        return 0
    write_atomic(out_path, doc)
    log(f"\nagenda.json écrit : {len(events)} événements.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Génère agenda.json à partir de DATAtourisme.")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT))
    ap.add_argument("--dry-run", action="store_true", help="n'écrit rien, affiche le rapport")
    ap.add_argument("--probe", action="store_true", help="affiche la structure d'une réponse (diagnostic)")
    ap.add_argument("--fixture", help="fichier JSON de fiches (test hors ligne)")
    ap.add_argument("--today", help="date du jour simulée AAAA-MM-JJ (tests)")
    ap.add_argument("--force", action="store_true", help="ignore le garde-fou de chute du nombre d'événements")
    ap.add_argument("--allow-empty", action="store_true")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
