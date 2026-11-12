# -*- coding: utf-8 -*-
"""Herkese açık site yayıncısı testleri (internet gerektirmez)."""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yayin  # noqa: E402

SABLON = os.path.join(os.path.dirname(__file__), "..", "site")


def ornek(**k):
    an = int(time.time())
    d = {"tur": "urun", "bitti": False, "magaza": "trendyol", "magaza_ad": "Trendyol", "baslik": "Kanal başlığı 🔥🔥",
         "magaza_basligi": "Philips Airfryer 5000 HD9285/96", "fiyat": 6100, "eski_fiyat": None, "ts": an - 600,
         "urun_link": "https://ty.gl/abc", "resim": "https://cdn.dsmcdn.com/a.jpg", "metin": "KANAL METNİ", "kanal": "firsatz",
         "dogrulama": {"durum": "onayli", "ts": an - 60, "fiyat": 5999, "liste_fiyati": 7999,
                       "son_url": "https://www.trendyol.com/philips/airfryer-p-123?boutiqueId=61&merchantId=968&utm_source=tg"},
         "akakce_dogrulama": None, "dusus_etiketi": None, "eski_fiyat_supheli": None}
    d.update(k)
    return d


class Link(unittest.TestCase):
    def test_temizle(self):
        self.assertEqual(yayin.link_temizle("https://www.trendyol.com/x/y-p-1?boutiqueId=2&merchantId=3&adjust_t=z", "trendyol"),
                         "https://www.trendyol.com/x/y-p-1?merchantId=3")
        self.assertEqual(yayin.link_temizle("https://www.amazon.com.tr/Philips-HD9285/dp/B0ABCDEFGH/ref=sr_1?tag=baskasi-21&th=1", "amazon"),
                         "https://www.amazon.com.tr/dp/B0ABCDEFGH")
        self.assertEqual(yayin.link_temizle("https://www.hepsiburada.com/x-p-HBC1?magaza=Hepsiburada&wt_af=tr.aff.1", "hepsiburada"),
                         "https://www.hepsiburada.com/x-p-HBC1?magaza=Hepsiburada")
        self.assertEqual(yayin.link_temizle("https://www.n11.com/urun/x-1?utm_medium=a&renk=kirmizi", "n11"),
                         "https://www.n11.com/urun/x-1?renk=kirmizi")

    def test_ortaklik(self):
        ayar = {"ortaklik": {"amazon": {"param": {"tag": "bizim-21"}},
                             "trendyol": {"sablon": "https://ortak.example/git?u={url_kodlu}"}}}
        l, o = yayin.ortaklik_linki("https://www.amazon.com.tr/dp/B0ABCDEFGH?tag=baskasi-21", "amazon", ayar)
        self.assertEqual(l, "https://www.amazon.com.tr/dp/B0ABCDEFGH?tag=bizim-21")
        self.assertTrue(o)
        l, o = yayin.ortaklik_linki("https://www.trendyol.com/a-p-1", "trendyol", ayar)
        self.assertEqual(l, "https://ortak.example/git?u=https%3A%2F%2Fwww.trendyol.com%2Fa-p-1")
        l, o = yayin.ortaklik_linki("https://www.n11.com/urun/x-1", "n11", ayar)
        self.assertFalse(o)


class Secim(unittest.TestCase):
    def test_onayli_urun_kanal_icerigi_olmadan(self):
        l = yayin.yayin_listesi([ornek()], {})
        self.assertEqual(len(l), 1)
        o = l[0]
        self.assertEqual(o["fiyat"], 5999)                         # mağazada okunan fiyat
        self.assertEqual(o["eski_fiyat"], 7999)
        self.assertEqual(o["yuzde"], 25)
        self.assertEqual(o["baslik"], "Philips Airfryer 5000 HD9285/96")   # kanal başlığı değil
        self.assertEqual(o["link"], "https://www.trendyol.com/philips/airfryer-p-123?merchantId=968")
        self.assertNotIn("KANAL", json.dumps(o, ensure_ascii=False))
        self.assertNotIn("firsatz", json.dumps(o))

    def test_elenenler(self):
        an = int(time.time())
        elenen = [
            ornek(dogrulama={**ornek()["dogrulama"], "durum": "red"}),
            ornek(dogrulama={**ornek()["dogrulama"], "durum": "okunamadi"}),
            ornek(dogrulama={**ornek()["dogrulama"], "ts": an - 7 * 3600}),                       # bayat teyit
            ornek(dogrulama={**ornek()["dogrulama"], "liste_fiyati": 6300}),                      # %5 indirim
            ornek(akakce_dogrulama={"durum": "ok", "en_ucuz": 4500, "fark": 33.3}),               # başka yerde ucuz
            ornek(bitti=True),
            ornek(tur="kupon"),
        ]
        for d in elenen:
            self.assertEqual(yayin.yayin_listesi([d], {}), [], d)

    def test_dip_fiyat_indirimsiz_de_girer(self):
        d = ornek(dogrulama={**ornek()["dogrulama"], "liste_fiyati": None}, dusus_etiketi="Son 30 günün en düşüğü")
        self.assertEqual(len(yayin.yayin_listesi([d], {})), 1)

    def test_ayni_urun_tek_kart_en_ucuz(self):
        a = ornek()
        b = ornek(dogrulama={**ornek()["dogrulama"], "fiyat": 5799,
                             "son_url": "https://www.trendyol.com/philips/airfryer-p-123?merchantId=968&utm_x=1"})
        l = yayin.yayin_listesi([a, b], {})
        self.assertEqual(len(l), 1)
        self.assertEqual(l[0]["fiyat"], 5799)


