# -*- coding: utf-8 -*-
"""Düşero — ürün kataloğu.

Mağazaların ve satış ortaklığı (affiliate) ağlarının verdiği ürün feed'lerini okur, bilgisayarda aranabilir bir
katalog tutar. Feed, mağazanın kendi hazırladığı ürün listesidir (ad, fiyat, link, görsel); mağaza sayfaları
taranmaz. Desteklenen biçimler:

  - Google Alışveriş / Merchant XML (RSS ya da Atom; g:price, g:sale_price ...)
  - YML (Admitad ve benzeri ağların "offer" listesi; price, oldprice ...)
  - Türkiye'deki e-ticaret altyapılarının XML çıktıları (Urun / product; fiyat, indirimli_fiyat, barkod ...)
  - CSV (ayraç otomatik bulunur; ilk satır sütun adları)
  - .gz ve .zip ile sıkıştırılmış halleri

Her güncellemede fiyat değişiklikleri kaydedilir; böylece "kayıtlı en düşük fiyat" bilgisi feed'den de oluşur.
Yalnızca Python standart kütüphanesi kullanılır.
"""
import codecs
import contextlib
import csv
import gzip
import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
import zlib

EN_FAZLA_BAYT = 400 * 1024 * 1024      # tek feed dosyası için indirme sınırı
EN_FAZLA_URUN = 300000                 # tek feed'den alınacak en fazla ürün
EN_FAZLA_FEED = 40
VARSAYILAN_ARALIK_SAAT = 6
SILME_GUN = 14                         # feed'den çıkan ürün bu kadar gün sonra katalogdan silinir
GECMIS_GUN = 400
DIP_EN_AZ_GUN = 14                     # "kayıtlı en düşük fiyat" demek için ürünün en az bu kadar izlenmiş olması gerekir
UA = "Mozilla/5.0 (compatible; DuseroKatalog/1.0; urun feed okuyucu)"
csv.field_size_limit(1 << 24)

_TR = str.maketrans({"ı": "i", "İ": "i", "I": "i"})


