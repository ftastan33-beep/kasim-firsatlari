#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kasım Fırsatları — Android telefonda (Termux) çalışan yerel indirim takipçisi.

Ne yapar?
  * Herkese açık Türk fırsat kanallarının Telegram web önizleme sayfalarını
    (https://t.me/s/<kanal>) belirli aralıklarla okur.
  * Her paylaşımdan ürün adı, fiyat, mağaza, kupon kodu ve indirim bilgisini çıkarır.
  * İndirim oranını şu kaynaklardan bulur (hangisi yüksekse o gösterilir):
      1) Paylaşımda yazan yüzde            ("%30 indirim")
      2) Paylaşımda yazan eski / yeni fiyat ("4.999 TL yerine 3.299 TL", üstü çizili fiyat)
      3) "3 al 2 öde" türü kampanyalar
      4) Programın kendi fiyat kaydı        (aynı ürünü daha önce daha pahalı gördüyse)
  * Sonuçları telefonun tarayıcısında açılan yerel sayfada gösterir:
      http://127.0.0.1:8765

Kurulum (Termux):
    pkg update && pkg install python
    python kasim_firsat.py

Yalnızca Python standart kütüphanesini kullanır; ek paket gerekmez.
"""

import argparse
import hmac
import html
import base64
import functools
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SURUM = "2.6"
VARSAYILAN_PORT = 8765
VERI_DIZINI = os.environ.get("KASIM_FIRSAT_DIZIN") or os.path.join(os.path.expanduser("~"), ".kasim_firsat")
VERI_DOSYASI = os.path.join(VERI_DIZINI, "veri.json")
AYAR_DOSYASI = os.path.join(VERI_DIZINI, "ayarlar.json")
ANAHTAR_DOSYASI = os.path.join(VERI_DIZINI, "erisim_anahtari.txt")

VARSAYILAN_AYARLAR = {
    "kanallar": ["dhsicak_firsatlar", "firsatz", "tazefirsat", "indirimbakanligi", "indirimhaber", "trendyolfirsat"],
    "sayfa": 6,            # kanal başına okunacak sayfa (her sayfa ~20 paylaşım)
    "aralik_dk": 5,        # otomatik yenileme aralığı (dakika)
    "bildirim_esik": 40,   # bu yüzde ve üstü yeni fırsatlarda bildirim (Termux:API gerekir)
    "saklama_gun": 14,      # paylaşımlar kaç gün saklansın
    "kupon_ara": True,     # her kanalda "kupon" ve "kod" aramasıyla ek paylaşım topla
    "takip": [],           # takip edilen kelimeler (ör. "airfryer", "ps5"); eşleşen yeni fırsatta bildirim
    "gecmis_tamamla": True,  # yeni ürünlerin eski fiyatlarını kanal arşivinde otomatik ara
    "ntfy_konu": "",       # telefona bildirim için ntfy konusu (boşsa kapalı)
    "ntfy_sunucu": "https://ntfy.sh",
    "alarmlar": [],        # hedef fiyat alarmları
}
COZUCU_SURUM = 3   # okuma kuralları değişince artırılır; kayıtlı paylaşımlar açılışta yeniden okunur
ESKI_VARSAYILAN_KANALLAR = ["dhsicak_firsatlar", "firsatz", "tazefirsat", "indirimbakanligi"]
NTFY_KONU_RE = re.compile(r"^[A-Za-z0-9_-]{12,64}$")
AYAR_SINIRLARI = {"sayfa": (1, 10), "aralik_dk": (5, 360), "bildirim_esik": (10, 95), "saklama_gun": (1, 30)}
GECMIS_GUN = 60            # "indirim oranı" için bakılan yakın geçmiş (gün)
ARSIV_GUN = 365            # fiyat arşivi (kanal geçmişinden bulunanlar dahil) kaç gün tutulsun
ARSIV_OTO_ADET = 5         # her yenilemede geçmişi kanal arşivinden tamamlanacak ürün sayısı
EN_FAZLA_GONDERI = 4000
UA = ("Mozilla/5.0 (Linux; Android 14; Mobile) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36")
KANAL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,63}$")

# ---------------------------------------------------------------------------
# Metin yardımcıları
# ---------------------------------------------------------------------------

_ASCII_BUYUK = [("ındırım", "indirim"), ("ındirim", "indirim"), ("ıle", "ile"), ("fıyat", "fiyat"), ("sepettekı", "sepetteki"),
                ("ılk", "ilk"), ("ıçın", "için"), ("ındır", "indir"), ("ıade", "iade"), ("hedıye", "hediye"),
                ("üzerı", "üzeri"), ("alışverış", "alışveriş"), ("geçerlı", "geçerli")]


def kucuk(s):
    """Türkçeye uygun küçük harf (İ→i, I→ı). "INDIRIM" gibi İ yerine I ile yazılmış büyük harfleri de düzeltir."""
    k = s.replace("İ", "i").replace("I", "ı").lower()
    if "ı" in k and "I" in s:
        for a, b in _ASCII_BUYUK:
            k = k.replace(a, b)
    return k


def sade(s):
    """Aksansız, küçük harf (anahtar üretmek için)."""
    s = unicodedata.normalize("NFKD", kucuk(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.replace("ı", "i")


def temizle(s):
    """Emoji ve süs karakterlerini atıp tek satırlık temiz metin döndürür."""
    s = unicodedata.normalize("NFC", s).replace("⟦", "").replace("⟧", "")
    s = "".join(c for c in s
                if unicodedata.category(c) not in ("So", "Sk", "Cs", "Co", "Cn", "Cf")
                and c not in "️‍")
    s = re.sub(r"\s+", " ", s)
    return s.strip(" *-–—:|•·>").strip()


SAYI = r"\d{1,3}(?:[. ]\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?"
PARA = r"(?:TL\b|₺|tl\b|Tl\b|TRY\b)"
FIYAT_RE = re.compile(r"(?<![\w.,])(" + SAYI + r")\s*" + PARA)
SAYI_RE = re.compile(r"(?<![\w.,])(" + SAYI + r")(?![\d])")
YERINE_RE = re.compile(r"(?<![\w.,])(" + SAYI + r")\s*" + PARA + r"?\s*(?:yerine|→|➡|⇒|->|=>)\s*(" + SAYI + r")\s*" + PARA)
DEN_RE = re.compile(r"(?<![\w.,])(" + SAYI + r")\s*" + PARA + r"\s*['’]?\s*(?:den|dan|ten|tan)\s+(" + SAYI + r")\s*" + PARA)
YUZDE_RE = re.compile(r"%\s?(\d{1,2}(?:[.,]\d)?)(?!\d)|(?<![\d.,])(\d{1,2}(?:[.,]\d)?)\s?%")
YUZDE_DISLA_RE = re.compile(
    r"faiz|pamuk|orijinal|nem|kakao|alkol|kdv|vergi|şarj|batarya|yün|keten|elastan|viskon|"
    r"polyester|naylon|spf|kumaş|saf\b|doğal|organik|yağ|protein|meyve oranı|geri dönüşüm")
XALY_RE = re.compile(r"(\d{1,2})\s*al\s*(\d{1,2})\s*öde")
ALANA_RE = re.compile(r"(\d{1,2})\s*alana\s*(\d{1,2})\s*(?:bedava|hediye)")
KOD_ONCE_RE = re.compile(r"(?<![\w])([A-ZÇĞİÖŞÜ0-9]{4,20})(?![\w])\s+(?:[Kk][Oo][Dd]|[Kk][Uu][Pp][Oo][Nn])")
KOD_SONRA_RE = re.compile(r"(?:[Kk]od(?:u|unu)?|[Kk]upon(?: [Kk]odu)?)\s*[:：]?\s*[\"“'`]?([A-ZÇĞİÖŞÜ0-9]{4,20})(?![\w])")
POZ_IKON = ("💰", "🏷", "💳", "✅", "🔥", "💵")
TR_SAAT = timezone(timedelta(hours=3))
# Fiyatın ancak sepette / ödeme ekranında düştüğünü gösteren ifadeler
SEPET_RE = re.compile(r"sepette|ödemede|ödeme (?:aşamasında|ekranında|adımında|sırasında)|son sayfada|ile fiyat düş|"
                      r"(?:kod|kupon)\w*\s*(?:ile|ıyla|iyle|yla|yle|la|le)?\s*[\d.,]+\s*(?:tl|₺)\s*['’]?\s*y?[ea]?\s*(?:düş|gel|in)|"
                      r"sepete \d{1,2} adet|\d{1,2} adet (?:ekle|alımda|alınca|alındığında|eklen)")
ADET_RE = re.compile(r"sepete (\d{1,2}) adet|(\d{1,2}) adet (?:ekle|alımda|alınca|alındığında|eklen)")
UYGULAMA_RE = re.compile(r"mobil uygulama|uygulamada geçerli|yalnızca uygulama|sadece uygulama|uygulamaya özel|"
                         r"uygulama üzerinden|uygulamadan")
ILK_RE = re.compile(r"ilk sipariş|ilk alışveriş|yeni üye|ilk kez|ilk üyelik")
AYLAR = ["ocak", "şubat", "mart", "nisan", "mayıs", "haziran", "temmuz", "ağustos", "eylül", "ekim", "kasım", "aralık"]
KUPON_TUTAR_RE = re.compile(r"(?<![\w.,])(" + SAYI + r")\s*" + PARA +
                            r"\s*['’]?\s*(?:lik|lık)?\s*(?:ek\s*|net\s*|anında\s*)*(?:indirim|kupon|hediye çeki)")
LIMIT_RE = re.compile(r"(?<![\w.,])(" + SAYI + r")\s*" + PARA + r"\s*(?:ve\s*)?(?:üzeri|üstü)")
LIMIT2_RE = re.compile(r"(?<![\w.,])(" + SAYI + r")\s*" + PARA + r"\s*['’]?\s*(?:lik|lık)\s*(?:alışveriş|sepet)")
YE_RE = re.compile(r"(?<![\w.,])(" + SAYI + r")\s*" + PARA + r"\s*['’]?\s*y?[ea]\s+(?:ek\s+)?(" + SAYI + r")\s*" +
                   PARA + r"\s*(?:ek\s*|net\s*)?(?:indirim|kupon)")
# "1.000 TL'ye 350 TL" (sonunda "indirim" yazmasa da) → sepet limiti + kampanya tutarı
YE_KISA_RE = re.compile(r"(?<![\w.,])(" + SAYI + r")\s*" + PARA + r"\s*['’]\s*y?[ea]\s+(?:ek\s+)?(" + SAYI + r")\s*" + PARA +
                        r"(?![\wçğıöşü])")
BITTI_RE = re.compile(r"(?:^|[^\wçğıöşü])(?:bitti|tükendi|stok(?:lar)? (?:bitti|tükendi)|sona erdi|süresi doldu|"
                      r"kampanya bitmiştir|fırsat bitmiştir|artık geçerli değil|geçersizdir artık)(?:[^\wçğıöşü]|$)")
# Önce kesin ifadeler (çakışmaları çözer), sonra sıralı kategori kelimeleri. Tümü aksansız küçük harf.
KATEGORI_KESIN = [
    ("cekirdek kahve", "market"), ("kapsul kahve", "market"), ("filtre kahve", "market"), ("turk kahvesi", "market"),
    ("kosu bandi", "spor"), ("yuruyus bandi", "spor"), ("dis fircasi", "kozmetik"), ("agiz dusu", "kozmetik"), ("sac kurutma", "kozmetik"), ("tiras", "kozmetik"),
    ("prezervatif", "kozmetik"), ("supurge", "ev"), ("ayakkabilik", "ev"), ("oyuncu koltugu", "ev"),
    ("oto koltugu", "bebek"), ("park yatak", "bebek"), ("kahve makinesi", "ev"), ("espresso makinesi", "ev"),
    ("kol saati", "giyim"), ("akilli saat", "elektronik"), ("islak mendil", "market"),
]
KATEGORILER = [
    ("kozmetik", "Kozmetik & bakım", ("parfum", "edp", "edt", "sampuan", "kremi", "sac ", "sac,", "duzlestirici",
                                       "makyaj", "ruj", "maskara", "deodorant", "bakim seti", "cilt", "serum",
                                       "epilasyon", "durex", "nivea", "dudak", "parlatici", "fondoten", "oje",
                                       "allik", "far paleti", "dis macunu", "sensodyne", "colgate", "maybelline", "loreal", "garnier", "rimel")),
    ("elektronik", "Elektronik", ("telefon", "kulaklik", "televizyon", "tv", "miniled", "qled", "oled", "monitor",
                                  "laptop", "dizustu", "bilgisayar", "all in one", "tablet", "ipad", "sarj aleti",
                                  "kablo", "ssd", "flash bellek", "usb", "ekran karti", "rtx", "playstation", "ps5",
                                  "xbox", "nintendo", "hoparlor", "kamera", "powerbank", "power bank", "modem",
                                  "mouse", "klavye", "airpods", "iphone", "galaxy", "buds", "projeksiyon", "router",
                                  "bluetooth", "kumanda", "watch")),
    ("yapi", "Yapı market & oto", ("tornavida", "matkap", "boru kesici", "lokma", "bits", "pense", "anahtar takimi",
                                   "dewalt", "makita", "bosch professional", "alet cantasi", "takim cantasi",
                                   "oto aksesuar", "lastik", "aku ")),
    ("bebek", "Anne, bebek & oyuncak", ("bebek", "lego", "oyuncak", "mama", "yurutec", "puset", "fisher-price",
                                         "fisher price", "hot wheels", "barbie", "boyama", "kids", "oyun seti")),
    ("ev", "Ev & mutfak", ("airfryer", "fritoz", "blender", "tabure", "sandalye", "isitici", "vantilator", "nemlendirici",
                           "hava temizleyici", "fincan", "bardak", "kase", "askil", "kurutmalik", "kurutma askisi", "karistirma kabi",
                           "rende", "saklama kabi", "dus basligi", "kahvalti takimi", "yemek takimi",
                           "porselen", "seramik", "mikser", "espresso", "utu", "tencere", "tava", "catal",
                           "kasik", "bicak", "termos", "ampul", "lamba", "nevresim", "battaniye", "havlu", "pestemal",
                           "cop torbasi", "cop poseti", "dikis makinesi", "camasir makinesi", "bulasik makinesi",
                           "buzdolabi", "cam temizleme", "su isiticisi", "kettle", "tost", "mutfak", "yastik", "hali",
                           "mobilya", "koltuk", "hava temizleyici", "grill", "izgara", "bardak")),
    ("giyim", "Giyim, ayakkabı & aksesuar", ("mont", "kazak", "tisort", "t-shirt", "sweatshirt", "ayakkabi", "boxer", "corap", "pijama",
                                              "pantolon", "jean", "elbise", "ceket", "canta", "polo", "gomlek", "hirka",
                                              "esofman", "sneaker", "polar", "kaban", "etek", "corap", "saat")),
    ("spor", "Spor & outdoor", ("spor", "kamp", "cadir", "bisiklet", "fitness", "dambil", "matara", "kosu", "yoga",
                                "outdoor")),
    ("market", "Market & gıda", ("cay", "kahve", "biskuvi", "cikolata", "seker", "ice tea", "zeytinyagi", "peynir",
                                 "kuruyemis", "makarna", "pirinc", "bakliyat", "icecek", "kremasi", "surup",
                                 "atistirmalik", "kasar", "tavuk", "pilic", "incir", "sut", "tuz", "deterjan", "kapsul", "yumusatici",
                                 "tuvalet kagidi", "temizleyici", "mendil", "pecete", "bez")),
]
KATEGORI_AD = {k: ad for k, ad, _ in KATEGORILER}
# Aramada birbirinin yerine geçen yazımlar (sade, küçük harf)
ESANLAMLI = {
    "ps5": ["playstation 5"], "playstation 5": ["ps5"], "ps4": ["playstation 4"], "playstation 4": ["ps4"],
    "airfryer": ["air fryer"], "air fryer": ["airfryer"], "tv": ["televizyon"], "televizyon": ["tv"],
    "laptop": ["dizustu"], "notebook": ["laptop"], "kulaklik": ["buds"], "iphone": ["apple iphone"],
}
KATEGORI_AD["diger"] = "Diğer"
KOD_DISLA = {"HEPSIBURADA", "HEPSİBURADA", "AMAZON", "TRENDYOL", "İNDİRİM", "INDIRIM", "KUPON", "KAMPANYA",
             "FIRSAT", "FIRSATI", "SEPETTE", "PREMIUM", "PRIME", "BOYNER", "MİGROS", "MIGROS", "PAZARAMA"}

ROZET_KURALLARI = {
    "dip": r"en düşük fiyat|dip fiyat|en ucuz fiyat|tüm zamanların en düşüğ|son \d+ ay[ıi]n en düşük|son \d+ gün[üu]n en düşük",
    "ikinci_el": r"ikinci el|2\. ?el\b|amazon depo|yenilenmiş|teşhir ürün",
    "stok_az": r"stokta sadece|son \d+ (?:adet|stok)|\b\d+ stok\b|sınırlı stok|stoklar? \d",
    "sepette": r"sepette|ödemede|ödeme (?:aşamasında|ekranında)|son sayfada",
    "kupon": r"kupon|\bkod(?:u|unu|uyla|la|ile)?\b|indirim kodu",
}

MAGAZALAR = [
    ("amazon", "Amazon", ("amazon.com.tr", "amzn.to", "amzn.eu", "amazon.com"), ("#amazon", "amazon")),
    ("hepsiburada", "Hepsiburada", ("hepsiburada.com", "hb.biz"), ("#hepsiburada", "hepsiburada")),
    ("trendyol", "Trendyol", ("trendyol.com", "ty.gl"), ("#trendyol", "trendyol")),
    ("n11", "n11", ("n11.com",), ("#n11", "n11")),
    ("mediamarkt", "MediaMarkt", ("mediamarkt.com.tr",), ("mediamarkt",)),
    ("teknosa", "Teknosa", ("teknosa.com",), ("teknosa",)),
    ("vatan", "Vatan", ("vatanbilgisayar.com",), ("vatan bilgisayar",)),
    ("migros", "Migros", ("migros.com.tr",), ("migros",)),
    ("pazarama", "Pazarama", ("pazarama.com",), ("pazarama",)),
    ("boyner", "Boyner", ("boyner.com.tr",), ("boyner",)),
    ("ciceksepeti", "Çiçeksepeti", ("ciceksepeti.com",), ("çiçeksepeti",)),
    ("aliexpress", "AliExpress", ("aliexpress.com", "aliexpress.us"), ("aliexpress",)),
    ("temu", "Temu", ("temu.com",), ("temu",)),
]
MAGAZA_AD = {k: ad for k, ad, _, _ in MAGAZALAR}
MAGAZA_AD["diger"] = "Diğer"


def tutar(s):
    """'43.893,00' → 43893.0, '1699' → 1699.0, '12.5' → 12.5"""
    s = s.replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", s):
        s = s.replace(".", "")
    return float(s)


def zaman(s):
    try:
        t = datetime.fromisoformat(s)
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def simdi():
    return datetime.now(timezone.utc)


def fiyat_yaz(v):
    if v is None:
        return ""
    s = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return (s[:-3] if s.endswith(",00") else s) + " TL"


# ---------------------------------------------------------------------------
# Telegram önizleme sayfasını çözme
# ---------------------------------------------------------------------------

POST_RE = re.compile(r'data-post="([A-Za-z0-9_]+)/(\d+)"')


def div_icerik(s, bas):
    """s[bas] konumunda başlayan <div ...> etiketinin iç HTML'ini döndürür."""
    acik = s.find(">", bas) + 1
    derinlik = 1
    for m in re.finditer(r"<(/?)div\b", s[acik:], re.I):
        derinlik += -1 if m.group(1) else 1
        if derinlik == 0:
            return s[acik:acik + m.start()]
    return s[acik:]


def html_metne(h):
    h = re.sub(r"<(s|del|strike)\b[^>]*>(.*?)</\1>", lambda m: "⟦" + m.group(2) + "⟧", h, flags=re.S | re.I)
    h = re.sub(r"<br\s*/?>", "\n", h, flags=re.I)
    h = re.sub(r"</(?:p|div|blockquote)>", "\n", h, flags=re.I)
    h = re.sub(r"<[^>]+>", "", h)
    t = html.unescape(h).replace(" ", " ")
    satirlar = [re.sub(r"[ \t]+", " ", x).strip() for x in t.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(satirlar)).strip()


def goruntulenme_coz(s):
    s = (s or "").strip().upper().replace(",", ".")
    m = re.match(r"([\d.]+)\s*([KM]?)", s)
    if not m:
        return 0
    try:
        v = float(m.group(1))
    except ValueError:
        return 0
    return int(v * {"K": 1_000, "M": 1_000_000}.get(m.group(2), 1))


# Kanalın kendi duyuruları (sabitleme, uygulama tanıtımı) fırsat değildir
HIZMET_RE = re.compile(r"\bpinned (?:a |an |the )?\w*|sabitledi|^[^\n]{0,40}(?:mobil|uygulama)\S*\s+indir\b", re.I)
KAMPANYA_SINYAL_RE = re.compile(r"indirim|kampanya|kupon|bedava|hediye|ücretsiz|puan|cashback|nakit iade|taksit|"
                                r"kargo|sepette|ek \d|\d+\s*al\s*\d+|%|\d\s*tl|₺")


def sayfayi_coz(h):
    """t.me/s/<kanal> HTML'inden paylaşımları çıkarır. (kanal_adi, [gonderi, ...]) döndürür."""
    ad = None
    m = re.search(r'<meta property="og:title" content="([^"]*)"', h)
    if m:
        ad = html.unescape(m.group(1)).strip() or None

    konumlar = list(POST_RE.finditer(h))
    bulunan = {}
    for i, m in enumerate(konumlar):
        blok = h[m.start(): konumlar[i + 1].start() if i + 1 < len(konumlar) else len(h)]
        kanal, no = m.group(1), int(m.group(2))
        gid = f"{kanal}/{no}"

        metin_html = None
        for mm in re.finditer(r'<div class="tgme_widget_message_text([^"]*)"', blok):
            if "reply" in mm.group(1):
                continue
            metin_html = div_icerik(blok, mm.start())
            break
        if metin_html is None:
            continue
        metin = html_metne(metin_html)
        if not metin or HIZMET_RE.search(metin):
            continue

        adaylar = re.findall(r'<a\s[^>]*?href="([^"]+)"', metin_html)
        for etiket in re.findall(r'<a\s[^>]*tgme_widget_message_inline_button[^>]*>', blok):
            mm = re.search(r'href="([^"]+)"', etiket)
            if mm:
                adaylar.append(mm.group(1))
        adaylar += re.findall(r'class="tgme_widget_message_link_preview"[^>]*href="([^"]+)"', blok)
        linkler = []
        for href in adaylar:
            href = html.unescape(html.unescape(href))   # Telegram bağlantıları iki kez kodluyor (&amp;amp;)
            if not href.startswith(("http://", "https://")) or href in linkler:
                continue
            host = (urllib.parse.urlparse(href).hostname or "").lower()
            if host.endswith(("t.me", "telegram.org", "telegram.me", "whatsapp.com")) or BAGLANTI_DISLA_RE.search(host):
                continue
            linkler.append(href)
        # Mağaza bağlantısı varsa onu öne al
        linkler.sort(key=lambda u: 0 if magaza_alani(u) else 1)

        kodlar = [temizle(html.unescape(re.sub(r"<[^>]+>", "", k)))
                  for k in re.findall(r"<code>(.*?)</code>", metin_html, flags=re.S)]

        resim = None
        mm = re.search(r"(?:tgme_widget_message_photo_wrap|link_preview_image|tgme_widget_message_video_thumb|"
                       r"link_preview_right_image)[^>]*?background-image:url\('([^']+)'\)", blok)
        if mm:
            resim = mm.group(1)
            if resim.startswith("//"):
                resim = "https:" + resim
        mm = re.search(r'<time[^>]*datetime="([^"]+)"', blok)
        tarih = zaman(mm.group(1)) if mm else simdi()
        mm = re.search(r'tgme_widget_message_views">([^<]+)<', blok)
        gor = goruntulenme_coz(mm.group(1)) if mm else 0

        g = gonderi_olustur(kanal, no, metin, linkler, resim, tarih, gor, kodlar)
        if (g["tur"] == "kampanya" and not KAMPANYA_SINYAL_RE.search(kucuk(metin))
                and not (g.get("kupon_tutar") or g.get("yuzde") or g.get("xaly"))):
            continue   # fiyat, kod ya da indirim sinyali olmayan duyuru
        if gid not in bulunan:
            bulunan[gid] = g
    return ad, list(bulunan.values())


# ---------------------------------------------------------------------------
# Paylaşım metninden fırsat bilgisi çıkarma
# ---------------------------------------------------------------------------

BAGLANTI_DISLA_RE = re.compile(r"(^|\.)(google\.[a-z.]+|tazefirsat\.com|bing\.com|yandex\.[a-z.]+|youtube\.com|instagram\.com|"
                               r"twitter\.com|x\.com|facebook\.com)$")


def magaza_alani(link):
    host = (urllib.parse.urlparse(link).hostname or "").lower()
    for anahtar, _, alanlar, _ in MAGAZALAR:
        if any(host == a or host.endswith("." + a) for a in alanlar):
            return anahtar
    return None


BASLIK_ATLA_RE = re.compile(r"yalnızca .{0,60}özel|paylaşılmıştır|sohbet grubumuz|kanalımıza|diğer kanallarımız|"
                            r"^\W*(?:fırsata git|ürüne git|detay)\W*$", re.I)


def baslik_bul(satirlar):
    # DH biçimi: ürün satırı 🛒 ile başlar; önce onu dene
    for s in satirlar:
        if s.strip().startswith("🛒") and not re.search(r"\bgit\b", kucuk(s)):
            t = temizle(FIYAT_RE.sub(" ", s))
            if len(re.findall(r"[A-Za-zÇĞİÖŞÜçğıöşü]", t)) >= 3:
                return t[:160]
    for s in satirlar:
        # "Ürün adı 🏷 199 TL (2 adet alımda)" → yalnızca ürün adı
        for ayrac in ("🏷", " ── ", "💰", "💳"):
            on = s.split(ayrac)[0]
            if ayrac in s and len(re.findall(r"[A-Za-zÇĞİÖŞÜçğıöşü]", on)) >= 3:
                s = on
        s = re.sub(r"\s+(?:yerine|ödemede|ödeme ekranında)\b.*$", "", FIYAT_RE.sub(" ", s), flags=re.I)
        s = re.sub(r"\s['’]\s?(?:y?[ae]|d[ae]n|t[ae]n|l[iı]k)\b", " ", s)   # fiyat silinince kalan "'ye" ekleri
        if BASLIK_ATLA_RE.search(s):
            continue
        t = temizle(s)
        if len(re.findall(r"[A-Za-zÇĞİÖŞÜçğıöşü]", t)) >= 3:
            return t[:160]
    return ""


def fiyat_adaylari(satirlar):
    adaylar = []
    for i, satir in enumerate(satirlar):
        if not satir:
            continue
        dsat = kucuk(satir)
        satir_puan = 0
        if any(ik in satir for ik in POZ_IKON) or re.search(r"fiyat|ödemede|sepette|son fiyat|net fiyat", dsat):
            satir_puan += 3
        if re.search(r"kupon|\bkod|alışveriş|üzeri|kazan|hediye|taksit|kargo|limit|eski|liste fiyat|önceki|"
                     r"normal fiyat|piyasa|❌", dsat):
            satir_puan -= 3
        onceki_satir = next((kucuk(x) for x in reversed(satirlar[:i]) if x.strip()), "")
        tetik = bool(re.search(r"düşüyor|düşer|iniyor|geliyor|fiyat düş", onceki_satir) and not FIYAT_RE.search(onceki_satir))
        if i > 0 and satir.lstrip().startswith(("🚨", "⬛")):
            satir_puan -= 6   # kampanya notu / ek bilgi satırı
        for m in FIYAT_RE.finditer(satir):
            on = satir[:m.start()]
            if on.count("⟦") > on.count("⟧"):
                continue  # üstü çizili (eski) fiyat
            sonra = kucuk(satir[m.end():m.end() + 24])
            oncesi = kucuk(satir[max(0, m.start() - 28):m.start()])
            puan = satir_puan
            if re.match(r"\s*['’]?\s*y?[ea]\s+\d", sonra):
                puan -= 6  # "800 TL'ye 200 TL indirim" → sepet limiti, ürün fiyatı değil
            if re.match(r"\s*\+?\s*['’]?\s*(?:y?[ea]\s*)?(?:düşüyor|düşer|düştü|geliyor|iniyor|indi|oluyor|alınabiliyor)(?![a-zçğıöşü])", sonra):
                puan += 12  # "MUTFAKTA250 Kodu İle 849 TL'ye Düşüyor" → ödenecek fiyat
            if satir.lstrip().startswith(("🔻", "➖", "▪")):
                puan -= 8  # aynı paylaşımdaki başka bir ürün
            if re.match(r"\s*['’]?\s*(?:lik|lık|luk|lük)?\s*(?:indirim|kupon|hediye|puan|money|nakit|iade|ve üzeri|"
                        r"üzeri|alışveriş|limit|taksit|tasarruf|kargo|harca|arası|cüzdan|tık hız)", sonra) or re.match(r"\s*\+", sonra):
                puan -= 6
            if re.search(r"(?:üzeri|alışveriş|sepette ek|kupon\w*|kod\w*|eski|liste|önceki|normal|piyasa\w*|yerine|"
                         r"tasarruf|limit)\W*$", oncesi):
                puan -= 4
            if re.search(r"(?:kuponla|kuponu? ile|koduyla|kodu? ile|\bile|ödemede|özel|tanesi|set fiyatı)(?:\s+(?:adet|adedi|tanesi|set fiyatı))?\s*$", oncesi):
                puan += 8   # "750 TL kuponla 2.719 TL", "kart ile ödemede 15.999 TL" → ödenecek fiyat
            if tetik:
                puan += 10  # "...sepette fiyat düşüyor" satırından sonraki fiyat
            try:
                deger = tutar(m.group(1))
            except ValueError:
                continue
            if deger > 0:
                adaylar.append((puan, i, deger))
    return adaylar


def eski_fiyat_bul(metin, satirlar, fiyat):
    adaylar = []
    for satir in satirlar:
        m = re.search(r"(" + SAYI + r")\s*TL\s*[-–/].*?(?:tanesi|set fiyatı|adet fiyatı)", satir, re.I)
        if m:
            adaylar.append(tutar_(m.group(1)))
    for i, satir in enumerate(satirlar):
        if re.search(r"düşüyor|düşer|iniyor|fiyat düş", kucuk(satir)) and not FIYAT_RE.search(satir):
            onceki = [tutar_(m.group(1)) for x in satirlar[:i] for m in FIYAT_RE.finditer(x)]
            adaylar.extend(v for v in onceki[-1:] if v)
    for ic in re.findall(r"⟦([^⟧]*)⟧", metin):
        m = SAYI_RE.search(ic)
        if m:
            adaylar.append(tutar(m.group(1)))
    for satir in satirlar:
        if re.search(r"eski fiyat|liste fiyat|önceki fiyat|normal fiyat|piyasa fiyat|piyasada|piyasası|etiket fiyat|❌",
                     kucuk(satir)):
            ks = re.search(r"eski fiyat|liste fiyat|önceki fiyat|normal fiyat|piyasa|etiket fiyat|❌", kucuk(satir))
            sonrasi = satir[ks.start():] if ks else satir
            bulunan = [tutar_(m.group(1)) for m in FIYAT_RE.finditer(sonrasi)] or \
                      [tutar_(m.group(1)) for m in SAYI_RE.finditer(sonrasi)][:1]
            adaylar.extend(v for v in bulunan if v)
    for a in adaylar:
        if fiyat * 1.01 < a < fiyat * 20:
            return a
    return None