class Site(unittest.TestCase):
    def test_dosyalar_ve_yer_tutucular(self):
        ayar = dict(yayin.VARSAYILAN, telegram={"kanal": "@dusero", "bot_token": ""}, iletisim="info@ornek.com")
        d = yayin.site_dosyalari(SABLON, ayar, yayin.yayin_listesi([ornek()], {}), "3.1")
        for yol in ("index.html", "app.js", "sw.js", "stil.css", "manifest.webmanifest", "ikon-192.png", "yasal.html", "veri.json", ".nojekyll"):
            self.assertIn(yol, d)
        for yol, icerik in d.items():
            if yol.endswith((".html", ".js", ".css", ".webmanifest")):
                self.assertNotIn(b"{{", icerik, yol)
        self.assertIn("Düşero".encode(), d["index.html"])
        self.assertIn(b"t.me/dusero", d["index.html"])
        self.assertIn(b"mailto:info@ornek.com", d["yasal.html"])
        self.assertEqual(len(json.loads(d["veri.json"])["urunler"]), 1)


class Yukleme(unittest.TestCase):
    def test_tek_commit(self):
        cagrilar = []

        def sahte(token, yontem, yol, govde=None, zaman_asimi=30):
            cagrilar.append((yontem, yol, govde))
            if yol.endswith("/git/ref/heads/main"):
                return {"object": {"sha": "c0"}}
            if "/git/commits/c0" in yol:
                return {"tree": {"sha": "t0"}}
            if yol.endswith("/git/blobs"):
                return {"sha": "b1"}
            if yol.endswith("/git/trees"):
                return {"sha": "t1"}
            if yol.endswith("/git/commits"):
                return {"sha": "c1"}
            return {}
        eski = yayin._gh
        yayin._gh = sahte
        try:
            sha = yayin.github_yukle({"depo": "a/b", "token": "x", "dal": "main"},
                                     {"index.html": b"<html>", "ikon-192.png": b"\x89PNG\x00\x01"}, "m")
        finally:
            yayin._gh = eski
        self.assertEqual(sha, "c1")
        agac = next(g for y, p, g in cagrilar if p.endswith("/git/trees"))
        self.assertEqual({e["path"] for e in agac["tree"]}, {"index.html", "ikon-192.png"})
        self.assertEqual([c[0] for c in cagrilar if "refs/heads" in c[1]], ["PATCH"])     # tek ref güncellemesi

    def test_degismeyen_liste_yuklenmez_ve_telegram_ilk_turda_dokmez(self):
        klasor = tempfile.mkdtemp()

        class SahteDepo:
            import threading
            kilit = threading.Lock()

            def firsatlar(self):
                return [ornek()]
        y = yayin.Yayinci(SahteDepo(), klasor, SABLON, "3.1")
        y.ayar["github"] = {"depo": "a/b", "token": "x", "dal": "main"}
        y.ayar["telegram"] = {"bot_token": "t", "kanal": "@k"}
        yuklenen, gonderilen = [], []
        eski_y, eski_t = yayin.github_yukle, yayin.telegram_gonder
        yayin.github_yukle = lambda gh, dosyalar, mesaj: yuklenen.append(set(dosyalar)) or "sha"
        yayin.telegram_gonder = lambda tg, o, ad: gonderilen.append(o["id"]) or True
        try:
            self.assertEqual(y.yayinla(), 1)
            self.assertIsNone(y.yayinla())                   # liste aynı: yükleme yok
            self.assertEqual(y.yayinla(zorla=True), 1)
        finally:
            yayin.github_yukle, yayin.telegram_gonder = eski_y, eski_t
        self.assertIn("index.html", yuklenen[0])
        self.assertEqual(gonderilen, [])                      # ilk çalıştırmada eldeki liste kanala dökülmez


class Marka(unittest.TestCase):
    def test_eski_varsayilan_ad_duseroya_gecer(self):
        ayar = {"site_adi": "Teyitli Fırsat"}
        self.assertTrue(yayin.marka_guncelle(ayar))
        self.assertEqual(ayar["site_adi"], "Düşero")

    def test_kullanicinin_kendi_adina_dokunulmaz(self):
        ayar = {"site_adi": "Benim Sitem"}
        self.assertFalse(yayin.marka_guncelle(ayar))
        self.assertEqual(ayar["site_adi"], "Benim Sitem")

    def test_kayitli_ayar_acilista_guncellenir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "yayin.json"), "w", encoding="utf-8") as f:
                json.dump({"site_adi": "Teyitli Fırsat", "slogan": "x"}, f)
            y = yayin.Yayinci(None, d, SABLON, "3.5.1")
            self.assertEqual(y.ayar["site_adi"], "Düşero")
            with open(os.path.join(d, "yayin.json"), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["site_adi"], "Düşero")

    def test_telegram_mesajinda_marka(self):
        o = yayin.yayin_listesi([ornek()], {})[0]
        self.assertIn("Düşero", yayin.telegram_metni(o, "Düşero"))


if __name__ == "__main__":
    unittest.main()
