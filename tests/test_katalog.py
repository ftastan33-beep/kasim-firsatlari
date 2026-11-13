# -*- coding: utf-8 -*-
"""Ürün kataloğu (feed) testleri (internet gerektirmez)."""
import gzip
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import katalog  # noqa: E402
import yayin  # noqa: E402

GOOGLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0"><channel><title>Mağaza</title><link>https://magaza.example</link>
<item><g:id>A1</g:id><title>Philips Airfryer XL HD9270</title><link>https://magaza.example/urun/a1?utm_source=feed</link>
 <g:image_link>https://cdn.example/a1.jpg</g:image_link><g:price>5999.00 TRY</g:price><g:sale_price>4499.90 TRY</g:sale_price>
 <g:availability>in stock</g:availability><g:brand>Philips</g:brand><g:gtin>8710103900000</g:gtin><g:product_type>Ev &gt; Mutfak</g:product_type></item>
<item><g:id>A2</g:id><title>Şarjlı Süpürge İnce Model</title><link>https://magaza.example/urun/a2</link>
 <g:image_link>http://cdn.example/a2.jpg</g:image_link><g:price>1.299,50 TL</g:price><g:availability>out of stock</g:availability></item>
<item><g:id>A3</g:id><title>Dolar fiyatlı ürün</title><link>https://magaza.example/urun/a3</link><g:price>19.99 USD</g:price></item>
<item><g:id>A4</g:id><title>Fiyatsız ürün</title><link>https://magaza.example/urun/a4</link></item>
</channel></rss>"""

YML = """<?xml version="1.0" encoding="UTF-8"?>
<yml_catalog date="2026-11-12"><shop><name>Ağ Mağazası</name><categories><category id="1">Telefon</category></categories>
<offers>
<offer id="77" available="true"><url>https://ad.example/g/abc/?ulp=https%3A%2F%2Fm.example%2Fp77</url><price>27599</price>
 <oldprice>31000</oldprice><currencyId>TRY</currencyId><categoryId>1</categoryId><picture>https://cdn.example/77.jpg</picture>
 <name>Samsung Galaxy S26 FE 256 GB</name><vendor>Samsung</vendor><param name="Renk">Siyah</param></offer>
<offer id="78" available="false"><url>https://ad.example/g/abc/?ulp=p78</url><price>950</price><currencyId>TRY</currencyId>
 <name>Galaxy S26 Kılıf</name></offer>
</offers></shop></yml_catalog>"""

TR_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Urunler><Urun><UrunKodu>K-5</UrunKodu><UrunAdi>Oyuncu Koltuğu Pro</UrunAdi><Fiyat>4.250,00</Fiyat>
<IndirimliFiyat>3.599,00</IndirimliFiyat><ParaBirimi>TL</ParaBirimi><StokAdedi>12</StokAdedi><Barkod>8690000000017</Barkod>
<UrunUrl>https://tr.example/oyuncu-koltugu-pro</UrunUrl><Resimler><Resim>https://tr.example/k5.jpg</Resim></Resimler>
<Marka>Rakip</Marka><Kategori>Mobilya</Kategori></Urun>
<Urun><UrunKodu>K-6</UrunKodu><UrunAdi>Stoksuz Ürün</UrunAdi><Fiyat>100</Fiyat><StokAdedi>0</StokAdedi>
<UrunUrl>https://tr.example/stoksuz</UrunUrl></Urun></Urunler>"""

CSV = "id;name;price;oldprice;url;picture;vendor;available\n" \
      "c1;Çelik Tencere Seti 8 Parça;1899,90;2499,90;https://csv.example/c1;https://csv.example/c1.jpg;Marka;true\n" \
      "c2;Eksik satır;;;https://csv.example/c2;;;true\n"


def yaz(dizin, ad, metin, sikistir=False):
    yol = os.path.join(dizin, ad)
    with (gzip.open if sikistir else open)(yol, "wb") as f:
        f.write(metin.encode("utf-8"))
    return yol


class Fiyat(unittest.TestCase):
    def test_bicimler(self):
        for metin, beklenen in [("1.299,90 TL", 1299.9), ("1299.90 TRY", 1299.9), ("1,299.90", 1299.9), ("1299", 1299.0),
                                ("12,5", 12.5), ("27.599", 27599.0), ("1.234.567,89", 1234567.89), ("4499.9", 4499.9),
                                ("₺ 349,00", 349.0), ("5999.00 TRY", 5999.0)]:
            self.assertEqual(katalog.fiyat_coz(metin)[0], beklenen, metin)
        self.assertEqual(katalog.fiyat_coz("19.99 USD"), (19.99, "USD"))
        self.assertEqual(katalog.fiyat_coz("yok"), (None, None))
        self.assertEqual(katalog.fiyat_coz("0"), (None, None))

    def test_sade(self):
        self.assertEqual(katalog.sade("Şarjlı SÜPÜRGE, İnce-Model!"), "sarjli supurge ince model")
        self.assertEqual(katalog.sade("IŞIK ığdır"), "isik igdir")