def yuzde_bul(metin):
    dmetin = kucuk(metin)
    en_iyi, varan = None, False
    for m in YUZDE_RE.finditer(dmetin):
        ham = m.group(1) or m.group(2)
        try:
            v = float(ham.replace(",", "."))
        except ValueError:
            continue
        if not 5 <= v <= 95:
            continue
        bag = dmetin[max(0, m.start() - 22): m.end() + 22]
        if YUZDE_DISLA_RE.search(bag):
            continue
        n = re.search(r"(\d)\s*\.\s*(?:ürün|üründe|ürüne)\D{0,15}$", dmetin[max(0, m.start() - 30):m.start()])
        if n and int(n.group(1)) >= 2:
            v = v / int(n.group(1))  # "2. ürüne %40" ≈ toplamda %20
        n2 = re.search(r"(\d)\s*adet\s*al\w*\W{0,3}(\d)\s*adet\w*\W{0,3}$", dmetin[max(0, m.start() - 40):m.start()])
        if n2 and int(n2.group(1)) > 0:
            v = v * int(n2.group(2)) / int(n2.group(1))  # "2 adet alın, 1 adette %60" ≈ toplamda %30
        bu_varan = bool(re.match(r"\s*['’]?\s*y?[ea]?\s*var[aı]n", dmetin[m.end():m.end() + 14]))
        if en_iyi is None or v > en_iyi:
            en_iyi, varan = v, bu_varan
    return en_iyi, varan


def magaza_bul(linkler, metin):
    for link in linkler:
        m = magaza_alani(link)
        if m:
            return m
    dmetin = kucuk(metin)
    for anahtar, _, _, kelimeler in MAGAZALAR:
        for k in kelimeler:
            if re.search(r"(?<![a-z0-9çğıöşü])" + re.escape(k) + r"(?![a-z0-9çğıöşü])", dmetin):
                return anahtar
    return "diger"


def urun_anahtari(link, magaza, baslik, ikinci_el):
    k = None
    if link:
        m = re.search(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", link)
        if m:
            k = "amz:" + m.group(1)
        elif "trendyol.com" in link and (m := re.search(r"-p-(\d+)", link)):
            k = "ty:" + m.group(1)
        elif "hepsiburada.com" in link and (m := re.search(r"-pm?-([A-Za-z0-9]+)", link)):
            k = "hb:" + m.group(1).upper()
        elif "n11.com" in link and (m := re.search(r"-P(\d+)", link)):
            k = "n11:" + m.group(1)
    if not k:
        yalin = re.sub(r"%\s?\d+\S*|\d+\s?%\S*|\b(?:indirim\w*|varan|firsat\w*)\b", " ", sade(baslik))
        kelimeler = re.findall(r"[a-z0-9]+", yalin)
        if len(kelimeler) < 2:
            return None
        k = "t:" + magaza + ":" + " ".join(kelimeler[:7])
    return k + (":2el" if ikinci_el else "")


def kodlari_bul(metin, html_kodlar):
    kodlar = []
    for k in html_kodlar:
        if 3 <= len(k) <= 30 and " " not in k:
            kodlar.append(k)
    for rx in (KOD_ONCE_RE, KOD_SONRA_RE):
        for m in rx.finditer(metin):
            k = m.group(1)
            if k in KOD_DISLA or not re.search(r"[A-ZÇĞİÖŞÜ]", k) or re.fullmatch(r"\d+(?:TL|TRY|₺)", k):
                continue
            # Rakamlı kodlar (GIDA200) ya da en az 6 harfli büyük harf kodlar (ERKENMEGA)
            if re.search(r"\d", k) or len(k) >= 6:
                kodlar.append(k)
    sonuc = []
    for k in kodlar:
        if k not in sonuc:
            sonuc.append(k)
    return sonuc[:6]


def son_tarih_bul(dmetin, tarih):
    """'30 Kasım'a kadar', '14.09'a kadar', 'sadece bugün' gibi ifadelerden son geçerlilik tarihini bulur."""
    yerel = tarih.astimezone(TR_SAAT)
    adaylar = []

    def ekle(y, ay, g, yil_yazili):
        try:
            d = datetime(y, ay, g, 23, 59, tzinfo=TR_SAAT)
        except ValueError:
            return
        if not yil_yazili and d < yerel - timedelta(days=60):
            d = d.replace(year=y + 1)
        adaylar.append(d)

    def baglam_uygun(bas, son):
        bag = dmetin[max(0, bas - 25):bas] + " " + dmetin[son:son + 30]
        return re.search(r"kadar|son gün|geçerli|bitiş|sona er", bag)

    for m in re.finditer(r"(?<!\d)(\d{1,2})\s*(" + "|".join(AYLAR) + r")", dmetin):
        if baglam_uygun(m.start(), m.end()):
            yil = re.match(r"[a-zçğıöşü'’]*\s*(\d{4})", dmetin[m.end():m.end() + 12])
            ekle(int(yil.group(1)) if yil else yerel.year, AYLAR.index(m.group(2)) + 1, int(m.group(1)), bool(yil))
    for m in re.finditer(r"(?<![\d.,])(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?![\d.,])(?!\s*(?:tl|₺))", dmetin):
        if baglam_uygun(m.start(), m.end()) and 1 <= int(m.group(2)) <= 12:
            y = m.group(3)
            y = (2000 + int(y) if len(y) == 2 else int(y)) if y else yerel.year
            ekle(y, int(m.group(2)), int(m.group(1)), bool(m.group(3)))
    if re.search(r"sadece bugün|yalnızca bugün|bugüne özel|bugün geçerli|gece yarısına kadar|23[.:]59['’]?[ae]? kadar",
                 dmetin):
        adaylar.append(yerel.replace(hour=23, minute=59, second=0, microsecond=0))
    if re.search(r"yarın(?:a)? kadar|yarın akşama", dmetin):
        adaylar.append((yerel + timedelta(days=1)).replace(hour=23, minute=59, second=0, microsecond=0))
    return max(adaylar).isoformat() if adaylar else None


def kupon_bilgisi(dmetin):
    """Kupon tutarı (TL) ve alt sepet limiti."""
    tutar = limit = None
    dmetin = re.sub(r"piyasa\w*[^\n]{0,25}", " ", dmetin)   # "Piyasası 3999 TL üstü" sepet limiti değildir
    m = YE_RE.search(dmetin) or YE_KISA_RE.search(dmetin)
    if m:
        limit, tutar = tutar_(m.group(1)), tutar_(m.group(2))
    if tutar is None:
        m = KUPON_TUTAR_RE.search(dmetin)
        if m:
            tutar = tutar_(m.group(1))
    if limit is None:
        for rx in (LIMIT_RE, LIMIT2_RE):
            for m in rx.finditer(dmetin):
                v = tutar_(m.group(1))
                if v and (tutar is None or v > tutar):
                    limit = v
                    break
            if limit:
                break
    if tutar and limit and tutar >= limit:
        limit = None
    return tutar, limit


def tutar_(s):
    try:
        return tutar(s)
    except ValueError:
        return None


def kategori_bul(baslik):
    s = " " + sade(baslik) + " "
    for kelime, anahtar in KATEGORI_KESIN:
        if re.search(r"(?<![a-z0-9])" + re.escape(kelime), s):
            return anahtar
    for anahtar, _, kelimeler in KATEGORILER:
        for k in kelimeler:
            if re.search(r"(?<![a-z0-9])" + re.escape(k), s):
                return anahtar
    return "diger"


BIRIM_RE = re.compile(r"^\d+(?:[.,]\d+)?(?:GB|TB|MB|ML|L|LT|KG|G|GR|W|MAH|HZ|MM|CM|INC|INCH|K|P|LI|LU|LÜ|Lİ|X|ADET|PARCA|PARÇA|V|A)$")
TEKNIK_DISLA = {"GDDR6", "GDDR7", "DDR4", "DDR5", "USB-C", "WI-FI", "PS4", "PS5", "MP3", "H264", "H265", "HDR10", "LTE",
                "IP68", "IP67", "M.2", "PCIE4", "PCIE5", "QI2"}


@functools.lru_cache(maxsize=8192)
def model_kodu(baslik):
    """Başlıktaki ürün model kodu (ör. EP3343, GX101EU, XG27AQDMGR); yoksa None."""
    adaylar = []
    for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9-]{2,}[A-Za-z0-9]", baslik):
        u = t.upper()
        harf, rakam = len(re.findall(r"[A-Z]", u)), len(re.findall(r"\d", u))
        if harf < 2 or rakam < 2 or len(u) < 4 or u in TEKNIK_DISLA or BIRIM_RE.match(u):
            continue
        if re.match(r"^(?:RTX|GTX|RX|RYZEN|I[3579]|M[1-4])\d*$", u) or re.match(r"^\d+(?:GB|TB)$", u):
            continue
        adaylar.append(u)
    return max(adaylar, key=len) if adaylar else None


@functools.lru_cache(maxsize=8192)
def anlamli_kelimeler(baslik):
    return [k for k in re.findall(r"[a-z0-9]+", sade(baslik)) if len(k) >= 2]


def ayni_urun(g, h):
    """İki paylaşım aynı ürünü mü anlatıyor? (mağaza kodu, model kodu ya da başlık benzerliği)"""
    ga, ha = (g.get("anahtar") or ""), (h.get("anahtar") or "")
    if ga and ha and not ga.startswith("t:") and ga.split(":2el")[0] == ha.split(":2el")[0]:
        return True
    model = model_kodu(g["baslik"])
    if model and model in (h["baslik"] + " " + h.get("metin", "")).upper():
        return True
    a, b = set(anlamli_kelimeler(g["baslik"])[:10]), set(anlamli_kelimeler(h["baslik"])[:10])
    return bool(a and b) and len(a & b) / len(a | b) >= 0.6


def arama_sorgusu(baslik):
    return model_kodu(baslik) or " ".join(re.findall(r"[0-9A-Za-zÇĞİÖŞÜçğıöşü]+", baslik)[:4])


def takip_eslesen(kelimeler, g):
    if not kelimeler:
        return []
    metin = " " + sade(g.get("baslik", "") + " " + g.get("metin", "")) + " "
    return [k for k in kelimeler if sade(k) and sade(k) in metin]


def fiyat_bilgisi(g, gk):
    """Kartta gösterilecek satış fiyatı (üstte) ve indirimli fiyat (altta)."""
    f = g.get("fiyat")
    if f is None:
        return {"satis_fiyati": None, "satis_kaynak": None, "indirimli_fiyat": None, "indirimli_not": None}
    satis = kaynak = not_ = None
    indirimli = f
    if g.get("sayfa_tahmini"):
        satis, kaynak = g["sayfa_tahmini"], f"Tahmini sayfa fiyatı ({g['xaly']})"
        not_ = f"{g.get('adet') or ''} adet alınca ödeme ekranında düşen adet fiyatı".strip()
    elif g.get("eski_fiyat"):
        satis, kaynak = g["eski_fiyat"], ("Paylaşımda yazan piyasa fiyatı" if g.get("eski_piyasa") else "Paylaşımda yazan eski fiyat")
    elif g.get("xaly_yuzde") and not g.get("sepet_fiyati") and not g.get("birim_net"):
        satis, kaynak = f, "Ürün sayfasındaki fiyat"
        x, y = (int(v) for v in re.findall(r"\d+", g["xaly"])[:2])
        indirimli = round(f * y / x, 2) if "öde" in g["xaly"] else round(f * x / (x + y), 2)
        not_ = f"{g['xaly']} ile adet fiyatı"
    elif gk and gk.get("fiyat"):
        satis, kaynak = gk["fiyat"], "Programın önceki kaydı"
    elif g.get("yuzde") and not g.get("yuzde_varan") and not g.get("sepet_fiyati") and g["yuzde"] < 90:
        satis, kaynak = round(f / (1 - g["yuzde"] / 100), 2), f"Paylaşımdaki %{int(g['yuzde'])} oranından hesaplandı (tahmini)"
    if g.get("birim_net") and not not_:
        not_ = "Kampanyalı adet fiyatı" + (f" ({g['xaly']})" if g.get("xaly") else "")
    if g.get("sepet_fiyati") and not not_:
        not_ = "Sepette / ödeme ekranında oluşan fiyat" + (f" ({g['adet']} adet eklenince)" if g.get("adet") else "")
    return {"satis_fiyati": satis, "satis_kaynak": kaynak, "indirimli_fiyat": indirimli, "indirimli_not": not_}


# Otomatik fiyat okumayı engelleyen mağazalar (bot koruması). Engel aşılmaz; bu ürünlerde güven,
# paylaşımın tazeliğinden ve kaç kanalın aynı fiyatı paylaştığından hesaplanır.
OTO_OKUNAMAZ = {"hepsiburada"}


def kanal_teyidi(d):
    """Aynı ürünü son 24 saatte aynı (±%2) fiyata paylaşan farklı kanal sayısı."""
    kanallar, ilk = {d["kanal"].lower()}, d["ts"]
    for z in d.get("diger") or []:
        if z.get("fiyat") and z.get("ts") and d["ts"] - z["ts"] <= 86400 \
                and abs(z["fiyat"] - d["fiyat"]) <= d["fiyat"] * 0.02:
            kanallar.add(z["kanal"].lower())
            ilk = min(ilk, z["ts"])
    return {"kanal": len(kanallar), "ilk_ts": ilk}


def kacirilmaz_bilgisi(d):
    """Kaçırılmayacak fırsat mı? Mağazada doğrulanmış fiyat + güçlü ucuzluk işareti ister."""
    if d.get("tur") != "urun" or not d.get("fiyat") or d.get("bitti") or "ikinci_el" in d.get("rozetler", []):
        return None
    c = d.get("canli") or {}
    yas = time.time() - d["ts"]
    dogru = c.get("durum") == "ok" and c.get("karar") in ("gecerli", "kampanya")
    dogrulanamaz = c.get("durum") == "engellendi" or (d.get("oto_okunamaz") and not c)
    teyit = (d.get("teyit") or {}).get("kanal", 1)
    if not dogru and not (dogrulanamaz and (yas < 3 * 3600 or (teyit >= 2 and yas < 6 * 3600))):
        return None
    neden, guc = [], 0
    if "dip" in d["rozetler"]:
        neden.append("kanal: son ayların en düşük fiyatı"); guc += 2
    etiket = d.get("dusus_etiketi") or ""
    if "kayit_dusuk" in d["rozetler"] or etiket.startswith("Son "):
        neden.append(etiket if etiket.startswith("Son ") else "kayıtlardaki en düşük fiyat"); guc += 2
    oz = d.get("fiyat_ozet") or {}
    if oz.get("dusus_ort", 0) >= 15:
        neden.append(f"ortalamanın %{int(round(oz['dusus_ort']))} altında"); guc += 1 + (oz["dusus_ort"] >= 25)
    # İndirim oranı: mağazada doğrulandıysa gerçekten ödenecek adet fiyatının sayfa fiyatına göre farkı
    if dogru and c.get("fiyat"):
        oran = int(round((1 - (c.get("birim") or d["fiyat"]) / c["fiyat"]) * 100)) if c.get("karar") == "kampanya" else 0
        if c.get("karar") == "gecerli" and d.get("satis_fiyati"):
            oran = int(round((1 - d["fiyat"] / d["satis_fiyati"]) * 100))
    else:
        oran = d.get("indirim") or 0
    if oran >= 40:
        neden.append(f"%{oran} indirim"); guc += 1 + (oran >= 50)
    if c.get("gercek", "").startswith("Mağaza fiyatı, kanalların"):
        neden.append("mağaza fiyatı kayıtların en düşüğünde"); guc += 1
    if dogrulanamaz and teyit >= 2:
        neden.append(f"{teyit} kanal aynı fiyatı paylaştı"); guc += 1
    if guc < 2 or (dogrulanamaz and guc < 3):
        return None
    tekil = []
    for n in neden:
        if kucuk(n) not in [kucuk(t) for t in tekil]:
            tekil.append(n)
    return {"neden": tekil[:3], "dogrulandi": dogru, "guc": guc}


def kosul_metni(g):
    """Paylaşılan fiyatın hangi koşulla oluştuğunu kısa yazar (ör. "2 adet alınca", "MEGA750 koduyla")."""
    p = []
    if g.get("xaly"):
        p.append(g["xaly"])
    elif g.get("adet") and g["adet"] > 1:
        p.append(f"{g['adet']} adet alınca")
    if g.get("kodlar"):
        p.append(g["kodlar"][0] + " koduyla")
    if g.get("kupon_tutar") and g.get("tur") == "urun":
        p.append(f"{fiyat_yaz(g['kupon_tutar'])} kuponla")
    if not p and g.get("sepet_fiyati"):
        p.append("sepette düşen fiyat")
    if g.get("birim_net") and not g.get("xaly"):
        p.append("adet fiyatı")
    return " · ".join(p) or None


def gonderi_olustur(kanal, no, metin, linkler, resim, tarih, goruntulenme, html_kodlar=()):
    gosterim = metin
    metin = re.sub(r"₺\s?(\d[\d.,]*\d|\d)", r"\1 TL", metin)
    metin = re.sub(r"(\d)\s*TLL\b", r"\1 TL", metin, flags=re.I)     # "3000 TLL YE 300 TL" yazım hatası
    satirlar = metin.split("\n")
    # Bir paylaşımda birden çok ürün varsa (DH: her ürün 🛒 ile başlar) fiyatı yalnızca ilk üründen al
    sepet_satirlari = [i for i, x in enumerate(satirlar) if x.lstrip().startswith("🛒") and not re.search(r"\bgit\b", kucuk(x))]
    fsat = satirlar[:sepet_satirlari[1]] if len(sepet_satirlari) >= 2 else satirlar
    fsat = [("" if x.lstrip().startswith(("🔻", "➖", "▪")) else x) for x in fsat]
    dmetin = kucuk(metin)
    baslik = baslik_bul(satirlar) or "(başlıksız paylaşım)"

    fiyat = eski = None
    m = YERINE_RE.search(metin) or DEN_RE.search(metin)
    if m:
        a, b = tutar(m.group(1)), tutar(m.group(2))
        if a > b > 0:
            eski, fiyat = a, b
    if fiyat is None:
        adaylar = fiyat_adaylari(fsat)
        if adaylar:
            adaylar.sort(key=lambda x: (-x[0], x[1]))
            if adaylar[0][0] >= 0:
                fiyat = adaylar[0][2]
    # "X TL'ye Y TL" kalıbında Y ürün fiyatı değil, sepet limitine verilen indirim tutarıdır
    kampanya_ye = None
    m = YE_KISA_RE.search(metin)
    if m:
        lim, tut = tutar_(m.group(1)), tutar_(m.group(2))
        if lim and tut and tut < lim:
            kampanya_ye = (tut, lim)
            if fiyat == tut and eski is None:
                fiyat = None
    if fiyat and eski is None:
        eski = eski_fiyat_bul(metin, fsat, fiyat)

    yuzde, varan = yuzde_bul("\n".join(fsat))
    xaly = xaly_yuzde = None
    m = XALY_RE.search(dmetin)
    if m:
        x, y = int(m.group(1)), int(m.group(2))
        if x > y > 0:
            xaly, xaly_yuzde = f"{x} al {y} öde", (x - y) / x * 100
    else:
        m = ALANA_RE.search(dmetin)
        if m:
            x, y = int(m.group(1)), int(m.group(2))
            if x > 0 and y > 0:
                xaly, xaly_yuzde = f"{x} alana {y} bedava", y / (x + y) * 100

    rozetler = [ad for ad, rx in ROZET_KURALLARI.items() if re.search(rx, dmetin)]
    kodlar = kodlari_bul(metin, html_kodlar)
    if kodlar and "kupon" not in rozetler:
        rozetler.append("kupon")
    magaza = magaza_bul(linkler, metin)
    urun_link = linkler[0] if linkler else None
    ikinci_el = "ikinci_el" in rozetler

    # Fiyat ürün sayfasındaki fiyat mı, yoksa sepette/ödemede koşulla düşen fiyat mı?
    sepet_fiyati = bool(fiyat and SEPET_RE.search(dmetin))
    if sepet_fiyati and "sepette" not in rozetler:
        rozetler.append("sepette")
    adet = None
    m = ADET_RE.search(dmetin)
    if m:
        adet = int(m.group(1) or m.group(2))
    # "Tanesi 59 TL", "Adet 1.065 TL": paylaşılan fiyat zaten kampanyalı adet fiyatı (X al Y öde tekrar uygulanmaz)
    birim_net = bool(fiyat and any(abs(tutar_(m.group(1)) - fiyat) < 0.01 for m in re.finditer(
        r"(?:tanesi|adedi|adet fiyatı|\badet)\s*:?\s*(" + SAYI + r")\s*tl", dmetin) if tutar_(m.group(1))))
    sayfa_tahmini = None
    if fiyat and xaly_yuzde and not birim_net:
        x, y = (int(v) for v in re.findall(r"\d+", xaly)[:2])
        if sepet_fiyati and "al" in xaly and "öde" in xaly:
            sayfa_tahmini = round(fiyat * x / y, 2)   # paylaşılan fiyat = X alınca adet fiyatı
            adet = adet or x

    ana_dmetin = kucuk("\n".join(x for x in satirlar if not x.lstrip().startswith(("🔻", "➖", "▪"))))
    kupon_tutar, kupon_limit = kupon_bilgisi(ana_dmetin) if (kodlar or "kupon" in rozetler or kampanya_ye) else (None, None)
    tur = "urun" if fiyat else ("kupon" if kodlar else "kampanya")

    return {
        "id": f"{kanal}/{no}",
        "kanal": kanal,
        "no": no,
        "tarih": tarih.isoformat(),
        "ts": int(tarih.timestamp()),
        "goruntulenme": goruntulenme,
        "baslik": baslik,
        "metin": gosterim[:900],
        "urun_link": urun_link,
        "magaza": magaza,
        "fiyat": fiyat,
        "eski_fiyat": eski,
        "eski_piyasa": bool(eski and "piyasa" in dmetin and "⟦" not in metin),
        "yuzde": round(yuzde, 1) if yuzde else None,
        "yuzde_varan": varan,
        "xaly": xaly,
        "xaly_yuzde": round(xaly_yuzde, 1) if xaly_yuzde else None,
        "rozetler": rozetler,
        "kodlar": kodlar,
        "resim": resim,
        "tur": tur,
        "anahtar": urun_anahtari(urun_link, magaza, baslik, ikinci_el) if fiyat else None,
        "sepet_fiyati": sepet_fiyati,
        "adet": adet,
        "sayfa_tahmini": sayfa_tahmini,
        "birim_net": birim_net,
        "kupon_tutar": kupon_tutar,
        "kupon_limit": kupon_limit,
        "uygulama": bool(UYGULAMA_RE.search(dmetin)),
        "ilk_siparis": bool(ILK_RE.search(dmetin)),
        "son_tarih": son_tarih_bul(dmetin, tarih),
        "bitti": bool(BITTI_RE.search(dmetin)),
        "kategori": kategori_bul(baslik) if tur == "urun" else "diger",
        "cozucu": COZUCU_SURUM,
    }


# ---------------------------------------------------------------------------
# İnternetten çekme
# ---------------------------------------------------------------------------

def sayfa_getir(url, zaman_asimi=25):
    istek = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.5",
    })
    with urllib.request.urlopen(istek, timeout=zaman_asimi) as yanit:
        return yanit.read().decode("utf-8", "replace")


def ntfy_gonder(sunucu, konu, baslik, mesaj, link=None, oncelik=3, etiketler=()):
    """ntfy.sh (ya da kendi ntfy sunucunuz) üzerinden telefona bildirim gönderir."""
    govde = {"topic": konu, "title": baslik[:150], "message": mesaj[:500] or baslik[:150],
             "priority": int(oncelik), "tags": list(etiketler)}
    if link and link.startswith(("http://", "https://")):
        govde["click"] = link
    istek = urllib.request.Request(sunucu.rstrip("/") + "/", data=json.dumps(govde).encode("utf-8"), method="POST",
                                   headers={"Content-Type": "application/json", "User-Agent": "KasimFirsat/" + SURUM})
    with urllib.request.urlopen(istek, timeout=15) as yanit:
        return 200 <= yanit.status < 300


# ---------------------------------------------------------------------------
# Anlık fiyat: kullanıcı bir ürünü açtığında mağaza sayfasına bir kez bakılır
# ---------------------------------------------------------------------------
TARAYICI_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/130.0.0.0 Safari/537.36")


class _GuvenliYonlendirme(urllib.request.HTTPRedirectHandler):
    """Yönlendirmeler yalnızca internetteki alan adlarına gidebilir (yerel ağa/IP adresine değil)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not guvenli_dis_adres(newurl):
            raise urllib.error.URLError("güvensiz yönlendirme")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def guvenli_dis_adres(url):
    try:
        u = urllib.parse.urlparse(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not host or "." not in host:
        return False
    if re.fullmatch(r"[\d.]+|\[?[0-9a-f:]+\]?", host) or host.endswith((".local", ".lan", ".home", ".internal", "localhost")):
        return False
    return True


def _fiyat_metinden(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    v = html.unescape(str(v)).replace("\xa0", " ")
    # "Yüzde 13 tasarruf ile 500,00 TL" gibi metinlerde yalnızca TL'den hemen önceki tutarı al
    tl_li = re.findall(r"(\d[\d.,]*)\s*(?:TL|₺|TRY)", v)
    t = re.sub(r"[^\d.,]", "", tl_li[-1] if tl_li else v)
    if not t:
        return None
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".") if t.rfind(",") > t.rfind(".") else t.replace(",", "")
    elif "," in t:
        t = t.replace(",", ".") if len(t.split(",")[-1]) <= 2 else t.replace(",", "")
    elif t.count(".") == 1 and len(t.split(".")[-1]) == 3:
        t = t.replace(".", "")
    try:
        f = float(t)
    except ValueError:
        return None
    return f if f > 0 else None


def sayfadan_fiyat(h, host):
    """Ürün sayfası HTML'inden satış fiyatını bulur. (fiyat, yöntem) döndürür."""
    # 1) Amazon: ödenecek fiyat kutusu
    if "amazon." in host:
        for desen in (r'id="apex-pricetopay-accessibility-label"[^>]*>([^<]+)<',
                      r'apex-pricetopay-value[^>]*><span class="a-offscreen">([^<]*\d[^<]*)</span>',
                      r'apex-core-price-identifier.{0,400}?<span class="a-offscreen">([^<]*\d[^<]*)</span>',
                      r'priceToPay[^>]*>.{0,300}?<span class="a-price-whole">([\d.]+)<span class="a-price-decimal">,</span></span>'
                      r'<span class="a-price-fraction">(\d+)</span>',
                      r'"priceAmount"\s*:\s*([\d.]+)'):
            m = re.search(desen, h, re.S)
            if m:
                ham = m.group(1) + ("," + m.group(2) if m.lastindex and m.lastindex > 1 else "")
                f = float(ham) if desen.startswith('"priceAmount"') else _fiyat_metinden(ham)
                if f:
                    return f, "Amazon fiyat kutusu"
    # 2) Trendyol: sayfadaki ürün verisi
    if "trendyol." in host:
        m = re.search(r'"discountedPrice"\s*:\s*\{[^}]*?"value"\s*:\s*([\d.]+)', h) or \
            re.search(r'"sellingPrice"\s*:\s*\{[^}]*?"value"\s*:\s*([\d.]+)', h)
        if m and _fiyat_metinden(float(m.group(1))):
            return float(m.group(1)), "Trendyol ürün verisi"
    # 3) Ortak yapı: JSON-LD (schema.org Product → offers.price)
    for blok in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', h, re.S | re.I):
        try:
            veri = json.loads(blok.strip())
        except ValueError:
            continue
        yigin = [veri]
        while yigin:
            x = yigin.pop()
            if isinstance(x, list):
                yigin.extend(x)
            elif isinstance(x, dict):
                teklif = x.get("offers")
                if teklif is not None:
                    for t in (teklif if isinstance(teklif, list) else [teklif]):
                        if isinstance(t, dict):
                            f = _fiyat_metinden(t.get("price") or t.get("lowPrice"))
                            if f and str(t.get("priceCurrency", "TRY")).upper() in ("TRY", "TL", ""):
                                return f, "Sayfadaki ürün bilgisi (schema.org)"
                yigin.extend(v for v in x.values() if isinstance(v, (dict, list)))
    # 4) Meta etiketleri
    m = re.search(r'<meta[^>]+(?:property|name|itemprop)="(?:product:price:amount|og:price:amount|price)"[^>]+content="([^"]+)"', h, re.I) or \
        re.search(r'itemprop="price"[^>]+content="([^"]+)"', h, re.I)
    if m and _fiyat_metinden(m.group(1)):
        return _fiyat_metinden(m.group(1)), "Sayfadaki fiyat etiketi"
    return None, None


def canli_fiyat_getir(url):
    """Mağaza sayfasını bir kez açar, güncel fiyatı okur. Sonuç: durum ok / engellendi / bulunamadi / hata."""
    if not url or not guvenli_dis_adres(url):
        return {"durum": "hata", "mesaj": "Bu paylaşımda mağaza bağlantısı yok."}
    acici = urllib.request.build_opener(_GuvenliYonlendirme())
    istek = urllib.request.Request(url, headers={
        "User-Agent": TARAYICI_UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.6"})
    try:
        with acici.open(istek, timeout=20) as yanit:
            son = yanit.geturl()
            ham = yanit.read(3_000_000)
            kod = yanit.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as e:
        if e.code in (403, 429, 503):
            return {"durum": "engellendi", "mesaj": f"Mağaza otomatik kontrole izin vermedi (HTTP {e.code}).",
                    "son_url": e.geturl() or url}
        return {"durum": "hata", "mesaj": f"Mağaza sayfası açılamadı (HTTP {e.code})."}
    except Exception as e:
        return {"durum": "hata", "mesaj": "Mağaza sayfası açılamadı: " + hata_metni(e)}
    h = ham.decode(kod, errors="replace")
    host = (urllib.parse.urlparse(son).hostname or "").lower()
    if re.search(r"captcha|robot check|/errors/validatecaptcha|cf-chl|attention required|access denied|px-captcha", h[:20000], re.I) \
            and not re.search(r'"@type"\s*:\s*"Product"', h):
        return {"durum": "engellendi", "mesaj": "Mağaza robot doğrulaması istedi; fiyatı sayfayı açarak görün.", "son_url": son}
    yol = urllib.parse.urlparse(son).path.lower()
    if re.search(r"/magaza/|/arama|/sr$|/s$|/kampanya|/butik/|/liste/|/store/", yol) or \
            not re.search(r"/dp/|/gp/product/|/gp/aw/d/|-p-\d|-pm-|/urun/|/product/|/p/|-p\d|\.html$|/[^/]*\d{5,}", yol):
        return {"durum": "urun_degil", "son_url": son,
                "mesaj": "Paylaşımdaki bağlantı ürün sayfasına değil, mağaza/arama sayfasına gidiyor; fiyatı ürünü seçerek görün."}
    fiyat, yontem = sayfadan_fiyat(h, host)
    if not fiyat:
        if re.search(r"Şu anda mevcut değil|Currently unavailable|id=\"outOfStock\"|Stokta yok|Tükendi", h):
            return {"durum": "stok_yok", "mesaj": "Ürün şu anda stokta yok (mağaza sayfası öyle diyor).", "son_url": son}
        if "amazon." in host:
            return {"durum": "bulunamadi", "son_url": son,
                    "mesaj": "Amazon'da öne çıkan satıcı yok; ürün yalnızca diğer satıcılarda olabilir. Fiyatı sayfada görün."}
        return {"durum": "bulunamadi", "mesaj": "Sayfa açıldı ama fiyat okunamadı (ürün kaldırılmış ya da stokta olmayabilir).",
                "son_url": son}
    stok_yok = bool(re.search(r"OutOfStock|Stokta yok|Tükendi|şu anda mevcut değil|Currently unavailable", h, re.I)) \
        and not re.search(r"InStock", h)
    return {"durum": "ok", "fiyat": fiyat, "yontem": yontem, "son_url": son, "magaza": magaza_alani(son),
            "stok_yok": stok_yok, "kampanyalar": sayfa_kampanyalari(h, host)}


def sayfa_kampanyalari(h, host):
    """Ürün sayfasında görünen adet/sepet kampanyaları (şimdilik Amazon'un kampanya kutusu)."""
    if "amazon." not in host:
        return None          # bu mağazanın sayfası kampanyaları göstermiyor → bilinmiyor
    sonuc = []
    for m in re.finditer(r'id="promoMessage[a-z0-9]+"[^>]*>(.{0,800}?)</(?:span|div)>', h, re.S):
        t = html.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))
        t = re.sub(r"\s+", " ", t).replace(" uygun ürünler", "").strip(" .")
        if t and t not in sonuc:
            sonuc.append(t[:140])
    return sonuc