def sade(s):
    """Arama için: küçük harf, Türkçe karakterler sadeleşir (ş→s, ı→i), noktalama boşluk olur."""
    s = unicodedata.normalize("NFD", (s or "").translate(_TR).lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _anahtar(ad):
    """Etiket / sütun adını karşılaştırılabilir yapar: 'g:sale_price', 'SalePrice', 'sale-price' → 'saleprice'."""
    ad = ad.rsplit("}", 1)[-1].rsplit(":", 1)[-1]
    return sade(ad).replace(" ", "")


ALAN = {
    "baslik": ("title", "name", "baslik", "urunadi", "urunad", "productname", "urunbaslik", "urunismi", "label", "isim", "ad"),
    "indirimli": ("saleprice", "indirimlifiyat", "indirimlifiyati", "indirimlisatisfiyati", "discountedprice",
                  "discountprice", "specialprice", "kampanyalifiyat", "kampanyafiyati", "salepricewithtax"),
    "fiyat": ("price", "fiyat", "satisfiyati", "satisfiyat", "sonfiyat", "pricewithtax", "kdvdahilfiyat", "price1",
              "sellingprice", "currentprice", "finalprice", "tutar"),
    "eski": ("oldprice", "listprice", "listefiyati", "listefiyat", "piyasafiyati", "piyasafiyat", "marketprice",
             "eskifiyat", "regularprice", "priceold", "ustfiyat", "originalprice", "strikeprice"),
    "link": ("link", "url", "producturl", "urunurl", "urunlink", "deeplink", "productlink", "urunadresi", "href"),
    "resim": ("imagelink", "picture", "image", "imageurl", "resim", "resim1", "img", "imgurl", "gorsel", "thumbnail",
              "mainimage", "image1", "imgsrc", "resimurl", "foto"),
    "gtin": ("gtin", "barcode", "barkod", "ean", "ean13", "gtin13"),
    "marka": ("brand", "vendor", "marka", "manufacturer", "uretici"),
    "kid": ("id", "sku", "stokkodu", "stockcode", "urunkodu", "productid", "urunid", "productcode", "code", "mpn", "kod"),
    "durum": ("availability", "available", "instock", "stokdurumu", "stockstatus"),
    "adet": ("stok", "stock", "quantity", "stokadedi", "stokmiktari", "miktar", "stockquantity", "stockamount", "stokadet"),
    "kategori": ("producttype", "category", "kategori", "categorypath", "kategoriadi", "maincategory", "categoryname",
                 "googleproductcategory", "anakategori"),
    "para": ("currency", "currencyid", "parabirimi", "doviz", "pricecurrency"),
}
URUN_ETIKETLERI = {"item", "entry", "offer", "product", "urun", "row", "record", "ilan", "stockitem"}
_SAYI_RE = re.compile(r"-?\d[\d.,\s ]*")
_PARA_RE = re.compile(r"[A-Za-z₺$€£]{1,4}")


def fiyat_coz(metin):
    """'1.299,90 TL', '1299.90 TRY', '1,299.90', '1299' → (1299.9, 'TRY' | None). Okunamazsa (None, None)."""
    if metin is None:
        return None, None
    metin = str(metin).strip()
    m = _SAYI_RE.search(metin)
    if not m:
        return None, None
    para = _PARA_RE.search(metin[:m.start()] + " " + metin[m.end():])
    para = para.group(0).upper() if para else None
    s = re.sub(r"[\s ]", "", m.group(0)).rstrip(".,")
    if "," in s and "." in s:
        ondalik = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if ondalik == "," else ",", "").replace(ondalik, ".")
    elif "," in s:
        # "1,299" (binlik) ile "12,5" / "1299,90" (kuruş) ayrımı: tam üç hane ve birden çok ya da kısa baş → binlik
        bas, _, son = s.rpartition(",")
        s = s.replace(",", "") if (s.count(",") > 1 or (len(son) == 3 and len(bas.lstrip("-")) <= 3)) else bas + "." + son
    elif "." in s:
        bas, _, son = s.rpartition(".")
        if s.count(".") > 1 or (len(son) == 3 and len(bas.lstrip("-")) <= 3):
            s = s.replace(".", "")
    try:
        v = float(s)
    except ValueError:
        return None, None
    return (round(v, 2), para) if v > 0 else (None, para)


def _para_uygun(*paralar):
    for p in paralar:
        if p and p not in ("TRY", "TL", "₺", "YTL", "TRL"):
            return False
    return True


def _ilk(ham, alan):
    for k in ALAN[alan]:
        v = ham.get(k)
        if v:
            return v
    return None


def _stok_coz(ham):
    d = (_ilk(ham, "durum") or "").strip().lower()
    if d:
        d2 = d.replace(" ", "").replace("_", "").replace("-", "")
        if d2 in ("false", "0", "no", "hayir", "yok", "outofstock", "soldout", "discontinued", "stokyok", "tukendi"):
            return 0
        if d2 in ("true", "1", "yes", "evet", "var", "instock", "preorder", "backorder", "stokvar", "limitedavailability"):
            return 1
    a = _ilk(ham, "adet")
    if a:
        v, _ = fiyat_coz(a)
        if v is None:
            return 0 if re.fullmatch(r"\s*0+([.,]0+)?\s*", a) else 1
        return 1 if v > 0 else 0
    return 1


