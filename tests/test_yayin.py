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


class Karar(unittest.TestCase):
    """Fiyat geçmişine göre indirim değerlendirmesi."""
    def gecmis(self, fiyatlar, gun_once=60):
        an = time.time()
        adim = gun_once * 86400 / max(1, len(fiyatlar) - 1)
        return [[int(an - gun_once * 86400 + i * adim) - 3600, f] for i, f in enumerate(fiyatlar)]

    def test_donemin_en_dusugu(self):
        k = yayin.fiyat_karari(1299, 2499, self.gecmis([1899, 1949, 1899, 1849, 1999, 1799]))
        self.assertEqual(k["karar"], "dip")
        self.assertEqual(k["normal"], 1899)
        self.assertEqual(k["gercek_yuzde"], 32)
        self.assertIn("günün en düşük", k["etiket"])

    def test_normalin_altinda(self):
        k = yayin.fiyat_karari(7999, 14999, self.gecmis([8999, 8799, 7499, 8899, 8999, 8799]))
        self.assertEqual(k["karar"], "iyi")

    def test_sisirilmis(self):
        k = yayin.fiyat_karari(3999, 7999, self.gecmis([3899, 3999, 3949, 3999, 3899, 3949]))
        self.assertEqual(k["karar"], "sisirilmis")
        self.assertLessEqual(k["gercek_yuzde"], 0)

    def test_hep_ayni_fiyat_dip_sayilmaz(self):
        k = yayin.fiyat_karari(500, 800, self.gecmis([500, 500, 500, 500]))
        self.assertEqual(k["karar"], "sisirilmis")

    def test_gecmis_yetersiz(self):
        k = yayin.fiyat_karari(500, 800, self.gecmis([700]))
        self.assertIsNone(k["karar"])
        k = yayin.fiyat_karari(500, 800, [], eski_supheli=True)
        self.assertEqual(k["karar"], "sisirilmis")

    def test_eski_kayitlar_ve_seyreltme(self):
        an = time.time()
        cok_eski = [[int(an - 200 * 86400), 100]]
        k = yayin.fiyat_karari(50, None, cok_eski + self.gecmis(list(range(200, 280)), 80), an)
        self.assertLessEqual(len(k["gecmis"]), yayin.GECMIS_NOKTA)
        self.assertNotIn(100, [p for _, p in k["gecmis"]])          # 90 günden eski kayıt alınmaz
        self.assertEqual(k["gecmis"][-1][1], 279)                    # en yeni kayıt korunur

    def test_listeye_yazilir(self):
        d = ornek(fiyat_gecmisi=self.gecmis([7999, 7899, 7999, 7499, 7999]), kategori="beyaz", kategori_ad="Beyaz eşya & ev aletleri")
        o = yayin.yayin_listesi([d], {})[0]
        self.assertEqual(o["karar"], "dip")
        self.assertEqual(o["kategori_ad"], "Beyaz eşya & ev aletleri")
        self.assertTrue(o["gecmis"])

    def test_eski_varsayilan_ad_duseroya_gecer(self):
        a = yayin.eski_varsayilani_guncelle({"site_adi": "Teyitli Fırsat", "slogan": "Fiyatı mağazada teyit edilmiş indirimler"})
        self.assertEqual(a["site_adi"], "Düşero")
        self.assertEqual(yayin.eski_varsayilani_guncelle({"site_adi": "Benim Sitem"})["site_adi"], "Benim Sitem")