def kampanyali_birim(sayfa, kampanyalar):
    """Sayfadaki adet kampanyalarına göre en düşük adet fiyatı: (fiyat, kampanya metni)."""
    en = None
    for k in kampanyalar or []:
        kk = kucuk(k)
        m = re.search(r"(\d+)\s*al\s*(\d+)\s*öde", kk)
        if m and int(m.group(1)) > int(m.group(2)) > 0:
            f = sayfa * int(m.group(2)) / int(m.group(1))
        else:
            m = re.search(r"(\d+)\s*adet satın alın,?\s*(\d+)\s*adette geçerli\s*%\s*(\d+)", kk)
            if m and int(m.group(1)) > 0:
                x, n, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                f = sayfa * (x - n * y / 100) / x
            else:
                continue
        if en is None or f < en[0]:
            en = (round(f, 2), k)
    return en


def alarm_eslesir(alarm, g):
    """Bu paylaşım alarmdaki ürünle aynı ürün mü? (2. el ürünler yalnızca 2. el alarmına eşleşir)"""
    if g.get("tur") != "urun" or not g.get("fiyat"):
        return False
    ga, aa = g.get("anahtar") or "", alarm.get("anahtar") or ""
    if (":2el" in ga) != (":2el" in aa):
        return False
    return ayni_urun({"anahtar": aa, "baslik": alarm.get("baslik", "")}, g)


def canli_karar(g, sayfa, gk, sonuc_kamp=None):
    """Mağazadaki anlık fiyatı paylaşımla ve kayıtlarla karşılaştırıp sade bir sonuç üretir."""
    p = g.get("fiyat")
    sepette = bool(g.get("sepet_fiyati") or g.get("kodlar") or g.get("kupon_tutar") or g.get("birim_net") or g.get("adet"))
    sonuc = {"paylasilan": p}
    if p:
        fark = (sayfa - p) / p * 100
        sonuc["fark"] = round(fark, 1)
        if fark <= 2:
            sonuc["karar"], sonuc["renk"] = ("gecerli", "yesil")
            sonuc["metin"] = "Paylaşılan fiyat hâlâ geçerli" if fark >= -2 else "Mağazada paylaşılandan da ucuz"
        elif sepette:
            neden = ("kod" if g.get("kodlar") else "kupon" if g.get("kupon_tutar") else
                     "adet kampanyası" if (g.get("xaly") or g.get("birim_net") or (g.get("adet") or 1) > 1) else "sepet indirimi")
            kampanyalar = kampanyalar_ if (kampanyalar_ := (sonuc_kamp or {}).get("kampanyalar")) is not None else None
            if neden == "adet kampanyası" and kampanyalar is not None:
                kb = kampanyali_birim(sayfa, kampanyalar)
                if kb and kb[0] <= p * 1.03:
                    sonuc["karar"], sonuc["renk"] = ("kampanya", "yesil")
                    sonuc["birim"] = kb[0]
                    sonuc["metin"] = f"Kampanya sayfada hâlâ aktif: “{kb[1]}” → adet {fiyat_yaz(kb[0])}"
                elif kb:
                    sonuc["karar"], sonuc["renk"] = ("sepette", "sari")
                    sonuc["birim"] = kb[0]
                    sonuc["metin"] = f"Sayfadaki kampanya ({kb[1]}) ile adet {fiyat_yaz(kb[0])}; paylaşımdaki {fiyat_yaz(p)} artık tutmuyor."
                else:
                    sonuc["karar"], sonuc["renk"] = ("bitti", "kirmizi")
                    sonuc["metin"] = f"Adet kampanyası ürün sayfasında artık yok; şu an tanesi {fiyat_yaz(sayfa)}."
            elif neden == "sepet indirimi":
                # Mağaza sepet indirimini ürün sayfasında göstermiyor; fiyat yükselmişse büyük ihtimalle bitmiştir
                sonuc["karar"], sonuc["renk"] = ("sepet_yok", "kirmizi")
                sonuc["metin"] = (f"Paylaşımdaki {fiyat_yaz(p)} bir sepet indirimiyle oluşuyordu; ürün sayfasında şu an {fiyat_yaz(sayfa)} "
                                  f"ve sepet indirimi görünmüyor. Büyük ihtimalle bitmiş — sepete ekleyip kontrol edin.")
            else:
                sonuc["karar"], sonuc["renk"] = ("sepette", "sari")
                sonuc["metin"] = (f"Sayfada {fiyat_yaz(sayfa)}; paylaşımdaki {fiyat_yaz(p)} {neden} ile oluşuyordu. "
                                  f"{neden.capitalize()} hâlâ geçerliyse sepette düşer — ödemeden önce sepet tutarına bakın.")
        else:
            sonuc["karar"], sonuc["renk"] = ("bitti", "kirmizi")
            sonuc["metin"] = f"Fiyat yükselmiş: paylaşımda {fiyat_yaz(p)}, şimdi {fiyat_yaz(sayfa)} (%{int(round(fark))} daha pahalı). Fırsat bitmiş görünüyor."
    if gk:
        oz = gk["ozet"]
        sonuc["kayit"] = {"en_dusuk": oz["en_dusuk"], "ortalama": oz["ortalama"], "adet": oz["adet"], "ilk_ts": oz["ilk_ts"]}
        if sayfa <= oz["en_dusuk"] * 1.01:
            sonuc["gercek"] = "Mağaza fiyatı, kanalların kaydettiği en düşük fiyat seviyesinde — gerçek bir indirim."
        elif sayfa < oz["ortalama"] * 0.97:
            sonuc["gercek"] = f"Mağaza fiyatı kayıtlı ortalamanın %{int(round((oz['ortalama'] - sayfa) / oz['ortalama'] * 100))} altında."
        else:
            sonuc["gercek"] = f"Mağaza fiyatı kayıtlı ortalamanın altında değil ({fiyat_yaz(oz['ortalama'])}); acele etmeyin."
    return sonuc


def kanal_cek(kanal, sayfa_sayisi, kupon_ara=True):
    kanal_adi, tum = kanal_sayfalari(kanal, sayfa_sayisi)
    if kupon_ara:
        # Kanalın kendi aramasıyla son kupon/kod paylaşımlarını da topla (son paylaşımlarda olmayanlar dahil)
        gorulen = {g["id"] for g in tum}
        for kelime in ("kupon", "kod"):
            try:
                time.sleep(1.2)
                _, bulunan = sayfayi_coz(sayfa_getir(f"https://t.me/s/{kanal}?q={kelime}"))
                for g in bulunan:
                    if g["id"] not in gorulen:
                        tum.append(g)
                        gorulen.add(g["id"])
            except Exception:
                pass  # arama sayfası alınamazsa normal paylaşımlarla devam
    return kanal_adi, tum


def kanal_sayfalari(kanal, sayfa_sayisi):
    tum, kanal_adi, once = [], None, None
    for i in range(sayfa_sayisi):
        url = f"https://t.me/s/{kanal}" + (f"?before={once}" if once else "")
        h = sayfa_getir(url)
        ad, gonderiler = sayfayi_coz(h)
        kanal_adi = kanal_adi or ad
        if not gonderiler:
            if i == 0:
                raise ValueError("Paylaşım bulunamadı (kanal adı yanlış ya da web önizlemesi kapalı olabilir)")
            break
        tum.extend(gonderiler)
        en_kucuk = min(g["no"] for g in gonderiler)
        if once is not None and en_kucuk >= once:
            break
        once = en_kucuk
        if i + 1 < sayfa_sayisi:
            time.sleep(1.2)
    return kanal_adi, tum


def hata_metni(e):
    if isinstance(e, urllib.error.HTTPError):
        return f"Sunucu hatası ({e.code})"
    if isinstance(e, urllib.error.URLError):
        return "Bağlantı kurulamadı — internet bağlantısını kontrol edin"
    if isinstance(e, TimeoutError):
        return "Zaman aşımı — bağlantı yavaş"
    return str(e)[:160]


# ---------------------------------------------------------------------------
# Veri deposu
# ---------------------------------------------------------------------------