def urun_duzenle(ham):
    """Feed satırı (anahtar → metin) → katalog ürünü. Fiyatı, başlığı ya da linki olmayan satır None döner."""
    baslik = re.sub(r"\s+", " ", _ilk(ham, "baslik") or "").strip()
    link = (_ilk(ham, "link") or "").strip()
    if len(baslik) < 3 or not re.match(r"https?://", link, re.I):
        return None
    para = (_ilk(ham, "para") or "").strip().upper() or None
    f, p1 = fiyat_coz(_ilk(ham, "fiyat"))
    ind, p2 = fiyat_coz(_ilk(ham, "indirimli"))
    eski, p3 = fiyat_coz(_ilk(ham, "eski"))
    if not _para_uygun(para, p1, p2, p3):
        return None                                   # TL dışındaki fiyatlar kataloga alınmaz
    if f and ind:
        fiyat, liste = (ind, f) if ind < f else (f, None)
    else:
        fiyat, liste = f or ind, None
    if not fiyat:
        return None
    if eski and eski > fiyat * 1.01 and (not liste or eski > liste):
        liste = eski
    if liste and liste > fiyat * 20:                  # üstü çizili fiyat 20 kat olamaz: feed hatası
        liste = None
    resim = (_ilk(ham, "resim") or "").strip()
    if resim.startswith("//"):
        resim = "https:" + resim
    gtin = re.sub(r"\D", "", _ilk(ham, "gtin") or "")
    kid = (_ilk(ham, "kid") or "").strip() or hashlib.sha1(link.encode("utf-8")).hexdigest()[:16]
    return {
        "kid": kid[:80], "baslik": baslik[:200], "fiyat": fiyat, "eski_fiyat": liste, "link": link[:2000],
        "resim": resim[:600] if resim.startswith("https://") else None,
        "gtin": gtin if len(gtin) in (8, 12, 13, 14) else None,
        "marka": re.sub(r"\s+", " ", _ilk(ham, "marka") or "").strip()[:60] or None,
        "kategori": re.sub(r"\s+", " ", _ilk(ham, "kategori") or "").strip()[:120] or None,
        "stok": _stok_coz(ham),
    }


# ---------------------------------------------------------------------------
# Feed okuma
# ---------------------------------------------------------------------------
class FeedHata(Exception):
    pass


def _xml_urunleri(akis):
    yigin, urun = [], None
    try:
        for olay, el in ET.iterparse(akis, events=("start", "end")):
            if olay == "start":
                yigin.append(el)
                if urun is None and _anahtar(el.tag) in URUN_ETIKETLERI:
                    urun = el
                continue
            yigin.pop()
            if el is not urun:
                continue
            ham = {}
            for k, v in el.attrib.items():
                ham.setdefault(_anahtar(k), v.strip())
            for alt in el.iter():
                if alt is el:
                    continue
                k = _anahtar(alt.tag)
                v = (alt.text or "").strip() or (alt.get("href") or alt.get("url") or "").strip()
                if v and k not in ham:
                    ham[k] = v
            urun = None
            el.clear()
            if yigin:
                with contextlib.suppress(ValueError):
                    yigin[-1].remove(el)             # belleği boşalt: büyük feed'lerde ağaç büyümesin
            yield ham
    except ET.ParseError as e:
        raise FeedHata(f"XML okunamadı (dosya bozuk ya da yarım inmiş olabilir): {e}") from None


def _csv_urunleri(akis):
    ornek = akis.read(65536)
    akis.seek(0)
    kodlama = "utf-8-sig"
    try:
        codecs.getincrementaldecoder("utf-8")().decode(ornek)
    except UnicodeDecodeError:
        kodlama = "cp1254"
    metin = io.TextIOWrapper(akis, encoding=kodlama, errors="replace", newline="")
    ilk = ornek.decode(kodlama, "replace").split("\n", 1)[0]
    ayrac = max(";,\t|", key=ilk.count)
    okuyucu = csv.reader(metin, delimiter=ayrac)
    try:
        basliklar = [_anahtar(b) for b in next(okuyucu)]
    except StopIteration:
        metin.detach()
        return
    try:
        for satir in okuyucu:
            yield {k: v.strip() for k, v in zip(basliklar, satir) if k and v and v.strip()}
    finally:
        with contextlib.suppress(ValueError):
            metin.detach()                               # alttaki dosyayı _ac kapatır


@contextlib.contextmanager
def _ac(yol):
    """Dosyayı (gerekirse .gz / .zip içinden) ikili akış olarak açar."""
    with open(yol, "rb") as f:
        imza = f.read(4)
    if imza[:2] == b"\x1f\x8b":
        with gzip.open(yol, "rb") as g:
            yield g
    elif imza[:2] == b"PK":
        with zipfile.ZipFile(yol) as z:
            adlar = [n for n in z.namelist() if not n.endswith("/")]
            if not adlar:
                raise FeedHata("Zip dosyası boş")
            en_buyuk = max(adlar, key=lambda n: z.getinfo(n).file_size)
            if z.getinfo(en_buyuk).file_size > EN_FAZLA_BAYT * 4:
                raise FeedHata("Zip içindeki dosya çok büyük")
            with z.open(en_buyuk) as g:
                yield g
    else:
        with open(yol, "rb") as g:
            yield g


