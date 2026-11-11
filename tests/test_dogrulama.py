# -*- coding: utf-8 -*-
"""Kasım Fırsatları 3.1 — ayrıştırıcı ve doğrulama hattı testleri (internet gerektirmez).
Çalıştırma:  python -m unittest discover -s tests -v
Sayfa örnekleri elle hazırlanmış yapılardır; gerçek mağaza sayfalarının birebir kopyası değildir."""
import json
import os
import sys
import tempfile
import time
import unittest

KLASOR = tempfile.mkdtemp(prefix="kf_test_")
os.environ["KASIM_FIRSAT_DIZIN"] = KLASOR
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import kasim_firsat as kf  # noqa: E402

TRENDYOL_GIBI = """<html><head><meta property="og:title" content="Philips Airfryer 5000 HD9285/96 XXL - Trendyol"></head><body>
<script>window.__PRODUCT_DETAIL_APP_INITIAL_STATE__={"product":{"id":123,"name":"Philips Airfryer 5000 Serisi HD9285/96 XXL",
"price":{"discountedPrice":{"text":"5.999 TL","value":5999},"sellingPrice":{"text":"6.499 TL","value":6499},
"originalPrice":{"text":"7.999 TL","value":7999}},"images":["https://cdn.example.com/a.jpg"],"inStock":true,"barcode":"8720389012345"},
"recommendations":[{"name":"Silikon Airfryer Kabı","price":{"sellingPrice":{"value":99}}}]};window.x=1;</script></body></html>"""

NEXT_GIBI = """<html><head><meta property="og:title" content="Samsung Galaxy S24 FE SM-S721B 256 GB"></head><body>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Samsung Galaxy S24 FE SM-S721B 256 GB",
"image":"https://img.example.com/s24.jpg","gtin13":"8806095426280","offers":{"@type":"Offer","price":"24999.00","priceCurrency":"TRY",
"availability":"https://schema.org/InStock"}}</script>
<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"product":{"name":"Samsung Galaxy S24 FE SM-S721B 256 GB",
"prices":{"finalPrice":22999,"listPrice":24999},"stockQuantity":4}}}}</script></body></html>"""

STOKSUZ = """<html><head><title>Dyson V15 Detect SV47 | Hepsiburada</title></head><body>
<script type="application/ld+json">[{"@type":"BreadcrumbList"},{"@type":"Product","name":"Dyson V15 Detect SV47",
"offers":{"@type":"Offer","price":"28999","priceCurrency":"TRY","availability":"http://schema.org/OutOfStock"}}]</script></body></html>"""

JSON_PARSE_GIBI = r"""<html><body><script>var s = JSON.parse("{\"product\":{\"productName\":\"Logitech MX Master 3S Mouse\",\"salePrice\":\"2.799,90 TL\",\"isInStock\":false}}");</script></body></html>"""

CLOUDFLARE = """<html><head><title>Just a moment...</title></head><body><div id="challenge-platform">cf-chl</div></body></html>"""

RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>DH Sıcak Fırsatlar</title>
<item><title>Philips HD9285/96 Airfryer 5.999 TL</title><link>https://forum.donanimhaber.com/philips-airfryer--160012345</link>
<pubDate>Tue, 10 Nov 2026 14:20:00 +0300</pubDate><description><![CDATA[Fırsat: <a href="https://www.trendyol.com/philips/airfryer-p-123">ürün</a>]]></description></item>
<item><title>Ana Konu: kurallar</title><link>https://forum.donanimhaber.com/kurallar--160000001</link></item>
</channel></rss>"""

AKAKCE_ARAMA = """<ul><li><a href="/fritoz/en-ucuz-philips-hd9285-96-fiyati,111.html" title="Philips Airfryer 5000 HD9285/96 XXL 7.2 lt">
<img src="https://cdn.akakce.com/a.jpg"></a><span>En Ucuz</span><b>5.799,00 TL</b></li>
<li><a href="/fritoz-aksesuar/en-ucuz-airfryer-kagidi-fiyati,222.html" title="Airfryer Pişirme Kağıdı 100'lü"></a><b>89,90 TL</b></li></ul>"""


class Ayristirici(unittest.TestCase):
    def test_trendyol_gomulu_state(self):
        u = kf.urun_verisi_coz(TRENDYOL_GIBI, "www.trendyol.com")
        self.assertEqual(u["fiyat"], 5999)              # indirimli fiyat, satış fiyatı değil
        self.assertEqual(u["liste_fiyati"], 7999)
        self.assertIs(u["stok"], True)
        self.assertIn("HD9285/96", u["baslik"])
        self.assertEqual(u["resim"], "https://cdn.example.com/a.jpg")
        self.assertEqual(u["gtin"], "8720389012345")

    def test_jsonld_ve_next_data_birlesir(self):
        u = kf.urun_verisi_coz(NEXT_GIBI, "www.ornek.com")
        self.assertEqual(u["fiyat"], 22999)             # JSON-LD 24.999 yazıyor, uygulama verisi indirimli 22.999
        self.assertEqual(u["liste_fiyati"], 24999)
        self.assertIs(u["stok"], True)
        self.assertEqual(u["gtin"], "8806095426280")

    def test_stoksuz_jsonld(self):
        u = kf.urun_verisi_coz(STOKSUZ, "www.hepsiburada.com")
        self.assertEqual(u["fiyat"], 28999)
        self.assertIs(u["stok"], False)
        self.assertEqual(u["baslik"], "Dyson V15 Detect SV47")

    def test_json_parse_dizesi(self):
        u = kf.urun_verisi_coz(JSON_PARSE_GIBI, "www.ornek.com")
        self.assertAlmostEqual(u["fiyat"], 2799.90)
        self.assertIs(u["stok"], False)

    def test_engel_algilama(self):
        self.assertTrue(kf.engel_mi(200, CLOUDFLARE))
        self.assertTrue(kf.engel_mi(403, "<html></html>"))
        self.assertFalse(kf.engel_mi(200, TRENDYOL_GIBI))

    def test_rss(self):
        o = kf.rss_coz(RSS)
        self.assertEqual(len(o), 2)
        self.assertEqual(o[0]["baslik"], "Philips HD9285/96 Airfryer 5.999 TL")
        self.assertEqual(o[0]["tarih"].utcoffset().total_seconds(), 3 * 3600)
        with self.assertRaises(ValueError):
            kf.rss_coz('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><rss></rss>')

    def test_dh_rss_magaza_linki(self):
        eski = kf.magaza_sayfasi_getir
        kf.magaza_sayfasi_getir = lambda *a, **k: {"durum": "ok", "html": RSS, "kod": 200, "denenen": []}
        try:
            konular = kf.dh_rss_cek()
        finally:
            kf.magaza_sayfasi_getir = eski
        self.assertEqual(konular[0]["magaza_link"], "https://www.trendyol.com/philips/airfryer-p-123")
        g = kf.dhforum_gonderi(konular[0], kf.simdi())
        self.assertEqual(g["magaza"], "trendyol")
        self.assertEqual(g["fiyat"], 5999)
        self.assertTrue(g["urun_link"].startswith("https://www.trendyol.com/"))

    def test_akakce_eslesme(self):
        eski = kf.magaza_sayfasi_getir
        kf.magaza_sayfasi_getir = lambda *a, **k: {"durum": "ok", "html": AKAKCE_ARAMA, "kod": 200, "denenen": []}
        try:
            r = kf.akakce_karsilastir("Philips Airfryer 5000 Serisi HD9285/96 XXL", 5999)
            yok = kf.akakce_karsilastir("Tefal Easy Fry EY5018 Airfryer", 4000)
        finally:
            kf.magaza_sayfasi_getir = eski
        self.assertEqual(r["durum"], "ok")
        self.assertEqual(r["en_ucuz"], 5799)
        self.assertEqual(r["eslesme"], "model kodu")
        self.assertAlmostEqual(r["fark"], 3.4, places=1)
        self.assertEqual(yok["durum"], "bulunamadi")     # model kodu tutmuyor → yanlış eşleşme yerine hiç


class Hat(unittest.TestCase):
    """Aşama 1 → kuyruk → Aşama 2 → liste"""

    def _depo(self):
        d = kf.Depo()
        d.veri["gonderiler"], d.veri["kuyruk"], d.veri["canli_son"] = {}, {}, {}
        d.ayarlar["akakce_dogrula"] = False
        return d

    def _urun(self, d, no, fiyat, link):
        g = kf.gonderi_olustur("firsatz", no, f"Philips Airfryer HD9285/96\n{kf.fiyat_yaz(fiyat)}", [link], None, kf.simdi(), None)
        d.veri["gonderiler"][g["id"]] = g
        return g["id"]

    def test_dogrulama_karar_ve_liste(self):
        d = self._depo()
        a = self._urun(d, 1, 5999, "https://www.trendyol.com/x/airfryer-p-1")
        b = self._urun(d, 2, 5999, "https://www.trendyol.com/x/airfryer-p-2")
        c = self._urun(d, 3, 5999, "https://www.amazon.com.tr/dp/B0TEST1234")
        e = self._urun(d, 4, 5999, "https://www.hepsiburada.com/x-p-HBC0001")
        yanit = {
            "https://www.trendyol.com/x/airfryer-p-1": {"durum": "ok", "fiyat": 5999, "stok_yok": False, "baslik": "Philips Airfryer 5000 HD9285/96",
                                                        "resim": "https://cdn/x.jpg", "kampanyalar": None, "motor": "urllib"},
            "https://www.trendyol.com/x/airfryer-p-2": {"durum": "ok", "fiyat": 7499, "stok_yok": False, "kampanyalar": None, "motor": "urllib"},
            "https://www.amazon.com.tr/dp/B0TEST1234": {"durum": "stok_yok", "motor": "urllib"},
            "https://www.hepsiburada.com/x-p-HBC0001": {"durum": "engellendi", "son_url": "https://www.hepsiburada.com/x-p-HBC0001",
                                                        "motor": "urllib"},
        }
        eski = kf.canli_fiyat_getir
        kf.canli_fiyat_getir = lambda url, tarayici=None: dict(yanit[url])
        try:
            with d.kilit:
                d._kuyruga_al([a, b, c, e])
            self.assertEqual(len(d.veri["kuyruk"]), 4)
            n = d.dogrulama_turu()
        finally:
            kf.canli_fiyat_getir = eski
        self.assertEqual(n, 4)
        self.assertEqual(d.veri["kuyruk"], {})
        gon = d.veri["gonderiler"]
        self.assertEqual(gon[a]["dogrulama"]["durum"], "onayli")
        self.assertEqual(gon[a]["resim"], "https://cdn/x.jpg")         # zenginleştirme
        self.assertEqual(gon[b]["dogrulama"]["durum"], "red")           # fiyat yükselmiş
        self.assertEqual(gon[c]["dogrulama"]["durum"], "red")           # stok yok
        self.assertEqual(gon[e]["dogrulama"]["durum"], "okunamadi")     # engel: listeden atılmaz
        self.assertIn("hepsiburada", d.veri["engel"])                   # 6 saat denenmez, kalıcı kayıt
        liste = {x["id"]: x for x in d.firsatlar()}
        tum = [x for x in liste.values()]
        gorunen = [x for x in tum if not x.get("bitti")]
        self.assertEqual(len(gorunen), 2)                               # a + e (b ve c elendi; aynı ürün tek kartta da olabilir)

    def test_gecici_hata_yeniden_denenir(self):
        d = self._depo()
        a = self._urun(d, 9, 1000, "https://www.n11.com/urun/x-123456")
        eski = kf.canli_fiyat_getir
        kf.canli_fiyat_getir = lambda url, tarayici=None: {"durum": "hata", "mesaj": "zaman aşımı"}
        try:
            with d.kilit:
                d._kuyruga_al([a])
            d.dogrulama_turu()
        finally:
            kf.canli_fiyat_getir = eski
        k = d.veri["kuyruk"][a]
        self.assertEqual(k["n"], 1)
        self.assertGreater(k["s"], time.time() + 1000)                   # 20 dk sonra

    def test_kanal_yeniden_okununca_dogrulama_korunur(self):
        d = self._depo()
        a = self._urun(d, 7, 5999, "https://www.trendyol.com/x/airfryer-p-7")
        d.veri["gonderiler"][a].update(dogrulama={"durum": "onayli", "ts": int(time.time())}, resim="https://cdn/r.jpg",
                                       magaza_basligi="Philips Airfryer 5000 HD9285/96")
        # yenile() içindeki kanal okuması paylaşımı baştan oluşturur
        yeni = kf.gonderi_olustur("firsatz", 7, "Philips Airfryer HD9285/96\n5.999 TL",
                                  ["https://www.trendyol.com/x/airfryer-p-7"], None, kf.simdi(), None)
        eski_kanal_cek = kf.kanal_cek
        kf.kanal_cek = lambda kanal, sayfa, kupon: ("Fırsat Z", [dict(yeni)] if kanal == "firsatz" else [])
        eski_dis, eski_takip = d._dis_kaynaklari_yenile, d._takipleri_ara
        d._dis_kaynaklari_yenile = lambda: []
        d._takipleri_ara = lambda: []
        d._takibimi_guncelle = d._kacirilmazlari_dogrula = d._geriye_tara = lambda *a, **k: None
        d._arsivi_tamamla = lambda *a: None
        try:
            d.yenile()
        finally:
            kf.kanal_cek = eski_kanal_cek
        g = d.veri["gonderiler"][a]
        self.assertEqual(g["dogrulama"]["durum"], "onayli")
        self.assertEqual(g["resim"], "https://cdn/r.jpg")
        self.assertNotIn(a, d.veri["kuyruk"])          # taze onay: yeniden kuyruğa girmez

    def test_eski_veri_dosyasi_aciliyor(self):
        # 3.0'ın yazdığı veri dosyası (kuyruk/engel alanı yok) sorunsuz açılmalı
        with open(kf.VERI_DOSYASI, "w", encoding="utf-8") as f:
            json.dump({"gonderiler": {}, "gecmis": {}}, f)
        d = kf.Depo()
        self.assertEqual(d.veri["kuyruk"], {})
        self.assertIn("dogrulama", d.durum())



class FarkliVaryantTesti(unittest.TestCase):
    """S26 FE ile S26 / S26 Ultra / S25 FE / farklı hafıza aynı ürün sayılmamalı."""
    B = "Samsung Galaxy S26 FE 256 GB 8 GB Ram (Samsung Türkiye Garantili) Grafit Gri"

    def test_farkli_modeller(self):
        import kasim_firsat as k
        for a in ("Samsung Galaxy S26 256 GB 12 GB Ram (Samsung Türkiye Garantili) Siyah",
                  "Samsung Galaxy S26 Ultra 256 GB", "Samsung Galaxy S26 FE 128 GB 8 GB Ram",
                  "Samsung Galaxy S25 FE 256 GB 8 GB Ram (Samsung Türkiye Garantili) Lacivert", "Galaxy S26+ 256GB"):
            self.assertTrue(k.farkli_varyant(self.B, a), a)
            self.assertFalse(k.ayni_urun({"baslik": self.B}, {"baslik": a}), a)
            self.assertEqual(k._benzerlik(self.B, a), 0.0, a)

    def test_ayni_urun_farkli_yazim(self):
        import kasim_firsat as k
        for a in ("Samsung Galaxy S26 FE", "Samsung S26 FE 256GB Grafit",
                  "Samsung Galaxy S26 FE 256 GB 8 GB Ram Lacivert"):
            self.assertFalse(k.farkli_varyant(self.B, a), a)
        self.assertFalse(k.farkli_varyant("Logitech MX Master 3S Mouse", "Logitech MX Master 3S Kablosuz Mouse Grafit"))

    def test_baglanti_baska_urune_gidiyor(self):
        import kasim_firsat as k
        g = {"baslik": self.B, "kanal": "x"}
        self.assertIsNotNone(k.baglanti_farkli_urun(g, {"baslik": "Samsung Galaxy S26 Ultra 512 GB"}))
        self.assertIsNone(k.baglanti_farkli_urun(g, {"baslik": "Samsung Galaxy S26 FE 256 GB Grafit"}))


if __name__ == "__main__":
    unittest.main()