def json_oku(yol, varsayilan):
    try:
        with open(yol, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return varsayilan


def json_yaz(yol, veri):
    os.makedirs(os.path.dirname(yol), exist_ok=True)
    gecici = yol + ".tmp"
    with open(gecici, "w", encoding="utf-8") as f:
        json.dump(veri, f, ensure_ascii=False)
    os.replace(gecici, yol)


class Depo:
    def __init__(self):
        self.kilit = threading.RLock()
        self.tetik = threading.Event()
        self.yenileniyor = False
        ayar = dict(VARSAYILAN_AYARLAR)
        ayar.update({k: v for k, v in json_oku(AYAR_DOSYASI, {}).items() if k in VARSAYILAN_AYARLAR})
        # Eski sürümün varsayılan kanal listesini hiç değiştirmemiş kullanıcıya yeni kanalları da ekle
        if [x.lower() for x in ayar["kanallar"]] == ESKI_VARSAYILAN_KANALLAR:
            ayar["kanallar"] = list(VARSAYILAN_AYARLAR["kanallar"])
        # Eski varsayılan sayfa/saklama değerlerini hiç değiştirmemiş kullanıcıda daha çok ürün göster
        if ayar.get("sayfa") == 3:
            ayar["sayfa"] = VARSAYILAN_AYARLAR["sayfa"]
        if ayar.get("saklama_gun") == 7:
            ayar["saklama_gun"] = VARSAYILAN_AYARLAR["saklama_gun"]
        # Eski varsayılan 30 dakikalık yenilemeyi hiç değiştirmemiş kullanıcıda 5 dakikaya indir
        if ayar.get("aralik_dk") == 30:
            ayar["aralik_dk"] = VARSAYILAN_AYARLAR["aralik_dk"]
        self.ayarlar = ayar
        v = json_oku(VERI_DOSYASI, {})
        self.veri = {
            "gonderiler": v.get("gonderiler") or {},
            "gecmis": v.get("gecmis") or {},
            "kaynaklar": v.get("kaynaklar") or {},
            "son_guncelleme": v.get("son_guncelleme"),
            "arsiv_arama": v.get("arsiv_arama") or {},
            "bildirilen": v.get("bildirilen") or {},
        }
        self.veri["canli"] = v.get("canli") or {}
        self.veri["canli_son"] = {k: x for k, x in (v.get("canli_son") or {}).items()
                                  if time.time() - x.get("ts", 0) < 6 * 3600 and x.get("surum") == SURUM}
        self.canli_engel, self.canli_son_istek = {}, {}
        self.canli_kilit = threading.Lock()
        self._eski_kayitlari_yeniden_coz()

    def _eski_kayitlari_yeniden_coz(self):
        """Okuyucu düzeltildiğinde kayıtlı paylaşımları yeni kurallarla yeniden okur ve fiyat geçmişini düzeltir."""
        eski = [g for g in self.veri["gonderiler"].values() if g.get("cozucu") != COZUCU_SURUM]
        if not eski:
            return
        degisen = set()
        for g in eski:
            try:
                yeni = gonderi_olustur(g["kanal"], g["no"], g["metin"], [g["urun_link"]] if g.get("urun_link") else [],
                                       g.get("resim"), datetime.fromisoformat(g["tarih"]), g.get("goruntulenme"),
                                       g.get("kodlar") or [])
            except Exception:
                continue
            self.veri["gonderiler"][g["id"]] = dict(g, **yeni)
            degisen.add(g["id"])
        for k in list(self.veri["gecmis"]):
            kalan = [x for x in self.veri["gecmis"][k] if x[2] not in degisen]
            if kalan:
                self.veri["gecmis"][k] = kalan
            else:
                del self.veri["gecmis"][k]
        self._gecmisi_guncelle()
        try:
            json_yaz(VERI_DOSYASI, self.veri)
        except OSError:
            pass

    # --- yenileme -----------------------------------------------------------
    def tetikle(self):
        with self.kilit:
            if self.yenileniyor:
                return
            self.yenileniyor = True
        self.tetik.set()

    def dongu(self):
        while True:
            self.yenile()
            self.tetik.wait(timeout=self.ayarlar["aralik_dk"] * 60)

    def yenile(self):
        with self.kilit:
            self.yenileniyor = True
            self.tetik.clear()
            ilk_calisma = not self.veri["gonderiler"]
            kanallar = list(self.ayarlar["kanallar"])
            sayfa = self.ayarlar["sayfa"]
            kupon_ara = bool(self.ayarlar.get("kupon_ara", True))
        yeni_idler = []
        try:
            for kanal in kanallar:
                zaman_s = simdi().isoformat()
                try:
                    ad, gonderiler = kanal_cek(kanal, sayfa, kupon_ara)
                    with self.kilit:
                        for g in gonderiler:
                            eski = self.veri["gonderiler"].get(g["id"])
                            if eski is None:
                                yeni_idler.append(g["id"])
                            self.veri["gonderiler"][g["id"]] = g
                        self.veri["kaynaklar"][kanal] = {"ok": True, "ad": ad, "adet": len(gonderiler),
                                                         "zaman": zaman_s, "hata": None}
                except Exception as e:  # bir kanal hata verirse diğerleri devam etsin
                    with self.kilit:
                        onceki = self.veri["kaynaklar"].get(kanal) or {}
                        self.veri["kaynaklar"][kanal] = {"ok": False, "ad": onceki.get("ad"), "adet": 0,
                                                         "zaman": zaman_s, "hata": hata_metni(e)}
                time.sleep(0.8)
            with self.kilit:
                self._gecmisi_guncelle()
                self._temizle()
                self.veri["son_guncelleme"] = simdi().isoformat()
                try:
                    json_yaz(VERI_DOSYASI, self.veri)
                except OSError as e:
                    print("Uyarı: veri kaydedilemedi:", e, file=sys.stderr)
        finally:
            with self.kilit:
                self.yenileniyor = False
        self._bildir(set(yeni_idler), ilk_calisma)
        self._kacirilmazlari_dogrula()
        if self.ayarlar.get("gecmis_tamamla", True):
            self._arsivi_tamamla(ARSIV_OTO_ADET)

    def _kacirilmazlari_dogrula(self, en_fazla=15):
        """Yeni ve çok ucuz görünen ürünlerin fiyatını mağazada doğrular (az sayıda, sırayla)."""
        with self.kilit:
            tum = self.firsatlar()
        aday = [d for d in tum if d["tur"] == "urun" and d.get("fiyat") and d.get("urun_link") and not d.get("canli")
                and not d.get("oto_okunamaz") and time.time() - d["ts"] < 6 * 3600 and "ikinci_el" not in d["rozetler"]
                and ((d.get("indirim") or 0) >= 35 or "dip" in d["rozetler"] or "kayit_dusuk" in d["rozetler"]
                     or ((d.get("fiyat_ozet") or {}).get("dusus_ort", 0) >= 15))]
        aday.sort(key=lambda d: -(d.get("indirim") or 0))
        for d in aday[:en_fazla]:
            try:
                self.canli_kontrol(d["id"])
            except Exception:
                pass
        with self.kilit:
            tum = self.firsatlar()
            bildirilen = self.veri["bildirilen"]
        gonderilen = 0
        for d in sorted(tum, key=lambda d: -((d.get("kacirilmaz") or {}).get("guc", 0))):
            k = d.get("kacirilmaz")
            if not k or not k["dogrulandi"] or f"k:{d['id']}" in bildirilen:
                continue
            if gonderilen >= 5:
                break
            gonderilen += 1
            fiyat = (d.get("canli") or {}).get("birim") or d["fiyat"]
            self.bildirim_gonder(f"🔥 Kaçırılmaz: {fiyat_yaz(fiyat)}" + (f" · %{d['indirim']}" if d.get("indirim") else ""),
                                 d["baslik"] + " — " + ", ".join(k["neden"]), d.get("urun_link") or d["link"], 4, ["fire"],
                                 "kf-k-" + re.sub(r"\W", "-", d["id"]))
            with self.kilit:
                bildirilen[f"k:{d['id']}"] = int(time.time())

    def gorunenleri_dogrula(self, en_fazla=80, sure_sn=420):
        """(Site üretimi) Son 24 saatteki ürünlerin fiyatını mağazada kontrol eder; süre ve adet sınırlı."""
        bas = time.time()
        with self.kilit:
            tum = self.firsatlar()
        aday = [d for d in tum if d["tur"] == "urun" and d.get("fiyat") and d.get("urun_link") and not d.get("canli")
                and not d.get("oto_okunamaz") and time.time() - d["ts"] < 86400 and "ikinci_el" not in d["rozetler"]]
        aday.sort(key=lambda d: (-(d.get("indirim") or 0) // 10, -d["ts"]))
        for d in aday[:en_fazla]:
            if time.time() - bas > sure_sn:
                break
            try:
                self.canli_kontrol(d["id"])
            except Exception:
                pass

    # --- kanal arşivinden geçmiş fiyat --------------------------------------
    def _arsivi_tamamla(self, adet):
        with self.kilit:
            adaylar = sorted((g for g in self.veri["gonderiler"].values()
                              if g.get("fiyat") and g.get("anahtar") and g["anahtar"] not in self.veri["arsiv_arama"]),
                             key=lambda g: -g["ts"])[:adet]
        for g in adaylar:
            try:
                self.gecmisi_getir(g["id"])
            except Exception:
                pass
            time.sleep(2)

    def gecmisi_getir(self, gid):
        """Ürünün eski paylaşımlarını kanalların arama sayfasında bulur, fiyatlarını arşive ekler."""
        with self.kilit:
            g = self.veri["gonderiler"].get(gid)
            if not g or not g.get("fiyat") or not g.get("anahtar"):
                return {"eklenen": 0, "sorgu": None}
            g = dict(g)
            kanallar = list(self.ayarlar["kanallar"])
        sorgu = arama_sorgusu(g["baslik"])
        bulunan, kilit = [], threading.Lock()

        def cek(kanal):
            try:
                _, liste = sayfayi_coz(sayfa_getir(f"https://t.me/s/{kanal}?q={urllib.parse.quote(sorgu)}", 15))
                with kilit:
                    bulunan.extend(liste)
            except Exception:
                pass

        isler = [threading.Thread(target=cek, args=(k,), daemon=True) for k in kanallar]
        for t in isler:
            t.start()
        for t in isler:
            t.join(timeout=20)
        eklenen = 0
        g_ikinci = "ikinci_el" in (g.get("rozetler") or [])
        with self.kilit:
            kayit = self.veri["gecmis"].setdefault(g["anahtar"], [])
            mevcut = {k[2] for k in kayit}
            for h in bulunan:
                if h["id"] == gid or h["id"] in mevcut or not h.get("fiyat"):
                    continue
                if ("ikinci_el" in h["rozetler"]) != g_ikinci or not 0.4 <= h["fiyat"] / g["fiyat"] <= 2.5:
                    continue
                if not ayni_urun(g, h):
                    continue
                kayit.append([h["ts"], h["fiyat"], h["id"]])
                mevcut.add(h["id"])
                eklenen += 1
            kayit.sort()
            self.veri["arsiv_arama"][g["anahtar"]] = int(time.time())
            try:
                json_yaz(VERI_DOSYASI, self.veri)
            except OSError:
                pass
        return {"eklenen": eklenen, "sorgu": sorgu}

    def _gecmisi_guncelle(self):
        gecmis = self.veri["gecmis"]
        for g in self.veri["gonderiler"].values():
            # Kanalların paylaştığı net fiyat (sepet/kod indirimi dahil) kayda girer; 2. el ürünler ayrı anahtardadır
            if not g.get("fiyat") or not g.get("anahtar"):
                continue
            kayitlar = gecmis.setdefault(g["anahtar"], [])
            for k in kayitlar:
                if k[2] == g["id"]:
                    k[1] = g["fiyat"]          # aynı paylaşım yeniden okunduysa fiyatı güncelle
                    break
            else:
                kayitlar.append([g["ts"], g["fiyat"], g["id"]])

    def _temizle(self):
        sinir = int((simdi() - timedelta(days=self.ayarlar["saklama_gun"])).timestamp())
        g = {k: v for k, v in self.veri["gonderiler"].items() if v.get("ts", 0) >= sinir}
        if len(g) > EN_FAZLA_GONDERI:
            g = dict(sorted(g.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)[:EN_FAZLA_GONDERI])
        self.veri["gonderiler"] = g
        gsinir = int((simdi() - timedelta(days=ARSIV_GUN)).timestamp())
        yeni = {}
        for k, kayitlar in self.veri["gecmis"].items():
            kalan = sorted(x for x in kayitlar if x[0] >= gsinir)[-80:]
            if kalan:
                yeni[k] = kalan
        self.veri["gecmis"] = yeni

    # --- bildirim -----------------------------------------------------------
    def bildirim_gonder(self, baslik, mesaj, link=None, oncelik=3, etiketler=(), kimlik="kf"):
        """Termux bildirimi (telefonda çalışıyorsa) ve ntfy (açıksa). Gönderilen kanal sayısını döndürür."""
        adet = 0
        if shutil.which("termux-notification"):
            komut = ["termux-notification", "--id", kimlik, "--group", "kasim-firsat",
                     "--title", baslik[:120], "--content", mesaj[:200]]
            if link and link.startswith(("http://", "https://")):
                komut += ["--action", "termux-open-url " + shlex.quote(link)]
            try:
                subprocess.run(komut, timeout=15, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                adet += 1
            except (OSError, subprocess.SubprocessError):
                pass
        konu = self.ayarlar.get("ntfy_konu")
        if konu:
            try:
                if ntfy_gonder(self.ayarlar.get("ntfy_sunucu") or "https://ntfy.sh", konu, baslik, mesaj,
                               link, oncelik, etiketler):
                    adet += 1
            except Exception as e:
                print("Uyarı: telefona bildirim gönderilemedi:", hata_metni(e), file=sys.stderr)
        return adet

    def _bildir(self, yeni_idler, ilk_calisma=False):
        if not (shutil.which("termux-notification") or self.ayarlar.get("ntfy_konu")):
            return
        esik = self.ayarlar["bildirim_esik"]
        with self.kilit:
            tum = [d for d in self.firsatlar() if not d.get("bitti")]
            bildirilen = self.veri["bildirilen"]
        gonderilecek = []
        # 1) Hedef fiyat alarmları: daha önce bildirilmemiş her eşleşme
        for d in tum:
            if d.get("alarm") and f"a:{d['alarm']['id']}:{d['id']}" not in bildirilen:
                gonderilecek.append((0, f"a:{d['alarm']['id']}:{d['id']}", d,
                                     f"🎯 Hedef fiyat: {fiyat_yaz(d['fiyat'])} (hedef {fiyat_yaz(d['alarm']['hedef'])})", 5, ["dart"]))
        # 2) Yeni gelen yüksek indirimler ve takip kelimeleri
        if not ilk_calisma:
            for d in tum:
                if d["id"] not in yeni_idler or f"y:{d['id']}" in bildirilen:
                    continue
                if not (d["takip"] or (d["tur"] == "urun" and (d.get("indirim") or 0) >= esik)):
                    continue
                parcalar = []
                if d["takip"]:
                    parcalar.append("Takip: " + ", ".join(d["takip"]))
                if d.get("indirim"):
                    parcalar.append(f"%{d['indirim']} indirim")
                if d.get("fiyat"):
                    parcalar.append(fiyat_yaz(d["fiyat"]))
                elif d.get("kodlar"):
                    parcalar.append("Kod: " + d["kodlar"][0])
                sira = 1 if d["takip"] else 2
                gonderilecek.append((sira, f"y:{d['id']}", d, " · ".join(parcalar) or "Yeni fırsat",
                                     4 if sira == 1 else 3, ["bell"] if sira == 1 else ["moneybag"]))
        gonderilecek.sort(key=lambda x: (x[0], -(x[2].get("indirim") or 0)))
        simdi_ts = int(time.time())
        for _, anahtar, d, baslik, oncelik, etiket in gonderilecek[:8]:
            hedef = d.get("urun_link") or d["link"]
            self.bildirim_gonder(baslik, d["baslik"], hedef, oncelik, etiket, "kf-" + re.sub(r"\W", "-", d["id"]))
            with self.kilit:
                bildirilen[anahtar] = simdi_ts
        with self.kilit:
            # Bir aydan eski kayıtları unut
            for k in [k for k, t in bildirilen.items() if t < simdi_ts - 30 * 86400]:
                del bildirilen[k]
            try:
                json_yaz(VERI_DOSYASI, self.veri)
            except OSError:
                pass

    # --- sunuma hazırlama ---------------------------------------------------
    def _gecmis_karsilastir(self, g):
        if not g.get("fiyat") or not g.get("anahtar"):
            return None
        f = g["fiyat"]
        tum = sorted((k[0], k[1]) for k in self.veri["gecmis"].get(g["anahtar"], [])
                     if k[2] != g["id"] and g["ts"] - ARSIV_GUN * 86400 <= k[0] < g["ts"] - 1800)
        if not tum:
            return None
        fiyatlar = [p for _, p in tum]
        ort = sum(fiyatlar) / len(fiyatlar)
        # "Önceki" = aynı fırsatın aynı gün başka kanalda tekrarı değil, en az 12 saat önceki son paylaşım
        eskiler = [x for x in tum if x[0] <= g["ts"] - 12 * 3600]
        en_d, en_y, son = min(tum, key=lambda x: x[1]), max(tum, key=lambda x: x[1]), (eskiler or tum)[-1]
        sonuc = {"liste": tum[-40:], "ozet": {
            "adet": len(tum), "ilk_ts": tum[0][0], "ortalama": round(ort, 2),
            "en_dusuk": en_d[1], "en_dusuk_ts": en_d[0], "en_yuksek": en_y[1], "en_yuksek_ts": en_y[0],
            "son": son[1], "son_ts": son[0],
            "dusus_son": round((son[1] - f) / son[1] * 100, 1),
            "dusus_ort": round((ort - f) / ort * 100, 1),
            "en_dusuge_gore": round((f - en_d[1]) / en_d[1] * 100, 1),
        }}
        onceki = [x for x in tum if x[0] >= g["ts"] - GECMIS_GUN * 86400]
        if not onceki:
            return sonuc
        sonuc["en_dusuk_onceki"] = min(p for _, p in onceki)
        en_buyuk = max(onceki, key=lambda x: x[1])
        if en_buyuk[1] >= g["fiyat"] * 1.03:
            sonuc.update(yuzde=(en_buyuk[1] - g["fiyat"]) / en_buyuk[1] * 100, fiyat=en_buyuk[1],
                         ts=en_buyuk[0])
        if len(onceki) >= 2 and g["fiyat"] <= min(p for _, p in onceki) * 0.995:
            sonuc["en_dusuk"] = True
        return sonuc or None

    def firsatlar(self):
        kanallar = {k.lower() for k in self.ayarlar["kanallar"]}
        liste = []
        for g in self.veri["gonderiler"].values():
            if g["kanal"].lower() not in kanallar:
                continue
            d = {k: v for k, v in g.items() if k not in ("anahtar_eski",)}
            d["link"] = f"https://t.me/{g['id']}"
            d["magaza_ad"] = MAGAZA_AD.get(g["magaza"], "Diğer")
            d["rozetler"] = list(g.get("rozetler") or [])
            adaylar = []
            if g.get("yuzde"):
                adaylar.append((g["yuzde"], "Paylaşımda yazıyor"))
            if g.get("fiyat") and g.get("eski_fiyat"):
                adaylar.append(((g["eski_fiyat"] - g["fiyat"]) / g["eski_fiyat"] * 100, "Eski fiyata göre"))
            if g.get("xaly_yuzde"):
                adaylar.append((g["xaly_yuzde"], g["xaly"] + (" (paylaşılan fiyata dahil)" if g.get("sayfa_tahmini") else "")))
            if g.get("tur") == "kupon" and g.get("kupon_tutar") and g.get("kupon_limit"):
                adaylar.append((g["kupon_tutar"] / g["kupon_limit"] * 100,
                                f"Kupon: {fiyat_yaz(g['kupon_tutar'])} / {fiyat_yaz(g['kupon_limit'])} alışveriş"))
            gk = self._gecmis_karsilastir(g)
            d["onceki"] = None
            d["fiyat_gecmisi"] = gk["liste"] if gk else []
            d["fiyat_ozet"] = gk["ozet"] if gk else None
            d["arsiv_arandi"] = bool(g.get("anahtar") and g["anahtar"] in self.veri["arsiv_arama"])
            d["akakce"] = ("https://www.akakce.com/arama/?q=" + urllib.parse.quote(arama_sorgusu(g["baslik"]))) if g.get("fiyat") else None
            cs = self.veri["canli_son"].get(g["id"])
            d["kosul"] = kosul_metni(g) if g.get("tur") == "urun" else None
            d["canli"] = cs if cs and time.time() - cs.get("ts", 0) < 6 * 3600 else None
            d["cimri"] = ("https://www.cimri.com/arama?q=" + urllib.parse.quote(arama_sorgusu(g["baslik"]))) if g.get("fiyat") else None
            d["model"] = model_kodu(g["baslik"]) if g.get("tur") == "urun" else None
            # Akakçe tarzı etiket: kayıtlı fiyatlara göre bu fiyat nerede?
            d["dusus_etiketi"] = None
            if gk and g.get("fiyat"):
                oz = gk["ozet"]
                gun = max(1, round((g["ts"] - oz["ilk_ts"]) / 86400))
                if oz["adet"] >= 2 and gun >= 7 and g["fiyat"] <= oz["en_dusuk"] * 1.001:
                    d["dusus_etiketi"] = f"Son {gun} günün en düşüğü"
                elif oz["adet"] >= 2 and oz["dusus_ort"] >= 5:
                    d["dusus_etiketi"] = f"Ortalamanın %{int(round(oz['dusus_ort']))} altında"
            # Paylaşım "indirimli" diyor ama program bu ürünü yakın zamanda aynı ya da daha ucuz fiyata gördüyse uyar
            d["daha_once_ucuz"] = bool(gk and g.get("fiyat") and gk.get("en_dusuk_onceki") is not None
                                       and gk["en_dusuk_onceki"] <= g["fiyat"] * 1.005)
            d.update(fiyat_bilgisi(g, gk if gk and gk.get("fiyat") else None))
            d["kategori"] = g.get("kategori") or "diger"
            d["kategori_ad"] = KATEGORI_AD.get(d["kategori"], "Diğer")
            d["takip"] = takip_eslesen(self.ayarlar.get("takip") or [], g)
            d["alarm"] = d["alarm_kurulu"] = None
            for a in (self.ayarlar.get("alarmlar") or []) if g["ts"] >= time.time() - 7 * 86400 else []:
                if alarm_eslesir(a, g):
                    d["alarm_kurulu"] = {"id": a["id"], "hedef": a["hedef"]}
                    if g["fiyat"] <= a["hedef"]:
                        d["alarm"] = {"id": a["id"], "hedef": a["hedef"]}
                        d["rozetler"].append("hedef")
                    break
            if gk:
                if gk.get("yuzde"):
                    adaylar.append((gk["yuzde"], "Programın fiyat kaydına göre"))
                    d["onceki"] = {"fiyat": gk["fiyat"], "ts": gk["ts"]}
                if gk.get("en_dusuk"):
                    d["rozetler"].append("kayit_dusuk")
            if adaylar:
                en = max(adaylar, key=lambda x: x[0])
                d["indirim"] = int(round(en[0]))
                d["indirim_kaynak"] = en[1]
                d["varan"] = en[1] == "Paylaşımda yazıyor" and bool(g.get("yuzde_varan"))
            else:
                d["indirim"] = None
                d["indirim_kaynak"] = None
                d["varan"] = False
            liste.append(d)

        # Aynı ürün birden fazla kanalda ya da tekrar paylaşıldıysa tek kart göster:
        # en yeni paylaşım esas alınır, eskileri fiyat kaydına ve "Ayrıca" satırına girer.
        liste.sort(key=lambda d: d["ts"], reverse=True)
        gruplar, sonuc = {}, []
        for d in liste:
            d["diger"] = []
            if d["tur"] == "kupon":
                kod_anahtar = "kod:" + d["kodlar"][0].upper()
                esi = gruplar.get(kod_anahtar)
                if esi is None:
                    gruplar[kod_anahtar] = d
                    sonuc.append(d)
                else:
                    esi["diger"].append({"kanal": d["kanal"], "link": d["link"]})
                    for alan in ("kupon_tutar", "kupon_limit", "son_tarih", "indirim", "indirim_kaynak"):
                        if not esi.get(alan) and d.get(alan):
                            esi[alan] = d[alan]
                    esi["uygulama"] = esi.get("uygulama") or d.get("uygulama")
                    esi["ilk_siparis"] = esi.get("ilk_siparis") or d.get("ilk_siparis")
                continue
            if d["tur"] != "urun" or not d.get("anahtar"):
                sonuc.append(d)
                continue
            esi = gruplar.get(d["anahtar"])
            if esi is None:
                gruplar[d["anahtar"]] = d
                sonuc.append(d)
                continue
            esi["diger"].append({"kanal": d["kanal"], "link": d["link"], "fiyat": d.get("fiyat"), "ts": d["ts"]})
            ayni_fiyat = abs(esi["fiyat"] - d["fiyat"]) <= esi["fiyat"] * 0.02
            if ayni_fiyat and (d.get("indirim") or 0) > (esi.get("indirim") or 0):
                for alan in ("indirim", "indirim_kaynak", "varan", "eski_fiyat"):
                    esi[alan] = d.get(alan)
            if ayni_fiyat:
                for r_ in d["rozetler"]:
                    if r_ not in esi["rozetler"]:
                        esi["rozetler"].append(r_)
                for k in d.get("kodlar") or []:
                    if k not in esi["kodlar"]:
                        esi["kodlar"].append(k)
            esi["goruntulenme"] = max(esi.get("goruntulenme") or 0, d.get("goruntulenme") or 0)
            if not esi.get("resim"):
                esi["resim"] = d.get("resim")
        # Mağazalara göre fiyatlar: aynı model kodu farklı mağazalarda paylaşıldıysa (son 30 gün)
        sinir = time.time() - 14 * 86400
        kova = {}
        for d in liste:
            if d.get("model") and d["tur"] == "urun" and d.get("fiyat") and not d.get("bitti") and d["ts"] >= sinir \
                    and "ikinci_el" not in d["rozetler"]:
                kova.setdefault(d["model"], []).append(d)
        for d in sonuc:
            d["teyit"] = kanal_teyidi(d) if d["tur"] == "urun" and d.get("fiyat") else None
            d["oto_okunamaz"] = d["tur"] == "urun" and d.get("magaza") in OTO_OKUNAMAZ
            d["kacirilmaz"] = kacirilmaz_bilgisi(d)
            d["magazalar"] = []
            if d.get("model") and len(kova.get(d["model"], [])) >= 2:
                en_yeni = {}
                for x in kova[d["model"]]:      # liste en yeniden eskiye sıralı
                    en_yeni.setdefault(x["magaza"], x)
                if len(en_yeni) >= 2:
                    d["magazalar"] = sorted(({"magaza": x["magaza"], "magaza_ad": x["magaza_ad"], "fiyat": x["fiyat"],
                                              "ts": x["ts"], "link": x.get("urun_link") or x["link"], "id": x["id"],
                                              "sepette": bool(x.get("sepet_fiyati")),
                                              # Bu mağazadaki fiyat mağaza sayfasında doğrulandıysa gerçek fiyatı
                                              "dogrulandi": (x.get("canli") or {}).get("fiyat")
                                              if (x.get("canli") or {}).get("durum") == "ok"
                                              and (x.get("canli") or {}).get("karar") in ("gecerli", "kampanya", "sepette") else None}
                                             for x in en_yeni.values()), key=lambda m: m["fiyat"])
        return sonuc

    def ara(self, q):
        """Kanalların kendi arama sayfalarında anında arar; bulunanları listeye ekler."""
        q = re.sub(r"\s+", " ", q).strip()[:40]
        if len(q) < 2:
            return []
        with self.kilit:
            kanallar = list(self.ayarlar["kanallar"])
        bulunan, kilit = [], threading.Lock()

        def cek(kanal, sorgu):
            try:
                _, g = sayfayi_coz(sayfa_getir(f"https://t.me/s/{kanal}?q={urllib.parse.quote(sorgu)}", 15))
                with kilit:
                    bulunan.extend(g)
            except Exception:
                pass

        sorgular = [q] + [e for e in ESANLAMLI.get(sade(q), []) if sade(e) != sade(q)][:1]

        def cek_hepsi(kanal):
            for sorgu in sorgular:
                cek(kanal, sorgu)

        isler = [threading.Thread(target=cek_hepsi, args=(k,), daemon=True) for k in kanallar]
        for t in isler:
            t.start()
        for t in isler:
            t.join(timeout=20)
        with self.kilit:
            for g in bulunan:
                self.veri["gonderiler"].setdefault(g["id"], g)
            self._gecmisi_guncelle()
        return [g["id"] for g in bulunan]

    def durum(self):
        with self.kilit:
            kaynaklar = []
            for k in self.ayarlar["kanallar"]:
                b = self.veri["kaynaklar"].get(k) or {}
                kaynaklar.append({"kanal": k, "ad": b.get("ad") or k, "ok": b.get("ok"), "hata": b.get("hata"),
                                  "adet": b.get("adet", 0), "zaman": b.get("zaman")})
            return {
                "surum": SURUM,
                "simdi": simdi().isoformat(),
                "son_guncelleme": self.veri.get("son_guncelleme"),
                "yenileniyor": self.yenileniyor,
                "ayarlar": self.ayarlar,
                "kaynaklar": kaynaklar,
                "bildirim_var": bool(shutil.which("termux-notification") or self.ayarlar.get("ntfy_konu")),
                "termux_bildirim": bool(shutil.which("termux-notification")),
                "firsatlar": self.firsatlar(),
            }

    # --- ayarlar ------------------------------------------------------------
    def canli_kontrol(self, gid, zorla=False):
        with self.kilit:
            g = self.veri["gonderiler"].get(str(gid))
            if not g:
                raise ValueError("Ürün bulunamadı")
            onbellek = self.veri["canli_son"].get(g["id"])
            if onbellek and not zorla and time.time() - onbellek["ts"] < 1800:
                return onbellek
            gk = self._gecmis_karsilastir(g)
        link = g.get("urun_link") or ""
        # Kısa bağlantılar (indirimbakanligi.com, yakaladin.com…) farklı mağazalara gidebilir; engel kaydı gidilen mağazaya göre tutulur
        alan = magaza_alani(link) or ""
        # Otomatik kontrolü engelleyen mağazayı 6 saat boyunca tekrar deneme (mağazayı yormamak için)
        engel = self.canli_engel.get(alan)
        if alan and engel and time.time() - engel < 6 * 3600:
            return {"durum": "engellendi", "mesaj": "Bu mağaza otomatik kontrole izin vermiyor; fiyatı mağazada açarak görün.",
                    "ts": int(time.time())}
        # Aynı mağazaya en fazla 1,5 saniyede bir istek
        with self.canli_kilit:
            anahtar_ = alan or (urllib.parse.urlparse(link).hostname or "")
            bekle = self.canli_son_istek.get(anahtar_, 0) + 1.5 - time.time()
            if bekle > 0:
                time.sleep(bekle)
            self.canli_son_istek[anahtar_] = time.time()
        s_ = canli_fiyat_getir(link)
        s_["ts"], s_["surum"] = int(time.time()), SURUM
        if s_["durum"] == "engellendi":
            son = s_.get("son_url") or link
            self.canli_engel[magaza_alani(son) or (urllib.parse.urlparse(son).hostname or "")] = time.time()
        if s_["durum"] == "ok":
            s_.update(canli_karar(g, s_["fiyat"], gk, s_))
            with self.kilit:
                kayit = self.veri.setdefault("canli", {}).setdefault(g.get("anahtar") or g["id"], [])
                kayit.append([s_["ts"], s_["fiyat"]])
                del kayit[:-50]
        with self.kilit:
            self.veri["canli_son"][g["id"]] = s_
        return s_

    def alarm_islem(self, veri):
        islem = veri.get("islem")
        with self.kilit:
            alarmlar = list(self.ayarlar.get("alarmlar") or [])
            if islem == "ekle":
                g = self.veri["gonderiler"].get(str(veri.get("id", "")))
                if not g or not g.get("fiyat"):
                    raise ValueError("Ürün bulunamadı")
                try:
                    hedef = round(float(str(veri.get("hedef", "")).replace(",", ".")), 2)
                except ValueError:
                    raise ValueError("Hedef fiyat sayı olmalı")
                if not 1 <= hedef <= 10_000_000:
                    raise ValueError("Hedef fiyat geçersiz")
                yeni = {"id": secrets.token_hex(4), "baslik": g["baslik"][:160], "anahtar": g.get("anahtar") or "",
                        "magaza": g.get("magaza"), "hedef": hedef, "ilk_fiyat": g["fiyat"], "ts": int(time.time())}
                alarmlar = [a for a in alarmlar if not alarm_eslesir(a, g)] + [yeni]
                if len(alarmlar) > 40:
                    raise ValueError("En fazla 40 alarm kurulabilir")
                # Şu an zaten hedefin altında olanlar için bildirim gönderme (kartta görünüyor)
                for h in self.veri["gonderiler"].values():
                    if alarm_eslesir(yeni, h) and h["fiyat"] <= hedef:
                        self.veri["bildirilen"][f"a:{yeni['id']}:{h['id']}"] = int(time.time())
            elif islem == "sil":
                alarmlar = [a for a in alarmlar if a["id"] != veri.get("alarm_id")]
            else:
                raise ValueError("Geçersiz işlem")
            self.ayarlar = dict(self.ayarlar, alarmlar=alarmlar)
            json_yaz(AYAR_DOSYASI, self.ayarlar)
            return alarmlar

    def deneme_bildirimi(self):
        if not self.ayarlar.get("ntfy_konu") and not shutil.which("termux-notification"):
            raise ValueError("Önce telefona bildirimi açın")
        if not self.bildirim_gonder("Kasım Fırsatları: deneme bildirimi",
                                    "Bu bildirimi görüyorsanız kurulum tamam. Yüksek indirimler ve hedef fiyatlar böyle gelecek.",
                                    None, 3, ["white_check_mark"], "kf-deneme"):
            raise ValueError("Bildirim gönderilemedi. İnternet bağlantısını kontrol edin.")
        return True

    def ayarlari_guncelle(self, yeni):
        with self.kilit:
            a = dict(self.ayarlar)
            if "kanallar" in yeni:
                if not isinstance(yeni["kanallar"], list):
                    raise ValueError("Kanal listesi hatalı")
                kl = []
                for k in yeni["kanallar"]:
                    k = re.sub(r"^(?:https?://)?(?:www\.)?t(?:elegram)?\.me/(?:s/)?", "", str(k).strip())
                    k = k.lstrip("@").strip("/").split("?")[0].split("/")[0]
                    if not k:
                        continue
                    if not KANAL_RE.match(k):
                        raise ValueError(f"Geçersiz kanal adı: {k}")
                    if k.lower() not in [x.lower() for x in kl]:
                        kl.append(k)
                if not kl:
                    raise ValueError("En az bir kanal gerekli")
                a["kanallar"] = kl[:15]
            for alan, (en_az, en_cok) in AYAR_SINIRLARI.items():
                if alan in yeni:
                    try:
                        v = int(yeni[alan])
                    except (TypeError, ValueError):
                        raise ValueError(f"{alan} sayı olmalı")
                    if not en_az <= v <= en_cok:
                        raise ValueError(f"{alan} {en_az}–{en_cok} arasında olmalı")
                    a[alan] = v
            if "kupon_ara" in yeni:
                a["kupon_ara"] = bool(yeni["kupon_ara"])
            if "gecmis_tamamla" in yeni:
                a["gecmis_tamamla"] = bool(yeni["gecmis_tamamla"])
            if "ntfy_konu" in yeni:
                k = str(yeni["ntfy_konu"] or "").strip()
                if k and not NTFY_KONU_RE.match(k):
                    raise ValueError("Bildirim konusu 12–64 karakter olmalı; yalnızca harf, rakam, - ve _")
                a["ntfy_konu"] = k
            if "ntfy_sunucu" in yeni:
                u = str(yeni["ntfy_sunucu"] or "").strip().rstrip("/") or "https://ntfy.sh"
                if not re.match(r"^https://[A-Za-z0-9.-]+(?::\d+)?(?:/[\w./-]*)?$", u):
                    raise ValueError("Bildirim sunucusu https:// ile başlamalı")
                a["ntfy_sunucu"] = u
            if "takip" in yeni:
                if not isinstance(yeni["takip"], list):
                    raise ValueError("Takip listesi hatalı")
                tk = []
                for k in yeni["takip"]:
                    k = re.sub(r"\s+", " ", str(k)).strip()[:40]
                    if len(k) < 2:
                        continue
                    if sade(k) not in [sade(x) for x in tk]:
                        tk.append(k)
                a["takip"] = tk[:30]
            kanal_degisti = [k.lower() for k in a["kanallar"]] != [k.lower() for k in self.ayarlar["kanallar"]]
            self.ayarlar = a
            json_yaz(AYAR_DOSYASI, a)
        if kanal_degisti:
            self.tetikle()
        return a


# ---------------------------------------------------------------------------
# Yerel web sunucusu
# ---------------------------------------------------------------------------

def yerel_ag_adresi():
    """Bilgisayarın/telefonun Wi-Fi ağındaki adresi (ör. 192.168.1.34). Paket göndermez."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as so:
            so.connect(("10.255.255.255", 1))
            ip = so.getsockname()[0]
            if not ip.startswith("127."):
                return ip
    except OSError:
        pass
    try:
        ip = socket.gethostbyname(socket.gethostname())
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


def erisim_anahtari():
    """Ağ modunda telefonun bağlantısına eklenen gizli anahtar; ilk çalıştırmada üretilir."""
    try:
        with open(ANAHTAR_DOSYASI, encoding="utf-8") as f:
            k = f.read().strip()
            if len(k) >= 10:
                return k
    except OSError:
        pass
    k = secrets.token_urlsafe(9)
    os.makedirs(VERI_DIZINI, exist_ok=True)
    with open(ANAHTAR_DOSYASI, "w", encoding="utf-8") as f:
        f.write(k)
    return k


# Uygulama simgeleri (PNG, base64) — ana ekrana eklerken kullanılır
IKONLAR = {
    "/ikon-32.png": "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAEi0lEQVR42u2XbYhUZRTHf+e+zO7M7LiuK77kS1q0H1xLDLIXsBDZQLTSUAoMdQ3S/VBkRCpFsBgRvUJ+iT5sIkZQEZIKFdXHJE3MUlS0spfZXZ1dd3Zn5/XOfU4f7szuzr6pLbFfvHAY5t7n3v85//N/zjmPXH6iRpnEy9FJhQfHMMkOTDoDk+/AdS2TCcLoOBrQa8Oq700M33b+WwrKj6xY/QSCV0w2BWMAjZ4CBbEd/FSSKQ9vYtq2dyHTA5Z9Q8A4Lqavm/bdq/DTvYjtjEjHmAyo7yGRGLXrnsWKTgHHBsu64eit2DScW27HO3MUKxwC9a+tAbEd/N4kU1c34y5ciqa7EMsB46PGDEQhYoHIAJiqIkOdNAZCVUg4hjFmVD2OZEBAix4SjTH1sRbw84jYAZAqEq0BQsFaLw3FkkBdF3HDkE0NOlX6Nb6PakkGw/Cs8r2BZ5ZDsT9N7KH1uAvvRnP9AfXGQFWE5I/f8fP2FZzf04yX7ALbBieEdzXBL8+tov/CKXDDwfoBSQnDccpmlT0rmyl6WNEYdWtbwBQQywYNqPQScU6/sglxbLp++IqLe3eDGwG3mrOvtwAQXXQ/WshU6mUYxlCzjELZ1HLw+tLEHnyc0MJ70EwfoKg44DjkE3HyiR4aXniHGSvW0nfmOEiI9k/3kjp/kkWt+8EUwFQKzTCIMdyGbENBvQJ23XTq1z+PZnr4a+casCzmv3EYPCE87w5qGxs50dJEsbeH27a9SuHyb1x4fxdL3vocp24OkIdQFWTTI2qKjluIBNT3casj2LFpgd9lMSFQ9LGjU7jrzc/oOHKAqplzmfXIFk5sfYA567ZSu3Q551o3kuv4mwVbXmTqsiY0m0KgQoDDRS+nVw8OJGI7FHuSzHr6ZWZsfg3t7wyKhx0C4wUhuCFwwoDDpQ920fn1J9z3xUXOtW6i++i31DTcSercSZYdOE6objq4VVzavZbU8W+wo7XosPRUaMD4PhKppuvIPordvyNVURArAEdABM1nQH1Sp77nz4/fo3HPfhCH3tPHmLFyHQ073iafSFJIxANnVQNxj2EVu8AYhVA1+Y443Uf2gRtFi95gW1JF3GoKXZ38uutJFmx+idji5aB5ZjZtIH6wjRPbm6htXEx4XgPkc4HTjL0LRvQCLbGQONRG/epmnFg9+F7pQ4rYLvn2i8zd0ML8jTsglwSEW7fsJDz7VnKd/zB7zVPYNTE0l0FK7xnAGkWIIythKcpcR5yuwx8xa3Mrmk4gthOU3kKW2OJ7iS1ZCbmrg6VGYeajzwA2FFNQyAfrERC7IupKDZT36VArsXDlUBvF7kuIU4X6pT6giuaymFRn8L9kany0vyu4n88F61C0kKWYTqFiYVRHYI3aDZWAhWx7nMSXbcxu3oM46aDslhQhYwwvMpRJCaH9V8jG/wAnhFG9jmZUfr/oY0cjdBz8kL6zP5Wqm9zYGCYW+B5+LhOkYZTWK8eaxjmYiKDGx2QzExgLBTscGdP5cWfCgEYbq6Z2QvOoqj/WRHbtqVhVwfdvHkxung3/t+tfUUd2bALrYXAAAAAASUVORK5CYII=",
    "/ikon-180.png": "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",
    "/ikon-192.png": "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",
    "/ikon-512.png": "iVBORw0KGgoAAAANSUhEUgAAAgAAAAIACAYAAAD0eNT6AABPU0lEQVR42u3deZxlVXnv/8/aw5lPnRp6AHqk7QFkBsWYKKCoCGpEbZCQ3ATQBAkhgg0ao4QQchF/nWYIqJB7sY2JiMpgVIIJMilJjEyCAlJNY3fTTY81nDrzsPf6/bFPQ8sVA9SurlNV3/frnhdcud23+1TV2d/1rLWex2z/cM4iIiIiM4qjt0BEREQBQERERBQARERERAFAREREFABEREREAUBEREQUAEREREQBQERERBQARERERAFAREREFABEREREAUBERERi5WkSkIiIiCoAIiIiogAgIiIiCgAiIiKiACAiIiIKACIiIqIAICIiIgoAIiIiogAgIiIiCgAiIiKyF3moFaCIiIgqACIiIqIAICIiImgYkIiIiKgCICIiIgoAIiIiogAgIiIiCgAiIiKiACAiIiIKACIiIqIAICIiIgoAIiIiogAgIiIiCgAiIiKiACAiIiIKACIiIoKGAYmIiKgCoLdAREREAUBEREQUAEREREQBQERERBQAREREZGry0DUAERERVQBEREREAUBEREQUAEREREQBQERERBQAREREBM0CEBEREVUARERERAFAREREFABEREREAUBEREQUAEREREQBQERERBQARERERAFAREREFABEREREAUBEREQUAERERBQAREREBA0DEhEREVUARERERAFAREREpiYP7QGIiIioAiAiIiIKACIiIqIAICIiIgoAIiIiogAgIiIiCgAiIiKiACAiIiIKACIiIqIAICIiImgYkIiIiKgCICIiIgoAIiIiogAgIiIiCgAiIiKiACAiIiIKACIiIqIAICIiogAgIiIiCgAiIiKiACAiIiIKACIiIjJFeWgYgIiIiIYBiYiICNoCEBEREQUAERERUQAQERERBQARERFRABAREREFABEREVEAEBEREQUAERERUQAQERERBQARERFRABAREREFABEREUHDgEREREQVABEREVEAEBERUQDQWyAiIqIAICIiIgoAIiIiogAgIiIiCgAiIiKiACAiIiJThIc6AYmIiKgCICIiIgoAIiIigmYBiIiIiCoAIiIiogAgIiIiCgAiIiKiACAiIiIKACIiIqIAICIiIgoAIiIiogAgIiIiCgAiIiLCuIYBibxWxug9EJmprPrIKgDIzH34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IiIKACIiIgoAIiIiogAgIiIiCgAiIiKiACAiIiJM1WFAmgYkIiKiCoCIiIgoAIiIiIgCgIiIiCgAiIiIiAKAiIiIKACIiIiIAoCIiIgoAIiIiIgCgIiIiCgAiIiIiAKAiIiIEOssAL0HIiIiqgCIiIiIAoCIiIgoAIiIiIgCgIiIiCgAiIiIiAKAiIiIKACIiIiIAoCIiIgoAIiIiMje46FWgCIiIqoAiIiIiAKAiIiIoGFAIiIiogqAiIiIKACIiIiIAoCIiIgoAIiIiIgCgIiIiCgAiIiIiAKAiIiIKACIiIiIAoCIiIgoAIiIiIgCgIiIiCgAiIiICBoGJCIiogqA3gIREREFABEREVEAEBEREQUAERERUQAQERERBQARERGZIjx0D1BEREQVABEREVEAEBEREQUAERERUQAQERERBQARERFBw4BEREREFQARERFRABAREREFABEREVEAEBEREQUAERERUQAQERERBQARERFRABAREREFABEREVEAEBEREQUAERERBQARERFBw4BEREREFQARERFRABAREZGpyUN7ACIiIqoAiIiIiAKAiIiIKACIiIiIAoCIiIgoAIiIiIgCgIiIiCgAiIiIiAKAiIiIoFkAIiIiogqAiIiIKACIiIiIAoCIiIgoAIiIiIgCgIiIiCgAiIiIiAKAiIiIKACIiIgoAIiIiIgCgIiIiCgAiIiIiAKAiIiITFH/PwpQcJUDuMGAAAAAAElFTkSuQmCC",
}
MANIFEST = json.dumps({
    "name": "Kasım Fırsatları", "short_name": "Fırsatlar", "lang": "tr", "start_url": "/", "scope": "/",
    "display": "standalone", "background_color": "#f6f3ef", "theme_color": "#c2410c",
    "description": "Telegram fırsat kanallarındaki indirimler, kuponlar ve fiyat geçmişi",
    "icons": [{"src": "/ikon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
              {"src": "/ikon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}],
}, ensure_ascii=False)


YETKISIZ_HTML = """<!doctype html><html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kasım Fırsatları</title><style>body{font:16px/1.5 system-ui,sans-serif;margin:0;padding:40px 20px;background:#f6f3ef;color:#1c1917}
main{max-width:420px;margin:0 auto}h1{font-size:20px}@media (prefers-color-scheme:dark){body{background:#13100e;color:#f4efe9}}</style></head>
<body><main><h1>Bu sayfa için erişim anahtarı gerekiyor</h1>
<p>Bilgisayardaki Kasım Fırsatları sayfasında <b>Ayarlar → Telefonda aç</b> bölümündeki QR kodu okutun ya da oradaki bağlantıyı açın.</p></main></body></html>"""


class Isleyici(BaseHTTPRequestHandler):
    depo = None
    ag_modu = False
    anahtar = ""
    ag_adresi = None      # ör. "http://192.168.1.34:8765/?k=..."
    server_version = "KasimFirsat/" + SURUM

    def log_message(self, *args):
        pass

    def _gonder(self, kod, govde, tur="application/json; charset=utf-8", ek=None):
        if isinstance(govde, str):
            govde = govde.encode("utf-8")
        self.send_response(kod)
        self.send_header("Content-Type", tur)
        self.send_header("Content-Length", str(len(govde)))
        if "Cache-Control" not in (ek or {}):
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (ek or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(govde)

    def _json(self, kod, veri):
        self._gonder(kod, json.dumps(veri, ensure_ascii=False))

    def _host_uygun(self):
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        return host in ("127.0.0.1", "localhost", "")

    def _yerel_istek(self):
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _cerez_anahtari(self):
        try:
            c = SimpleCookie(self.headers.get("Cookie") or "")
            return c["kf"].value if "kf" in c else ""
        except Exception:
            return ""

    def _yetkili(self):
        """Yerel modda yalnızca bu cihaz; ağ modunda bu cihaz ya da anahtarlı çerezi olan telefon."""
        if self._yerel_istek() and self._host_uygun():
            return True
        if self.ag_modu and self.anahtar:
            return hmac.compare_digest(self._cerez_anahtari(), self.anahtar)
        return False

    def do_GET(self):
        parca = urllib.parse.urlparse(self.path)
        yol = parca.path
        if self.ag_modu and yol in ("/", "/index.html"):
            k = urllib.parse.parse_qs(parca.query).get("k", [""])[0]
            if k and hmac.compare_digest(k, self.anahtar):
                # Anahtarı bir yıllık çereze yaz ve sayfayı hemen göster. (Yönlendirme yapılmaz: başka bir
                # uygulamadan açılan bağlantıda SameSite çerezi yönlendirmeden sonra gönderilmeyebiliyor.)
                return self._gonder(200, ARAYUZ_HTML, "text/html; charset=utf-8", {
                    "Set-Cookie": f"kf={self.anahtar}; Path=/; Max-Age=31536000; SameSite=Lax; HttpOnly"})
        # Simge ve manifest gizli değildir; tarayıcı bunları çerezsiz ister
        if yol in IKONLAR or yol == "/favicon.ico":
            return self._gonder(200, base64.b64decode(IKONLAR.get(yol, IKONLAR["/ikon-32.png"])), "image/png",
                                {"Cache-Control": "public, max-age=604800"})
        if yol == "/manifest.webmanifest":
            return self._gonder(200, MANIFEST, "application/manifest+json; charset=utf-8")
        if not self._yetkili():
            if yol in ("/", "/index.html"):
                return self._gonder(403, YETKISIZ_HTML, "text/html; charset=utf-8")
            return self._json(403, {"hata": "izin yok"})
        if yol in ("/", "/index.html"):
            self._gonder(200, ARAYUZ_HTML, "text/html; charset=utf-8")
        elif yol == "/api/durum":
            d = self.depo.durum()
            d["ag"] = {"acik": self.ag_modu, "adres": self.ag_adresi if self._yerel_istek() else None,
                       "bu_cihaz": self._yerel_istek()}
            self._json(200, d)
        elif yol == "/api/ara":
            q = urllib.parse.parse_qs(parca.query).get("q", [""])[0]
            self._json(200, {"idler": self.depo.ara(q)})
        else:
            self._json(404, {"hata": "bulunamadı"})

    def do_POST(self):
        # Özel başlık: başka sitelerin bu adrese istek atmasını engeller.
        if not self._yetkili() or self.headers.get("X-Kasim") != "1":
            return self._json(403, {"hata": "izin yok"})
        uzunluk = int(self.headers.get("Content-Length") or 0)
        if uzunluk > 20000:
            return self._json(413, {"hata": "istek çok büyük"})
        govde = self.rfile.read(uzunluk) if uzunluk else b""
        yol = self.path.split("?")[0]
        if yol == "/api/yenile":
            self.depo.tetikle()
            return self._json(200, {"ok": True})
        if yol == "/api/gecmis":
            try:
                veri = json.loads(govde or b"{}")
                return self._json(200, self.depo.gecmisi_getir(str(veri.get("id", ""))))
            except ValueError:
                return self._json(400, {"hata": "Geçersiz istek"})
        if yol == "/api/canli":
            try:
                veri = json.loads(govde or b"{}")
                return self._json(200, self.depo.canli_kontrol(str(veri.get("id", "")), bool(veri.get("zorla"))))
            except ValueError as e:
                return self._json(400, {"hata": str(e)})
        if yol in ("/api/alarm", "/api/bildirim-dene"):
            try:
                veri = json.loads(govde or b"{}")
                if not isinstance(veri, dict):
                    raise ValueError("Geçersiz istek")
                if yol == "/api/alarm":
                    return self._json(200, {"ok": True, "alarmlar": self.depo.alarm_islem(veri)})
                return self._json(200, {"ok": self.depo.deneme_bildirimi()})
            except ValueError as e:
                return self._json(400, {"hata": str(e)})
        if yol == "/api/ayarlar":
            try:
                veri = json.loads(govde or b"{}")
                if not isinstance(veri, dict):
                    raise ValueError("Geçersiz istek")
                return self._json(200, {"ok": True, "ayarlar": self.depo.ayarlari_guncelle(veri)})
            except ValueError as e:
                return self._json(400, {"hata": str(e)})
        self._json(404, {"hata": "bulunamadı"})


def tarayicida_ac(url):
    if shutil.which("termux-open-url"):
        subprocess.run(["termux-open-url", url], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    try:
        import webbrowser
        return webbrowser.open(url)
    except Exception:
        return False


def kisayol_olustur():
    dizin = os.path.join(os.path.expanduser("~"), ".shortcuts")
    os.makedirs(dizin, exist_ok=True)
    yol = os.path.join(dizin, "Kasim-Firsatlari.sh")
    betik = os.path.abspath(__file__)
    with open(yol, "w", encoding="utf-8") as f:
        f.write("#!/data/data/com.termux/files/usr/bin/bash\n")
        f.write(f"python {shlex.quote(betik)}\n")
    os.chmod(yol, 0o755)
    print("Kısayol oluşturuldu:", yol)
    print("Termux:Widget uygulamasını kurup ana ekrana 'Termux:Widget' bileşenini ekleyin;")
    print("listede 'Kasim-Firsatlari.sh' görünecek.")


def tek_sefer():
    depo = Depo()
    print("Kanallar okunuyor:", ", ".join(depo.ayarlar["kanallar"]))
    depo.yenile()
    for k in depo.durum()["kaynaklar"]:
        print(f"  {'✓' if k['ok'] else '✗'} {k['ad']}: {k['adet']} paylaşım" + (f" — {k['hata']}" if k["hata"] else ""))
    liste = [d for d in depo.firsatlar() if d["tur"] == "urun"]
    liste.sort(key=lambda d: (-(d["indirim"] or -1), -d["ts"]))
    print("\nEn yüksek indirimler:")
    for d in liste[:15]:
        oran = f"%{d['indirim']:>2}" if d["indirim"] is not None else (" dip" if "dip" in d["rozetler"] else "   -")
        print(f"{oran}  {fiyat_yaz(d['fiyat']):>14}  {d['magaza_ad']:<11} {d['baslik'][:60]}")


def dosyadan_coz(yol):
    with open(yol, encoding="utf-8") as f:
        ad, gonderiler = sayfayi_coz(f.read())
    print("Kanal:", ad, "· paylaşım:", len(gonderiler))
    for g in gonderiler:
        print(json.dumps({k: g[k] for k in ("id", "baslik", "fiyat", "eski_fiyat", "yuzde", "xaly", "rozetler",
                                             "kodlar", "magaza", "tur", "anahtar")}, ensure_ascii=False))


STATIK_ARAYUZ_EKI = r"""<script>
"use strict";
// Statik site (GitHub Pages): veriler veri.json'dan, kişisel ayarlar bu telefonda (localStorage)
(() => {
  const asil = window.fetch.bind(window);
  const sakla = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} };
  const oku = (k, v) => { try { const x = JSON.parse(localStorage.getItem(k)); return x == null ? v : x; } catch (e) { return v; } };
  const sade = t => String(t || "").toLocaleLowerCase("tr-TR").normalize("NFD").replace(/[\u0300-\u036f]/g, "").replace(/ı/g, "i");
  const yanit = (v, st) => new Response(JSON.stringify(v), {status: st || 200, headers: {"Content-Type": "application/json"}});
  let son = null;
  async function veriAl(){
    const r = await asil("veri.json?t=" + Date.now(), {cache: "no-store"});
    son = await r.json();
    return son;
  }
  function kisisel(v){
    const takip = oku("kf_takip", []), alarmlar = oku("kf_alarmlar", []);
    v.ayarlar = Object.assign({}, v.ayarlar, {takip, alarmlar});
    for (const x of v.firsatlar) {
      const m = sade(x.baslik + " " + x.metin);
      x.takip = takip.filter(k => m.includes(sade(k)));
      x.alarm = x.alarm_kurulu = null;
      const a = alarmlar.find(a => a.anahtar && a.anahtar === x.anahtar);
      if (a && x.fiyat != null) { x.alarm_kurulu = {id: a.id, hedef: a.hedef}; if (x.fiyat <= a.hedef) { x.alarm = x.alarm_kurulu; if (!x.rozetler.includes("hedef")) x.rozetler.push("hedef"); } }
    }
    return v;
  }
  window.fetch = async (url, opt) => {
    url = String(url);
    if (url.startsWith("/api/durum")) return yanit(kisisel(await veriAl()));
    if (url.startsWith("/api/yenile")) { setTimeout(() => typeof toast === "function" && toast("Site yaklaşık 5 dakikada bir kendiliğinden güncellenir"), 50); return yanit({ok: true}); }
    if (url.startsWith("/api/ayarlar")) {
      const a = JSON.parse((opt && opt.body) || "{}");
      if ("takip" in a) { sakla("kf_takip", a.takip); return yanit({ok: true}); }
      return yanit({hata: "Sitede bu ayar değiştirilemez"}, 400);
    }
    if (url.startsWith("/api/alarm")) {
      const b = JSON.parse((opt && opt.body) || "{}"); let al = oku("kf_alarmlar", []);
      if (b.islem === "ekle") {
        const x = (son && son.firsatlar || []).find(y => y.id === b.id); const h = +String(b.hedef).replace(",", ".");
        if (!x || !(h >= 1)) return yanit({hata: "Hedef fiyat geçersiz"}, 400);
        al = al.filter(a => a.anahtar !== x.anahtar).concat([{id: Math.random().toString(16).slice(2, 10), baslik: x.baslik, anahtar: x.anahtar, hedef: h, ilk_fiyat: x.fiyat, ts: Date.now() / 1000}]);
      } else al = al.filter(a => a.id !== b.alarm_id);
      sakla("kf_alarmlar", al); return yanit({ok: true, alarmlar: al});
    }
    if (url.startsWith("/api/ara") || url.startsWith("/api/gecmis")) return yanit({idler: [], ok: true});
    if (url.startsWith("/api/canli")) return yanit({durum: "hata", mesaj: "Sitede anlık kontrol yok"});
    if (url.startsWith("/api/")) return yanit({hata: "Sitede bu işlem yok"}, 400);
    return asil(url, opt);
  };
})();
</script>
"""


def statik_site(cikis):
    """GitHub Pages gibi bir yerde yayınlanacak, kendi kendine güncellenen site üretir."""
    os.makedirs(cikis, exist_ok=True)
    depo = Depo()
    konu = os.environ.get("NTFY_KONU", "").strip()
    if konu and NTFY_KONU_RE.match(konu):
        depo.ayarlar["ntfy_konu"] = konu
    depo.yenile()
    depo.gorunenleri_dogrula()
    with depo.kilit:
        d = depo.durum()
    d["statik"] = True
    # Günlükte hangi mağazanın fiyatının okunabildiği görünsün (sorun teşhisi için)
    say = {}
    for x in d["firsatlar"]:
        c = x.get("canli")
        if c:
            k = (x.get("magaza_ad") or "?", c.get("durum"))
            say[k] = say.get(k, 0) + 1
    for (m, durum), n in sorted(say.items()):
        print(f"  mağaza kontrolü · {m}: {durum} × {n}")
    d["bildirim_var"] = bool(konu)
    d.pop("ag", None)
    d["ayarlar"] = {k: v for k, v in d["ayarlar"].items() if k not in ("ntfy_konu", "ntfy_sunucu", "alarmlar", "takip")}
    with open(os.path.join(cikis, "veri.json"), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
    h = ARAYUZ_HTML.replace('href="/manifest.webmanifest"', 'href="manifest.webmanifest"') \
        .replace('href="/ikon-32.png"', 'href="ikon-32.png"').replace('href="/ikon-180.png"', 'href="ikon-180.png"')
    h = h.replace('<script>\n"use strict";\ntry {', STATIK_ARAYUZ_EKI + '<script>\n"use strict";\ntry {', 1)
    with open(os.path.join(cikis, "index.html"), "w", encoding="utf-8") as f:
        f.write(h)
    man = json.loads(MANIFEST)
    man["start_url"] = man["scope"] = "./"
    for i in man["icons"]:
        i["src"] = i["src"].lstrip("/")
    with open(os.path.join(cikis, "manifest.webmanifest"), "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False)
    for yol, b64 in IKONLAR.items():
        with open(os.path.join(cikis, yol.lstrip("/")), "wb") as f:
            f.write(base64.b64decode(b64))
    with open(os.path.join(cikis, ".nojekyll"), "w") as f:
        f.write("")
    print(f"Site hazır: {cikis} · {len(d['firsatlar'])} paylaşım · {sum(1 for x in d['firsatlar'] if x.get('kacirilmaz'))} kaçırılmaz")


def main():
    ap = argparse.ArgumentParser(description="Kasım Fırsatları — telefonda çalışan indirim takipçisi")
    ap.add_argument("--port", type=int, default=VARSAYILAN_PORT, help="yerel sayfanın portu (varsayılan 8765)")
    ap.add_argument("--tarayici-acma", action="store_true", help="tarayıcıyı otomatik açma")
    ap.add_argument("--kisayol", action="store_true", help="Termux:Widget için ana ekran kısayolu oluştur")
    ap.add_argument("--tek-sefer", action="store_true", help="bir kez çek, en iyi 15 fırsatı terminale yaz")
    ap.add_argument("--statik", metavar="KLASOR", help="bir kez çek ve internette yayınlanacak siteyi bu klasöre yaz")
    ap.add_argument("--dosyadan", metavar="HTML", help="(test) kaydedilmiş t.me/s sayfasını çöz")
    ap.add_argument("--ag", action="store_true",
                    help="aynı Wi-Fi'deki telefondan açılabilsin (bilgisayarda çalıştırırken kullanın)")
    a = ap.parse_args()
    for akis in (sys.stdout, sys.stderr):
        try:
            akis.reconfigure(errors="replace")   # Windows konsolunda Türkçe/simge hatası olmasın
        except (AttributeError, ValueError):
            pass

    if a.kisayol:
        return kisayol_olustur()
    if a.tek_sefer:
        return tek_sefer()
    if a.statik:
        return statik_site(a.statik)
    if a.dosyadan:
        return dosyadan_coz(a.dosyadan)

    url = f"http://127.0.0.1:{a.port}/"
    try:
        sunucu = ThreadingHTTPServer(("0.0.0.0" if a.ag else "127.0.0.1", a.port), Isleyici)
    except OSError:
        print("Program zaten çalışıyor görünüyor; sayfa açılıyor:", url)
        if not a.tarayici_acma:
            tarayicida_ac(url)
        return

    depo = Depo()
    Isleyici.depo = depo
    if a.ag:
        Isleyici.ag_modu = True
        Isleyici.anahtar = erisim_anahtari()
        ip = yerel_ag_adresi()
        Isleyici.ag_adresi = f"http://{ip}:{a.port}/?k={Isleyici.anahtar}" if ip else None
    threading.Thread(target=depo.dongu, daemon=True).start()
    print(f"Kasım Fırsatları {SURUM} çalışıyor → {url}")
    print(f"Kanallar: {', '.join(depo.ayarlar['kanallar'])} · her {depo.ayarlar['aralik_dk']} dk yenilenir")
    if a.ag:
        if Isleyici.ag_adresi:
            print("\nTelefondan açmak için (telefon aynı Wi-Fi'de olmalı):")
            print("  " + Isleyici.ag_adresi)
            print("  ya da bilgisayardaki sayfada Ayarlar → Telefonda aç bölümündeki QR kodu okutun.")
            print("  Windows güvenlik duvarı sorarsa 'Özel ağlar' için İzin ver'i seçin.")
        else:
            print("\nUyarı: Wi-Fi ağ adresi bulunamadı; bilgisayar bir ağa bağlı mı?")
    print("\nKapatmak için bu pencereyi kapatın ya da CTRL + C'ye basın.")
    if not a.tarayici_acma:
        threading.Timer(1.0, tarayicida_ac, args=(url,)).start()
    try:
        sunucu.serve_forever()
    except KeyboardInterrupt:
        print("\nKapatıldı.")
    finally:
        sunucu.server_close()


# ---------------------------------------------------------------------------
# Arayüz (tek sayfa)
# ---------------------------------------------------------------------------

ARAYUZ_HTML = r"""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#c2410c">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" type="image/png" sizes="32x32" href="/ikon-32.png">
<link rel="apple-touch-icon" href="/ikon-180.png">
<meta name="apple-mobile-web-app-title" content="Fırsatlar">
<meta name="application-name" content="Fırsatlar">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>Kasım Fırsatları</title>
<style>
:root{
  --bg:#f6f3ef; --yuzey:#ffffff; --yuzey2:#efe9e1; --metin:#1c1917; --soluk:#6b635b; --cizgi:#e3dbd1;
  --vurgu:#c2410c; --vurgu-ust:#ffffff; --vurgu-yumusak:#fde7d9;
  --yesil:#166534; --yesil-y:#dcfce7; --mavi:#1e40af; --mavi-y:#dbeafe; --sari:#854d0e; --sari-y:#fef3c7;
  --kirmizi:#b91c1c; --kirmizi-y:#fee2e2; --gri-y:#ece6de;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#13100e; --yuzey:#1d1916; --yuzey2:#29231f; --metin:#f4efe9; --soluk:#a69c92; --cizgi:#342d28;
    --vurgu:#fb923c; --vurgu-ust:#1a0f08; --vurgu-yumusak:#3a2315;
    --yesil:#86efac; --yesil-y:#14301f; --mavi:#a5c0ff; --mavi-y:#1a2440; --sari:#fcd34d; --sari-y:#2e2610;
    --kirmizi:#fca5a5; --kirmizi-y:#3a1717; --gri-y:#2a2420;
  }
}
*{box-sizing:border-box}
[hidden]{display:none!important}
html,body{margin:0}
body{background:var(--bg);color:var(--metin);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;-webkit-tap-highlight-color:transparent}
button{font:inherit;color:inherit;cursor:pointer}
a{color:inherit}
header{position:sticky;top:0;z-index:5;background:var(--bg);padding:12px 16px 0;border-bottom:1px solid var(--cizgi)}
.kap{max-width:760px;margin:0 auto}
.ust{display:flex;align-items:center;gap:12px}
.ust .bas{flex:1;min-width:0}
h1{font-size:20px;margin:0;letter-spacing:-.01em}
.durum{font-size:12.5px;color:var(--soluk);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.durum .hata{color:var(--kirmizi);text-decoration:underline;background:none;border:0;padding:0;font-size:inherit}
.yenile{flex:none;display:flex;align-items:center;gap:6px;border:1px solid var(--cizgi);background:var(--yuzey);border-radius:999px;padding:8px 14px;font-weight:600;font-size:14px}
.yenile svg{width:16px;height:16px}
.yenile.don svg{animation:don 1s linear infinite}
@keyframes don{to{transform:rotate(360deg)}}
.sayac{display:flex;gap:6px;overflow-x:auto;padding:10px 0 2px;scrollbar-width:none}
.sayac::-webkit-scrollbar,.cipler::-webkit-scrollbar,nav::-webkit-scrollbar{display:none}
.sayac span{flex:none;font-size:12.5px;background:var(--yuzey2);border-radius:999px;padding:4px 10px}
.sayac b{color:var(--vurgu)}
nav{display:flex;gap:2px;overflow-x:auto;scrollbar-width:none;margin-top:4px}
nav button{flex:none;background:none;border:0;padding:10px 10px 9px;font-weight:600;font-size:14.5px;color:var(--soluk);border-bottom:2px solid transparent}
nav button[aria-selected=true]{color:var(--metin);border-color:var(--vurgu)}
nav .say{font-weight:500;font-size:12px;color:var(--soluk);margin-left:4px}
main{padding:12px 16px 90px}
.filtre{display:flex;flex-direction:column;gap:8px;margin-bottom:12px}
input[type=search],input[type=text],input[type=number],select{font:inherit;color:inherit;background:var(--yuzey);border:1px solid var(--cizgi);border-radius:10px;padding:9px 12px;width:100%;min-width:0}
.satir{display:flex;gap:8px;align-items:center}
.satir>*{flex:1}
.cipler{display:flex;gap:6px;overflow-x:auto;scrollbar-width:none}
.cip{flex:none;border:1px solid var(--cizgi);background:var(--yuzey);border-radius:999px;padding:6px 12px;font-size:13.5px}
.cip[aria-pressed=true]{background:var(--metin);color:var(--bg);border-color:var(--metin)}
.anahtar{display:flex;align-items:center;gap:8px;font-size:13.5px;flex:none !important;white-space:nowrap}
.anahtar input{width:18px;height:18px;accent-color:var(--vurgu)}
.not{font-size:12.5px;color:var(--soluk);background:var(--yuzey2);border-radius:10px;padding:8px 12px;margin-bottom:12px}
.kart{display:grid;grid-template-columns:76px 1fr;gap:12px;background:var(--yuzey);border:1px solid var(--cizgi);border-radius:14px;padding:12px;margin-bottom:10px}
.resim{width:76px;height:76px;border-radius:10px;background:var(--yuzey2);object-fit:cover;display:block}
.resim.bos{display:flex;align-items:center;justify-content:center;font-size:12px;color:var(--soluk);font-weight:700}
.govde{min-width:0}
.kbas{display:flex;gap:10px;align-items:flex-start}
.kbaslik{font-weight:600;font-size:14.5px;flex:1;min-width:0;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere}
.oran{flex:none;text-align:center;background:var(--vurgu);color:var(--vurgu-ust);border-radius:10px;padding:5px 8px 4px;font-weight:800;font-size:18px;line-height:1.05;min-width:56px}
.oran small{display:block;font-size:10px;font-weight:600;opacity:.9;letter-spacing:.02em}
.oran.orta{background:var(--vurgu-yumusak);color:var(--vurgu)}
.fiyat{margin-top:6px;font-size:18px;font-weight:800;letter-spacing:-.01em}
.fiyat s{color:var(--soluk);font-weight:500;font-size:13.5px;margin-left:6px}
.meta{font-size:12.5px;color:var(--soluk);margin-top:3px;overflow-wrap:anywhere}
.fiyat small{font-size:13px;font-weight:600;color:var(--soluk)}
.fiyatnot{font-size:12.5px;line-height:1.4;color:var(--vurgu);background:var(--vurgu-yumusak);border-radius:8px;padding:5px 9px;margin-top:6px}
.kart.kupon{display:block;border-style:dashed;border-width:1.5px}
.kupon-ust{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.kupon-ust .magaza{font-weight:700;font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--vurgu)}
.kupon-ust .meta{margin:0;text-align:right}
.kupon-bas{font-size:22px;font-weight:800;letter-spacing:-.01em;margin-top:4px;overflow-wrap:anywhere}
.kodlar{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.fiyatlar{margin-top:8px;display:grid;gap:2px}
.fsatir{display:flex;align-items:baseline;justify-content:space-between;gap:10px}
.fsatir .fet{font-size:12px;font-weight:600;color:var(--soluk);text-transform:uppercase;letter-spacing:.04em}
.fsatir.satis .fdeger{font-size:14.5px;color:var(--soluk);font-variant-numeric:tabular-nums}
.fsatir.indirimli .fdeger{font-size:20px;font-weight:800;letter-spacing:-.01em;font-variant-numeric:tabular-nums}
.fsatir.indirimli .fet{color:var(--metin)}
.fsatir small{font-size:13px;font-weight:600;color:var(--soluk)}
.fkaynak{font-size:11.5px;color:var(--yesil);font-weight:600}
.eylem a.kaynak-link{border:0;background:none;padding:7px 4px;color:var(--soluk);text-decoration:underline}
.eylem button.ikincil{font-weight:600;font-size:13.5px;border-radius:9px;padding:7px 11px;border:1px solid var(--cizgi);background:var(--yuzey)}
.eylem button.ikincil[aria-pressed=true]{color:var(--vurgu);border-color:var(--vurgu)}
.rozet.r-takip{background:var(--vurgu);color:var(--vurgu-ust)}
.hedef-form{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px;padding:10px;border:1px dashed var(--cizgi);border-radius:10px}
.hedef-form input{width:130px;flex:none}
.hedef-form .ipucu{flex-basis:100%;margin:0}
.alarm{display:flex;gap:10px;align-items:center;padding:10px 0;border-bottom:1px solid var(--cizgi)}
.alarm .ad{flex:1;min-width:0}.alarm .ad b{display:block;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.alarm .ad div{font-size:12.5px;color:var(--soluk)}
.alarm button{flex:none;font:inherit;font-size:13px;font-weight:600;border-radius:9px;padding:6px 10px;border:1px solid var(--cizgi);background:var(--yuzey);color:inherit}
.alarm.tuttu .ad b{color:var(--yesil,#15803d)}
.adimlar{margin:6px 0 0;padding-left:20px;font-size:14px}.adimlar li{margin:4px 0}
.konu{font-family:ui-monospace,Menlo,Consolas,monospace;font-weight:700}
.cipler.sar{flex-wrap:wrap}
.takipcip{display:inline-flex;align-items:center;gap:6px}
.takipcip button{border:0;background:none;font-size:16px;line-height:1;padding:0 2px;color:var(--soluk)}
.bolum{font-size:15px;margin:18px 0 10px}
.bolum .say{font-weight:500;color:var(--soluk);font-size:13px}
.kart.soluk{opacity:.7}
ul.gecmis{list-style:none;padding:0;margin:6px 0 0;display:grid;gap:3px}
ul.gecmis li{display:flex;justify-content:space-between;font-variant-numeric:tabular-nums}
ul.gecmis li.simdi{color:var(--metin);font-weight:700}
.arama-ozet{font-size:13.5px;color:var(--soluk);margin-bottom:12px}
.ipucu{font-size:12.5px;color:var(--soluk)}
.ozet{margin-top:10px;border-top:1px solid var(--cizgi);padding-top:8px;display:grid;gap:4px}
.ozet.bos{display:flex;flex-wrap:wrap;align-items:center;gap:8px 14px}
.o-satir{display:flex;justify-content:space-between;gap:10px;font-size:12.5px;font-variant-numeric:tabular-nums}
.o-satir span:first-child{color:var(--soluk)}
.o-satir span:last-child{text-align:right}
.o-alt{display:flex;flex-wrap:wrap;justify-content:space-between;gap:4px 12px;margin-top:2px}
b.dus{color:var(--yesil)} b.art{color:var(--kirmizi)}
.grafik{width:100%;height:56px;display:block}
.kbaslik[data-detay],.resim[data-detay]{cursor:pointer}
.kbaslik[data-detay]:hover{text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:3px}
.rozet.r-dusus{background:var(--yesil-y,#dcfce7);color:var(--yesil,#15803d)}
#detay{position:fixed;inset:0;z-index:30;background:var(--bg);overflow-y:auto;overscroll-behavior:contain;-webkit-overflow-scrolling:touch}
#detay[hidden]{display:none!important}
.d-ust{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--cizgi);padding:10px 16px;display:flex;align-items:center;gap:10px}
.d-ust button{font:inherit;font-weight:700;font-size:15px;border:1px solid var(--cizgi);background:var(--yuzey);color:inherit;border-radius:10px;padding:7px 12px}
.d-ust span{font-size:13px;color:var(--soluk);overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.d-ic{max-width:760px;margin:0 auto;padding:14px 16px 90px}
.d-bas{display:flex;gap:14px;align-items:flex-start}
.d-bas img{width:96px;height:96px;object-fit:contain;border-radius:12px;background:#fff;flex:none}
.d-bas h1{font-size:19px;line-height:1.3;margin:0 0 4px}
.d-bolum{margin-top:18px}
.d-bolum h2{font-size:15px;margin:0 0 8px}
.d-istat{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}
.d-istat div{border:1px solid var(--cizgi);border-radius:12px;padding:9px 10px;background:var(--yuzey)}
.d-istat small{display:block;font-size:11.5px;color:var(--soluk)}
.d-istat b{display:block;font-size:15px;margin-top:2px;font-variant-numeric:tabular-nums}
.d-istat em{display:block;font-style:normal;font-size:11px;color:var(--soluk)}
@media (max-width:520px){.d-istat{grid-template-columns:repeat(2,minmax(0,1fr))}}
.bgrafik{width:100%;height:auto;display:block;border:1px solid var(--cizgi);border-radius:12px;background:var(--yuzey)}
.bgrafik text{font-size:12px;fill:var(--soluk);font-family:inherit}
.bg-izgara{stroke:var(--cizgi);stroke-width:1}
.bg-ort{stroke:var(--soluk);stroke-width:1;stroke-dasharray:4 4}
.d-tablo{width:100%;border-collapse:collapse;font-size:14px;font-variant-numeric:tabular-nums}
.d-tablo td,.d-tablo th{padding:8px 6px;border-bottom:1px solid var(--cizgi);text-align:left}
.d-tablo th{font-size:12px;color:var(--soluk);font-weight:600}
.d-tablo td.sag,.d-tablo th.sag{text-align:right}
.d-tablo .dus{color:var(--yesil,#15803d);font-weight:700}.d-tablo .art{color:var(--kirmizi,#b91c1c);font-weight:700}
.d-tablo tr.enucuz td{font-weight:700}
.d-tablo a{color:inherit}
.eski-liste{margin-top:22px}
.k-bitti{opacity:.62}
.rozet.r-kac{background:#b91c1c;color:#fff;font-weight:700}
.fdeger.bilinmiyor{font-size:20px;color:var(--soluk)}.fdeger.bilinmiyor small{display:block;font-size:11px;font-weight:500;text-align:right}
.fsatir.kosullu .fet{color:var(--yesil);font-weight:700}.fsatir.kosullu .fdeger{color:var(--yesil)}
.fsatir.eski .fdeger{color:var(--soluk);font-size:15px}
.fdurum{margin-top:6px;font-size:12.5px;font-weight:700;border-radius:8px;padding:5px 8px}
.fdurum.c-yesil{background:var(--yesil-y);color:var(--yesil)}.fdurum.c-sari{background:var(--sari-y);color:var(--sari)}.fdurum.c-kirmizi{background:var(--kirmizi-y);color:var(--kirmizi)}
.canli{margin-top:10px;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.canli.c-yesil,.canli.c-sari,.canli.c-kirmizi,.canli.c-gri{display:block;border-radius:12px;padding:10px 12px;border:1px solid var(--cizgi)}
.canli.c-yesil{background:var(--yesil-y);border-color:transparent}.canli.c-sari{background:var(--sari-y);border-color:transparent}
.canli.c-kirmizi{background:var(--kirmizi-y);border-color:transparent}
.c-ust{display:flex;justify-content:space-between;align-items:baseline;gap:10px}.c-ust span{font-size:13px;font-weight:600}
.c-ust b{font-size:20px;font-variant-numeric:tabular-nums}
.c-alt{font-size:12px;color:var(--soluk);margin-top:2px}.c-karar{font-size:14px;font-weight:600;margin-top:6px}.eski-liste td small{color:var(--soluk)}
.d-dugmeler{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.d-dugmeler a,.d-dugmeler button{font:inherit;font-size:14px;font-weight:600;text-decoration:none;border-radius:10px;padding:9px 13px;border:1px solid var(--cizgi);background:var(--yuzey);color:inherit}
.d-dugmeler a.ana{background:var(--vurgu);color:var(--vurgu-ust);border-color:var(--vurgu)}
.g-cizgi{stroke:var(--vurgu);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.g-alan{fill:var(--vurgu-yumusak)}
.g-nokta{fill:var(--yuzey);stroke:var(--vurgu);stroke-width:1.5}
.g-son{fill:var(--vurgu);stroke:var(--yuzey);stroke-width:2}
.g-eksen{display:flex;justify-content:space-between;gap:6px;font-size:11px;color:var(--soluk);font-variant-numeric:tabular-nums}
a.akakce{font-size:12.5px;font-weight:600;color:var(--mavi)}
.gecmis-btn{border:1px solid var(--vurgu);color:var(--vurgu);background:var(--yuzey);border-radius:9px;padding:6px 11px;font-weight:600;font-size:13px}
.gecmis-btn:disabled{opacity:.6}
.telefon-panel{background:var(--yuzey);border:1px solid var(--cizgi);border-radius:14px;padding:4px 14px 14px;margin-bottom:6px}
.tp-govde{display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.qr{flex:none;width:184px;min-height:184px;background:#fff;border-radius:10px;padding:8px;display:flex;align-items:center;justify-content:center;font-size:12.5px;color:#444;text-align:center}
.qr img,.qr canvas{display:block}
.tp-adim{margin:0;padding-left:18px;font-size:13.5px;flex:1;min-width:200px}
.tp-adim li{margin-bottom:4px}
.yenile.telefon{background:var(--vurgu-yumusak);border-color:var(--vurgu);color:var(--vurgu)}
.kod.buyuk{font-size:15px;padding:7px 12px;border-width:1.5px;border-radius:8px}
.kaynak{font-size:12px;color:var(--soluk);margin-top:2px}
.rozetler{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px}
.rozet{font-size:11.5px;font-weight:600;border-radius:6px;padding:2px 7px;background:var(--gri-y)}
.r-yesil{background:var(--yesil-y);color:var(--yesil)}
.r-mavi{background:var(--mavi-y);color:var(--mavi)}
.r-sari{background:var(--sari-y);color:var(--sari)}
.r-kirmizi{background:var(--kirmizi-y);color:var(--kirmizi)}
.kod{display:inline-flex;align-items:center;gap:6px;border:1px dashed var(--vurgu);color:var(--vurgu);border-radius:6px;padding:2px 8px;font:600 12.5px ui-monospace,Menlo,monospace;background:none}
.eylem{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
.eylem a{text-decoration:none;font-weight:600;font-size:13.5px;border-radius:9px;padding:7px 12px;border:1px solid var(--cizgi);background:var(--yuzey)}
.eylem a.ana{background:var(--vurgu);border-color:var(--vurgu);color:var(--vurgu-ust)}
details.metin{margin-top:8px;font-size:13px}
details.metin summary{color:var(--soluk);cursor:pointer}
details.metin div{white-space:pre-wrap;margin-top:6px;color:var(--soluk);overflow-wrap:anywhere}
.bos-durum{text-align:center;color:var(--soluk);padding:40px 12px}
.daha{display:block;width:100%;padding:12px;border:1px solid var(--cizgi);background:var(--yuzey);border-radius:12px;font-weight:600}
.takvim .olay{display:grid;grid-template-columns:92px 1fr;gap:12px;padding:12px 0;border-bottom:1px solid var(--cizgi)}
.takvim .tarih{font-size:12.5px;font-weight:700;color:var(--soluk)}
.takvim .ad{font-weight:600}
.takvim .ac{font-size:12.5px;color:var(--soluk)}
.durumcip{display:inline-block;font-size:11px;font-weight:700;border-radius:6px;padding:1px 6px;margin-left:6px;vertical-align:1px}
.ayar h2{font-size:15px;margin:18px 0 8px}
.ayar label{display:block;font-size:13.5px;font-weight:600;margin:12px 0 4px}
.ayar .ipucu{font-size:12.5px;color:var(--soluk);margin-top:3px}
.kanal{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--cizgi)}
.kanal .ad{flex:1;min-width:0}
.kanal .ad div{font-size:12.5px;color:var(--soluk);overflow-wrap:anywhere}
.kanal button{border:1px solid var(--cizgi);background:var(--yuzey);border-radius:8px;padding:5px 10px;font-size:13px}
.nokta{width:9px;height:9px;border-radius:50%;flex:none;background:var(--soluk)}
.nokta.ok{background:#16a34a}.nokta.hata{background:#dc2626}
.kaydet{margin-top:16px;width:100%;padding:12px;border:0;border-radius:12px;background:var(--vurgu);color:var(--vurgu-ust);font-weight:700}
.mesaj{margin-top:10px;font-size:13.5px}
.mesaj.hata{color:var(--kirmizi)}
.bilgi{font-size:13.5px;line-height:1.5}
.bilgi li{margin-bottom:4px}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:var(--metin);color:var(--bg);padding:9px 16px;border-radius:999px;font-size:13.5px;font-weight:600;opacity:0;transition:opacity .2s;pointer-events:none}
.toast.goster{opacity:1}
</style>
</head>
<body>
<header><div class="kap">
  <div class="ust">
    <div class="bas"><h1>Kasım Fırsatları</h1><div class="durum" id="durum">Yükleniyor…</div></div>
    <button class="yenile telefon" id="telefonBtn" type="button" hidden data-git="ayar">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="7" y="2.5" width="10" height="19" rx="2.2"/><path d="M11 18.5h2"/></svg>
      <span>Telefonda aç</span>
    </button>
    <button class="yenile" id="yenileBtn" type="button" aria-label="Şimdi yenile">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>
      <span>Yenile</span>
    </button>
  </div>
  <div class="sayac" id="sayac"></div>
  <nav id="nav" role="tablist"></nav>
</div></header>
<main><div class="kap" id="icerik"></div></main>
<div class="toast" id="toast"></div>
<script>
"use strict";
try { if (/[?&]k=/.test(location.search)) history.replaceState(null, "", location.pathname); } catch (e) {}
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const link = u => (typeof u === "string" && /^https?:\/\//i.test(u)) ? u : null;
const trK = s => String(s || "").toLocaleLowerCase("tr-TR");
const nf = new Intl.NumberFormat("tr-TR", {maximumFractionDigits: 2});
const kf = new Intl.NumberFormat("tr-TR", {notation: "compact", maximumFractionDigits: 1});
const tl = v => v == null ? "" : nf.format(v) + " TL";

const SEKMELER = [["kac","🔥 Kaçırılmaz"],["one","Öne çıkanlar"],["ucuz","Ucuzlayanlar"],["tum","Tüm ürünler"],["kupon","Kupon & kampanya"],["takip","Takibim"],["takvim","Takvim"],["ayar","Ayarlar"]];
const ROZET = {
  dip:["Dip fiyat","r-yesil"], kayit_dusuk:["Kaydettiğim en düşük","r-yesil"], ikinci_el:["2. el","r-sari"],
  stok_az:["Stok az","r-kirmizi"], sepette:["Fiyat sepette düşer","r-mavi"], kupon:["Kupon / kod","r-mavi"],
  hedef:["🎯 Hedef fiyatına indi","r-takip"]
};
const SAYAC = [["11.11","2026-11-11"],["Efsane Cuma","2026-11-27"],["Siber Pazartesi","2026-11-30"]];
const TAKVIM = [
  {bas:"2026-10-26", son:"2026-11-30", ad:"Hepsiburada Efsane Kasım", ac:"Kasım boyunca ek %10–15 net indirim, bazı günlerde %20’ye kadar"},
  {bas:"2026-10-26", son:"2026-11-30", ad:"Efsane Xiaomi Günleri (mi.com)"},
  {bas:"2026-10-30", son:null, ad:"Amazon Gülümseten Kasım", ac:"Bitiş tarihi duyurulmadı"},
  {bas:"2026-11-02", son:"2026-11-11", ad:"Hepsiburada Efsane 11 İndirimleri"},
  {bas:"2026-11-02", son:"2026-11-11", ad:"LC Waikiki Bambaşka Kasım"},
  {bas:"2026-11-06", son:"2026-11-11", ad:"Trendyol Süper Alışveriş Günleri"},
  {bas:"2026-11-11", son:"2026-11-11", ad:"11.11 Bekarlar Günü", ac:"n11, Hepsiburada, Trendyol, Amazon"},
  {bas:null, son:"2026-11-19", ad:"AliExpress 11.11 Big Sale", ac:"19 Kasım 10.59’a kadar"},
  {bas:"2026-11-23", son:"2026-11-29", ad:"Efsane Hafta", ac:"Beklenti — kesin tarih değil", tahmin:true},
  {bas:"2026-11-27", son:"2026-11-27", ad:"Efsane Cuma (Black Friday)"},
  {bas:"2026-11-30", son:"2026-11-30", ad:"Siber Pazartesi"},
  {bas:null, son:"2026-11-30", ad:"Trendyol peşin fiyatına 3–6 taksit", ac:"1.000 TL üzeri 3 taksit; 6.000 TL üzeri seçili ürünlerde 6 taksit"},
  {bas:"2026-12-17", son:"2027-01-04", ad:"Steam Kış İndirimi", ac:"Kasım’da Steam’de mağaza geneli indirim yok"}
];

const S = {veri:null, sekme:"one", limit:60, f:{ara:"", magaza:"hepsi", min:0, ikinciEl:true, sirala:"indirim", kategori:"hepsi", sure:{one:72, ucuz:72, tum:72, kupon:72, takip:0}, sureSurum:2, kuponTur:"hepsi", gecmisGizle:true}, zamanlayici:null, ayarTaslak:null};
try {
  const k = JSON.parse(localStorage.getItem("kf-filtre") || "null"); if (k) { const sure = Object.assign({}, S.f.sure, k.sureSurum === 2 ? (k.sure || {}) : {}); k.sureSurum = 2; Object.assign(S.f, k); S.f.sure = sure; }
  const s = localStorage.getItem("kf-sekme"); if (s && SEKMELER.some(x => x[0] === s)) S.sekme = s;
} catch (e) {}
function sakla(){ try { localStorage.setItem("kf-filtre", JSON.stringify(S.f)); localStorage.setItem("kf-sekme", S.sekme); } catch (e) {} }

function toast(m){ const t = $("#toast"); t.textContent = m; t.classList.add("goster"); clearTimeout(t._z); t._z = setTimeout(() => t.classList.remove("goster"), 1800); }
function gunFarki(iso){ const b = new Date(); b.setHours(0,0,0,0); const h = new Date(iso + "T00:00:00"); return Math.round((h - b) / 86400000); }
function once(ts){
  const s = Math.max(0, Date.now()/1000 - ts);
  if (s < 90) return "az önce";
  if (s < 3600) return Math.round(s/60) + " dk önce";
  if (s < 86400) return Math.round(s/3600) + " sa önce";
  return Math.round(s/86400) + " gün önce";
}
function saat(iso){ if (!iso) return "—"; const d = new Date(iso); return d.toLocaleTimeString("tr-TR", {hour:"2-digit", minute:"2-digit"}); }
function kanalAd(k){ const x = (S.veri?.kaynaklar || []).find(z => z.kanal.toLowerCase() === String(k).toLowerCase()); return x ? x.ad : k; }

async function yukle(){
  try {
    const r = await fetch("/api/durum", {cache:"no-store"});
    S.veri = await r.json();
  } catch (e) {
    $("#durum").textContent = "Programa bağlanılamadı — Termux’ta çalışıyor mu?";
  }
  ciz(); planla();
}
function planla(){ clearTimeout(S.zamanlayici); S.zamanlayici = setTimeout(yukle, S.veri && S.veri.yenileniyor ? 3000 : 60000); }
async function yenileIste(){
  try { await fetch("/api/yenile", {method:"POST", headers:{"X-Kasim":"1"}}); } catch (e) {}
  if (S.veri) S.veri.yenileniyor = true;
  ust(); planla();
}

function ust(){
  const v = S.veri; const b = $("#yenileBtn");
  if (!v) return;
  $("#telefonBtn").hidden = !(v.ag && v.ag.acik && v.ag.bu_cihaz && v.ag.adres);
  b.classList.toggle("don", !!v.yenileniyor); b.querySelector("span").textContent = v.yenileniyor ? "Yenileniyor" : "Yenile";
  const hatali = v.kaynaklar.filter(k => k.ok === false).length;
  const urun = v.firsatlar.filter(x => x.tur === "urun").length;
  let m = v.son_guncelleme ? `Son güncelleme ${saat(v.son_guncelleme)} · ${urun} ürün` : (v.yenileniyor ? "İlk veriler çekiliyor…" : "Henüz veri yok");
  $("#durum").innerHTML = esc(m) + (hatali ? ` · <button class="hata" type="button" data-git="ayar">${hatali} kaynakta hata</button>` : "");
  const sy = SAYAC.map(([a, t]) => { const g = gunFarki(t); return g < 0 ? "" : `<span>${esc(a)} · <b>${g === 0 ? "bugün" : g + " gün"}</b></span>`; }).join("");
  $("#sayac").innerHTML = sy;
  $("#sayac").style.display = sy ? "" : "none";
}

const KATEGORI = [["elektronik","Elektronik"],["ev","Ev & mutfak"],["market","Market & gıda"],["kozmetik","Kozmetik & bakım"],["giyim","Giyim, ayakkabı & aksesuar"],["bebek","Anne, bebek & oyuncak"],["spor","Spor & outdoor"],["yapi","Yapı market & oto"],["diger","Diğer"]];
function kayitlilar(){ try { return JSON.parse(localStorage.getItem("kf-kayitli") || "{}") || {}; } catch (e) { return S.kayitliYedek || {}; } }
function kayitliYaz(k){ S.kayitliYedek = k; try { localStorage.setItem("kf-kayitli", JSON.stringify(k)); } catch (e) {} }

function sekmeVerisi(sekme){
  const d = S.veri ? S.veri.firsatlar.filter(x => !x.bitti) : [];
  if (sekme === "kac") return d.filter(x => x.kacirilmaz && !bittiMi(x));
  if (sekme === "one") return d.filter(x => x.tur === "urun" && !bittiMi(x) && !(dogrulanamadi(x) && Date.now() / 1000 - x.ts > 86400) && ((x.indirim || 0) >= 25 || x.rozetler.includes("dip") || x.rozetler.includes("kayit_dusuk")));
  if (sekme === "ucuz") return d.filter(x => x.tur === "urun" && x.fiyat_ozet && (x.fiyat_ozet.dusus_son >= 3 || x.fiyat_ozet.dusus_ort >= 5));
  if (sekme === "tum") return d.filter(x => x.tur === "urun");
  if (sekme === "kupon") return d.filter(x => x.tur === "kupon" || x.tur === "kampanya" || (x.kodlar && x.kodlar.length));
  if (sekme === "takip") return d.filter(x => x.takip && x.takip.length);
  return [];
}
const SAAT = 3600;
function suresiGecti(x){ return !!x.son_tarih && new Date(x.son_tarih).getTime() < Date.now(); }
function sonGunYazi(x){
  if (!x.son_tarih) return null;
  const d = new Date(x.son_tarih);
  if (d.getTime() < Date.now()) return ["Süresi geçti", "r-kirmizi"];
  const g = gunFarki(x.son_tarih.slice(0, 10));
  if (g <= 0) return ["Bugün bitiyor", "r-kirmizi"];
  if (g === 1) return ["Yarın bitiyor", "r-sari"];
  return ["Son gün: " + d.toLocaleDateString("tr-TR", {day:"numeric", month:"long"}), "r-mavi"];
}

function navCiz(){
  $("#nav").innerHTML = SEKMELER.map(([k, a]) => {
    let n = "";
    if (S.veri && ["kac","one","ucuz","tum","kupon"].includes(k)) n = `<span class="say">${sekmeVerisi(k).length}</span>`;
    if (S.veri && k === "takip") { const t = sekmeVerisi("takip").length + Object.keys(kayitlilar()).length; if (t) n = `<span class="say">${t}</span>`; }
    return `<button role="tab" type="button" data-sekme="${k}" aria-selected="${S.sekme === k}">${esc(a)}${n}</button>`;
  }).join("");
}

const sadeJS = s => String(s || "").toLocaleLowerCase("tr-TR").normalize("NFD").replace(/[\u0300-\u036f]/g, "").replace(/ı/g, "i");
const ESANLAM = {"kahve makinesi":["kahve makinesi","espresso makinesi","kahve makinasi"],"robot supurge":["robot supurge","robot vakum"],"bilgisayar":["bilgisayar","laptop","notebook","dizustu"],"saat":["saat","watch"],"ayakkabi":["ayakkabi","sneaker","bot"],"iphone":["iphone"],"airpods":["airpods"],"ps5":["ps5","playstation 5","playstation5"],"ps4":["ps4","playstation 4","playstation4"],"playstation":["playstation","ps5","ps4"],"airfryer":["airfryer","air fryer","fritoz"],"tv":["tv","televizyon"],"televizyon":["televizyon","tv"],"laptop":["laptop","dizustu","notebook"],"kulaklik":["kulaklik","buds","airpods","earbuds"],"telefon":["telefon","iphone","galaxy","redmi"],"supurge":["supurge"],"xbox":["xbox"],"nintendo":["nintendo","switch"]};
function aramaParcalari(q){
  // "playstation 5" gibi iki kelimelik yazımları tek parça say
  const t = q.split(/\s+/).filter(Boolean), out = [];
  for (let i = 0; i < t.length; i++) {
    const ikili = t[i] + " " + (t[i + 1] || "");
    if (t[i + 1] && ESANLAM[ikili]) { out.push(ikili); i++; } else out.push(t[i]);
  }
  return out;
}
const bitisik = s => s.replace(/[\s\-_.\/]+/g, "");
function aramaEslesir(x, parca){
  const m = sadeJS(x.baslik + " " + x.metin + " " + (x.kodlar || []).join(" ") + " " + x.magaza_ad), mb = bitisik(m);
  return parca.every(p => (ESANLAM[p] || [p]).some(a => m.includes(a) || (bitisik(a).length >= 3 && mb.includes(bitisik(a)))));
}
function aramaPuani(x, q, parca){
  const b = sadeJS(x.baslik);
  if (b.includes(q) || bitisik(b).includes(bitisik(q))) return 0;
  if (parca.every(p => (ESANLAM[p] || [p]).some(a => b.includes(a) || bitisik(b).includes(bitisik(a))))) return 1;
  return 2;
}
function aramaVar(){ return S.f.ara.trim().length > 0 && ["one","ucuz","tum","kupon"].includes(S.sekme); }
function filtreUygula(d){
  const f = S.f, q = sadeJS(f.ara.trim()), arama = aramaVar();
  // Arama yapılırken mağaza/kategori/oran filtreleri uygulanmaz: yazılan ürün her zaman çıksın
  if (!arama) {
    if (f.magaza !== "hepsi") d = d.filter(x => x.magaza === f.magaza);
    if (f.kategori !== "hepsi" && S.sekme !== "kupon") d = d.filter(x => x.kategori === f.kategori);
    if (f.min === -1) d = d.filter(x => x.indirim != null);
    else if (f.min > 0) d = d.filter(x => (x.indirim || 0) >= f.min);
    if (!f.ikinciEl) d = d.filter(x => !x.rozetler.includes("ikinci_el"));
  }
  if (q && arama) { const parca = aramaParcalari(q); d = d.filter(x => aramaEslesir(x, parca)); d.forEach(x => x._puan = aramaPuani(x, q, parca)); }
  const sure = arama ? 0 : ((f.sure || {})[S.sekme] || 0);
  if (sure > 0) { const sinir = Date.now() / 1000 - sure * SAAT; d = d.filter(x => x.ts >= sinir); }
  if (S.sekme === "kupon" && !arama) {
    if (f.kuponTur === "kod") d = d.filter(x => x.tur === "kupon");
    else if (f.kuponTur === "urun") d = d.filter(x => x.tur === "urun");
    else if (f.kuponTur === "kampanya") d = d.filter(x => x.tur === "kampanya");
    if (f.gecmisGizle) d = d.filter(x => !suresiGecti(x));
  }
  const dip = x => x.rozetler.includes("dip") || x.rozetler.includes("kayit_dusuk") ? 1 : 0;
  const dusus = x => x.fiyat_ozet ? Math.max(x.fiyat_ozet.dusus_son, x.fiyat_ozet.dusus_ort) : -1;
  if (S.sekme === "kac" && !arama) return d.slice().sort((a, b) => ((b.kacirilmaz || {}).guc || 0) - ((a.kacirilmaz || {}).guc || 0) || (b.indirim || 0) - (a.indirim || 0) || b.ts - a.ts);
  if (S.sekme === "ucuz" && f.sirala === "indirim" && !arama) return d.slice().sort((a, b) => (dusus(b) - dusus(a)) || (b.ts - a.ts));
  const s = {
    indirim: (a, b) => ((b.indirim ?? -1) - (a.indirim ?? -1)) || (dip(b) - dip(a)) || (b.ts - a.ts),
    yeni: (a, b) => b.ts - a.ts,
    populer: (a, b) => (b.goruntulenme || 0) - (a.goruntulenme || 0),
    ucuz: (a, b) => (a.indirimli_fiyat ?? a.fiyat ?? 1e12) - (b.indirimli_fiyat ?? b.fiyat ?? 1e12)
  }[f.sirala] || ((a, b) => b.ts - a.ts);
  const bitti = x => bittiMi(x) ? 2 : (dogrulanamadi(x) && Date.now() / 1000 - x.ts > 86400) ? 1 : 0;
  return d.slice().sort(arama ? ((a, b) => ((a._puan ?? 2) - (b._puan ?? 2)) || bitti(a) - bitti(b) || s(a, b)) : ((a, b) => bitti(a) - bitti(b) || s(a, b)));
}

function dogrulanamadi(x){
  const c = canliSonuc(x);
  if (x.tur === "urun" && x.oto_okunamaz && !(c && c.durum === "ok")) return true;
  return x.tur === "urun" && c && ["engellendi", "urun_degil", "bulunamadi", "hata"].includes(c.durum);
}
function sureMetni(sn){
  return sn >= 86400 ? Math.floor(sn / 86400) + " gün" : sn >= 3600 ? Math.round(sn / 3600) + " saat" : Math.max(1, Math.round(sn / 60)) + " dk";
}
// Mağaza fiyatı okunamayan ürünler (ör. Hepsiburada): güveni tazelik + kaç kanalın aynı fiyatı paylaştığı belirler
function guvenBlok(x){
  const yas = Date.now() / 1000 - x.ts, n = (x.teyit && x.teyit.kanal) || 1;
  const c = canliSonuc(x);
  const kim = x.oto_okunamaz ? (x.magaza_ad || "Bu mağaza") + " fiyatı otomatik okunamıyor"
    : c && c.durum === "engellendi" ? "Mağaza otomatik okumaya izin vermedi" : "Fiyat mağazada doğrulanamadı";
  const teyit = n >= 2 ? ` · <b>${n} kanal aynı fiyatı paylaştı</b>` : "";
  let sinif, ikon, sonuc;
  if (yas < 3 * 3600) { sinif = n >= 2 ? "c-yesil" : "c-sari"; ikon = n >= 2 ? "🟢" : "🟡"; sonuc = "Büyük ihtimalle geçerli."; }
  else if (yas < 86400) { sinif = "c-sari"; ikon = "⚠️"; sonuc = "Fiyat değişmiş olabilir."; }
  else { sinif = "c-kirmizi"; ikon = "❌"; sonuc = "Bu fiyat büyük ihtimalle artık yok."; }
  const alt = (x.magazalar || []).find(m => m.dogrulandi && m.magaza !== x.magaza);
  const altSatir = alt ? `<div class="fdurum c-yesil">✅ Aynı ürün, ${esc(alt.magaza_ad)} sayfasında doğrulanmış fiyat: <b>${tl(alt.dogrulandi)}</b>${link(alt.link) ? ` · <a href="${esc(link(alt.link))}" target="_blank" rel="noopener noreferrer">Git</a>` : ""}</div>` : "";
  const ak = link(x.akakce) ? ` <a href="${esc(x.akakce)}" target="_blank" rel="noopener noreferrer">Akakçe’de bak</a>` : "";
  return `<div class="fdurum ${sinif}">${ikon} ${esc(kim)} · ${sureMetni(yas)} önce paylaşıldı${teyit}. ${esc(sonuc)} Sepetteki tutara bakın.${ak}</div>${altSatir}`;
}
function bittiMi(x){ const c = canliSonuc(x); return !!c && ((c.durum === "ok" && (c.karar === "bitti" || c.karar === "sepet_yok")) || c.durum === "stok_yok"); }
function canliSonuc(x){ const c = (S.canli || {})[x.id] || x.canli; return c && !c.yukleniyor ? c : null; }
function fiyatBlok(x){
  const c = canliSonuc(x);
  if (x.fiyat != null && c && c.durum === "ok") {
    const saat = new Date(c.ts * 1000).toLocaleTimeString("tr-TR", {hour:"2-digit", minute:"2-digit"});
    const sat = [];
    sat.push(`<div class="fsatir indirimli"><span class="fet">Satış fiyatı <small>· mağazada şu an, ${saat}</small></span><span class="fdeger">${tl(c.fiyat)}</span></div>`);
    if (c.karar === "kampanya") sat.push(`<div class="fsatir kosullu"><span class="fet">İndirimli · ${esc(x.kosul || "kampanyayla")}</span><span class="fdeger">${tl(c.birim)}<small> / adet</small></span></div>`);
    if (c.karar === "sepette") sat.push(`<div class="fsatir kosullu"><span class="fet">İndirimli · ${esc(x.kosul || "sepette")}</span><span class="fdeger">${tl(x.fiyat)}${x.sayfa_tahmini || x.birim_net || (x.kosul || "").includes("adet") ? "<small> / adet</small>" : ""}</span></div>`);
    if (c.karar === "bitti" || c.karar === "sepet_yok") sat.push(`<div class="fsatir eski"><span class="fet">Paylaşılan fiyat</span><span class="fdeger"><s>${tl(x.fiyat)}</s></span></div>`);
    const etiket = {gecerli: ["c-yesil", "✅ Paylaşılan fiyat geçerli"], sepette: ["c-sari", "⚠️ Bu fiyat yalnızca koşulla (" + (x.kosul || "sepette") + ") oluşur"], bitti: ["c-kirmizi", "❌ " + (c.metin && c.metin.startsWith("Adet") ? c.metin : "Fırsat bitmiş, fiyat yükselmiş")],
      kampanya: ["c-yesil", "✅ " + (c.metin || "Kampanya hâlâ aktif")], sepet_yok: ["c-kirmizi", "❌ Sepet indirimi artık görünmüyor; büyük ihtimalle bitmiş"]}[c.karar];
    return `<div class="fiyatlar">${sat.join("")}${etiket ? `<div class="fdurum ${etiket[0]}">${esc(etiket[1])}</div>` : ""}</div>`;
  }
  return fiyatBlokEski(x);
}
function fiyatBlokEski(x){
  if (x.fiyat == null) return "";
  const satirlar = [];
  const c = canliSonuc(x);
  if (x.satis_fiyati != null) {
    const tahmini = /tahmin/i.test(x.satis_kaynak || "");
    satirlar.push(`<div class="fsatir satis"><span class="fet">Satış fiyatı</span><span class="fdeger"><s>${tahmini ? "≈ " : ""}${tl(x.satis_fiyati)}</s></span></div>`);
  } else if (x.kosul || x.oto_okunamaz || (c && c.durum !== "ok")) {
    const neden = (c && c.durum === "engellendi") || (x.oto_okunamaz && !c) ? "mağaza otomatik okumaya izin vermiyor" : c && c.durum === "stok_yok" ? "stokta yok" : c ? "okunamadı" : S.veri && S.veri.statik ? "kontrol edilmedi" : "kontrol ediliyor…";
    satirlar.push(`<div class="fsatir satis"><span class="fet">Satış fiyatı</span><span class="fdeger bilinmiyor">? <small>${esc(neden)}</small></span></div>`);
  }
  const etiket = x.kosul ? "İndirimli · " + x.kosul : x.satis_fiyati != null ? "İndirimli fiyat" : "Paylaşılan fiyat";
  const birim = x.sayfa_tahmini || (x.indirimli_not && /adet fiyatı/.test(x.indirimli_not)) ? `<small> / adet</small>` : "";
  satirlar.push(`<div class="fsatir indirimli"><span class="fet">${etiket}</span><span class="fdeger">${tl(x.indirimli_fiyat ?? x.fiyat)}${birim}</span></div>`);
  const fark = x.satis_fiyati != null && x.indirimli_fiyat != null ? x.satis_fiyati - x.indirimli_fiyat : 0;
  const alt = [];
  if (fark > 0.5) alt.push(`${tl(Math.round(fark * 100) / 100)} kazanç`);
  if (x.satis_kaynak) alt.push("Satış fiyatı: " + x.satis_kaynak.charAt(0).toLocaleLowerCase("tr-TR") + x.satis_kaynak.slice(1));
  const uyari = dogrulanamadi(x) ? guvenBlok(x)
    : (c && c.durum === "stok_yok") ? `<div class="fdurum c-kirmizi">❌ Ürün şu anda stokta yok</div>` : "";
  return `<div class="fiyatlar">${satirlar.join("")}${alt.length ? `<div class="fkaynak">${esc(alt.join(" · "))}</div>` : ""}${uyari}</div>`;
}

const ESKI_GUN = 7;   // bundan eski paylaşımların fiyatı "güncel değil" sayılır
const tarihKisa = ts => new Date(ts * 1000).toLocaleDateString("tr-TR", {day:"numeric", month:"short", year: (Date.now()/1000 - ts > 60*86400) ? "numeric" : undefined});
function grafik(x){
  const n = (x.fiyat_gecmisi || []).map(([ts, f]) => [ts, f]).concat([[x.ts, x.fiyat]]);
  if (n.length < 2) return "";
  const G = 260, Y = 56, P = 4;
  const t0 = n[0][0], t1 = n[n.length - 1][0] || t0 + 1;
  const fs = n.map(z => z[1]), mn = Math.min(...fs), mx = Math.max(...fs), ar = (mx - mn) || 1;
  const px = t => P + (t1 === t0 ? 0 : (t - t0) / (t1 - t0)) * (G - 2 * P);
  const py = f => P + (1 - (f - mn) / ar) * (Y - 2 * P);
  const yol = n.map(([t, f], i) => `${i ? "L" : "M"}${px(t).toFixed(1)} ${py(f).toFixed(1)}`).join(" ");
  const alan = yol + ` L${px(t1).toFixed(1)} ${Y - P} L${px(t0).toFixed(1)} ${Y - P} Z`;
  const son = n[n.length - 1];
  return `<svg class="grafik" viewBox="0 0 ${G} ${Y}" role="img" aria-label="Fiyat grafiği: ${esc(tl(mn))} ile ${esc(tl(mx))} arası">
    <path d="${alan}" class="g-alan"/><path d="${yol}" class="g-cizgi" fill="none"/>
    ${n.slice(0, -1).map(([t, f]) => `<circle cx="${px(t).toFixed(1)}" cy="${py(f).toFixed(1)}" r="2.2" class="g-nokta"/>`).join("")}
    <circle cx="${px(son[0]).toFixed(1)}" cy="${py(son[1]).toFixed(1)}" r="4" class="g-son"/></svg>
    <div class="g-eksen"><span>${tarihKisa(t0)}</span><span>en düşük ${tl(mn)} · en yüksek ${tl(mx)}</span><span>şimdi</span></div>`;
}
function ozetBlok(x){
  if (x.tur !== "urun" || x.fiyat == null) return "";
  const o = x.fiyat_ozet;
  const ak = link(x.akakce) ? `<a class="akakce" href="${esc(x.akakce)}" target="_blank" rel="noopener noreferrer">Akakçe’de 1 yıllık fiyat geçmişi</a>` : "";
  if (!o) {
    const dugme = x.arsiv_arandi ? `<span class="ipucu">Kanallarda bu ürünün eski paylaşımı bulunamadı.</span>`
      : `<button type="button" class="gecmis-btn" data-gecmis="${esc(x.id)}">Geçmiş fiyatları getir</button>`;
    return `<div class="ozet bos">${dugme}${ak}</div>`;
  }
  const yon = v => v > 0.5 ? `<b class="dus">%${nf.format(Math.abs(v))} ucuz</b>` : v < -0.5 ? `<b class="art">%${nf.format(Math.abs(v))} pahalı</b>` : `<b>aynı</b>`;
  const satirlar = [
    `<div class="o-satir"><span>Önceki paylaşım (${tarihKisa(o.son_ts)})</span><span>${tl(o.son)} → ${yon(o.dusus_son)}</span></div>`,
    `<div class="o-satir"><span>Önceki ortalama (${o.adet} kayıt)</span><span>${tl(o.ortalama)} → ${yon(o.dusus_ort)}</span></div>`,
    `<div class="o-satir"><span>Önceki en düşük (${tarihKisa(o.en_dusuk_ts)})</span><span>${tl(o.en_dusuk)} · ${o.en_dusuge_gore < -0.5 ? `<b class="dus">şimdi tüm kayıtlardan ucuz</b>` : o.en_dusuge_gore <= 0.5 ? `<b class="dus">en düşük seviyede</b>` : `şimdi %${nf.format(o.en_dusuge_gore)} daha pahalı`}</span></div>`
  ];
  return `<div class="ozet">${grafik(x)}${satirlar.join("")}<div class="o-alt"><span class="ipucu">Kaynak: kanalların ${tarihKisa(o.ilk_ts)} tarihinden beri paylaştığı net fiyatlar (sepet ve kod indirimi dahil olabilir)</span>${ak}</div></div>`;
}

function kart(x){
  const res = link(x.resim);
  const hedef = link(x.urun_link);
  const oranSinif = (x.indirim || 0) >= 30 ? "oran" : "oran orta";
  const cs = canliSonuc(x), canliOk = cs && cs.durum === "ok";
  const oran = x.indirim != null && !bittiMi(x) ? `<div class="${oranSinif}" title="${esc(x.indirim_kaynak)}">%${x.indirim}<small>${x.varan ? "’e varan" : "indirim"}</small></div>` : "";
  let fiyatNot = x.indirimli_not ? x.indirimli_not + "." : "";
  if (x.sepet_fiyati) fiyatNot += ` Ürün sayfasında daha yüksek görünür${(x.kodlar || []).length ? "; kodu sepette uygulayın" : ""}.`;
  const yas = Date.now() / 1000 - x.ts;
  const kaynakNot = [];
  if (x.indirim_kaynak) kaynakNot.push("İndirim: " + x.indirim_kaynak);
  const digerler = [...new Set((x.diger || []).map(z => kanalAd(z.kanal)).filter(a => a !== kanalAd(x.kanal)))];
  if (digerler.length) kaynakNot.push("Ayrıca: " + digerler.join(", "));
  const rozet = [];
  (x.takip || []).forEach(t => rozet.push(`<span class="rozet r-takip">Takip: ${esc(t)}</span>`));
  if (x.kacirilmaz && !bittiMi(x)) rozet.push(`<span class="rozet r-kac">🔥 Kaçırılmaz${x.kacirilmaz.dogrulandi ? "" : " · doğrulanamadı"}: ${esc(x.kacirilmaz.neden.join(", "))}</span>`);
  if (x.dusus_etiketi) rozet.push(`<span class="rozet r-dusus">📉 ${esc(x.dusus_etiketi)}</span>`);
  if ((x.magazalar || []).length >= 2) rozet.push(`<span class="rozet r-mavi">${x.magazalar.length} mağazada · en ucuz ${esc(x.magazalar[0].magaza_ad)}</span>`);
  if (x.daha_once_ucuz) rozet.push(`<span class="rozet r-kirmizi">Daha önce bu fiyata ya da daha ucuza görüldü</span>`);
  x.rozetler.filter(r => ROZET[r] && r !== "kupon" && !(r === "sepette" && fiyatNot)).forEach(r => rozet.push(`<span class="rozet ${ROZET[r][1]}">${ROZET[r][0]}</span>`));
  if (x.xaly && !String(x.indirim_kaynak || "").startsWith(x.xaly)) rozet.push(`<span class="rozet r-mavi">${esc(x.xaly)}</span>`);
  if (yas > 3 * 24 * SAAT) rozet.push(`<span class="rozet r-kirmizi">⚠️ ${tarihKisa(x.ts)} tarihli fiyat · bugün geçerli olmayabilir</span>`);
  else if (yas > 12 * SAAT) rozet.push(`<span class="rozet r-sari">${Math.round(yas / SAAT)} saat önce paylaşıldı, fiyat değişmiş olabilir</span>`);
  const sg = sonGunYazi(x); if (sg) rozet.push(`<span class="rozet ${sg[1]}">${sg[0]}</span>`);
  const kodlar = (x.kodlar || []).map(k => `<button type="button" class="kod" data-kod="${esc(k)}" title="Kopyala">${esc(k)} ⧉</button>`);
  const gor = x.goruntulenme ? ` · ${kf.format(x.goruntulenme)} görüntülenme` : "";
  const kayitli = !!kayitlilar()[x.id];
  return `<article class="kart${bittiMi(x) ? " k-bitti" : ""}" ${x.tur === "urun" && link(x.urun_link) && !canliSonuc(x) ? `data-oto="${esc(x.id)}"` : ""}>
    ${res ? `<img class="resim" data-detay="${esc(x.id)}" src="${esc(res)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="var d=document.createElement('div');d.className='resim bos';this.replaceWith(d)">` : `<div class="resim bos">${esc((x.magaza_ad || "").slice(0, 10))}</div>`}
    <div class="govde">
      <div class="kbas"><div class="kbaslik" ${x.tur === "urun" ? `data-detay="${esc(x.id)}" role="button" tabindex="0"` : ""}>${esc(x.baslik)}</div>${oran}</div>
      ${fiyatBlok(x)}
      ${fiyatNot && !canliOk && !x.kosul ? `<div class="fiyatnot">${esc(fiyatNot.trim())}</div>` : ""}
      ${x.tur === "urun" ? canliBlok(x) : ""}
      <div class="meta">${esc(x.magaza_ad)} · ${esc(x.kategori_ad || "")} · ${esc(kanalAd(x.kanal))} · ${once(x.ts)}${gor}</div>
      ${kaynakNot.length ? `<div class="kaynak">${esc(kaynakNot.join(" · "))}</div>` : ""}
      ${(rozet.length || kodlar.length) ? `<div class="rozetler">${rozet.join("")}${kodlar.join("")}</div>` : ""}
      <div class="eylem">
        ${hedef ? `<a class="ana" href="${esc(hedef)}" target="_blank" rel="noopener noreferrer">Fırsata git</a>` : ""}
        <button type="button" class="ikincil" data-kaydet="${esc(x.id)}" aria-pressed="${kayitli}">${kayitli ? "★ Kaydedildi" : "☆ Kaydet"}</button>
        <button type="button" class="ikincil" data-paylas="${esc(x.id)}">Paylaş</button>
        ${x.fiyat != null && !(S.veri && S.veri.onizleme && false) ? `<button type="button" class="ikincil" data-hedef="${esc(x.id)}" aria-pressed="${!!x.alarm_kurulu}">${x.alarm_kurulu ? "🎯 Hedef: " + tl(x.alarm_kurulu.hedef) : "🎯 Hedef fiyat"}</button>` : ""}
        ${x.tur === "urun" && x.fiyat != null ? `<button type="button" class="ikincil" data-detay="${esc(x.id)}">📈 Fiyat geçmişi</button>` : ""}
        <a class="kaynak-link" href="${esc(x.link)}" target="_blank" rel="noopener noreferrer">Kaynak</a>
      </div>
      ${S.hedefAcik === x.id && !S.detay ? hedefForm(x) : ""}
      ${ozetBlok(x)}
      <details class="metin"><summary>Paylaşım metni</summary><div>${esc(x.metin)}</div></details>
    </div>
  </article>`;
}

function buyukGrafik(x){
  const n = (x.fiyat_gecmisi || []).map(([ts, f]) => [ts, f]).concat([[x.ts, x.fiyat]]);
  if (n.length < 2) return "";
  const G = 400, Y = 210, SOL = 66, SAG = 10, UST = 14, ALT = 28;
  const t0 = n[0][0], t1 = Math.max(n[n.length - 1][0], t0 + 1);
  const fs = n.map(z => z[1]); let mn = Math.min(...fs), mx = Math.max(...fs);
  const pay = (mx - mn) * 0.12 || mx * 0.05 || 1; mn = Math.max(0, mn - pay); mx = mx + pay;
  const px = t => SOL + (t - t0) / (t1 - t0) * (G - SOL - SAG);
  const py = f => UST + (1 - (f - mn) / (mx - mn)) * (Y - UST - ALT);
  const kisa = v => v >= 10000 ? nf.format(Math.round(v / 100) * 100) : nf.format(Math.round(v));
  const izgara = [0, 0.5, 1].map(k => { const v = mn + (mx - mn) * k, y = py(v).toFixed(1);
    return `<line x1="${SOL}" x2="${G - SAG}" y1="${y}" y2="${y}" class="bg-izgara"/><text x="${SOL - 8}" y="${(+y + 4).toFixed(1)}" text-anchor="end">${kisa(v)} TL</text>`; }).join("");
  const ort = x.fiyat_ozet ? x.fiyat_ozet.ortalama : null;
  const ortCizgi = ort ? `<line x1="${SOL}" x2="${G - SAG}" y1="${py(ort).toFixed(1)}" y2="${py(ort).toFixed(1)}" class="bg-ort"/><text x="${G - SAG}" y="${(py(ort) - 5).toFixed(1)}" text-anchor="end">ortalama</text>` : "";
  // Basamaklı çizgi: fiyat bir sonraki paylaşıma kadar geçerli kabul edilir
  let yol = ""; n.forEach(([t, f], i) => { yol += i ? ` H${px(t).toFixed(1)} V${py(f).toFixed(1)}` : `M${px(t).toFixed(1)} ${py(f).toFixed(1)}`; });
  const alan = yol + ` V${Y - ALT} H${px(t0).toFixed(1)} Z`;
  const noktalar = n.map(([t, f], i) => `<circle cx="${px(t).toFixed(1)}" cy="${py(f).toFixed(1)}" r="${i === n.length - 1 ? 5 : 3}" class="${i === n.length - 1 ? "g-son" : "g-nokta"}"><title>${tarihKisa(t)}: ${tl(f)}</title></circle>`).join("");
  const orta = t0 + (t1 - t0) / 2;
  return `<svg class="bgrafik" viewBox="0 0 ${G} ${Y}" role="img" aria-label="Fiyat geçmişi grafiği">
    ${izgara}${ortCizgi}<path d="${alan}" class="g-alan"/><path d="${yol}" class="g-cizgi" fill="none"/>${noktalar}
    <text x="${SOL}" y="${Y - 9}">${tarihKisa(t0)}</text><text x="${px(orta).toFixed(1)}" y="${Y - 9}" text-anchor="middle">${tarihKisa(orta)}</text><text x="${G - SAG}" y="${Y - 9}" text-anchor="end">şimdi</text></svg>`;
}
function detayHTML(x){
  const o = x.fiyat_ozet, res = link(x.resim), hedef = link(x.urun_link);
  const n = (x.fiyat_gecmisi || []).map(([ts, f]) => ({ts, f})).concat([{ts: x.ts, f: x.fiyat, simdi: true}]);
  const istat = o ? `<div class="d-istat">
      <div><small>En düşük</small><b>${tl(Math.min(o.en_dusuk, x.fiyat))}</b><em>${x.fiyat <= o.en_dusuk ? "şimdi" : tarihKisa(o.en_dusuk_ts)}</em></div>
      <div><small>Ortalama</small><b>${tl(o.ortalama)}</b><em>${o.adet} önceki kayıt</em></div>
      <div><small>En yüksek</small><b>${tl(Math.max(o.en_yuksek, x.fiyat))}</b><em>${x.fiyat >= o.en_yuksek ? "şimdi" : tarihKisa(o.en_yuksek_ts)}</em></div>
      <div><small>Şimdi</small><b>${tl(x.fiyat)}</b><em>${o.dusus_ort > 0.5 ? `ortalamadan %${nf.format(o.dusus_ort)} ucuz` : o.dusus_ort < -0.5 ? `ortalamadan %${nf.format(-o.dusus_ort)} pahalı` : "ortalamada"}</em></div>
    </div>` : "";
  const gecmisBolum = o ? `<section class="d-bolum"><h2>Fiyat geçmişi</h2>${buyukGrafik(x)}${istat}
      <div class="ipucu" style="margin-top:8px">Kaynak: fırsat kanallarının ${tarihKisa(o.ilk_ts)} tarihinden beri bu ürün için paylaştığı fiyatlar. Mağazanın her günkü fiyatı değil; sepet ve kod indirimi dahil olabilir.</div></section>`
    : `<section class="d-bolum"><h2>Fiyat geçmişi</h2><div class="ozet bos">${x.arsiv_arandi ? `<span class="ipucu">Kanalların arşivinde bu ürünün eski bir paylaşımı bulunamadı. Uzun dönem geçmiş için Akakçe’ye bakın.</span>` : `<button type="button" class="gecmis-btn" data-gecmis="${esc(x.id)}">Kanal arşivinden geçmiş fiyatları getir</button>`}</div></section>`;
  const mg = x.magazalar || [];
  const magazaBolum = mg.length >= 2 ? `<section class="d-bolum"><h2>Mağazalara göre fiyatlar</h2>
      <table class="d-tablo"><thead><tr><th>Mağaza</th><th>Paylaşıldı</th><th class="sag">Fiyat</th><th></th></tr></thead><tbody>
      ${mg.map((m, i) => `<tr class="${i === 0 ? "enucuz" : ""}"><td>${esc(m.magaza_ad)}${i === 0 ? " · en ucuz" : ""}</td><td>${once(m.ts)}</td><td class="sag">${tl(m.fiyat)}${m.sepette ? "<br><small>sepette</small>" : ""}</td><td class="sag">${link(m.link) ? `<a href="${esc(m.link)}" target="_blank" rel="noopener noreferrer">Git</a>` : ""}</td></tr>`).join("")}
      </tbody></table><div class="ipucu" style="margin-top:6px">Son 14 günde aynı model kodunun farklı mağazalarda paylaşılan en son fiyatları.</div></section>` : "";
  const satirlar = n.slice().reverse().map((z, i, a) => { const onceki = a[i + 1];
    const deg = onceki ? (z.f - onceki.f) / onceki.f * 100 : null;
    return `<tr><td>${new Date(z.ts * 1000).toLocaleString("tr-TR", {day:"numeric", month:"short", year:"numeric", hour:"2-digit", minute:"2-digit"})}${z.simdi ? " · bu paylaşım" : ""}</td><td class="sag">${tl(z.f)}</td><td class="sag">${deg == null || Math.abs(deg) < 0.5 ? "–" : `<span class="${deg < 0 ? "dus" : "art"}">${deg < 0 ? "▼" : "▲"} %${nf.format(Math.abs(Math.round(deg * 10) / 10))}</span>`}</td></tr>`; }).join("");
  const tabloBolum = n.length >= 2 ? `<section class="d-bolum"><h2>Tüm fiyat kayıtları</h2><table class="d-tablo"><thead><tr><th>Tarih</th><th class="sag">Fiyat</th><th class="sag">Değişim</th></tr></thead><tbody>${satirlar}</tbody></table></section>` : "";
  const kayitli = !!kayitlilar()[x.id];
  return `<div class="d-ust"><button type="button" data-detaykapat="1">← Geri</button><span>${esc(x.magaza_ad)} · ${esc(x.kategori_ad || "")}</span></div>
    <div class="d-ic">
      <div class="d-bas">${res ? `<img src="${esc(res)}" alt="" referrerpolicy="no-referrer" onerror="this.remove()">` : ""}<div><h1>${esc(x.baslik)}</h1><div class="meta">${esc(kanalAd(x.kanal))} · ${once(x.ts)}${x.dusus_etiketi ? ` · <b class="dus">📉 ${esc(x.dusus_etiketi)}</b>` : ""}</div></div></div>
      ${Date.now() / 1000 - x.ts > 3 * 86400 ? `<div class="not" style="margin-top:10px">⚠️ Bu fiyat ${tarihKisa(x.ts)} tarihli bir paylaşımdan. Bugünkü fiyat farklıdır; güncel fiyat için “Fırsata git” ya da Akakçe’ye bakın.</div>` : ""}
      ${fiyatBlok(x)}
      ${canliBlok(x)}
      <div class="d-dugmeler">
        ${hedef ? `<a class="ana" href="${esc(hedef)}" target="_blank" rel="noopener noreferrer">Fırsata git</a>` : ""}
        ${link(x.akakce) ? `<a href="${esc(x.akakce)}" target="_blank" rel="noopener noreferrer">Akakçe’de karşılaştır</a>` : ""}
        ${link(x.cimri) ? `<a href="${esc(x.cimri)}" target="_blank" rel="noopener noreferrer">Cimri’de karşılaştır</a>` : ""}
        <button type="button" data-hedef="${esc(x.id)}">${x.alarm_kurulu ? "🎯 Hedef: " + tl(x.alarm_kurulu.hedef) : "🎯 Hedef fiyat"}</button>
        <button type="button" data-kaydet="${esc(x.id)}" aria-pressed="${kayitli}">${kayitli ? "★ Kaydedildi" : "☆ Kaydet"}</button>
      </div>
      ${S.hedefAcik === x.id ? hedefForm(x) : ""}
      ${magazaBolum}${gecmisBolum}${tabloBolum}
      <details class="metin d-bolum"><summary>Paylaşım metni</summary><div>${esc(x.metin)}</div></details>
    </div>`;
}
function canliBlok(x){
  const c = (S.canli || {})[x.id] || x.canli;
  if (S.veri && S.veri.statik && !(c && c.durum === "ok")) return "";
  if (c && c.durum === "ok" && !S.detay) return `<div class="canli"><button type="button" class="cip" data-canli="${esc(x.id)}" data-yeniden="1" style="padding:3px 9px;font-size:12px">↻ Fiyatı yeniden kontrol et</button></div>`;
  const zaman = c && c.ts ? new Date(c.ts * 1000).toLocaleTimeString("tr-TR", {hour:"2-digit", minute:"2-digit"}) : "";
  if (!link(x.urun_link)) return "";
  if (!c) return `<div class="canli"><button type="button" class="cip" data-canli="${esc(x.id)}">🔄 Mağazadaki fiyatı kontrol et</button></div>`;
  if (c.yukleniyor) return `<div class="canli"><span class="ipucu">Mağaza sayfası kontrol ediliyor…</span></div>`;
  if (c.durum !== "ok" && !S.detay) return `<div class="canli"><a class="cip" style="text-decoration:none" href="${esc(link(x.urun_link))}" target="_blank" rel="noopener noreferrer">Fiyatı mağazada gör</a></div>`;
  if (c.durum !== "ok") return `<div class="canli c-sari"><b>Anlık fiyat alınamadı.</b> ${esc(c.mesaj || "")} <a href="${esc(link(x.urun_link))}" target="_blank" rel="noopener noreferrer">Mağazada aç</a>
      <button type="button" class="cip" data-canli="${esc(x.id)}" data-yeniden="1" style="flex:none">Tekrar dene</button></div>`;
  const ikon = {yesil: "✅", sari: "⚠️", kirmizi: "❌"}[c.renk] || "ℹ️";
  return `<div class="canli c-${c.renk || "gri"}">
      <div class="c-ust"><span>Mağazada şu an</span><b>${tl(c.fiyat)}</b></div>
      <div class="c-alt">${zaman} itibarıyla · ${esc(c.yontem || "")}${c.stok_yok ? " · <b>stokta yok görünüyor</b>" : ""}</div>
      ${c.metin ? `<div class="c-karar">${ikon} ${esc(c.metin)}</div>` : ""}
      ${c.gercek ? `<div class="c-karar">📊 ${esc(c.gercek)}</div>` : ""}
      <div class="c-alt">Ödemeden önce sepetteki son tutarı kontrol edin; beden/renk seçimi fiyatı değiştirebilir. <button type="button" class="cip" data-canli="${esc(x.id)}" data-yeniden="1" style="flex:none;padding:3px 9px">Yenile</button></div>
    </div>`;
}
const CANLI_SIRA = [], CANLI_GOREN = new Set(); let canliCalisan = 0;
function canliSirayaEkle(id){ if (CANLI_GOREN.has(id)) return; CANLI_GOREN.add(id); CANLI_SIRA.push(id); canliIsle(); }
async function canliIsle(){
  while (canliCalisan < 2 && CANLI_SIRA.length) {
    const id = CANLI_SIRA.shift(); const x = S.veri && S.veri.firsatlar.find(z => z.id === id);
    if (!x || (x.canli && Date.now() / 1000 - x.canli.ts < 1800)) continue;
    canliCalisan++; canliKontrol(id, false, true).finally(() => { canliCalisan--; canliIsle(); });
  }
}
const canliGozcu = "IntersectionObserver" in window ? new IntersectionObserver(es => es.forEach(e => {
  if (e.isIntersecting) { canliSirayaEkle(e.target.dataset.oto); canliGozcu.unobserve(e.target); } }), {rootMargin: "200px"}) : null;
function canliGozle(){
  if (!canliGozcu || (S.veri && (S.veri.onizleme || S.veri.statik))) return;
  const el = [...document.querySelectorAll("main [data-oto]")];
  el.slice(0, 6).forEach(e => canliSirayaEkle(e.dataset.oto));   // listenin ilk ürünleri hemen
  el.forEach(e => canliGozcu.observe(e));                          // diğerleri ekrana geldikçe
}
async function canliKontrol(id, yeniden, sessiz){
  S.canli = S.canli || {};
  if (S.canli[id] && !yeniden && !S.canli[id].yukleniyor) return;
  if (!sessiz) { S.canli[id] = {yukleniyor: true}; detayCiz(); ciz(); }
  try {
    const r = await fetch("/api/canli", {method:"POST", headers:{"Content-Type":"application/json","X-Kasim":"1"}, body: JSON.stringify({id, zorla: !!yeniden})});
    const j = await r.json(); S.canli[id] = r.ok ? j : {durum: "hata", mesaj: j.hata || "Kontrol edilemedi"};
  } catch (e) { S.canli[id] = {durum: "hata", mesaj: "Programa ulaşılamadı"}; }
  if (sessiz) { clearTimeout(S.canliCiz); S.canliCiz = setTimeout(() => { if (!(document.activeElement && document.activeElement.id === "ara")) ciz(); else detayCiz(); }, 700); }
  else { detayCiz(); ciz(); }
}
function detayCiz(){
  let el = document.getElementById("detay");
  if (!el) { el = document.createElement("div"); el.id = "detay"; el.hidden = true; el.setAttribute("role", "dialog"); el.setAttribute("aria-modal", "true"); document.body.appendChild(el); }
  const x = S.detay && S.veri ? S.veri.firsatlar.find(z => z.id === S.detay) : null;
  if (!x) { el.hidden = true; el.innerHTML = ""; document.body.style.overflow = ""; return; }
  if (document.activeElement && el.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return;
  const ilk = el.hidden;
  el.innerHTML = detayHTML(x); el.hidden = false; document.body.style.overflow = "hidden";
  if (ilk) { el.scrollTop = 0; const b = el.querySelector("[data-detaykapat]"); if (b) b.focus({preventScroll: true}); }
}
function detayAc(id){ S.detay = id; S.hedefAcik = null; try { history.pushState({detay: id}, ""); } catch (e) {} detayCiz();
  const x = S.veri && S.veri.firsatlar.find(z => z.id === id);
  if (x && x.tur === "urun" && link(x.urun_link) && !(S.veri && (S.veri.onizleme || S.veri.statik))) canliKontrol(id); }
function detayKapat(){ if (!S.detay) return; if (history.state && history.state.detay) history.back(); else { S.detay = null; detayCiz(); } }
window.addEventListener("popstate", () => { if (S.detay) { S.detay = null; S.hedefAcik = null; detayCiz(); ciz(); } });
document.addEventListener("click", e => {
  const d = e.target.closest("[data-detay]"); if (d) { e.preventDefault(); detayAc(d.dataset.detay); return; }
  if (e.target.closest("[data-detaykapat]")) detayKapat();
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && S.detay) detayKapat();
  if ((e.key === "Enter" || e.key === " ") && e.target.matches && e.target.matches(".kbaslik[data-detay]")) { e.preventDefault(); detayAc(e.target.dataset.detay); }
});

function hedefForm(x){
  const oneri = x.alarm_kurulu ? x.alarm_kurulu.hedef : Math.floor(x.fiyat * 0.9);
  return `<div class="hedef-form">
    <label for="hedefTL-${esc(x.id)}" style="margin:0">Fiyat şuna inince haber ver:</label>
    <input type="number" inputmode="decimal" min="1" step="1" id="hedefTL-${esc(x.id)}" value="${oneri}">
    <button type="button" class="cip" data-hedefkur="${esc(x.id)}" style="flex:none">${x.alarm_kurulu ? "Güncelle" : "Alarm kur"}</button>
    ${x.alarm_kurulu ? `<button type="button" class="cip" data-alarmsil="${esc(x.alarm_kurulu.id)}" style="flex:none">Alarmı kaldır</button>` : ""}
    <div class="ipucu">Kanallar bu ürünü (aynı mağaza kodu ya da model) bu fiyata veya altına paylaşınca ${S.veri && S.veri.bildirim_var ? "telefona bildirim gelir" : "kartta işaretlenir; telefona bildirim için Ayarlar → Telefona bildirim"}. Şu anki fiyat ${tl(x.fiyat)}.</div>
  </div>`;
}

function kuponKart(x){
  const hedef = link(x.urun_link);
  const kodlar = (x.kodlar || []).map(k => `<button type="button" class="kod buyuk" data-kod="${esc(k)}" title="Kopyala">${esc(k)} <span aria-hidden="true">⧉</span></button>`).join("");
  let bas = x.kupon_tutar ? `${tl(x.kupon_tutar)} indirim` : (x.indirim ? `%${x.indirim} indirim` : esc(x.baslik));
  const alt = [];
  if (x.kupon_limit) alt.push(`${tl(x.kupon_limit)} ve üzeri alışverişte`);
  if (x.kupon_tutar && x.kupon_limit) alt.push(`sepete oranı %${Math.round(x.kupon_tutar / x.kupon_limit * 100)}`);
  const rozet = [];
  (x.takip || []).forEach(t => rozet.push(`<span class="rozet r-takip">Takip: ${esc(t)}</span>`));
  const sg = sonGunYazi(x); if (sg) rozet.push(`<span class="rozet ${sg[1]}">${sg[0]}</span>`);
  if (x.uygulama) rozet.push(`<span class="rozet r-mavi">Yalnızca mobil uygulamada</span>`);
  if (x.ilk_siparis) rozet.push(`<span class="rozet r-sari">İlk siparişe özel</span>`);
  const kanallar = [...new Set([kanalAd(x.kanal)].concat((x.diger || []).map(z => kanalAd(z.kanal))))];
  if (kanallar.length > 1) rozet.push(`<span class="rozet r-yesil">${kanallar.length} kanalda paylaşıldı</span>`);
  const kayitli = !!kayitlilar()[x.id];
  return `<article class="kart kupon">
    <div class="kupon-ust"><span class="magaza">${esc(x.magaza_ad)}</span><span class="meta">${esc(kanalAd(x.kanal))} · ${once(x.ts)}</span></div>
    <div class="kupon-bas">${bas}</div>
    ${alt.length ? `<div class="meta">${esc(alt.join(" · "))}</div>` : ""}
    <div class="kodlar">${kodlar}</div>
    ${rozet.length ? `<div class="rozetler">${rozet.join("")}</div>` : ""}
    <div class="eylem">
      ${hedef ? `<a class="ana" href="${esc(hedef)}" target="_blank" rel="noopener noreferrer">Mağazaya git</a>` : ""}
      <button type="button" class="ikincil" data-kaydet="${esc(x.id)}" aria-pressed="${kayitli}">${kayitli ? "★ Kaydedildi" : "☆ Kaydet"}</button>
      <button type="button" class="ikincil" data-paylas="${esc(x.id)}">Paylaş</button>
      <a class="kaynak-link" href="${esc(x.link)}" target="_blank" rel="noopener noreferrer">Kaynak</a>
    </div>
    <details class="metin"><summary>Paylaşım metni ve koşullar</summary><div>${esc(x.metin)}</div></details>
  </article>`;
}

function kartSec(x){ return x.tur === "kupon" ? kuponKart(x) : kart(x); }

function takipCiz(){
  const v = S.veri; if (!v) return;
  const kelimeler = v.ayarlar.takip || [];
  const eslesen = filtreUygula(sekmeVerisi("takip"));
  const kayitli = Object.values(kayitlilar()).sort((a, b) => b.ts - a.ts);
  const guncel = id => v.firsatlar.find(x => x.id === id);
  const cipler = kelimeler.map((k, i) => `<span class="cip takipcip">${esc(k)}<button type="button" data-takipsil="${i}" aria-label="${esc(k)} kelimesini kaldır">×</button></span>`).join("");
  const alarmlar = (v.ayarlar.alarmlar || []).slice().sort((a, b) => b.ts - a.ts);
  const alarmSatir = a => {
    const es = v.firsatlar.filter(x => x.alarm_kurulu && x.alarm_kurulu.id === a.id && !x.bitti);
    const enUcuz = es.reduce((m, x) => (m && m.fiyat <= x.fiyat) ? m : x, null);
    const tuttu = enUcuz && enUcuz.fiyat <= a.hedef;
    const durum = enUcuz ? `Şu an en ucuz ${tl(enUcuz.fiyat)} · ${esc(kanalAd(enUcuz.kanal))} · ${once(enUcuz.ts)}` : "Son paylaşımlarda yok; yeni paylaşım bekleniyor";
    return `<div class="alarm ${tuttu ? "tuttu" : ""}"><div class="ad"><b>${tuttu ? "🎯 " : ""}${esc(a.baslik)}</b><div>Hedef ${tl(a.hedef)} · alarm kurulurken ${tl(a.ilk_fiyat)} · ${durum}</div></div>
      ${enUcuz && link(enUcuz.urun_link) ? `<a class="cip" style="flex:none;text-decoration:none" href="${esc(link(enUcuz.urun_link))}" target="_blank" rel="noopener noreferrer">Git</a>` : ""}
      <button type="button" data-alarmsil="${esc(a.id)}">Kaldır</button></div>`;
  };
  $("#icerik").innerHTML = `
    <h2 class="bolum">Hedef fiyat alarmları <span class="say">${alarmlar.length}</span></h2>
    ${alarmlar.length ? alarmlar.map(alarmSatir).join("") + `<div class="ipucu" style="margin-top:6px">${v.bildirim_var ? "Hedefe inen yeni paylaşımda telefona bildirim gelir." : "Telefona bildirim almak için Ayarlar → Telefona bildirim."}</div>` : `<div class="bos-durum">Bir ürün kartındaki “🎯 Hedef fiyat”a dokunup istediğin fiyatı yaz; ürün o fiyata inince haber verelim.</div>`}
    <div class="filtre" style="margin-top:14px">
      <div class="satir"><input type="text" id="takipKelime" placeholder="Takip et: ör. airfryer, ps5, kahve makinesi" autocomplete="off" maxlength="40"><button type="button" class="cip" id="takipEkle" style="flex:none">Ekle</button></div>
      ${cipler ? `<div class="cipler sar">${cipler}</div>` : ""}
      <div class="ipucu">${v.bildirim_var ? "Bu kelimelere uyan yeni bir fırsat çıkınca indirim oranına bakılmadan bildirim gelir." : "Bu kelimelere uyan fırsatlar burada toplanır. Telefona bildirim için Ayarlar → Telefona bildirim."}</div>
    </div>
    <h2 class="bolum">Kelimelerine uyan fırsatlar <span class="say">${eslesen.length}</span></h2>
    ${kelimeler.length ? (eslesen.length ? eslesen.slice(0, S.limit).map(kartSec).join("") : `<div class="bos-durum">Şu an kelimelerine uyan fırsat yok. Yeni paylaşımlar her yenilemede kontrol edilir.</div>`) : `<div class="bos-durum">Henüz kelime eklemedin. Yukarıdan bir ürün adı ekle.</div>`}
    <h2 class="bolum">Kaydettiklerin <span class="say">${kayitli.length}</span></h2>
    ${kayitli.length ? kayitli.map(k => { const g = guncel(k.id); return g ? kartSec(g) : kartSec(Object.assign({}, k, {rozetler: (k.rozetler || []), _eski: true})).replace('<article class="kart', '<article class="kart soluk'); }).join("") : `<div class="bos-durum">Kartlardaki “☆ Kaydet” ile fırsatları buraya ekleyebilirsin.</div>`}`;
}

async function takipKaydet(liste){
  try {
    const r = await fetch("/api/ayarlar", {method:"POST", headers:{"Content-Type":"application/json","X-Kasim":"1"}, body: JSON.stringify({takip: liste})});
    const j = await r.json();
    if (!r.ok) throw new Error(j.hata || "Kaydedilemedi");
    S.veri.ayarlar = j.ayarlar; await yukle();
  } catch (e) { toast(e.message); }
}

function listeCiz(){
  const arama = aramaVar();
  const ham = arama ? S.veri.firsatlar.filter(x => !x.bitti) : sekmeVerisi(S.sekme);
  const magazalar = {}, kategoriler = {};
  ham.forEach(x => {
    magazalar[x.magaza] = magazalar[x.magaza] || {ad: x.magaza_ad, n: 0}; magazalar[x.magaza].n++;
    if (x.tur === "urun") kategoriler[x.kategori] = (kategoriler[x.kategori] || 0) + 1;
  });
  if (S.f.magaza !== "hepsi" && !magazalar[S.f.magaza]) S.f.magaza = "hepsi";
  if (S.f.kategori !== "hepsi" && !kategoriler[S.f.kategori]) S.f.kategori = "hepsi";
  const d = filtreUygula(ham);
  const cipler = [`<button type="button" class="cip" data-magaza="hepsi" aria-pressed="${S.f.magaza === "hepsi"}">Tüm mağazalar</button>`]
    .concat(Object.entries(magazalar).sort((a, b) => b[1].n - a[1].n).map(([k, v]) =>
      `<button type="button" class="cip" data-magaza="${esc(k)}" aria-pressed="${S.f.magaza === k}">${esc(v.ad)} ${v.n}</button>`)).join("");
  const katCipler = S.sekme === "kupon" ? "" : [`<button type="button" class="cip kat" data-kategori="hepsi" aria-pressed="${S.f.kategori === "hepsi"}">Tüm kategoriler</button>`]
    .concat(KATEGORI.filter(([k]) => kategoriler[k]).map(([k, a]) => `<button type="button" class="cip kat" data-kategori="${k}" aria-pressed="${S.f.kategori === k}">${esc(a)} ${kategoriler[k]}</button>`)).join("");
  const bitenler = S.veri ? S.veri.firsatlar.filter(x => x.bitti).length : 0;
  const not = S.sekme === "kac"
    ? `<div class="not">Burada yalnızca <b>fiyatı mağazada az önce doğrulanmış</b> ve gerçekten ucuz olan ürünler var: kanal “son ayların en düşüğü” diyor, programın kayıtlarındaki en düşük fiyatta, ortalamanın %15+ altında ya da %50+ indirimli (daha düşük indirimde en az iki işaret gerekir). Mağazası otomatik kontrole izin vermeyen (Hepsiburada gibi) ürünler yalnızca ilk 3 saat ve çok güçlü işaretle “doğrulanamadı” etiketiyle girer. Yeni bir ürün bu listeye girince telefona bildirim gelir.</div>`
    : S.sekme === "ucuz"
    ? `<div class="not">Ürünün şimdiki paylaşım fiyatı, kanalların aynı ürün için daha önce paylaştığı fiyatlarla karşılaştırılır (son 1 yıl). Program her yenilemede birkaç ürünün geçmişini kanal arşivinden tamamlar; bir ürünün geçmişini hemen görmek için kartındaki “Geçmiş fiyatları getir”e dokunun. Karşılaştırılan fiyatlar kanalların paylaştığı net fiyatlardır; 2. el ürünler sıfır ürünle karşılaştırılmaz.</div>`
    : S.sekme === "one"
    ? `<div class="not">%25 ve üstü indirimler ile dip fiyat işaretli ürünler. Satış fiyatı paylaşımdan ya da programın fiyat kaydından gelir; “≈” işaretli olanlar tahmindir. Almadan önce ürün sayfasında kontrol edin.${bitenler ? ` Kanalın “bitti” diye güncellediği ${bitenler} fırsat gizlendi.` : ""}</div>`
    : S.sekme === "kupon" ? `<div class="not">Kodlar kanalların paylaştığı haliyle gösterilir; koda dokununca kopyalanır. Program bir kodun hâlâ geçerli olduğunu doğrulayamaz: sepette kabul edilmezse süresi dolmuş, stok bitmiş ya da kategori/limit koşulu tutmuyor olabilir. “Paylaşım metni ve koşullar”daki hariç tutulan ürünlere bakın.</div>`
    : `<div class="not">Fiyatlar kanalın paylaştığı andaki fiyattır. Turuncu notlu ürünlerde fiyat sepette ya da birden fazla adet alınca oluşur; ürün sayfasında daha yüksek görünür.${bitenler ? ` Kanalın “bitti” diye güncellediği ${bitenler} fırsat gizlendi.` : ""}</div>`;
  let govde;
  const aramaBas = arama ? `<div class="arama-ozet"><b>“${esc(S.f.ara.trim())}”</b> için ${d.length} sonuç${S.araDurum === "araniyor" ? " · kanallarda da aranıyor…" : S.araDurum === "bitti" ? " · kanallar da tarandı" : ""}
      · <a href="https://www.akakce.com/arama/?q=${encodeURIComponent(S.f.ara.trim())}" target="_blank" rel="noopener noreferrer">Akakçe’de fiyatları karşılaştır</a></div>
      <div class="ipucu" style="margin:-4px 0 10px">Arama tüm sekmelerde ve tüm kayıtlarda yapılır; mağaza, kategori ve oran filtreleri arama sırasında uygulanmaz. Adında geçenler önce gelir.</div>${S.araDurum === "onizleme" ? `<div class="not">Ön izleme kanallarda arama yapamaz; yalnızca bu sayfadaki paylaşımlarda arar. Telefondaki program aynı aramayı Telegram kanallarında da yapar ve daha eski paylaşımları da getirir.</div>` : ""}` : "";
  if (!S.veri) govde = `<div class="bos-durum">Yükleniyor…</div>`;
  else if (arama && !d.length) govde = `<div class="bos-durum">${S.araDurum === "araniyor" ? "Kanallarda aranıyor…" : S.araDurum === "onizleme" ? "Bu sayfadaki paylaşımlarda bulunamadı." : S.araDurum === "bitti" ? "Kayıtlı fırsatlarda ve kanalların aramasında bulunamadı. Marka ya da model adıyla deneyin." : "Kayıtlı fırsatlarda yok; kanallarda aranıyor…"}</div>`;
  else if (!ham.length) govde = `<div class="bos-durum">${S.veri.yenileniyor ? "Fırsatlar çekiliyor, birkaç saniye sürebilir…" : "Bu sekmede henüz fırsat yok. “Yenile”ye dokunun ya da “Tüm ürünler” sekmesine bakın."}</div>`;
  else if (!d.length) govde = `<div class="bos-durum">Filtreye uyan fırsat yok. Zaman aralığını “Tüm kayıtlar” yapmayı deneyin.</div>`;
  else if (arama) {
    const sinir = Date.now() / 1000 - ESKI_GUN * 86400;
    const guncel = d.filter(x => x.ts >= sinir), eski = d.filter(x => x.ts < sinir).sort((a, b) => b.ts - a.ts);
    govde = (guncel.length ? guncel.slice(0, S.limit).map(kartSec).join("") : `<div class="bos-durum">Son ${ESKI_GUN} günde “${esc(S.f.ara.trim())}” için güncel fırsat paylaşılmadı.</div>`)
      + (eski.length ? `<section class="eski-liste"><h2 class="bolum">Eski paylaşımlar <span class="say">${eski.length}</span></h2>
          <div class="not">Bunlar ${ESKI_GUN} günden eski paylaşımlar; yazan fiyat o tarihteki fiyattır, <b>bugün geçerli değildir</b>. Yalnızca geçmiş fiyat fikri vermek için listelenir.</div>
          <table class="d-tablo"><thead><tr><th>Ürün</th><th>Tarih</th><th class="sag">O günkü fiyat</th></tr></thead><tbody>
          ${eski.slice(0, 40).map(x => `<tr><td>${x.tur === "urun" ? `<a href="#" data-detay="${esc(x.id)}">${esc(x.baslik)}</a>` : esc(x.baslik)}<br><small>${esc(x.magaza_ad)} · ${esc(kanalAd(x.kanal))}</small></td><td>${tarihKisa(x.ts)}</td><td class="sag">${x.fiyat != null ? tl(x.fiyat) : "–"}</td></tr>`).join("")}
          </tbody></table></section>` : "");
  }
  else govde = d.slice(0, S.limit).map(kartSec).join("") + (d.length > S.limit ? `<button type="button" class="daha" id="daha">Daha fazla göster (${d.length - S.limit})</button>` : "");
  $("#icerik").innerHTML = `
    <div class="filtre">
      <input type="search" id="ara" placeholder="Ürün ara (ör. airfryer, kulaklık)" value="${esc(S.f.ara)}" autocomplete="off">
      ${katCipler && !arama ? `<div class="cipler">${katCipler}</div>` : ""}
      ${arama ? "" : `<div class="cipler">${cipler}</div>`}
      <div class="satir">
        ${arama ? "" : `<select id="min" aria-label="En az indirim">
          ${[[0,"Tüm oranlar"],[-1,"Oranı bilinenler"],[10,"%10 ve üstü"],[20,"%20 ve üstü"],[30,"%30 ve üstü"],[50,"%50 ve üstü"]].map(([v,a]) => `<option value="${v}" ${S.f.min === v ? "selected" : ""}>${a}</option>`).join("")}
        </select>`}
        <select id="sirala" aria-label="Sıralama">
          ${[["indirim","En yüksek indirim"],["yeni","En yeni"],["populer","En çok görülen"],["ucuz","En ucuz"]].map(([v,a]) => `<option value="${v}" ${S.f.sirala === v ? "selected" : ""}>${a}</option>`).join("")}
        </select>
      </div>
      ${arama ? "" : `<div class="satir">
        <select id="sure" aria-label="Paylaşım zamanı">
          ${[[6,"Son 6 saat"],[24,"Son 24 saat"],[72,"Son 3 gün"],[0,"Tüm kayıtlar"]].map(([v,a]) => `<option value="${v}" ${((S.f.sure || {})[S.sekme] ?? 72) === v ? "selected" : ""}>${a}</option>`).join("")}
        </select>
        ${S.sekme === "kupon"
          ? `<label class="anahtar"><input type="checkbox" id="gecmisGizle" ${S.f.gecmisGizle ? "checked" : ""}> Süresi geçenleri gizle</label>`
          : `<label class="anahtar"><input type="checkbox" id="ikinciEl" ${S.f.ikinciEl ? "checked" : ""}> 2. el ürünler</label>`}
      </div>`}
      ${S.sekme === "kupon" && !arama ? `<div class="cipler">${[["hepsi","Hepsi"],["kod","Kupon kodları"],["urun","Kodlu ürünler"],["kampanya","Kampanyalar"]].map(([v,a]) => `<button type="button" class="cip" data-kupontur="${v}" aria-pressed="${S.f.kuponTur === v}">${a}</button>`).join("")}</div>` : ""}
    </div>${arama ? aramaBas : not}${govde}`;
}

function takvimCiz(){
  const bugun = new Date().toISOString().slice(0, 10);
  const ay = ["Oca","Şub","Mar","Nis","May","Haz","Tem","Ağu","Eyl","Eki","Kas","Ara"];
  const yaz = t => { const [y, m, g] = t.split("-"); return `${+g} ${ay[+m - 1]}`; };
  const satirlar = TAKVIM.slice().sort((a, b) => (a.bas || a.son).localeCompare(b.bas || b.son)).map(o => {
    const bitti = o.son && o.son < bugun, suruyor = (!o.bas || o.bas <= bugun) && (!o.son || o.son >= bugun);
    const cip = o.tahmin ? ["Beklenti","r-sari"] : bitti ? ["Bitti",""] : suruyor ? ["Sürüyor","r-yesil"] : ["Yaklaşıyor","r-mavi"];
    const tarih = o.bas && o.son ? (o.bas === o.son ? yaz(o.bas) : `${yaz(o.bas)} – ${yaz(o.son)}`) : o.bas ? `${yaz(o.bas)}’den beri` : `${yaz(o.son)}’e kadar`;
    return `<div class="olay" style="${bitti ? "opacity:.55" : ""}"><div class="tarih">${esc(tarih)}</div><div><span class="ad">${esc(o.ad)}</span><span class="durumcip rozet ${cip[1]}">${cip[0]}</span>${o.ac ? `<div class="ac">${esc(o.ac)}</div>` : ""}</div></div>`;
  }).join("");
  $("#icerik").innerHTML = `<div class="takvim">${satirlar}</div><p class="not" style="margin-top:14px">Takvim 7 Kasım 2026’da haber kaynaklarından derlendi; markalar tarihleri değiştirebilir. Fırsat listesi ise her yenilemede internetten güncellenir.</p>`;
}

function ayarCiz(){
  const v = S.veri; if (!v) return;
  if (v.statik) {
    const g = v.son_guncelleme ? new Date(v.son_guncelleme).toLocaleString("tr-TR", {day:"numeric", month:"long", hour:"2-digit", minute:"2-digit"}) : "–";
    $("#icerik").innerHTML = `<div class="ayar"><section class="telefon-panel"><h2>Bu site nasıl çalışır?</h2>
      <div class="ipucu" style="margin-top:0">Liste yaklaşık 5 dakikada bir internette kendiliğinden güncellenir; bilgisayarın açık olması gerekmez. Son güncelleme: <b>${esc(g)}</b>.</div>
      <ol class="adimlar"><li>Kanallar: ${esc(v.ayarlar.kanallar.join(", "))}.</li>
      <li>Öne çıkan ve çok ucuz görünen ürünlerin fiyatı güncelleme sırasında mağazada kontrol edilir (Amazon, n11, MediaMarkt, Pazarama). Hepsiburada otomatik kontrole izin vermez.</li>
      <li>Takip kelimelerin ve hedef fiyat alarmların yalnızca bu telefonda saklanır.</li>
      <li>${v.bildirim_var ? "Kaçırılmaz fırsatlar ve yüksek indirimler ntfy uygulamasına bildirim olarak gelir." : "Telefona bildirim henüz açılmadı."}</li></ol>
      <div class="ipucu">Ana ekrana eklemek için tarayıcı menüsü (⋮) > <b>Ana ekrana ekle</b>.</div></section></div>`;
    return;
  }
  if (!S.ayarTaslak) S.ayarTaslak = JSON.parse(JSON.stringify(v.ayarlar));
  const a = S.ayarTaslak;
  const kaynak = k => v.kaynaklar.find(x => x.kanal.toLowerCase() === k.toLowerCase());
  const kanallar = a.kanallar.map((k, i) => {
    const b = kaynak(k);
    const durum = !b || b.ok == null ? "Henüz okunmadı" : b.ok ? `${b.adet} paylaşım okundu · ${saat(b.zaman)}` : `Hata: ${b.hata}`;
    return `<div class="kanal"><span class="nokta ${b && b.ok ? "ok" : b && b.ok === false ? "hata" : ""}"></span><div class="ad"><b>${esc(b ? b.ad : k)}</b><div>t.me/${esc(k)} · ${esc(durum)}</div></div><button type="button" data-sil="${i}">Kaldır</button></div>`;
  }).join("");
  const sayi = (id, et, min, max, ip) => `<label for="${id}">${et}</label><input type="number" id="${id}" min="${min}" max="${max}" value="${a[id]}"><div class="ipucu">${ip}</div>`;
  const ag = v.ag || {};
  const telefonPanel = ag.acik && ag.bu_cihaz
    ? (ag.adres
      ? `<section class="telefon-panel"><h2>Telefonda aç</h2>
          <div class="tp-govde"><div id="qr" class="qr" aria-label="Telefonda açma bağlantısının QR kodu"></div>
          <ol class="tp-adim"><li>Telefon bu bilgisayarla <b>aynı Wi‑Fi</b>’ye bağlı olsun.</li>
          <li>Telefonun kamerasıyla QR kodu okutun ya da bağlantıyı açın.</li>
          <li>Açılan sayfada tarayıcı menüsünden <b>Ana ekrana ekle</b>’ye dokunun; uygulama gibi açılır.</li></ol></div>
          <div class="satir" style="margin-top:8px"><input type="text" id="agAdres" readonly value="${esc(ag.adres)}" aria-label="Telefon bağlantısı"><button type="button" class="cip" id="agKopyala" style="flex:none">Kopyala</button></div>
          <div class="ipucu">Bağlantıdaki gizli anahtar sayesinde aynı ağdaki başkaları sayfayı açamaz; bağlantıyı başkasıyla paylaşmayın. Telefon yalnızca bilgisayar açıkken ve program çalışırken bağlanabilir.</div></section>`
      : `<section class="telefon-panel"><h2>Telefonda aç</h2><div class="ipucu">Bilgisayarın Wi‑Fi adresi bulunamadı. Bilgisayarın bir ağa bağlı olduğundan emin olup programı yeniden başlatın.</div></section>`)
    : "";
  const konu = v.ayarlar.ntfy_konu;
  const bildirimPanel = `<section class="telefon-panel"><h2>Telefona bildirim</h2>
    ${konu ? `<div class="ipucu" style="margin-top:0">Açık. Yüksek indirimler, takip kelimeleri ve hedef fiyatlar telefona bildirim olarak gelir.</div>
      <ol class="adimlar">
        <li>Telefona ücretsiz <b>ntfy</b> uygulamasını kurun: <a href="https://play.google.com/store/apps/details?id=io.heckel.ntfy" target="_blank" rel="noopener noreferrer">Google Play</a> · <a href="https://f-droid.org/packages/io.heckel.ntfy/" target="_blank" rel="noopener noreferrer">F-Droid</a> · <a href="https://apps.apple.com/app/ntfy/id1625396347" target="_blank" rel="noopener noreferrer">iPhone</a></li>
        <li>Uygulamada <b>+</b> (Abone ol) → konu adına şunu yazın: <span class="konu">${esc(konu)}</span> <button type="button" class="cip" id="konuKopyala" style="flex:none;padding:3px 9px">Kopyala</button></li>
        <li>Aşağıdaki düğmeyle deneme bildirimi gönderin.</li>
      </ol>
      <div class="satir" style="margin-top:10px"><button type="button" class="cip" id="bildirimDene" style="flex:none">Deneme bildirimi gönder</button><button type="button" class="cip" id="bildirimKapat" style="flex:none">Kapat</button></div>
      <div class="ipucu">Bildirimler ntfy.sh sunucusundan geçer. Konu adı bir şifre gibidir: bilen herkes bildirimleri görebilir, kimseyle paylaşmayın. Bildirimde yalnızca ürün adı, fiyat ve bağlantı olur.</div>`
    : `<div class="ipucu" style="margin-top:0">Program bilgisayarda çalışırken yüksek indirimleri, takip kelimelerini ve hedef fiyata inen ürünleri telefona bildirim olarak gönderir. Ücretsiz ntfy uygulamasıyla çalışır; hesap açmak gerekmez.</div>
      <button type="button" class="kaydet" id="bildirimAc" style="margin-top:10px">Telefona bildirimi aç</button>`}
    ${v.termux_bildirim ? `<div class="ipucu">Termux:API bildirimi de açık (bu telefonda).</div>` : ""}
  </section>`;
  $("#icerik").innerHTML = `<div class="ayar">
    ${telefonPanel}
    ${bildirimPanel}
    <h2>Kaynak kanallar</h2>
    <div class="ipucu">Herkese açık Telegram fırsat kanalları. Kanal adını ya da t.me bağlantısını ekleyin.</div>
    ${kanallar}
    <div class="satir" style="margin-top:10px"><input type="text" id="yeniKanal" placeholder="ör. kanaladi veya t.me/kanaladi" autocomplete="off"><button type="button" class="cip" id="kanalEkle" style="flex:none">Ekle</button></div>
    <h2>Yenileme ve bildirim</h2>
    ${sayi("aralik_dk","Otomatik yenileme (dakika)",5,360,"Program açıkken bu aralıkla yeniden okur. Kısa aralık daha çok veri ve pil harcar.")}
    ${sayi("sayfa","Kanal başına sayfa",1,10,"Her sayfa yaklaşık 20 paylaşım. Daha çok sayfa = daha eski fırsatlar da gelir.")}
    ${sayi("bildirim_esik","Bildirim eşiği (%)",10,95, v.bildirim_var ? "Bu oran ve üstündeki yeni fırsatlarda telefona bildirim gönderilir." : "Bildirim şu an kapalı; yukarıdaki “Telefona bildirim” bölümünden açın.")}
    ${sayi("saklama_gun","Paylaşımları sakla (gün)",1,30,"Fiyat geçmişi ayrıca 1 yıl tutulur.")}
    <label class="anahtar" style="margin-top:14px"><input type="checkbox" id="kupon_ara" ${a.kupon_ara !== false ? "checked" : ""}> Her kanalda “kupon” ve “kod” araması da yap</label>
    <div class="ipucu">Son paylaşımların dışında kalan kupon paylaşımlarını da getirir. Her yenilemede kanal başına 2 sayfa daha okur.</div>
    <label class="anahtar" style="margin-top:14px"><input type="checkbox" id="gecmis_tamamla" ${a.gecmis_tamamla !== false ? "checked" : ""}> Yeni ürünlerin eski fiyatlarını kanal arşivinde otomatik ara</label>
    <div class="ipucu">Her yenilemede en yeni 5 ürün için kanalların arama sayfasına bakar; “Ucuzlayanlar” sekmesi bu kayıtlarla dolar.</div>
    <button type="button" class="kaydet" id="kaydet">Ayarları kaydet</button>
    <div class="mesaj" id="mesaj"></div>
    <h2>İndirim oranı nasıl bulunuyor?</h2>
    <ul class="bilgi">
      <li>Paylaşımda yüzde yazıyorsa (“%30 indirim”) o kullanılır.</li>
      <li>Eski ve yeni fiyat yazıyorsa (“4.999 TL yerine 3.299 TL”, üstü çizili fiyat) aradaki fark hesaplanır.</li>
      <li>“3 al 2 öde” gibi kampanyalar orana çevrilir.</li>
      <li>Program aynı ürünü daha önce daha pahalı gördüyse düşüş oranı gösterilir. Bu kayıt program açık kaldıkça büyür.</li>
      <li>“Dip fiyat”: paylaşımda “son 6 ayın en düşük fiyatı” gibi bir ifade var demektir.</li>
      <li>Paylaşımda “sepette”, “ödeme ekranında” ya da “3 adet ekleyin” geçiyorsa gösterilen fiyat sayfa fiyatı değildir; kartta turuncu notla belirtilir. “3 al 2 öde” varsa tahmini sayfa fiyatı da yazılır.</li>
      <li>Kupon kartında tutar, alt limit, son gün ve “yalnızca uygulamada” gibi koşullar paylaşım metninden okunur. Program kodu mağazada deneyemez.</li>
    </ul>
    <p class="not">Bağlantılar kanalların kendi bağlantılarıdır (ortaklık/affiliate bağlantısı olabilir). Program hiçbir hesaba giriş yapmaz, ödeme bilgisi istemez; veriler yalnızca programın çalıştığı cihazda saklanır. Sürüm ${esc(v.surum)}</p>
  </div>`;
}

async function kanallardaAra(q){
  S.araOnbellek = S.araOnbellek || {};
  if (S.veri && S.veri.onizleme) { S.araDurum = "onizleme"; ciz(); return; }
  if (S.araOnbellek[sadeJS(q)]) { S.araDurum = "bitti"; ciz(); return; }
  S.araDurum = "araniyor"; ciz();
  try {
    await fetch("/api/ara?q=" + encodeURIComponent(q), {cache: "no-store"});
    S.araOnbellek[sadeJS(q)] = true;
    if (S.f.ara.trim() !== q) return;
    S.araDurum = "bitti";
    await yukle();
  } catch (e) { S.araDurum = null; ciz(); }
}

function qrCiz(){
  const kutu = $("#qr"); const adres = S.veri && S.veri.ag && S.veri.ag.adres;
  if (!kutu || !adres) return;
  const ciz = () => { try { kutu.innerHTML = ""; new QRCode(kutu, {text: adres, width: 168, height: 168, correctLevel: QRCode.CorrectLevel.M}); } catch (e) { kutu.textContent = "QR kod çizilemedi; bağlantıyı kopyalayıp telefona gönderin."; } };
  if (window.QRCode) return ciz();
  const sc = document.createElement("script");
  sc.src = "https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js";
  sc.onload = ciz;
  sc.onerror = () => { kutu.textContent = "QR kod yüklenemedi (internet yok). Bağlantıyı kopyalayıp telefona gönderin."; };
  document.head.appendChild(sc);
}

function ciz(){ cizGovde(); detayCiz(); canliGozle(); }
function cizGovde(){
  ust(); navCiz();
  if (S.sekme === "takvim") takvimCiz();
  else if (S.sekme === "takip") { if (document.activeElement && document.activeElement.id === "takipKelime") return; takipCiz(); }
  else if (S.sekme === "ayar") { if (document.activeElement && document.activeElement.closest && document.activeElement.closest(".ayar")) return; ayarCiz(); qrCiz(); }
  else {
    if (document.activeElement && document.activeElement.id && document.activeElement.id.startsWith("hedefTL-")) return;
    const odak = document.activeElement && document.activeElement.id === "ara";
    const pos = odak ? document.activeElement.selectionStart : null;
    listeCiz();
    if (odak) { const a = $("#ara"); a.focus(); try { a.setSelectionRange(pos, pos); } catch (e) {} }
  }
}

document.addEventListener("click", async e => {
  const t = e.target.closest("button"); if (!t) return;
  if (t.id === "yenileBtn") return yenileIste();
  if (t.dataset.sekme || t.dataset.git) { S.sekme = t.dataset.sekme || t.dataset.git; S.limit = 60; S.ayarTaslak = null; sakla(); ciz(); window.scrollTo(0, 0); return; }
  if (t.dataset.magaza) { S.f.magaza = t.dataset.magaza; S.limit = 60; sakla(); ciz(); return; }
  if (t.dataset.kategori) { S.f.kategori = t.dataset.kategori; S.limit = 60; sakla(); ciz(); return; }
  if (t.dataset.kaydet) {
    const k = kayitlilar(); const id = t.dataset.kaydet;
    if (k[id]) { delete k[id]; toast("Kayıttan çıkarıldı"); }
    else { const x = S.veri.firsatlar.find(z => z.id === id); if (x) { k[id] = x; toast("Kaydedildi · Takibim sekmesinde"); } }
    kayitliYaz(k); ciz(); return;
  }
  if (t.dataset.paylas) {
    const x = S.veri.firsatlar.find(z => z.id === t.dataset.paylas) || kayitlilar()[t.dataset.paylas]; if (!x) return;
    const fiyatYazi = x.fiyat != null ? ` – ${tl(x.indirimli_fiyat ?? x.fiyat)}` : (x.kodlar && x.kodlar.length ? ` – Kod: ${x.kodlar[0]}` : "");
    const metin = `${x.baslik}${fiyatYazi}${x.indirim != null ? ` (%${x.indirim} indirim)` : ""}`;
    const url = link(x.urun_link) || x.link;
    try { if (navigator.share) { await navigator.share({title: x.baslik, text: metin, url}); return; } } catch (err) { if (err && err.name === "AbortError") return; }
    try { await navigator.clipboard.writeText(metin + "\n" + url); toast("Bağlantı kopyalandı"); } catch (err) { toast(url); }
    return;
  }
  if (t.id === "takipEkle") {
    const g = $("#takipKelime").value.trim(); if (g.length < 2) { toast("En az 2 harf yazın"); return; }
    if (document.activeElement) document.activeElement.blur(); takipKaydet((S.veri.ayarlar.takip || []).concat([g])); return;
  }
  if (t.dataset.takipsil != null) { const l = (S.veri.ayarlar.takip || []).slice(); l.splice(+t.dataset.takipsil, 1); takipKaydet(l); return; }
  if (t.dataset.gecmis) {
    t.disabled = true; t.textContent = "Kanallarda aranıyor…";
    try {
      const r = await fetch("/api/gecmis", {method:"POST", headers:{"Content-Type":"application/json","X-Kasim":"1"}, body: JSON.stringify({id: t.dataset.gecmis})});
      const j = await r.json();
      toast(j.eklenen ? `${j.eklenen} eski fiyat bulundu` : "Eski paylaşım bulunamadı");
    } catch (err) { toast("Aranamadı, bağlantıyı kontrol edin"); }
    await yukle(); return;
  }
  if (t.dataset.canli) { canliKontrol(t.dataset.canli, !!t.dataset.yeniden); return; }
  if (t.dataset.hedef) { S.hedefAcik = S.hedefAcik === t.dataset.hedef ? null : t.dataset.hedef; ciz();
    const i = document.getElementById("hedefTL-" + t.dataset.hedef); if (i) { i.focus(); i.select(); } return; }
  if (t.dataset.hedefkur) {
    const id = t.dataset.hedefkur, hedef = (document.getElementById("hedefTL-" + id) || {}).value;
    try {
      const r = await fetch("/api/alarm", {method:"POST", headers:{"Content-Type":"application/json","X-Kasim":"1"}, body: JSON.stringify({islem:"ekle", id, hedef})});
      const j = await r.json(); if (!r.ok) throw new Error(j.hata || "Kurulamadı");
      S.hedefAcik = null; if (document.activeElement) document.activeElement.blur();
      toast("Alarm kuruldu · Takibim sekmesinde"); await yukle();
    } catch (err) { toast(err.message); }
    return;
  }
  if (t.dataset.alarmsil) {
    try {
      const r = await fetch("/api/alarm", {method:"POST", headers:{"Content-Type":"application/json","X-Kasim":"1"}, body: JSON.stringify({islem:"sil", alarm_id: t.dataset.alarmsil})});
      if (!r.ok) throw new Error("Kaldırılamadı");
      S.hedefAcik = null; toast("Alarm kaldırıldı"); await yukle();
    } catch (err) { toast(err.message); }
    return;
  }
  if (t.id === "bildirimAc" || t.id === "bildirimKapat") {
    let konu = "";
    if (t.id === "bildirimAc") { const b = new Uint8Array(12); crypto.getRandomValues(b); konu = "kasim-" + Array.from(b, x => "abcdefghijkmnpqrstuvwxyz23456789"[x % 32]).join(""); }
    try {
      const r = await fetch("/api/ayarlar", {method:"POST", headers:{"Content-Type":"application/json","X-Kasim":"1"}, body: JSON.stringify({ntfy_konu: konu})});
      const j = await r.json(); if (!r.ok) throw new Error(j.hata || "Kaydedilemedi");
      S.ayarTaslak = null; if (document.activeElement) document.activeElement.blur();
      toast(konu ? "Açıldı · şimdi telefona ntfy’yi kurun" : "Telefona bildirim kapatıldı"); await yukle();
    } catch (err) { toast(err.message); }
    return;
  }
  if (t.id === "konuKopyala") {
    const k = S.veri.ayarlar.ntfy_konu;
    try { await navigator.clipboard.writeText(k); toast("Konu adı kopyalandı"); } catch (err) { toast(k); }
    return;
  }
  if (t.id === "bildirimDene") {
    t.disabled = true; t.textContent = "Gönderiliyor…";
    try {
      const r = await fetch("/api/bildirim-dene", {method:"POST", headers:{"Content-Type":"application/json","X-Kasim":"1"}, body: "{}"});
      const j = await r.json(); if (!r.ok) throw new Error(j.hata || "Gönderilemedi");
      toast("Gönderildi · telefonunuza bakın");
    } catch (err) { toast(err.message); }
    t.disabled = false; t.textContent = "Deneme bildirimi gönder"; return;
  }
  if (t.dataset.kupontur) { S.f.kuponTur = t.dataset.kupontur; S.limit = 60; sakla(); ciz(); return; }
  if (t.id === "daha") { S.limit += 60; ciz(); return; }
  if (t.dataset.kod != null) {
    const k = t.dataset.kod;
    try { await navigator.clipboard.writeText(k); toast(k + " kopyalandı"); }
    catch (err) { toast("Kod: " + k); }
    return;
  }
  if (t.dataset.sil != null) { S.ayarTaslak.kanallar.splice(+t.dataset.sil, 1); ayarCiz(); return; }
  if (t.id === "kanalEkle") {
    const g = $("#yeniKanal").value.trim(); if (!g) return;
    S.ayarTaslak.kanallar.push(g); ayarCiz(); return;
  }
  if (t.id === "agKopyala") {
    const a = $("#agAdres").value;
    try { await navigator.clipboard.writeText(a); toast("Bağlantı kopyalandı"); } catch (err) { $("#agAdres").select(); toast("Seçildi, kopyalayın"); }
    return;
  }
  if (t.id === "kaydet") {
    const a = S.ayarTaslak;
    ["aralik_dk","sayfa","bildirim_esik","saklama_gun"].forEach(k => a[k] = +$("#" + k).value);
    const m = $("#mesaj");
    try {
      const r = await fetch("/api/ayarlar", {method:"POST", headers:{"Content-Type":"application/json","X-Kasim":"1"}, body: JSON.stringify(a)});
      const j = await r.json();
      if (!r.ok) throw new Error(j.hata || "Kaydedilemedi");
      S.ayarTaslak = null; m.className = "mesaj"; m.textContent = "Kaydedildi."; toast("Ayarlar kaydedildi");
      if (document.activeElement) document.activeElement.blur();
      setTimeout(yukle, 400);
    } catch (err) { m.className = "mesaj hata"; m.textContent = err.message; }
  }
});
document.addEventListener("keydown", e => {
  if (e.key === "Enter" && e.target.id && e.target.id.startsWith("hedefTL-")) {
    const b = document.querySelector(`[data-hedefkur="${CSS.escape(e.target.id.slice(8))}"]`); if (b) b.click();
  }
  if (e.key === "Enter" && e.target.id === "takipKelime") { const b = $("#takipEkle"); if (b) b.click(); }
});
document.addEventListener("input", e => {
  if (e.target.id === "ara") {
    S.f.ara = e.target.value; S.limit = 60; S.araDurum = null; sakla(); ciz();
    clearTimeout(S.araZaman);
    const q = S.f.ara.trim();
    if (q.length >= 3) S.araZaman = setTimeout(() => kanallardaAra(q), 700);
  }
});
document.addEventListener("keydown", e => {
  if (e.key === "Enter" && e.target.id === "takipKelime") { e.preventDefault(); $("#takipEkle").click(); }
  if (e.key === "Enter" && e.target.id === "ara") { e.target.blur(); const q = S.f.ara.trim(); if (q.length >= 2) { clearTimeout(S.araZaman); kanallardaAra(q); } }
});
document.addEventListener("change", e => {
  const t = e.target;
  if (t.id === "min") { S.f.min = +t.value; sakla(); ciz(); }
  else if (t.id === "sirala") { S.f.sirala = t.value; sakla(); ciz(); }
  else if (t.id === "ikinciEl") { S.f.ikinciEl = t.checked; sakla(); ciz(); }
  else if (t.id === "gecmisGizle") { S.f.gecmisGizle = t.checked; sakla(); ciz(); }
  else if (t.id === "sure") { S.f.sure = Object.assign({}, S.f.sure, {[S.sekme]: +t.value}); sakla(); ciz(); }
  else if (t.id === "kupon_ara" && S.ayarTaslak) { S.ayarTaslak.kupon_ara = t.checked; }
  else if (t.id === "gecmis_tamamla" && S.ayarTaslak) { S.ayarTaslak.gecmis_tamamla = t.checked; }
  else if (S.ayarTaslak && ["aralik_dk","sayfa","bildirim_esik","saklama_gun"].includes(t.id)) { S.ayarTaslak[t.id] = +t.value; }
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) yukle(); });
yukle();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