def feed_oku(yol, en_fazla=EN_FAZLA_URUN):
    """Feed dosyasındaki ürünleri sırayla verir (urun_duzenle çıktısı). Biçim içerikten anlaşılır."""
    try:
        with _ac(yol) as akis:
            bas = akis.read(4096)
        if not bas.strip():
            raise FeedHata("Dosya boş")
        if b"<!ENTITY" in bas.upper():
            raise FeedHata("Feed'de izin verilmeyen XML tanımı var")
        xml_mi = bas.lstrip(b"\xef\xbb\xbf\xff\xfe \t\r\n\x00").startswith(b"<")
        if xml_mi and re.match(rb"\s*<(!doctype\s+html|html)\b", bas.lstrip(b"\xef\xbb\xbf"), re.I):
            raise FeedHata("Adres ürün listesi yerine bir web sayfası döndürdü (feed linki yanlış ya da giriş gerekiyor)")
        n = 0
        with _ac(yol) as akis:
            for ham in (_xml_urunleri(akis) if xml_mi else _csv_urunleri(akis)):
                u = urun_duzenle(ham)
                if u:
                    yield u
                    n += 1
                    if n >= en_fazla:
                        return
    except (OSError, EOFError, zlib.error, zipfile.BadZipFile, UnicodeError, csv.Error) as e:
        raise FeedHata(f"Dosya okunamadı: {str(e)[:120]}") from None