class Site(unittest.TestCase):
    def test_dosyalar_ve_yer_tutucular(self):
        ayar = dict(yayin.VARSAYILAN, telegram={"kanal": "@teyitli", "bot_token": ""}, iletisim="info@ornek.com")
        d = yayin.site_dosyalari(SABLON, ayar, yayin.yayin_listesi([ornek()], {}), "3.1")
        for yol in ("index.html", "app.js", "sw.js", "stil.css", "manifest.webmanifest", "ikon-192.png", "yasal.html", "veri.json", ".nojekyll"):
            self.assertIn(yol, d)
        for yol, icerik in d.items():
            if yol.endswith((".html", ".js", ".css", ".webmanifest")):
                self.assertNotIn(b"{{", icerik, yol)
        self.assertIn("Düşero".encode(), d["index.html"])
        self.assertIn(b"t.me/teyitli", d["index.html"])
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

        sabit = [ornek()]                                   # her çağrıda aynı zaman damgaları

        class SahteDepo:
            import threading
            kilit = threading.Lock()

            def firsatlar(self):
                return sabit
        y = yayin.Yayinci(SahteDepo(), klasor, SABLON, "3.1")
        y.ayar["github"] = {"depo": "a/b", "token": "x", "dal": "main"}
        y.ayar["telegram"] = {"bot_token": "t", "kanal": "@k"}
        yuklenen, gonderilen = [], []
        eski_y, eski_t, eski_s = yayin.github_yukle, yayin.telegram_gonder, yayin.time.sleep
        yayin.github_yukle = lambda gh, dosyalar, mesaj: yuklenen.append(set(dosyalar)) or "sha"
        yayin.telegram_gonder = lambda tg, o, ad: gonderilen.append(o["id"]) or True
        yayin.time.sleep = lambda sn: None
        try:
            self.assertEqual(y.yayinla(), 1)
            self.assertIsNone(y.yayinla())                   # liste aynı: yükleme yok
            self.assertEqual(y.yayinla(zorla=True), 1)
        finally:
            yayin.github_yukle, yayin.telegram_gonder, yayin.time.sleep = eski_y, eski_t, eski_s
        self.assertIn("index.html", yuklenen[0])
        # İlk çalıştırmada yalnızca en iyi birkaç fırsat gider; aynı fırsat ikinci kez gönderilmez
        self.assertEqual(gonderilen, [sabit and yayin.yayin_listesi(sabit, {})[0]["id"]])

    def test_telegram_ilk_tur_sisirilmisi_atlar(self):
        an = time.time()
        gecmis = [[int(an - (60 - i) * 86400), 5999] for i in range(0, 60, 10)]    # fiyat hep aynı → şişirilmiş
        sabit = [ornek(fiyat_gecmisi=gecmis)]

        class SahteDepo:
            import threading
            kilit = threading.Lock()

            def firsatlar(self):
                return sabit
        y = yayin.Yayinci(SahteDepo(), tempfile.mkdtemp(), SABLON, "3.1")
        y.ayar["telegram"] = {"bot_token": "t", "kanal": "@k"}
        liste = y.liste()
        self.assertEqual(liste[0]["karar"], "sisirilmis")
        gonderilen = []
        eski_t = yayin.telegram_gonder
        yayin.telegram_gonder = lambda tg, o, ad: gonderilen.append(o["id"]) or True
        try:
            y._telegram(liste)
        finally:
            yayin.telegram_gonder = eski_t
        self.assertEqual(gonderilen, [])


