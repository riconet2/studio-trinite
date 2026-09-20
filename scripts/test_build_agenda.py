#!/usr/bin/env python3
"""
Tests du générateur — 100 % hors ligne.
Les fiches ci-dessous sont FICTIVES (titres préfixés [TEST]) : elles servent uniquement à vérifier la logique.
Lancer :  python scripts/test_build_agenda.py
"""
import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from collections import Counter
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_agenda as ba  # noqa: E402

TODAY = date(2026, 9, 20)
FAKE_KEY = "SECRET-KEY-ABC123-DO-NOT-LEAK"


def poi_a(uuid, title, city, start, end=None, st=None, et=None, types=None, desc=None, url=None,
          producer="Office de Tourisme Test", place="Place du Test", lat=47.58, lon=-3.03):
    """Forme 'multilingue + listes' (probable)."""
    return {
        "uuid": uuid, "label": {"fr": [title]}, "type": types or ["EntertainmentAndEvent", "PointOfInterest"],
        "takesPlaceAt": [{"startDate": start, "endDate": end or start, "startTime": st, "endTime": et}],
        "isLocatedAt": [{"label": {"fr": [place]}, "geo": {"latitude": lat, "longitude": lon},
                         "address": [{"streetAddress": ["1 rue du Test"], "hasAddressCity": {"label": {"fr": [city]}}}]}],
        "hasDescription": [{"shortDescription": {"fr": [desc]}}] if desc else [],
        "hasContact": [{"homepage": [url]}] if url else [],
        "hasBeenCreatedBy": [{"legalName": {"fr": [producer]}}],
    }


def poi_b(uuid, title, city, start, end=None, st=None, url=None, producer="Association Test"):
    """Forme 'valeurs simples + préfixes schema:' (variante)."""
    return {
        "uuid": uuid, "rdfs:label": title, "@type": "schema:Festival",
        "takesPlaceAt": {"schema:startDate": start, "schema:endDate": end or start, "schema:startTime": st},
        "isLocatedAt": {"schema:name": "Port test",
                        "address": {"schema:addressLocality": city, "schema:streetAddress": "Quai test"}},
        "hasDescription": {"description": "<p>Grande <b>fête</b> du port.<br/>Animations pour tous.</p>"},
        "hasContact": {"foaf:homepage": url} if url else {},
        "hasBeenCreatedBy": {"legalName": producer},
    }