def feed_indir(url, hedef, zaman_asimi=120):
    """Feed'i hedef dosyaya indirir (boyut sınırlı). Yerel dosya yolu verilirse kopyalamadan onu döndürür."""
    if not re.match(r"https?://", url, re.I):
        if os.path.isfile(url):
            return url
        raise FeedHata("Feed adresi http(s):// ile başlamalı ya da var olan bir dosya olmalı")
    istek = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/xml,text/xml,text/csv,*/*",
                                                 "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(istek, timeout=zaman_asimi) as y, open(hedef, "wb") as f:
            toplam = 0
            while True:
                parca = y.read(1 << 16)
                if not parca:
                    break
                toplam += len(parca)
                if toplam > EN_FAZLA_BAYT:
                    raise FeedHata(f"Feed {EN_FAZLA_BAYT // (1024 * 1024)} MB sınırını aşıyor")
                f.write(parca)
    except FeedHata:
        raise
    except Exception as e:
        kod = getattr(e, "code", None)
        if kod in (401, 403):
            raise FeedHata(f"Feed adresi erişime izin vermedi (HTTP {kod}); link süresi dolmuş ya da program onayı bekliyor olabilir") from None
        if kod == 404:
            raise FeedHata("Feed adresi bulunamadı (HTTP 404); linki kontrol edin") from None
        raise FeedHata(f"Feed indirilemedi: {str(getattr(e, 'reason', e))[:120]}") from None
    return hedef


def link_hazirla(link, sablon):
    """Mağazanın düz ürün linkini ortaklık şablonuna yerleştirir ({url_kodlu} ya da {url})."""
    if not sablon:
        return link
    return sablon.replace("{url_kodlu}", urllib.parse.quote(link, safe="")).replace("{url}", link)


# ---------------------------------------------------------------------------
# Katalog (SQLite)
# ---------------------------------------------------------------------------
SEMA = """
CREATE TABLE IF NOT EXISTS urun(
  id TEXT PRIMARY KEY, feed TEXT NOT NULL, baslik TEXT NOT NULL, ara TEXT NOT NULL, marka TEXT, magaza_ad TEXT,
  fiyat REAL NOT NULL, eski_fiyat REAL, link TEXT NOT NULL, resim TEXT, gtin TEXT, kategori TEXT,
  stok INTEGER NOT NULL DEFAULT 1, ortaklik INTEGER NOT NULL DEFAULT 0, ilk INTEGER NOT NULL, son INTEGER NOT NULL,
  onceki_fiyat REAL, degisim INTEGER, en_dusuk REAL, en_dusuk_ts INTEGER);
CREATE INDEX IF NOT EXISTS urun_feed ON urun(feed, son);
CREATE INDEX IF NOT EXISTS urun_gtin ON urun(gtin);
CREATE TABLE IF NOT EXISTS gecmis(id TEXT NOT NULL, ts INTEGER NOT NULL, fiyat REAL NOT NULL);
CREATE INDEX IF NOT EXISTS gecmis_id ON gecmis(id, ts);
"""
SUTUNLAR = ("id", "feed", "baslik", "marka", "magaza_ad", "fiyat", "eski_fiyat", "link", "resim", "gtin", "kategori",
            "stok", "ortaklik", "ilk", "son", "onceki_fiyat", "degisim", "en_dusuk", "en_dusuk_ts")


class Katalog:
    def __init__(self, yol):
        self.yol = yol
        self.yazma = threading.Lock()
        with self._bag() as b:
            b.executescript(SEMA)

    @contextlib.contextmanager
    def _bag(self):
        b = sqlite3.connect(self.yol, timeout=60)
        try:
            b.execute("PRAGMA journal_mode=WAL")
            b.execute("PRAGMA synchronous=NORMAL")
            with b:
                yield b
        finally:
            b.close()

    def guncelle(self, feed, magaza_ad, urunler, ortaklik=False, sablon=None, an=None):
        """Feed'in ürünlerini kataloğa işler. Tek işlemde yapılır: okuma yarıda kesilirse katalog eski halinde kalır.
        Dönen: {'adet', 'yeni', 'degisen', 'dusen'}."""
        an = int(an or time.time())
        oz = {"adet": 0, "yeni": 0, "degisen": 0, "dusen": 0}
        with self.yazma, self._bag() as b:
            for u in urunler:
                uid = f"{feed}:{u['kid']}"
                link = link_hazirla(u["link"], sablon)
                ara = sade(" ".join(x for x in (u["baslik"], u.get("marka"), magaza_ad) if x))
                eski = b.execute("SELECT fiyat, en_dusuk, son FROM urun WHERE id=?", (uid,)).fetchone()
                if eski and eski[2] == an:
                    continue                                 # aynı ürün feed'de iki kez geçiyor
                oz["adet"] += 1
                if eski is None:
                    oz["yeni"] += 1
                    b.execute("INSERT INTO urun(id,feed,baslik,ara,marka,magaza_ad,fiyat,eski_fiyat,link,resim,gtin,kategori,"
                              "stok,ortaklik,ilk,son,en_dusuk,en_dusuk_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (uid, feed, u["baslik"], ara, u.get("marka"), magaza_ad, u["fiyat"], u.get("eski_fiyat"), link,
                               u.get("resim"), u.get("gtin"), u.get("kategori"), u["stok"], int(bool(ortaklik or sablon)),
                               an, an, u["fiyat"], an))
                    b.execute("INSERT INTO gecmis(id,ts,fiyat) VALUES(?,?,?)", (uid, an, u["fiyat"]))
                    continue
                onceki, en_dusuk = eski[0], eski[1]
                degisti = abs(onceki - u["fiyat"]) > 0.005
                if degisti:
                    oz["degisen"] += 1
                    oz["dusen"] += u["fiyat"] < onceki
                    b.execute("INSERT INTO gecmis(id,ts,fiyat) VALUES(?,?,?)", (uid, an, u["fiyat"]))
                    b.execute("UPDATE urun SET onceki_fiyat=?, degisim=? WHERE id=?", (onceki, an, uid))
                if en_dusuk is None or u["fiyat"] < en_dusuk - 0.005:
                    b.execute("UPDATE urun SET en_dusuk=?, en_dusuk_ts=? WHERE id=?", (u["fiyat"], an, uid))
                b.execute("UPDATE urun SET baslik=?,ara=?,marka=?,magaza_ad=?,fiyat=?,eski_fiyat=?,link=?,resim=?,gtin=?,"
                          "kategori=?,stok=?,ortaklik=?,son=? WHERE id=?",
                          (u["baslik"], ara, u.get("marka"), magaza_ad, u["fiyat"], u.get("eski_fiyat"), link, u.get("resim"),
                           u.get("gtin"), u.get("kategori"), u["stok"], int(bool(ortaklik or sablon)), an, uid))
            if not oz["adet"]:
                raise FeedHata("Feed'de okunabilir ürün bulunamadı (ad, TL fiyat ve link içeren satır yok)")
            # Bu turda feed'de olmayan ürün: önce stokta yok sayılır, uzun süre dönmezse silinir
            b.execute("UPDATE urun SET stok=0 WHERE feed=? AND son<?", (feed, an))
            b.execute("DELETE FROM urun WHERE feed=? AND son<?", (feed, an - SILME_GUN * 86400))
            b.execute("DELETE FROM gecmis WHERE ts<?", (an - GECMIS_GUN * 86400,))
            b.execute("DELETE FROM gecmis WHERE id NOT IN (SELECT id FROM urun) AND id LIKE ?", (feed + ":%",))
        self._ozet = None
        return oz

    def feed_sil(self, feed):
        with self.yazma, self._bag() as b:
            b.execute("DELETE FROM gecmis WHERE id IN (SELECT id FROM urun WHERE feed=?)", (feed,))
            b.execute("DELETE FROM urun WHERE feed=?", (feed,))
        self._ozet = None

    @staticmethod
    def _sozluk(satir, an):
        d = dict(zip(SUTUNLAR, satir))
        d["stok"], d["ortaklik"] = bool(d["stok"]), bool(d["ortaklik"])
        e = d["eski_fiyat"]
        d["yuzde"] = int(round((e - d["fiyat"]) / e * 100)) if e and e > d["fiyat"] * 1.01 else None
        izlenen = (an - d["ilk"]) // 86400
        d["izlenen_gun"] = int(izlenen)
        d["dip"] = bool(izlenen >= DIP_EN_AZ_GUN and d["en_dusuk"] is not None and d["fiyat"] <= d["en_dusuk"] + 0.005
                        and d["onceki_fiyat"] and d["onceki_fiyat"] > d["fiyat"])
        d["dusus"] = (int(round((d["onceki_fiyat"] - d["fiyat"]) / d["onceki_fiyat"] * 100))
                      if d["onceki_fiyat"] and d["onceki_fiyat"] > d["fiyat"] and d["degisim"]
                      and an - d["degisim"] < 7 * 86400 else None)
        return d

    def ara(self, q, limit=60, sirala="uygun"):
        """Sorgudaki bütün kelimeleri içeren ürünler. Dönen: (liste, toplam eşleşme)."""
        kelimeler = sade(q).split()[:8]
        if not kelimeler or max(len(k) for k in kelimeler) < 2:
            return [], 0
        kosul = " AND ".join(["ara LIKE ? ESCAPE '\\'"] * len(kelimeler))
        deger = ["%" + k.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for k in kelimeler]
        an = int(time.time())
        with self._bag() as b:
            toplam = b.execute(f"SELECT COUNT(*) FROM urun WHERE {kosul}", deger).fetchone()[0]
            # Aday seçimi: "iphone" gibi geniş aramada en ucuz 600 ürün kılıf/aksesuar olur; ifadeyi aynen içerenler
            # ve pahalı (asıl) ürünler öne alınır. "En ucuz" sıralamasında adaylar fiyata göre seçilir.
            sira = "fiyat" if sirala == "ucuz" else "(' '||ara||' ') LIKE ? ESCAPE '\\' DESC, fiyat DESC"
            ek = [] if sirala == "ucuz" else ["% " + " ".join(d[1:-1] for d in deger) + " %"]
            satirlar = b.execute(f"SELECT {','.join(SUTUNLAR)}, ara FROM urun WHERE {kosul} ORDER BY stok DESC, {sira} LIMIT 600",
                                 deger + ek).fetchall()
        tam = " ".join(kelimeler)
        sonuc = []
        for s in satirlar:
            d, metin = self._sozluk(s[:-1], an), " " + s[-1] + " "
            kelime_basi = sum((" " + k) in metin for k in kelimeler)
            d["_puan"] = (not d["stok"], -((" " + tam + " ") in metin), -kelime_basi, metin.find(kelimeler[0]), len(metin))
            sonuc.append(d)
        if sirala == "ucuz":
            sonuc.sort(key=lambda d: (not d["stok"], d["fiyat"]))
        elif sirala == "indirim":
            sonuc.sort(key=lambda d: (not d["stok"], -(d["yuzde"] or 0), d["fiyat"]))
        else:
            sonuc.sort(key=lambda d: d["_puan"] + (d["fiyat"],))
        for d in sonuc:
            d.pop("_puan")
        return sonuc[:limit], toplam

    def ozet(self):
        """Feed başına ürün sayısı. Arayüz sık sorduğu için 60 saniye önbellekte tutulur."""
        on = getattr(self, "_ozet", None)
        if on and time.time() - on[0] < 60:
            return on[1]
        with self._bag() as b:
            v = {f: {"adet": n, "stokta": s or 0} for f, n, s in
                 b.execute("SELECT feed, COUNT(*), SUM(stok) FROM urun GROUP BY feed")}
        self._ozet = (time.time(), v)
        return v

    def disa_aktar(self, en_fazla):
        """Herkese açık site için stoktaki ürünler (id sırasıyla; sıra sabit kalsın ki dosyalar gereksiz değişmesin).
        Sınır aşılırsa önce indirimli ve fiyatı yeni düşmüş ürünler alınır."""
        an = int(time.time())
        with self._bag() as b:
            satirlar = b.execute(
                f"SELECT {','.join(SUTUNLAR)} FROM urun WHERE stok=1 ORDER BY "
                "(CASE WHEN onceki_fiyat>fiyat THEN 0 WHEN eski_fiyat>fiyat THEN 1 ELSE 2 END), degisim DESC, id LIMIT ?",
                (int(en_fazla),)).fetchall()
        return sorted((self._sozluk(s, an) for s in satirlar), key=lambda d: d["id"])


# ---------------------------------------------------------------------------
# Feed listesi ve zamanlayıcı
# ---------------------------------------------------------------------------
class Feedler:
    """feedler.json: hangi feed'ler okunacak. Katalogla birlikte çalışır."""

    def __init__(self, veri_dizini):
        os.makedirs(veri_dizini, exist_ok=True)
        self.dizin = veri_dizini
        self.yol = os.path.join(veri_dizini, "feedler.json")
        self.katalog = Katalog(os.path.join(veri_dizini, "katalog.db"))
        self.kilit = threading.RLock()
        self.calisiyor = False
        self.tetik = threading.Event()
        try:
            with open(self.yol, encoding="utf-8") as f:
                self.ayar = json.load(f)
        except (OSError, ValueError):
            self.ayar = {}
        self.ayar.setdefault("feedler", [])
        self.ayar.setdefault("aralik_saat", VARSAYILAN_ARALIK_SAAT)

    def _kaydet(self):
        gecici = self.yol + ".tmp"
        with open(gecici, "w", encoding="utf-8") as f:
            json.dump(self.ayar, f, ensure_ascii=False, indent=1)
        os.replace(gecici, self.yol)

    def ekle(self, ad, url, ortaklik=False, sablon=None, dosya_izin=False):
        ad = re.sub(r"\s+", " ", str(ad or "")).strip()[:40]
        url = str(url or "").strip()
        sablon = str(sablon or "").strip() or None
        if len(ad) < 2:
            raise ValueError("Mağaza adını yazın (ör. Teknosa)")
        if not re.match(r"https?://\S+$", url, re.I) and not (dosya_izin and os.path.isfile(url)):
            raise ValueError("Feed adresi https:// ile başlamalı")
        if sablon and (not re.match(r"https://\S+$", sablon) or ("{url_kodlu}" not in sablon and "{url}" not in sablon)):
            raise ValueError("Ortaklık şablonu https:// ile başlamalı ve içinde {url_kodlu} geçmeli")
        with self.kilit:
            if len(self.ayar["feedler"]) >= EN_FAZLA_FEED:
                raise ValueError(f"En fazla {EN_FAZLA_FEED} feed eklenebilir")
            if any(f["url"] == url for f in self.ayar["feedler"]):
                raise ValueError("Bu feed zaten ekli")
            taban = sade(ad).replace(" ", "-")[:24] or "feed"
            kod, i = taban, 2
            while any(f["kod"] == kod for f in self.ayar["feedler"]):
                kod, i = f"{taban}-{i}", i + 1
            f = {"kod": kod, "ad": ad, "url": url, "ortaklik": bool(ortaklik), "sablon": sablon,
                 "eklenme": int(time.time()), "son": 0, "adet": 0, "hata": None}
            self.ayar["feedler"].append(f)
            self._kaydet()
        self.tetik.set()
        return f

    def sil(self, kod):
        with self.kilit:
            once = len(self.ayar["feedler"])
            self.ayar["feedler"] = [f for f in self.ayar["feedler"] if f["kod"] != kod]
            if len(self.ayar["feedler"]) == once:
                return False
            self._kaydet()
        self.katalog.feed_sil(kod)
        return True

    def birini_guncelle(self, f):
        """Tek feed'i indirir ve kataloğa işler; sonucu feed kaydına yazar."""
        bas = time.time()
        gecici = tempfile.NamedTemporaryFile(prefix="feed-", suffix=".tmp", dir=self.dizin, delete=False)
        gecici.close()
        try:
            dosya = feed_indir(f["url"], gecici.name)
            oz = self.katalog.guncelle(f["kod"], f["ad"], feed_oku(dosya), f.get("ortaklik"), f.get("sablon"))
            sonuc = {"son": int(time.time()), "adet": oz["adet"], "yeni": oz["yeni"], "dusen": oz["dusen"], "hata": None}
        except (FeedHata, sqlite3.Error) as e:
            sonuc = {"son": int(time.time()), "hata": str(e)[:200]}
        finally:
            with contextlib.suppress(OSError):
                os.remove(gecici.name)
        sonuc["sure"] = round(time.time() - bas, 1)
        with self.kilit:
            for g in self.ayar["feedler"]:
                if g["kod"] == f["kod"]:
                    g.update(sonuc)
            self._kaydet()
        return sonuc

    def guncelle(self, zorla=False, bildir=None):
        """Süresi gelen (ya da zorla: tüm) feed'leri günceller. Aynı anda tek tur çalışır."""
        with self.kilit:
            if self.calisiyor:
                return 0
            self.calisiyor = True
            aralik = max(1, float(self.ayar.get("aralik_saat") or VARSAYILAN_ARALIK_SAAT)) * 3600
            sira = [dict(f) for f in self.ayar["feedler"]
                    if zorla or time.time() - (f.get("son") or 0) >= (aralik if not f.get("hata") else min(aralik, 3600))]
        try:
            for f in sira:
                s = self.birini_guncelle(f)
                if bildir:
                    bildir(f, s)
            return len(sira)
        finally:
            self.calisiyor = False

    def dongu(self, bildir=None):
        time.sleep(20)
        while True:
            try:
                self.guncelle(bildir=bildir)
            except Exception as e:                       # zamanlayıcı hiçbir hatada durmasın
                print("[katalog] Hata:", str(e)[:160])
            self.tetik.wait(timeout=600)
            self.tetik.clear()

    def ozet(self):
        sayilar = self.katalog.ozet()
        with self.kilit:
            feedler = [{"kod": f["kod"], "ad": f["ad"], "url": f["url"], "ortaklik": bool(f.get("ortaklik") or f.get("sablon")),
                        "son": f.get("son") or 0, "hata": f.get("hata"), "sure": f.get("sure"),
                        "adet": (sayilar.get(f["kod"]) or {}).get("adet", 0),
                        "stokta": (sayilar.get(f["kod"]) or {}).get("stokta", 0)} for f in self.ayar["feedler"]]
        return {"feedler": feedler, "toplam": sum(f["adet"] for f in feedler), "calisiyor": self.calisiyor,
                "aralik_saat": self.ayar.get("aralik_saat")}

    def son_guncelleme(self):
        with self.kilit:
            return max([f.get("son") or 0 for f in self.ayar["feedler"] if not f.get("hata")] or [0])


def log_satiri(f, s):
    if s.get("hata"):
        print(f"[katalog] {f['ad']}: HATA · {s['hata']}")
    else:
        print(f"[katalog] {f['ad']}: {s['adet']} ürün · {s.get('yeni', 0)} yeni · {s.get('dusen', 0)} fiyatı düşen · {s['sure']} sn")