class Okuma(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_google(self):
        l = list(katalog.feed_oku(yaz(self.d, "g.xml", GOOGLE)))
        self.assertEqual([u["kid"] for u in l], ["A1", "A2"])          # dolar fiyatlı ve fiyatsız ürün alınmaz
        a1, a2 = l
        self.assertEqual((a1["fiyat"], a1["eski_fiyat"]), (4499.9, 5999.0))
        self.assertEqual(a1["gtin"], "8710103900000")
        self.assertEqual(a1["marka"], "Philips")
        self.assertEqual(a1["stok"], 1)
        self.assertEqual(a1["kategori"], "Ev > Mutfak")
        self.assertEqual((a2["fiyat"], a2["eski_fiyat"], a2["stok"]), (1299.5, None, 0))
        self.assertIsNone(a2["resim"])                                  # http görsel sitede gösterilemez

    def test_yml_ve_gzip(self):
        l = list(katalog.feed_oku(yaz(self.d, "y.xml.gz", YML, sikistir=True)))
        self.assertEqual(len(l), 2)
        self.assertEqual(l[0]["baslik"], "Samsung Galaxy S26 FE 256 GB")
        self.assertEqual((l[0]["fiyat"], l[0]["eski_fiyat"], l[0]["stok"]), (27599.0, 31000.0, 1))
        self.assertTrue(l[0]["link"].startswith("https://ad.example/g/abc/"))   # ağın linki olduğu gibi kalır
        self.assertEqual(l[1]["stok"], 0)

    def test_turkce_xml(self):
        l = list(katalog.feed_oku(yaz(self.d, "t.xml", TR_XML)))
        self.assertEqual(l[0]["kid"], "K-5")
        self.assertEqual((l[0]["fiyat"], l[0]["eski_fiyat"]), (3599.0, 4250.0))
        self.assertEqual(l[0]["resim"], "https://tr.example/k5.jpg")
        self.assertEqual(l[0]["gtin"], "8690000000017")
        self.assertEqual(l[1]["stok"], 0)

    def test_csv(self):
        l = list(katalog.feed_oku(yaz(self.d, "c.csv", CSV)))
        self.assertEqual(len(l), 1)
        self.assertEqual((l[0]["baslik"], l[0]["fiyat"], l[0]["eski_fiyat"]), ("Çelik Tencere Seti 8 Parça", 1899.9, 2499.9))

    def test_hatali_dosyalar(self):
        with self.assertRaises(katalog.FeedHata):
            list(katalog.feed_oku(yaz(self.d, "h.xml", "<!doctype html><html><body>Giriş yapın</body></html>")))
        with self.assertRaises(katalog.FeedHata):
            list(katalog.feed_oku(yaz(self.d, "b.xml", GOOGLE[:600])))          # yarım inmiş dosya
        with self.assertRaises(katalog.FeedHata):
            list(katalog.feed_oku(yaz(self.d, "e.xml", '<!DOCTYPE x [<!ENTITY a "b">]><rss/>')))
        with self.assertRaises(katalog.FeedHata):
            list(katalog.feed_oku(yaz(self.d, "bos.xml", "  ")))


class Depolama(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.k = katalog.Katalog(os.path.join(self.d, "k.db"))
        self.an = int(time.time())

    def urun(self, kid="1", baslik="Philips Airfryer XL", fiyat=5000.0, **k):
        u = {"kid": kid, "baslik": baslik, "fiyat": fiyat, "eski_fiyat": None, "link": f"https://m.example/{kid}",
             "resim": None, "gtin": None, "marka": None, "kategori": None, "stok": 1}
        u.update(k)
        return u

    def test_fiyat_gecmisi_ve_dip(self):
        gun = 86400
        self.k.guncelle("m", "Mağaza", [self.urun()], an=self.an - 30 * gun)
        oz = self.k.guncelle("m", "Mağaza", [self.urun(fiyat=5000.0)], an=self.an - 20 * gun)
        self.assertEqual((oz["yeni"], oz["degisen"]), (0, 0))
        oz = self.k.guncelle("m", "Mağaza", [self.urun(fiyat=4000.0)], an=self.an - gun)
        self.assertEqual((oz["degisen"], oz["dusen"]), (1, 1))
        d = self.k.ara("airfryer")[0][0]
        self.assertEqual((d["fiyat"], d["onceki_fiyat"], d["dusus"]), (4000.0, 5000.0, 20))
        self.assertTrue(d["dip"])
        self.assertIsNone(d["yuzde"])                         # mağaza üstü çizili fiyat vermedi: indirim oranı uydurulmaz
        self.k.guncelle("m", "Mağaza", [self.urun(fiyat=4500.0)], an=self.an)
        self.assertFalse(self.k.ara("airfryer")[0][0]["dip"])  # fiyat yükseldi

    def test_yeni_urun_dip_sayilmaz(self):
        self.k.guncelle("m", "Mağaza", [self.urun()], an=self.an - 3600)
        self.k.guncelle("m", "Mağaza", [self.urun(fiyat=100.0)], an=self.an)
        self.assertFalse(self.k.ara("airfryer")[0][0]["dip"])  # 14 günden az izlenen üründe "en düşük" denmez

    def test_feedden_cikan_urun(self):
        self.k.guncelle("m", "Mağaza", [self.urun("1"), self.urun("2", "Başka Ürün")], an=self.an - 86400)
        self.k.guncelle("m", "Mağaza", [self.urun("1")], an=self.an)
        self.assertFalse(self.k.ara("baska")[0][0]["stok"])
        self.k.guncelle("m", "Mağaza", [self.urun("1")], an=self.an + 20 * 86400)
        self.assertEqual(self.k.ara("baska"), ([], 0))          # 14 günden uzun süredir feed'de yok: silinir

    def test_yarim_feed_katalogu_bozmaz(self):
        self.k.guncelle("m", "Mağaza", [self.urun("1"), self.urun("2", "Başka Ürün")], an=self.an - 86400)

        def bozuk():
            yield self.urun("1", fiyat=1.0)
            raise katalog.FeedHata("yarım")

        with self.assertRaises(katalog.FeedHata):
            self.k.guncelle("m", "Mağaza", bozuk(), an=self.an)
        self.assertEqual(self.k.ara("airfryer")[0][0]["fiyat"], 5000.0)
        self.assertTrue(self.k.ara("baska")[0][0]["stok"])
        with self.assertRaises(katalog.FeedHata):
            self.k.guncelle("m", "Mağaza", [], an=self.an)       # boş feed de ürünleri silmez
        self.assertTrue(self.k.ara("baska")[0][0]["stok"])

    def test_arama(self):
        self.k.guncelle("m", "Teknosa", [
            self.urun("1", "Apple iPhone 16 128 GB Siyah", 64999.0),
            self.urun("2", "iPhone 16 Şeffaf Kılıf", 199.0),
            self.urun("3", "Şarjlı Süpürge", 3000.0, stok=0),
            self.urun("4", "%100 Pamuk_Havlu", 250.0)], an=self.an)
        self.assertEqual([d["baslik"] for d in self.k.ara("IPHONE 16")[0]],
                         ["iPhone 16 Şeffaf Kılıf", "Apple iPhone 16 128 GB Siyah"])
        self.assertEqual(self.k.ara("iphone", sirala="ucuz")[0][0]["fiyat"], 199.0)
        self.assertEqual(self.k.ara("supurge")[0][0]["baslik"], "Şarjlı Süpürge")     # Türkçe harf duyarsız
        self.assertEqual(self.k.ara("teknosa iphone")[1], 2)                           # mağaza adıyla da bulunur
        self.assertEqual(self.k.ara("yok boyle bir sey")[1], 0)
        self.assertEqual(self.k.ara("%")[1], 0)
        self.assertEqual(self.k.ara("a")[1], 0)
        self.assertEqual(self.k.ara("pamuk havlu")[1], 1)

    def test_ortaklik_sablonu(self):
        self.k.guncelle("m", "Mağaza", [self.urun()], sablon="https://ag.example/g/x/?ulp={url_kodlu}", an=self.an)
        d = self.k.ara("airfryer")[0][0]
        self.assertEqual(d["link"], "https://ag.example/g/x/?ulp=https%3A%2F%2Fm.example%2F1")
        self.assertTrue(d["ortaklik"])


class FeedListesi(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.fl = katalog.Feedler(self.d)

    def test_ekle_guncelle_sil(self):
        yol = yaz(self.d, "g.xml", GOOGLE)
        with self.assertRaises(ValueError):
            self.fl.ekle("Mağaza", yol)                          # dosya yolu yalnızca komut satırından eklenir
        with self.assertRaises(ValueError):
            self.fl.ekle("", "https://x.example/feed.xml")
        with self.assertRaises(ValueError):
            self.fl.ekle("Mağaza", "https://x.example/f.xml", sablon="https://ag.example/")   # {url_kodlu} yok
        f = self.fl.ekle("Örnek Mağaza", yol, dosya_izin=True)
        self.assertEqual(f["kod"], "ornek-magaza")
        with self.assertRaises(ValueError):
            self.fl.ekle("Örnek Mağaza", yol, dosya_izin=True)   # aynı adres ikinci kez eklenmez
        self.assertEqual(self.fl.guncelle(), 1)
        oz = self.fl.ozet()
        self.assertEqual((oz["toplam"], oz["feedler"][0]["adet"], oz["feedler"][0]["stokta"]), (2, 2, 1))
        self.assertIsNone(oz["feedler"][0]["hata"])
        self.assertEqual(self.fl.guncelle(), 0)                  # süresi gelmeden yeniden indirilmez
        self.assertEqual(katalog.Feedler(self.d).ozet()["toplam"], 2)   # yeniden açılınca ayar ve katalog yerinde
        self.assertTrue(self.fl.sil("ornek-magaza"))
        self.assertEqual(self.fl.ozet()["toplam"], 0)
        self.assertFalse(self.fl.sil("ornek-magaza"))

    def test_hata_kaydedilir(self):
        self.fl.ekle("Bozuk", yaz(self.d, "b.xml", GOOGLE[:600]), dosya_izin=True)
        self.fl.guncelle()
        f = self.fl.ozet()["feedler"][0]
        self.assertIn("XML", f["hata"])
        self.assertEqual(f["adet"], 0)


class Yayin(unittest.TestCase):
    def test_katalog_dosyalari(self):
        d = tempfile.mkdtemp()
        k = katalog.Katalog(os.path.join(d, "k.db"))
        urunler = [{"kid": str(i), "baslik": f"Ürün {i}", "fiyat": 100.0 + i, "eski_fiyat": 150.0 if i % 2 else None,
                    "link": f"https://www.amazon.com.tr/dp/B0ABCDEF{i:02d}?utm_source=x", "resim": None, "gtin": None,
                    "marka": None, "kategori": None, "stok": 0 if i == 7 else 1} for i in range(40)]
        k.guncelle("m", "Amazon", urunler)
        disari = k.disa_aktar(1000)
        self.assertEqual(len(disari), 39)                         # stokta olmayan siteye gitmez
        self.assertEqual(len(k.disa_aktar(10)), 10)
        eski_parca = yayin.KATALOG_PARCA
        yayin.KATALOG_PARCA = 10
        try:
            ayar = {"ortaklik": {"amazon": {"param": {"tag": "bizim-21"}}}}
            dosyalar = yayin.katalog_dosyalari(disari, 1234, ayar)
            self.assertEqual(dosyalar, yayin.katalog_dosyalari(disari, 1234, ayar))   # aynı girdi aynı dosyalar
        finally:
            yayin.KATALOG_PARCA = eski_parca
        oz = json.loads(dosyalar["katalog/ozet.json"])
        self.assertEqual((oz["adet"], oz["parca"], oz["guncelleme"]), (39, 4, 1234))
        hepsi = [u for i in range(oz["parca"]) for u in json.loads(dosyalar[f"katalog/{i}.json"])]
        self.assertEqual(len(hepsi), 39)
        self.assertEqual(len({u[0] for u in hepsi}), 39)
        bir = next(u for u in hepsi if u[1] == "Ürün 1")
        self.assertEqual(bir[2:5], ["Amazon", 101.0, 150.0])
        self.assertEqual(bir[5], "https://www.amazon.com.tr/dp/B0ABCDEF01?tag=bizim-21")   # izleme kodu temizlenir, bizimki eklenir
        self.assertEqual(bir[7], 1)
        self.assertEqual(yayin.katalog_dosyalari([], 1), {})

    def test_magaza_kodu(self):
        self.assertEqual(yayin.magaza_kodu("https://www.amazon.com.tr/dp/X"), "amazon")
        self.assertEqual(yayin.magaza_kodu("https://m.trendyol.com/a-p-1"), "trendyol")
        self.assertEqual(yayin.magaza_kodu("https://teknosa.com/x"), "teknosa")


if __name__ == "__main__":
    unittest.main()