def fixture():
    return [
        # La Trinité : nautique, plusieurs jours, horaire connu
        poi_a("u1", "[TEST] Régates du Port", "La Trinité-sur-Mer", "2026-10-03", "2026-10-05",
              desc="Trois jours de régates au large du port.", url="https://exemple.test/regates"),
        # Doublon du même événement, autre producteur (moins officiel) avec une info en plus (heure de début)
        poi_a("u2", "[TEST] Regates du port 2026", "La Trinité sur Mer", "2026-10-03", "2026-10-05",
              producer="Association Voile Test", place="Quai Test", desc=None, url=None),
        # Carnac : concert avec horaires
        poi_a("u3", "[TEST] Concert de jazz", "Carnac", "2026-10-10", st="20:30:00", et="22:30:00",
              types=["EntertainmentAndEvent", "Concert"], url="https://exemple.test/jazz"),
        # Deux séances distinctes le même jour (ne doivent PAS fusionner)
        poi_a("u4", "[TEST] Visite guidée des alignements", "Carnac", "2026-10-11", st="10:00:00", desc="Visite."),
        poi_a("u5", "[TEST] Visite guidée des alignements", "Carnac", "2026-10-11", st="16:00:00", desc="Visite."),
        # Orthographe sans apostrophe pour Crac'h
        poi_a("u6", "[TEST] Marché nocturne des créateurs", "Crach", "2026-10-15", st="18:00:00"),
        # Auray, forme b
        poi_b("u7", "[TEST] Fête du port", "Auray", "2026-10-18", "2026-10-19", st="10:00:00", url="www.exemple.test/fete"),
        # Vannes : important -> gardé (festival multi-jours)
        poi_a("u8", "[TEST] Festival international de musique", "Vannes", "2026-10-24", "2026-10-26",
              types=["EntertainmentAndEvent", "Festival", "Concert"]),
        # Vannes : peu marquant -> écarté (sortie à la journée)
        poi_a("u9", "[TEST] Atelier de couture", "Vannes", "2026-10-12"),
        # Lorient : festival mais trop loin dans le temps -> hors horizon
        poi_a("u10", "[TEST] Festival lointain", "Lorient", "2027-09-15", types=["Festival"]),
        # Sans intérêt
        poi_a("u11", "[TEST] Assemblée générale de l'association", "La Trinité-sur-Mer", "2026-10-07"),
        poi_a("u12", "[TEST] Séminaire professionnel", "Carnac", "2026-10-08", types=["BusinessEvent"]),
        poi_a("u13", "[TEST] Concert annulé", "Carnac", "2026-10-09", types=["Concert"]),
        # Hors périmètre
        poi_a("u14", "[TEST] Concert à Quiberon", "Quiberon", "2026-10-10", types=["Concert"]),
        # Passé
        poi_a("u15", "[TEST] Concert passé", "Carnac", "2026-09-01", types=["Concert"]),
        # En cours (commencé avant, finit après) -> gardé
        poi_a("u16", "[TEST] Exposition de peinture", "La Trinité-sur-Mer", "2026-09-10", "2026-10-05",
              types=["ExhibitionEvent"], desc="Peintures de marins."),
        # Sans date, sans titre, sans commune
        {"uuid": "u17", "label": {"fr": ["[TEST] Sans date"]}, "isLocatedAt": [{"address": [{"hasAddressCity": {"label": "Carnac"}}]}]},
        poi_a("u18", "", "Carnac", "2026-10-20"),
        poi_a("u19", "[TEST] Sans commune", "", "2026-10-20"),
        # Catégorie indéterminée à La Trinité (gardée, priorité basse) et à Carnac (écartée)
        poi_a("u20", "[TEST] Rencontre autour d'un projet", "La Trinité-sur-Mer", "2026-10-21"),
        poi_a("u21", "[TEST] Rencontre autour d'un projet", "Carnac", "2026-10-21"),
        # Description longue avec HTML
        poi_a("u22", "[TEST] Fête du pardon", "La Trinité-sur-Mer", "2026-10-25",
              desc="<p>" + ("Une longue description. " * 40) + "</p>"),
    ]


def run_main(pois, extra=None, out=None, today="2026-09-20"):
    tmp = Path(tempfile.mkdtemp())
    fx = tmp / "fx.json"
    fx.write_text(json.dumps({"objects": pois}), encoding="utf-8")
    output = out or (tmp / "agenda.json")
    argv = ["--fixture", str(fx), "--output", str(output), "--today", today] + (extra or [])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = ba.main(argv)
    return code, buf.getvalue(), Path(output)


class TestPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.code, cls.log, cls.path = run_main(fixture())
        cls.doc = json.loads(cls.path.read_text(encoding="utf-8"))
        cls.events = cls.doc["events"]
        cls.by_title = {}
        for e in cls.events:
            cls.by_title.setdefault(e["title"], []).append(e)

    def test_exit_ok_and_file_written(self):
        self.assertEqual(self.code, 0)
        self.assertTrue(self.path.exists())

    def test_schema_stable(self):
        required = {"id", "title", "start", "end", "location", "city", "description", "url",
                    "category", "priority", "proximity", "source"}
        ids = set()
        for e in self.events:
            self.assertTrue(required <= set(e), e)
            self.assertRegex(e["start"], r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?$")
            if e["end"]:
                self.assertRegex(e["end"], r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?$")
                self.assertGreaterEqual(e["end"], e["start"])
            self.assertIn(e["priority"], (1, 2, 3))
            self.assertIn(e["proximity"], ("immediate", "near", "daytrip"))
            self.assertEqual(e["source"], "DATAtourisme")
            self.assertNotIn(e["id"], ids)
            ids.add(e["id"])
            if e["url"]:
                self.assertRegex(e["url"], r"^https?://")

    def test_proximity_tiers(self):
        tier = {e["title"]: e["proximity"] for e in self.events}
        self.assertEqual(tier["[TEST] Régates du Port"], "immediate")
        self.assertEqual(tier["[TEST] Concert de jazz"], "near")
        self.assertEqual(tier["[TEST] Marché nocturne des créateurs"], "near")  # "Crach" sans apostrophe
        self.assertEqual(tier["[TEST] Festival international de musique"], "daytrip")

    def test_dedupe_keeps_official_and_fills_blanks(self):
        regates = [e for e in self.events if "gates" in e["title"]]
        self.assertEqual(len(regates), 1)
        r = regates[0]
        self.assertEqual(r["source_name"], "Office de Tourisme Test")  # source officielle conservée
        self.assertEqual(r["url"], "https://exemple.test/regates")
        self.assertEqual(r["end"], "2026-10-05")

    def test_distinct_sessions_not_merged(self):
        visites = self.by_title["[TEST] Visite guidée des alignements"]
        self.assertEqual(sorted(v["start"] for v in visites), ["2026-10-11T10:00", "2026-10-11T16:00"])

    def test_filters(self):
        titles = set(self.by_title)
        for bad in ("[TEST] Assemblée générale de l'association", "[TEST] Séminaire professionnel",
                    "[TEST] Concert annulé", "[TEST] Concert à Quiberon", "[TEST] Concert passé",
                    "[TEST] Festival lointain", "[TEST] Atelier de couture", "[TEST] Sans date",
                    "[TEST] Sans commune", ""):
            self.assertNotIn(bad, titles)
        self.assertIn("[TEST] Exposition de peinture", titles)  # en cours -> gardé
        uncategorized = self.by_title["[TEST] Rencontre autour d'un projet"]
        self.assertEqual([e["city"] for e in uncategorized], ["La Trinité-sur-Mer"])
        self.assertEqual(uncategorized[0]["priority"], 3)

    def test_times_and_formats(self):
        jazz = self.by_title["[TEST] Concert de jazz"][0]
        self.assertEqual((jazz["start"], jazz["end"]), ("2026-10-10T20:30", "2026-10-10T22:30"))
        self.assertEqual(jazz["category"], "musique")

    def test_form_b_and_html(self):
        fete = self.by_title["[TEST] Fête du port"][0]
        self.assertEqual(fete["city"], "Auray")
        self.assertEqual(fete["url"], "https://www.exemple.test/fete")
        self.assertEqual(fete["description"], "Grande fête du port. Animations pour tous.")
        self.assertEqual(fete["start"], "2026-10-18T10:00")

    def test_description_shortened_never_invented(self):
        pardon = self.by_title["[TEST] Fête du pardon"][0]
        self.assertLessEqual(len(pardon["description"]), 281)
        self.assertNotIn("<", pardon["description"])
        peinture = self.by_title["[TEST] Exposition de peinture"][0]
        self.assertIsNone(peinture["url"])  # absent de la source -> null, pas inventé
        self.assertEqual(peinture["end"], "2026-10-05")

    def test_sorted_chronologically(self):
        starts = [e["start"] for e in self.events]
        self.assertEqual(starts, sorted(starts))

    def test_priority_ranking(self):
        reg = next(e for e in self.events if "gates" in e["title"])
        self.assertEqual(reg["priority"], 1)

    def test_no_change_no_rewrite(self):
        before = self.path.stat().st_mtime_ns
        code, log, _ = run_main(fixture(), out=self.path)
        self.assertEqual(code, 0)
        self.assertIn("inchangé", log)
        self.assertEqual(self.path.stat().st_mtime_ns, before)


class TestGuards(unittest.TestCase):
    def test_empty_fetch_keeps_previous(self):
        tmp = Path(tempfile.mkdtemp()) / "agenda.json"
        tmp.write_text('{"version":1,"events":[{"id":"x"}]}', encoding="utf-8")
        code, _, _ = run_main([], out=tmp)
        self.assertEqual(code, 1)
        self.assertIn('"x"', tmp.read_text(encoding="utf-8"))

    def test_unexpected_structure_refused(self):
        junk = [{"uuid": str(i), "foo": {"bar": i}} for i in range(10)]
        tmp = Path(tempfile.mkdtemp()) / "agenda.json"
        code, log, _ = run_main(junk, out=tmp)
        self.assertEqual(code, 3)
        self.assertFalse(tmp.exists())
        self.assertIn("structure de réponse inattendue", log)

    def test_sudden_drop_refused(self):
        tmp = Path(tempfile.mkdtemp()) / "agenda.json"
        tmp.write_text(json.dumps({"version": 1, "events": [{"id": str(i)} for i in range(50)]}), encoding="utf-8")
        code, log, _ = run_main(fixture(), out=tmp)
        self.assertEqual(code, 4)
        self.assertIn("chute suspecte", log)
        code2, _, _ = run_main(fixture(), extra=["--force"], out=tmp)
        self.assertEqual(code2, 0)

    def test_dry_run_writes_nothing(self):
        tmp = Path(tempfile.mkdtemp()) / "agenda.json"
        code, log, _ = run_main(fixture(), extra=["--dry-run"], out=tmp)
        self.assertEqual(code, 0)
        self.assertFalse(tmp.exists())


class TestSecrecy(unittest.TestCase):
    """La clé ne doit apparaître ni dans les logs, ni dans agenda.json, ni dans les erreurs."""

    def test_key_never_leaks_on_http_error(self):
        import urllib.error
        body = io.BytesIO(('{"error":"bad request for api_key=%s and header %s"}' % (FAKE_KEY, FAKE_KEY)).encode())
        err = urllib.error.HTTPError("https://api.datatourisme.fr/v1/x?api_key=" + FAKE_KEY, 400, "Bad", {}, body)

        def boom(*a, **k):
            raise err

        buf = io.StringIO()
        with mock.patch.dict("os.environ", {ba.ENV_KEY: FAKE_KEY}), \
                mock.patch("urllib.request.urlopen", side_effect=boom), \
                contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            tmp = Path(tempfile.mkdtemp()) / "agenda.json"
            code = ba.main(["--output", str(tmp)])
        self.assertEqual(code, 1)
        self.assertNotIn(FAKE_KEY, buf.getvalue())
        self.assertFalse(tmp.exists())

    def test_key_sent_in_header_not_url(self):
        captured = {}

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"objects": [], "meta": {"total": 0}}'

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            captured["headers"] = {k.lower(): v for k, v in req.header_items()}
            return Resp()

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ba.api_get("/entertainmentAndEvent", {"page": 1}, FAKE_KEY)
        self.assertNotIn(FAKE_KEY, captured["url"])
        self.assertNotIn("api_key", captured["url"])
        self.assertEqual(captured["headers"]["x-api-key"], FAKE_KEY)

    def test_output_file_has_no_key_and_repo_has_no_literal_key(self):
        ba.register_secret(FAKE_KEY)
        _, _, path = run_main(fixture())
        self.assertNotIn(FAKE_KEY, path.read_text(encoding="utf-8"))
        root = Path(__file__).resolve().parent.parent
        for f in list(root.rglob("*.py")) + list(root.rglob("*.json")) + list(root.rglob("*.yml")) + list(root.rglob("*.md")):
            if f.name == "test_build_agenda.py":
                continue
            self.assertNotIn(FAKE_KEY, f.read_text(encoding="utf-8"), f)

    def test_redact_masks_api_key_param(self):
        self.assertEqual(ba.redact("GET /v1?api_key=abc123&x=1"), "GET /v1?api_key=***&x=1")

    def test_missing_key_is_reported_without_crash(self):
        buf = io.StringIO()
        with mock.patch.dict("os.environ", {}, clear=True), contextlib.redirect_stdout(buf):
            code = ba.main(["--output", str(Path(tempfile.mkdtemp()) / "a.json")])
        self.assertEqual(code, 2)
        self.assertIn("absente", buf.getvalue())


class TestFetch(unittest.TestCase):
    """Négociation des variantes de requête et pagination (API simulée)."""

    def setUp(self):
        self.cfg = ba.Config.load(ba.DEFAULT_CONFIG)
        self.calls = []

    def _fake(self, reject_fields=(), total=600, per_page=250):
        def fake(path, params, key, **kw):
            self.calls.append(dict(params))
            self.assertNotIn("api_key", params)
            if params["fields"] in reject_fields:
                raise ba.ApiError(400, "champ inconnu")
            page = params["page"]
            n = min(per_page, max(0, total - (page - 1) * per_page))
            return {"objects": [{"uuid": f"{page}-{i}"} for i in range(n)],
                    "meta": {"total": total, "page": page, "total_pages": -(-total // per_page)}}
        return fake

    def test_falls_back_to_simpler_fields_and_paginates(self):
        with mock.patch.object(ba, "api_get", side_effect=self._fake(reject_fields=(ba.FIELDS_FULL,))), \
                mock.patch.object(ba.time, "sleep"):
            objs, info = ba.fetch_datatourisme(self.cfg, FAKE_KEY, TODAY)
        self.assertEqual(len(objs), 600)
        self.assertIn("simples", info["method"])
        self.assertEqual([c["page"] for c in self.calls if c["fields"] == ba.FIELDS_SIMPLE], [1, 2, 3])
        self.assertTrue(all("geo_distance" in c for c in self.calls))

    def test_all_variants_rejected_raises(self):
        allf = (ba.FIELDS_FULL, ba.FIELDS_SIMPLE, ba.FIELDS_MIN)
        with mock.patch.object(ba, "api_get", side_effect=self._fake(reject_fields=allf)):
            with self.assertRaises(ba.ApiError):
                ba.fetch_datatourisme(self.cfg, FAKE_KEY, TODAY)

    def test_auth_error_stops_immediately(self):
        def deny(*a, **k):
            raise ba.ApiError(403, "forbidden")
        with mock.patch.object(ba, "api_get", side_effect=deny) as m:
            with self.assertRaises(ba.ApiError) as ctx:
                ba.fetch_datatourisme(self.cfg, FAKE_KEY, TODAY)
        self.assertEqual(m.call_count, 1)
        self.assertNotIn(FAKE_KEY, str(ctx.exception))

    def test_date_filter_only_when_too_many_results(self):
        with mock.patch.object(ba, "api_get", side_effect=self._fake(total=9500, per_page=250)), \
                mock.patch.object(ba.time, "sleep"):
            ba.fetch_datatourisme(self.cfg, FAKE_KEY, TODAY)
        self.assertTrue(any("takesPlaceAt.endDate[gte]=2026-09-20" in c.get("filters", "") for c in self.calls))
        self.calls.clear()
        with mock.patch.object(ba, "api_get", side_effect=self._fake(total=600)), mock.patch.object(ba.time, "sleep"):
            ba.fetch_datatourisme(self.cfg, FAKE_KEY, TODAY)
        self.assertFalse(any("filters" in c for c in self.calls))


class TestHelpers(unittest.TestCase):
    def test_commune_matching(self):
        cfg = ba.Config.load(ba.DEFAULT_CONFIG)
        t = cfg.commune_tier
        for name, tier in [("La Trinité-sur-Mer", "immediate"), ("LA TRINITE SUR MER", "immediate"),
                           ("Trinité-sur-Mer", "immediate"), ("Crac'h", "near"), ("Crach", "near"),
                           ("Crac’h", "near"), ("Auray", "near"), ("Vannes", "daytrip"), ("Lorient", "daytrip")]:
            self.assertEqual(t.get(ba.norm_commune(name)), tier, name)
        self.assertIsNone(t.get(ba.norm_commune("Quiberon")))

    def test_time_parsing(self):
        self.assertIsNone(ba.parse_time("00:00:00"))
        self.assertEqual(ba.parse_time("20:30:00"), "20:30")
        self.assertEqual(ba.parse_time("2026-10-10T09:05:00+02:00"), "09:05")
        self.assertIsNone(ba.parse_time(None))

    def test_url_scheme_whitelist(self):
        self.assertEqual(ba.url_of({"hasContact": [{"homepage": ["javascript:alert(1)", "https://ok.test/x"]}]}), "https://ok.test/x")
        self.assertEqual(ba.url_of({"hasContact": [{"homepage": ["javascript:alert(1)"]}]}), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