class KanalKalitesi(unittest.TestCase):
    def test_baslik_temizle(self):
        t = yayin.baslik_temizle
        self.assertEqual(t("Osram Ampul 55W 12V Pgj19-2 Fs1 Osr 64211ULT : Amazon.com.tr: Otomotiv"),
                         "Osram Ampul 55W 12V Pgj19-2 Fs1 Osr 64211ULT")
        self.assertEqual(t("Adidas Tensaur Run 3.0 Pembe 23.5 Fiyatları ve Özellikleri"), "Adidas Tensaur Run 3.0 Pembe 23.5")
        self.assertEqual(t("Haki Regular Düz Bisiklet Yaka Sweatshirt Fiyatları ve Modelleri - Pazarama"),
                         "Haki Regular Düz Bisiklet Yaka Sweatshirt")
        self.assertEqual(t("2'Li Beyaz Patik Erkek Çorap - 15418341 | Boyner"), "2'Li Beyaz Patik Erkek Çorap")
        self.assertEqual(t("Dream Notes A5 Defter - Fiyatı"), "Dream Notes A5 Defter")
        self.assertEqual(t("Philips Airfryer 5000 HD9285/96"), "Philips Airfryer 5000 HD9285/96")   # temiz başlık bozulmaz

    def test_kanal_uygun(self):
        u = yayin.kanal_uygun
        self.assertTrue(u({"karar": "dip"}))
        self.assertTrue(u({"karar": "iyi", "gercek_yuzde": 12}))
        self.assertFalse(u({"karar": "iyi", "gercek_yuzde": 6}))
        self.assertTrue(u({"karar": None, "yuzde": 25}))
        self.assertFalse(u({"karar": None, "yuzde": 12}))
        self.assertFalse(u({"karar": None, "yuzde": None, "akakce": {"fark": 0}}))   # yalnızca "Akakçe en ucuzu" yetmez
        self.assertFalse(u({"karar": "sisirilmis", "yuzde": 60}))

    def test_metin_normal_fiyat_ve_aralik(self):
        o = {"baslik": "Ürün", "fiyat": 900, "eski_fiyat": None, "yuzde": None, "normal": 1200, "gercek_yuzde": 25,
             "karar": "iyi", "etiket": "Normal fiyatın %25 altında", "en_dusuk": 900, "en_yuksek": 1300, "gun": 30,
             "magaza_ad": "Trendyol", "teyit": int(time.time()), "link": "https://www.trendyol.com/a-p-1", "ortaklik": True}
        m = yayin.telegram_metni(o, "Düşero")
        self.assertIn("<s>1.200 TL</s>", m)
        self.assertIn("Son 30 gün: en düşük 900 TL · en yüksek 1.300 TL", m)
        self.assertIn("#işbirliği", m)
        b = yayin.telegram_metni(o, "Düşero", bitis="Mağazada stokta yok")
        self.assertTrue(b.startswith("❌ <b>FIRSAT SONA ERDİ</b> · Mağazada stokta yok"))
        self.assertNotIn("Mağazada gör", b)

    def _yayinci(self, kayitlar):
        class SahteDepo:
            import threading
            kilit = threading.Lock()

            def firsatlar(self):
                return kayitlar
        y = yayin.Yayinci(SahteDepo(), tempfile.mkdtemp(), SABLON, "3.1")
        y.ayar["telegram"] = {"bot_token": "t", "kanal": "@k"}
        return y

    def test_biten_firsat_mesaji_duzenlenir(self):
        kayitlar = [ornek()]
        y = self._yayinci(kayitlar)
        gonderilen, duzenlenen = [], []
        eski_g, eski_b, eski_s = yayin.telegram_gonder, yayin.telegram_bitti_isaretle, yayin.time.sleep
        yayin.telegram_gonder = lambda tg, o, ad: gonderilen.append(o["id"]) or {"mid": 41, "tur": "foto"}
        yayin.telegram_bitti_isaretle = lambda tg, m, neden, ad: duzenlenen.append((m["mid"], neden)) or True
        yayin.time.sleep = lambda sn: None
        try:
            y._telegram(y.liste())
            self.assertEqual(len(gonderilen), 1)
            y._telegram(y.liste())                           # hâlâ geçerli: dokunulmaz
            self.assertEqual(duzenlenen, [])
            kayitlar[0]["dogrulama"] = dict(kayitlar[0]["dogrulama"], durum="red", neden="Mağazada stokta yok",
                                            ts=int(time.time()) + 5)
            y._telegram(y.liste())
            y._telegram(y.liste())                           # ikinci kez düzenlenmez
        finally:
            yayin.telegram_gonder, yayin.telegram_bitti_isaretle, yayin.time.sleep = eski_g, eski_b, eski_s
        self.assertEqual(duzenlenen, [(41, "Mağazada stokta yok")])

    def test_fiyat_yukselince_sona_erdi(self):
        kayitlar = [ornek()]
        y = self._yayinci(kayitlar)
        duzenlenen = []
        eski_g, eski_b, eski_s = yayin.telegram_gonder, yayin.telegram_bitti_isaretle, yayin.time.sleep
        yayin.telegram_gonder = lambda tg, o, ad: {"mid": 7, "tur": "yazi"}
        yayin.telegram_bitti_isaretle = lambda tg, m, neden, ad: duzenlenen.append(neden) or True
        yayin.time.sleep = lambda sn: None
        try:
            y._telegram(y.liste())
            kayitlar[0]["dogrulama"] = dict(kayitlar[0]["dogrulama"], fiyat=6500)
            y._telegram(y.liste())
        finally:
            yayin.telegram_gonder, yayin.telegram_bitti_isaretle, yayin.time.sleep = eski_g, eski_b, eski_s
        self.assertEqual(duzenlenen, ["Fiyat 6.500 TL oldu"])

    def test_zayif_indirim_kanala_gitmez(self):
        y = self._yayinci([ornek(dogrulama=dict(ornek()["dogrulama"], liste_fiyati=6500),          # %8 indirim
                                 akakce_dogrulama={"durum": "ok", "en_ucuz": 5999, "fark": 0})])
        liste = y.liste()
        self.assertEqual(len(liste), 1)                     # sitede durur
        gonderilen = []
        eski_g = yayin.telegram_gonder
        yayin.telegram_gonder = lambda tg, o, ad: gonderilen.append(o["id"]) or True
        try:
            y._telegram(liste)
        finally:
            yayin.telegram_gonder = eski_g
        self.assertEqual(gonderilen, [])


if __name__ == "__main__":
    unittest.main()
