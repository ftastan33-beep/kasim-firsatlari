"""Mağaza sayfalarından üstü çizili (liste) fiyatın okunması.

Örnek sayfalar (tests/ornek/) 13.11.2026'da gerçek ürün sayfalarından sadeleştirildi: yalnızca fiyatla ilgili
gömülü veri ve schema.org kayıtları bırakıldı. İnternet gerektirmez.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import kasim_firsat as k  # noqa: E402

ORNEK = os.path.join(os.path.dirname(__file__), "ornek")


def coz(ad, host):
    with open(os.path.join(ORNEK, ad + ".html"), encoding="utf-8") as f:
        return k.urun_verisi_coz(f.read(), host)


class ListeFiyati(unittest.TestCase):
    def test_trendyol_koseli_parantezli_veri(self):
        # window["__envoy__SHARED_PROPS"] = {... merchantListing.winnerVariant.price.originalPrice ...}
        r = coz("trendyol", "www.trendyol.com")
        self.assertEqual((r["fiyat"], r["liste_fiyati"]), (529.0, 877.26))
        self.assertEqual(r["gtin"], "8683936711428")

    def test_n11_schema_listprice_ve_uygulama_verisi(self):
        r = coz("n11", "www.n11.com")
        self.assertEqual((r["fiyat"], r["liste_fiyati"]), (1249.0, 1449.42))

    def test_boyner_strikethroughprice(self):
        r = coz("boyner", "www.boyner.com.tr")
        self.assertEqual((r["fiyat"], r["liste_fiyati"]), (151.23, 274.97))

    def test_amazon_ana_kutudaki_cizili_fiyat(self):
        r = coz("amazon_cizili", "www.amazon.com.tr")
        self.assertEqual((r["fiyat"], r["liste_fiyati"]), (82.95, 129.9))

    def test_amazon_oneri_kartinin_fiyati_alinmaz(self):
        r = coz("amazon_cizgisiz", "www.amazon.com.tr")
        self.assertEqual(r["fiyat"], 82.95)
        self.assertIsNone(r["liste_fiyati"])

    def test_mantiksiz_liste_fiyati_atilir(self):
        self.assertEqual(k._fiyatlar_coz({"name": "x", "price": 100, "originalPrice": 99}), (100.0, None))
        self.assertEqual(k._fiyatlar_coz({"name": "x", "price": 100, "originalPrice": 5000}), (100.0, None))
        self.assertEqual(k._fiyatlar_coz({"name": "x", "price": 100, "originalPrice": 150}), (100.0, 150.0))

    def test_satis_fiyati_spec_ile_karismaz(self):
        teklif = {"price": "1249.00", "priceSpecification": {"priceType": "https://schema.org/ListPrice", "price": "1449.42"}}
        self.assertEqual(k._fiyatlar_coz({"name": "x", "offers": teklif}), (1249.0, 1449.42))


if __name__ == "__main__":
    unittest.main()
