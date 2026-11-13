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
import gzip
import hmac
import html
import base64
import functools
import http.cookiejar
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
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SURUM = "3.9"
GITHUB_ACTIONS = bool(os.environ.get("GITHUB_ACTIONS"))   # GitHub sunucusunda mı çalışıyor (veri merkezi IP'si)
VARSAYILAN_PORT = 8765
VERI_DIZINI = os.environ.get("KASIM_FIRSAT_DIZIN") or os.path.join(os.path.expanduser("~"), ".kasim_firsat")
VERI_DOSYASI = os.path.join(VERI_DIZINI, "veri.json")
AYAR_DOSYASI = os.path.join(VERI_DIZINI, "ayarlar.json")
ANAHTAR_DOSYASI = os.path.join(VERI_DIZINI, "erisim_anahtari.txt")

VARSAYILAN_AYARLAR = {
    "kanallar": ["dhsicak_firsatlar", "firsatz", "tazefirsat", "indirimbakanligi", "indirimhaber", "trendyolfirsat",
                 "firsatdolu", "indirimalarmi_tr", "amazonindirimtr", "ucuzkenaldim", "firsatzade",
                 "gunlukfirsatbulteni", "uygunfiyatdedektifi", "onual_ekstra",
                 "indirimseri", "amazonozel", "firsatlarco", "umutlufirsatlar", "yurticifirsat"],
    "akakce": True,        # Akakçe listelerini de oku (fark atan, son 6 ayın en ucuzu, broşür)
    # DonanımHaber Sıcak Fırsatlar (RSS → forum sayfası). GitHub sunucusundan okunamadığı için orada varsayılan kapalı
    "dhforum": not GITHUB_ACTIONS,
    "akakce_dogrula": True,  # doğrulanan ürünü Akakçe'deki en ucuz fiyatla karşılaştır
    "red_gizle": True,       # mağazada fiyatı yükselmiş / stoğu bitmiş ürünleri listeden çıkar
    "oyun": True,          # Steam / Epic oyun indirimleri (CheapShark)
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
COZUCU_SURUM = 5   # okuma kuralları değişince artırılır; kayıtlı paylaşımlar açılışta yeniden okunur
ESKI_VARSAYILAN_KANALLAR = [   # önceki sürümlerin varsayılan listeleri: hiç değiştirilmediyse yeni listeye geçilir
    ["dhsicak_firsatlar", "firsatz", "tazefirsat", "indirimbakanligi"],
    ["dhsicak_firsatlar", "firsatz", "tazefirsat", "indirimbakanligi", "indirimhaber", "trendyolfirsat"],
    ["dhsicak_firsatlar", "firsatz", "tazefirsat", "indirimbakanligi", "indirimhaber", "trendyolfirsat",
     "firsatdolu", "indirimalarmi_tr", "amazonindirimtr", "ucuzkenaldim", "firsatzade",
     "gunlukfirsatbulteni", "uygunfiyatdedektifi", "onual_ekstra"],
]
AKAKCE = "akakce"          # Akakçe'den gelen ürünlerin "kanal" adı
AKAKCE_LISTELERI = [       # (kısa ad, adres, görünen ad, okunacak sayfa; ~32 ürün/sayfa)
    ("fark", "https://www.akakce.com/fark-atan-fiyatlar/", "Fark Atan Fiyatlar", 3),
    ("dip", "https://www.akakce.com/son-alti-ayin-en-ucuz-fiyatli-urunleri/", "Son 6 Ayın En Ucuzu", 3),
    ("brosur", "https://www.akakce.com/brosurlerde-en-iyi-fiyatlar/", "Broşürlerdeki Daha Ucuz Fiyatlar", 2),
]
DHFORUM = "dhforum"
DHFORUM_URL = "https://forum.donanimhaber.com/sicak-firsatlar--f193"
DHFORUM_SAYFA = 3
# Önce RSS denenir (hafif, yapısı sabit); okunamazsa forum sayfası. Ortam değişkeniyle başka adres eklenebilir.
DH_RSS_ADRESLERI = [u for u in [os.environ.get("KF_DH_RSS", "").strip(),
                                "https://www.donanimhaber.com/rss/tum-sicak-firsatlar.xml"] if u]
OYUN = "oyun"
OYUN_URL = ("https://www.cheapshark.com/api/1.0/deals?storeID=1,25,7&onSale=1&sortBy=Deal%20Rating"
            "&pageSize=60&steamRating=70")
OYUN_MAGAZA = {"1": "Steam", "25": "Epic Games", "7": "GOG"}
OYUN_EN_AZ_INDIRIM = 50
DIS_KAYNAKLAR = {AKAKCE: "Akakçe", DHFORUM: "DonanımHaber Forum · Sıcak Fırsatlar", OYUN: "Oyun indirimleri (Steam/Epic/GOG)"}
NTFY_KONU_RE = re.compile(r"^[A-Za-z0-9_-]{12,64}$")
AYAR_SINIRLARI = {"sayfa": (1, 10), "aralik_dk": (5, 360), "bildirim_esik": (10, 95), "saklama_gun": (1, 30)}
GECMIS_GUN = 60            # "indirim oranı" için bakılan yakın geçmiş (gün)
ARSIV_GUN = 365            # fiyat arşivi (kanal geçmişinden bulunanlar dahil) kaç gün tutulsun
ARSIV_OTO_ADET = 5         # her yenilemede geçmişi kanal arşivinden tamamlanacak ürün sayısı
ARAMA_ARSIV_GUN = 90       # aramada bulunabilecek eski paylaşımların en fazla yaşı (gün)
ARAMA_ARSIV_EN_FAZLA = 40000
GERIYE_TARAMA_SN = 60      # her yenilemede kanalların daha eski sayfalarını okumaya ayrılan süre (saniye)
TAKIP_ARAMA_ADET = 3       # her yenilemede kanallarda otomatik aranacak takip kelimesi sayısı (sırayla döner)
TAKIBIM = "takibim"        # kullanıcının mağaza bağlantısıyla kendisinin eklediği ürünlerin "kanal" adı
DOGRULAMA_YAS_SN = 48 * 3600     # kuyruğa yalnızca bu kadar yeni ürünler alınır
DOGRULAMA_TAZE_SN = 3 * 3600     # doğrulanmış ürün bu süreden sonra yeniden kontrol edilir (fiyat değişmiş olabilir)
DOGRULAMA_DENEME = 3             # okunamayan ürün en fazla kaç kez denensin
ENGEL_BEKLE_SN = 6 * 3600        # otomatik okumayı engelleyen mağaza bu süre denenmez (mağazayı yormamak için)
AKAKCE_ONBELLEK_SN = 12 * 3600
AKAKCE_COZUCU = 2           # Akakçe okuma kuralı sürümü; değişince eski (hatalı) Akakçe kayıtları temizlenir
AKAKCE_TAKIP_SN = 3 * 3600  # takip kelimesinin Akakçe araması kaç saatte bir yenilensin
AKAKCE_TAKIP_ADET = 12      # takip kelimesi başına Akakçe'den alınan ürün sayısı
EN_FAZLA_KANAL = 25
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
KOD_SONRA_RE = re.compile(r"(?:(?i:kod)(?:(?i:u|unu))?|(?i:kupon)(?: (?i:kodu))?)\s*[:：]?\s*[\"“'`]?([A-ZÇĞİÖŞÜ0-9]{4,20})(?![\w])")
# "Bu kod sadece uygulamada geçerlidir: APP150" — kod kelimesinden sonra aynı satırda iki nokta ve kod
KOD_SATIR_RE = re.compile(r"(?i:kod|kupon)[^\n:：]{0,45}[:：]\s*[\"“'`]?([A-ZÇĞİÖŞÜ0-9]{4,20})(?![\w])")
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
                      r"kampanya bitmiştir|fırsat bitmiştir|artık geçerli değil|geçersizdir artık|"
                      r"(?:kod|kupon)\w* (?:artık )?(?:çalışmıyor|geçmiyor|geçersiz|bitti|tükendi|doldu|kapandı|sona erdi)|"
                      r"(?:kullanım )?limit(?:i)? doldu|kupon(?:lar)? (?:tükendi|bitti)|kod(?:lar)? tükendi|"
                      r"stok(?:lar)? tükenmiştir|tükenmiştir|kampanya sona ermiştir)(?:[^\wçğıöşü]|$)")
# Önce kesin ifadeler (çakışmaları çözer; sıra önemlidir, ilk eşleşen kazanır), sonra sıralı kategori kelimeleri.
# Tümü aksansız küçük harf. Kelimeler kelime başından eşleşir; sonuna boşluk konan kelime tam kelime olarak aranır.
KATEGORI_KESIN = [
    # temizlik sarf malzemesi (makine adı geçse de cihaz değildir)
    ("bulasik makinesi tablet", "temizlik"), ("bulasik makinesi deterjan", "temizlik"), ("makine tableti", "temizlik"),
    ("bulasik tableti", "temizlik"), ("camasir makinesi temizleyici", "temizlik"), ("arap sabunu", "temizlik"),
    ("kagit havlu", "temizlik"), ("tuvalet kagidi", "temizlik"), ("islak mendil", "temizlik"), ("temizlik havlusu", "temizlik"),
    ("asmali havlu", "temizlik"), ("cop torbasi", "temizlik"), ("cop poseti", "temizlik"),
    # yapı ve oto (genel "yıkama makinesi" / "süpürge" kurallarından önce)
    ("basincli yikama", "yapi"), ("kaynak makinesi", "yapi"), ("ufleme makinesi", "yapi"), ("uzatma kablosu", "yapi"),
    ("arac kamerasi", "oto"), ("arac supurgesi", "oto"), ("aku takviye", "oto"), ("motor kaski", "oto"),
    ("elektrikli scooter", "oto"), ("elektrikli motosiklet", "oto"), ("oto koltugu", "bebek"), ("park yatak", "bebek"),
    ("mama sandalyesi", "bebek"), ("devam sutu", "bebek"), ("bebek bezi", "bebek"), ("bebek mamasi", "bebek"),
    ("bebek monitoru", "bebek"),
    # ev aletleri
    ("bulasik makinesi", "beyaz"), ("camasir makinesi", "beyaz"), ("kurutma makinesi", "beyaz"), ("yikama makinesi", "beyaz"),
    ("kahve makinesi", "beyaz"), ("espresso makinesi", "beyaz"), ("cay makine", "beyaz"), ("su sebili", "beyaz"),
    ("derin dondurucu", "beyaz"), ("nem alma", "beyaz"), ("buhar makinesi", "beyaz"), ("tost makinesi", "beyaz"),
    ("sac kurutma", "kozmetik"), ("dis fircasi", "kozmetik"), ("agiz dusu", "kozmetik"), ("tiras", "kozmetik"),
    ("epilasyon", "kozmetik"), ("hasta bezi", "kozmetik"), ("emici kulot", "kozmetik"),
    # gıda (kahve makinesi kuralından sonra)
    ("cekirdek kahve", "gida"), ("kapsul kahve", "gida"), ("filtre kahve", "gida"), ("turk kahvesi", "gida"),
    ("granul kahve", "gida"), ("aycicek yagi", "gida"), ("findik kremasi", "gida"), ("findik ezmesi", "gida"),
    # bilgisayar ve oyun donanımı
    ("oyuncu monitoru", "bilgisayar"), ("oyuncu mouse", "bilgisayar"), ("oyuncu kulakligi", "bilgisayar"),
    ("oyuncu klavye", "bilgisayar"), ("oyun konsolu", "bilgisayar"), ("ekran karti", "bilgisayar"),
    ("oyuncu koltugu", "ev"), ("ofis koltugu", "ev"), ("calisma koltugu", "ev"), ("uyku seti", "ev"), ("pazar arabasi", "ev"),
    ("kosu bandi", "spor"), ("yuruyus bandi", "spor"), ("kamp buzlugu", "spor"),
    ("akilli saat", "elektronik"), ("yazar kasa", "elektronik"), ("kol saati", "giyim"),
    ("hava temizleyici", "beyaz"), ("philips hue", "ev"), ("on ve arka kamera", "oto"), ("outdoor bot", "giyim"), ("bot ", "giyim"),
]
KATEGORILER = [
    ("evcil", "Evcil hayvan", ("kedi", "kopek", "kopegi", "kus yemi", "akvaryum", "pet shop", "tasma ")),
    ("bebek", "Anne, bebek & oyuncak", ("bebek", "lego", "oyuncak", "mama", "yurutec", "puset", "fisher-price",
                                         "fisher price", "hot wheels", "barbie", "boyama", "oyun seti", "emzik", "biberon")),
    ("kozmetik", "Kişisel bakım & kozmetik", ("parfum", "edp", "edt ", "sampuan", "kremi", "krem,", "nemlendirici krem", "sac ", "sac,", "duzlestirici",
                                       "makyaj", "ruj", "maskara", "deodorant", "bakim seti", "cilt", "serum",
                                       "durex", "nivea", "dudak", "parlatici", "fondoten", "oje", "sabun",
                                       "allik", "far paleti", "dis macunu", "sensodyne", "colgate", "maybelline", "loreal",
                                       "garnier", "rimel", "ped ", "pedi", "vazelin", "prezervatif", "kolonya", "jilet")),
    ("temizlik", "Temizlik & kâğıt ürünleri", ("deterjan", "yumusatici", "camasir suyu", "bulasik", "temizleyici", "temizlik",
                                               "pecete", "mendil", "kapsul", "leke cikarici", "cam sil", "yuzey")),
    ("gida", "Gıda & içecek", ("cay ", "cayi", "caykur", "kahve", "biskuvi", "cikolata", "seker", "ice tea", "zeytinyagi",
                               "zeytin", "peynir", "kuruyemis", "makarna", "pirinc", "bakliyat", "icecek", "kremasi",
                               "surup", "atistirmalik", "kasar", "tavuk", "pilic", "incir", "sut ", "sutu", "tuz ", "salca",
                               "bal ", "bali", "recel", "un ", "yag ", "yagi", "nutella", "gofret", "cips", "kraker",
                               "meyve suyu", "maden suyu", "gazoz", "kola", "icme suyu", "kaynak suyu", "damacana su", "yumurta", "et ", "sucuk", "salam",
                               "nescafe", "lipton", "tchibo", "jacobs", "ezmesi", "helva", "tahin", "pekmez", "konserve")),
    ("bilgisayar", "Bilgisayar, oyun & donanım", ("laptop", "dizustu", "notebook", "bilgisayar", "all in one", "monitor",
                                                  "ekran karti", "rtx", "radeon", "rx ", "anakart", "islemci", "ryzen",
                                                  "intel core", "ddr4", "ddr5", "ssd", "nvme", "harddisk", "hdd",
                                                  "flash bellek", "usb bellek", "sd kart", "micro sd", "mouse", "klavye",
                                                  "yazici", "murekkep", "toner", "playstation", "ps5", "ps4", "xbox",
                                                  "nintendo", "konsol", "dualsense", "router", "modem", "antivirus",
                                                  "norton", "kaspersky", "lisans", "webcam")),
    ("elektronik", "Telefon, TV & elektronik", ("telefon", "kulaklik", "televizyon", "tv", "miniled", "qled", "oled",
                                                "tablet", "ipad", "sarj aleti", "sarj adaptoru", "adaptor", "kablo", "usb",
                                                "hoparlor", "kamera", "powerbank", "power bank", "airpods", "earpods",
                                                "iphone", "galaxy", "buds", "earfun", "tws", "projeksiyon", "bluetooth",
                                                "kumanda", "watch", "drone", "honor ", "reeder", "redmi", "xiaomi 1",
                                                "oppo", "realme", "poco ", "infinix", "tecno ", "soundbar", "media box",
                                                "mediabox", "tv box", "uydu")),
    ("beyaz", "Beyaz eşya & ev aletleri", ("supurge", "buzdolabi", "dondurucu", "firin", "mikrodalga", "airfryer", "air fryer",
                                            "fritoz", "blender", "mikser", "dograyici", "kettle", "su isiticisi", "tost",
                                            "utu", "isitici", "vantilator", "klima", "nemlendirici", "humidifier",
                                            "hava temizleyici", "espresso", "izgara", "robot ", "aspirator", "davlumbaz",
                                            "ocak ", "sebil", "termosifon", "kombi ")),
    ("ev", "Ev, mutfak & mobilya", ("tabure", "sandalye", "fincan", "bardak", "kase", "askil", "kurutmalik", "kurutma askisi",
                                    "karistirma kabi", "rende", "saklama kabi", "dus basligi", "kahvalti takimi",
                                    "yemek takimi", "porselen", "seramik", "tencere", "tava", "catal", "kasik", "bicak",
                                    "termos", "ampul", "lamba", "aydinlatma", "hue ", "nevresim", "battaniye", "havlu",
                                    "pestemal", "dikis makinesi", "mutfak", "yastik", "yorgan", "hali", "mobilya", "koltuk",
                                    "yatak", "baza", "dolap", "kitaplik", "masa ", "perde", "ayakkabilik", "vazo", "cerceve")),
    ("giyim", "Giyim, ayakkabı & aksesuar", ("mont", "kazak", "tisort", "t-shirt", "sweatshirt", "ayakkabi", "boxer", "corap",
                                              "pijama", "pantolon", "jean", "elbise", "ceket", "canta", "polo", "gomlek",
                                              "hirka", "esofman", "sneaker", "polar", "kaban", "etek", "saat", "bot ",
                                              "cizme", "terlik", "sandalet", "gozluk", "cuzdan", "kemer", "bere", "atki")),
    ("spor", "Spor & outdoor", ("spor", "kamp", "cadir", "bisiklet", "fitness", "dambil", "matara", "kosu", "yoga",
                                "outdoor", "uyku tulumu", "pilates", "kondisyon")),
    ("yapi", "Yapı market & bahçe", ("tornavida", "matkap", "boru kesici", "lokma", "bits", "pense", "anahtar takimi",
                                     "dewalt", "makita", "einhell", "bosch professional", "alet cantasi", "takim cantasi",
                                     "testere", "akulu", "taslama", "zimpara", "merdiven", "boya ", "silikon", "vida",
                                     "dubel", "bahce", "cim bicme", "budama", "tirpan", "sulama", "hortum", "jenerator",
                                     "kompresor", "priz")),
    ("oto", "Oto, motosiklet & scooter", ("oto ", "otomobil", "arac ", "lastik", "aku ", "motosiklet", "kask", "scooter",
                                          "jant", "silecek", "motor yagi", "navigasyon")),
]
KATEGORI_AD = {k: ad for k, ad, _ in KATEGORILER}
# Aramada birbirinin yerine geçen yazımlar (sade, küçük harf)
ESANLAMLI = {
    "ps5": ["playstation 5"], "playstation 5": ["ps5"], "ps4": ["playstation 4"], "playstation 4": ["ps4"],
    "airfryer": ["air fryer"], "air fryer": ["airfryer"], "tv": ["televizyon"], "televizyon": ["tv"],
    "laptop": ["dizustu"], "notebook": ["laptop"], "kulaklik": ["buds"], "iphone": ["apple iphone"],
    # Takip kelimeleri: ilk eş anlamlı kanal/Akakçe aramasında da kullanılır (mağazalar "oyuncu koltuğu" diye yazıyor)
    "oyuncu sandalyesi": ["oyuncu koltugu", "oyuncu sandalye", "oyun koltugu", "gaming chair", "gamer koltuk"],
    "oyuncu koltugu": ["oyuncu sandalyesi", "oyun koltugu", "gaming chair", "gamer koltuk"],
    "gaming chair": ["oyuncu koltugu", "oyuncu sandalyesi"],
    "evrak cantasi": ["evrak canta", "is cantasi", "briefcase", "dokuman cantasi"],
    "is cantasi": ["evrak cantasi", "evrak canta"],
}
# Takip kelimesinin Akakçe'de hangi sözcükle aranacağı (yoksa kelimenin kendisi)
AKAKCE_TAKIP_SORGU = {"oyuncu sandalyesi": "oyuncu koltuğu", "gaming chair": "oyuncu koltuğu", "is cantasi": "evrak çantası"}
KATEGORI_AD["oyun"] = "Dijital oyunlar"
KATEGORI_AD["diger"] = "Diğer"
KOD_DISLA = {"HEPSIBURADA", "HEPSİBURADA", "AMAZON", "TRENDYOL", "İNDİRİM", "INDIRIM", "KUPON", "KAMPANYA",
             "FIRSAT", "FIRSATI", "SEPETTE", "PREMIUM", "PRIME", "BOYNER", "MİGROS", "MIGROS", "PAZARAMA",
             "KODU", "KODUNU", "KODUYLA", "KUPONU", "KUPONLAR", "GEÇERLİ", "GECERLI", "TIKLA", "TIKLAYIN", "LİNK", "LINK",
             "SADECEBUGÜN", "SADECEBUGUN", "BUGÜN", "UYGULAMA", "UYGULAMADA", "ÜCRETSİZ", "UCRETSIZ", "KARGO"}

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
        if s.strip().startswith("🛒") and not re.search(r"\bgit\b", kucuk(s)) and not BASLIK_ATLA_RE.search(temizle(s)) \
                and not re.match(r"\s*(?:https?://|www\.)\S+\s*$", temizle(s.strip().lstrip("🛒"))):   # "🛒 https://..." başlık değil
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
    for rx in (KOD_ONCE_RE, KOD_SONRA_RE, KOD_SATIR_RE):
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
        if re.search(r"kadar|son gün|geçerli|bitiş|sona er|tarihleri arası|arasında|arası", bag):
            return True
        # "11.11'e özel", "29 Kasım'a özel", "15 Kasım günü": tek günlük kampanya → o günün sonunda biter
        return re.match(r"\s*['’]?\s*(?:y?[ae]|n[ae])?\s*(?:özel|günü(?:ne)?\s+özel|günü\b)", dmetin[son:son + 20])

    # "12-14 Kasım", "12 - 14 Kasım arası": aralığın son günü
    for m in re.finditer(r"(?<!\d)(\d{1,2})\s*[-–]\s*(\d{1,2})\s*(" + "|".join(AYLAR) + r")", dmetin):
        if int(m.group(1)) < int(m.group(2)):
            ekle(yerel.year, AYLAR.index(m.group(3)) + 1, int(m.group(2)), False)

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


VARYANT_KELIMELER = frozenset({"fe", "ultra", "plus", "pro", "max", "mini", "lite", "se", "edge", "neo", "slim", "air",
                               "fold", "flip", "xl", "xr", "xs"})
_KIMLIK_BIRIM = r"(?:gb|tb|mb)"
_KIMLIK_DISLA_RE = re.compile(r"^(?:[45]g|wifi\d*|usb\d*|ip6\d|\d+(?:mah|hz|w|mm|cm|ml|lt?|kg|gr?|k|p|x|v|a|adet|li|lu|inc|inch))$")


@functools.lru_cache(maxsize=8192)
def urun_kimligi(baslik):
    """Başlıktaki ürünü ayırt eden parçalar: seri/model (s26, 17), varyant (fe, ultra, pro…) ve hafıza (256gb, 8gb)."""
    t = re.sub(r"(?<=[0-9a-z])\+", " plus", sade(baslik or ""))
    t = re.sub(r"\b(\d+)\s*(" + _KIMLIK_BIRIM + r")\b", r"\1\2", t)
    seri, varyant, hafiza = set(), set(), set()
    kelimeler = re.findall(r"[a-z0-9]+", t)
    for i, k in enumerate(kelimeler):
        if k in VARYANT_KELIMELER:
            varyant.add(k)
        elif re.fullmatch(r"\d+" + _KIMLIK_BIRIM, k):
            hafiza.add(k)
        elif _KIMLIK_DISLA_RE.match(k):
            continue
        elif re.fullmatch(r"[a-z]{1,3}\d{1,4}[a-z]?", k) or (re.fullmatch(r"\d{1,3}", k) and i > 0 and
                                                          not re.fullmatch(r"\d+", kelimeler[i - 1])):
            seri.add(k)
    return frozenset(seri), frozenset(varyant), frozenset(hafiza)


def farkli_varyant(a, b):
    """İki başlık açıkça farklı ürünü mü anlatıyor? (S26 ↔ S26 FE, S25 FE ↔ S26 FE, 128 GB ↔ 256 GB)
    Bir başlık diğerinden yalnızca daha ayrıntılıysa (biri hafızayı yazmamış gibi) farklı sayılmaz; varyant kelimesi
    (FE, Ultra, Pro…) ise birinde olup diğerinde yoksa farklı üründür."""
    if not a or not b:
        return False
    sa, va, ha = urun_kimligi(a)
    sb, vb, hb = urun_kimligi(b)
    if va != vb and (sa & sb or not (sa or sb)):
        return True
    if (sa - sb) and (sb - sa):
        return True
    return bool((ha - hb) and (hb - ha))


def ayni_urun(g, h):
    """İki paylaşım aynı ürünü mü anlatıyor? (mağaza kodu, model kodu ya da başlık benzerliği)"""
    ga, ha = (g.get("anahtar") or ""), (h.get("anahtar") or "")
    if ga and ha and not ga.startswith("t:") and ga.split(":2el")[0] == ha.split(":2el")[0]:
        return True
    # Barkod (GTIN/EAN) ikisinde de okunduysa kesin cevap: aynıysa aynı ürün, farklıysa farklı ürün
    if g.get("gtin") and h.get("gtin"):
        return g["gtin"] == h["gtin"]
    # Kelimelerin çoğu ortak olsa da model/varyant/hafıza farklıysa başka üründür (S26 FE ≠ S26 ≠ S25 FE)
    if farkli_varyant(g.get("baslik"), h.get("baslik")):
        return False
    model = model_kodu(g["baslik"])
    if model and model in (h["baslik"] + " " + h.get("metin", "")).upper():
        return True
    a, b = set(anlamli_kelimeler(g["baslik"])[:10]), set(anlamli_kelimeler(h["baslik"])[:10])
    return bool(a and b) and len(a & b) / len(a | b) >= 0.6


def arama_sorgusu(baslik):
    return model_kodu(baslik) or " ".join(re.findall(r"[0-9A-Za-zÇĞİÖŞÜçğıöşü]+", baslik)[:4])


def _kok(w):
    """Türkçe ekleri kaba biçimde atar: "sandalyesi" → "sandaly", "çantası" → "cant", "koltuğu" → "koltu"."""
    w = sade(w)
    return w if len(w) <= 4 or re.search(r"\d", w) else w[:max(4, len(w) - 3)]


def _kelimelerle_eslesir(ifade, kelimeler):
    """İfadenin kelimeleri metinde art arda (ekli hâlleriyle) geçiyor mu?"""
    parca = [_kok(x) for x in re.findall(r"[a-z0-9]+", sade(ifade))]
    if not parca:
        return False
    for i in range(len(kelimeler) - len(parca) + 1):
        if all(kelimeler[i + j].startswith(p) for j, p in enumerate(parca)):
            return True
    return False


def takip_eslesen(kelimeler, g):
    """Takip kelimesi başlıkta/metinde geçiyor mu? Eş anlamlılar ("oyuncu sandalyesi" = "oyuncu koltuğu")
    ve Türkçe ekler ("evrak çantası" = "evrak çantaları", "evrak çanta") de eşleşir."""
    if not kelimeler:
        return []
    metin = " " + sade(g.get("baslik", "") + " " + g.get("metin", "")) + " "
    sozcukler = re.findall(r"[a-z0-9]+", metin)
    sonuc = []
    for k in kelimeler:
        sk = sade(k)
        if not sk:
            continue
        secenek = [sk] + [sade(e) for e in ESANLAMLI.get(sk, [])]
        if any(x in metin for x in secenek) or any(_kelimelerle_eslesir(x, sozcukler) for x in secenek):
            sonuc.append(k)
    return sonuc


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


# Otomatik fiyat okumayı engelleyen mağazalar artık sabit yazılmıyor: engel gördükçe Depo.veri["engel"]'e kaydedilir
# (ENGEL_BEKLE_SN boyunca denenmez). Bu ürünlerde güven, paylaşımın tazeliğinden ve kaç kanalın aynı fiyatı
# paylaştığından hesaplanır.


def kanal_teyidi(d):
    """Aynı ürünü son 24 saatte aynı (±%2) fiyata paylaşan farklı kanal sayısı."""
    kanallar, ilk = {d["kanal"].lower()}, d["ts"]
    for z in d.get("diger") or []:
        if z.get("fiyat") and z.get("ts") and d["ts"] - z["ts"] <= 86400 \
                and abs(z["fiyat"] - d["fiyat"]) <= d["fiyat"] * 0.02:
            kanallar.add(z["kanal"].lower())
            ilk = min(ilk, z["ts"])
    return {"kanal": len(kanallar), "ilk_ts": ilk}


URUN_YOLU_RE = re.compile(r"/dp/|/gp/product/|/gp/aw/d/|-p-\d|-pm-|/urun/|/product/|-p\d")
GENEL_KAMPANYA_RE = re.compile(r"ürünlerinde|ürünlerde|kategori|alışveriş|sepette|tüm |seçili|markalarda|mağaza|üzeri|"
                               r"kampanya|indirim kodu|kupon", re.I)


def urun_gibi(d):
    """Kod/kampanya paylaşımı genel bir kampanya değil de tek bir ürünü mü anlatıyor?"""
    link = d.get("urun_link") or ""
    alan = (urllib.parse.urlparse(link).hostname or "").lower()
    yol = urllib.parse.urlparse(link).path.lower()
    if any(alan.endswith(m) for _, _, alanlar, _ in MAGAZALAR for m in alanlar) and URUN_YOLU_RE.search(yol) \
            and not re.search(r"/kampanya|/promotion|/magaza/|/butik/|/liste/|/store/", yol):
        return True
    baslik = d.get("baslik") or ""
    for k in d.get("kodlar") or []:
        baslik = re.sub(re.escape(k), " ", baslik, flags=re.I)
    return bool(model_kodu(baslik)) and not GENEL_KAMPANYA_RE.search(kucuk(d.get("metin") or baslik))


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
    if d.get("kontrol_et") and not dogru:      # şüpheli ani düşüş mağazada doğrulanmadan "kaçırılmaz" olmaz
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


def gtin_coz(x):
    """JSON-LD Product kaydındaki barkod (GTIN/EAN 8-14 hane); yoksa None. Mağazalar arası ürün eşleştirmenin tek güvenilir yolu."""
    for alan in ("gtin13", "gtin", "gtin14", "gtin12", "gtin8", "isbn"):
        v = x.get(alan)
        if isinstance(v, list):
            v = v[0] if v else None
        if v is None:
            continue
        s = re.sub(r"\D", "", str(v))
        if 8 <= len(s) <= 14 and s.strip("0"):
            return s.zfill(13) if len(s) in (12, 13) else s
    return None


def sayfadan_baslik(h):
    """Ürün sayfasından ürün adı, görseli ve barkodu: JSON-LD Product → og:title → <title>. (başlık, görsel, gtin) döndürür."""
    baslik = resim = gtin = None
    for blok in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', h, re.S | re.I):
        try:
            veri = json.loads(blok.strip())
        except ValueError:
            continue
        yigin = [veri]
        while yigin and not baslik:
            x = yigin.pop()
            if isinstance(x, list):
                yigin.extend(x)
            elif isinstance(x, dict):
                if str(x.get("@type", "")).lower() == "product" and x.get("name"):
                    baslik = str(x["name"])
                    r = x.get("image")
                    if isinstance(r, list):
                        r = r[0] if r else None
                    if isinstance(r, dict):
                        r = r.get("url") or r.get("contentUrl")
                    resim = r if isinstance(r, str) else None
                    gtin = gtin_coz(x)
                    if not gtin:
                        for t in (x.get("offers") if isinstance(x.get("offers"), list) else [x.get("offers")]):
                            if isinstance(t, dict) and (gtin := gtin_coz(t)):
                                break
                    break
                yigin.extend(v for v in x.values() if isinstance(v, (dict, list)))
        if baslik:
            break
    if not gtin:
        # Sayfa verisinde düz alan olarak yazılmış barkod (Trendyol/Hepsiburada ürün JSON'u)
        m = re.search(r'"(?:barcode|gtin|ean|gtin13)"\s*:\s*"?(\d{8,14})"?', h)
        if m:
            gtin = gtin_coz({"gtin": m.group(1)})
    if not baslik:
        m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', h, re.I) or \
            re.search(r'<meta[^>]+content="([^"]+)"[^>]+property="og:title"', h, re.I) or \
            re.search(r"<title[^>]*>([^<]+)</title>", h, re.I)
        if m:
            baslik = m.group(1)
    if not resim:
        m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', h, re.I)
        resim = m.group(1) if m else None
    if baslik:
        baslik = html.unescape(baslik)
        baslik = re.sub(r"\s*[|\-–]\s*(?:Amazon\.com\.tr|Trendyol|Hepsiburada|n11\.com|n11|Teknosa|MediaMarkt|Vatan Bilgisayar|Pazarama|Çiçeksepeti).*$", "", baslik, flags=re.I)
        baslik = re.sub(r"\s+", " ", baslik).strip()[:160] or None
    return baslik, (html.unescape(resim) if resim and resim.startswith("http") else None), gtin


# ---------------------------------------------------------------------------
# Mağaza sayfası okuma katmanı
#   Motor sırası: curl_cffi (gerçek Chrome TLS parmak izi, kuruluysa) → urllib (mobil Chrome başlıkları)
#   → başsız tarayıcı (Playwright, yalnızca kuruluysa ve izin verildiyse).
#   Engel ASILMAZ: CAPTCHA çözülmez, vekil sunucu döndürülmez. Engelleyen mağaza bir süre hiç denenmez.
# ---------------------------------------------------------------------------
try:                                    # isteğe bağlı: pip install curl_cffi
    from curl_cffi import requests as _cffi
except Exception:                       # kurulu değil ya da bu cihazda (ör. Termux) çalışmıyor
    _cffi = None
try:                                    # isteğe bağlı: pip install playwright && python -m playwright install chromium
    from playwright.sync_api import sync_playwright as _sync_playwright
except Exception:
    _sync_playwright = None

MOBIL_UA = ("Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.6723.102 Mobile Safari/537.36")
CFFI_PROFIL = os.environ.get("KF_CFFI_PROFIL", "chrome")    # curl_cffi taklit profili
TARAYICI_IZIN = os.environ.get("KF_TARAYICI", "") == "1"     # başsız tarayıcı yedeği yalnızca açıkça istenirse
ENGEL_RE = re.compile(r"captcha|robot check|/errors/validatecaptcha|cf-chl|challenge-platform|attention required|"
                      r"access denied|px-captcha|datadome|geo\.captcha-delivery|just a moment\.\.\.|"
                      r"istek(?:iniz)? engellendi|güvenlik doğrulaması", re.I)
_CEREZLER = http.cookiejar.CookieJar()   # mağazanın ilk ziyarette verdiği çerezler sonraki isteklerde de gönderilir
_ISPARCACIGI = threading.local()
_TARAYICI_KILIT = threading.Lock()


def motor_ozeti():
    """Hangi okuma motorları kullanılabiliyor (ayarlar sayfasında ve günlükte gösterilir)."""
    m = (["curl_cffi"] if _cffi else []) + ["urllib"]
    if _sync_playwright and TARAYICI_IZIN and not GITHUB_ACTIONS:
        m.append("tarayıcı")
    return m


def tarayici_basliklari(url):
    """Android Chrome'un bir ürün sayfasını açarken gönderdiği başlıklar. Google'dan gelinmiş gibi görünür
    (Telegram linkleri de genelde aynı yoldan açılır). Tüm başlıklar aynı tarayıcıyı anlatmalı: karışık
    imza (ör. masaüstü UA + mobil istemci ipucu) WAF'lar için en belirgin bot işaretidir."""
    return {
        "User-Agent": MOBIL_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate",
        "Referer": "https://www.google.com/",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "cross-site", "Sec-Fetch-User": "?1",
        "Sec-CH-UA": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
        "Sec-CH-UA-Mobile": "?1", "Sec-CH-UA-Platform": '"Android"',
    }


def _coz_govde(ham, kodlama, charset):
    kodlama = (kodlama or "").lower()
    try:
        if "gzip" in kodlama:
            ham = gzip.decompress(ham)
        elif "deflate" in kodlama:
            try:
                ham = zlib.decompress(ham)
            except zlib.error:
                ham = zlib.decompress(ham, -zlib.MAX_WBITS)
    except (OSError, zlib.error, EOFError):
        pass
    return ham.decode(charset or "utf-8", errors="replace")


def _urllib_getir(url, zaman_asimi):
    acici = urllib.request.build_opener(_GuvenliYonlendirme(), urllib.request.HTTPCookieProcessor(_CEREZLER))
    istek = urllib.request.Request(url, headers=tarayici_basliklari(url))
    try:
        with acici.open(istek, timeout=zaman_asimi) as y:
            return y.status, y.geturl(), _coz_govde(y.read(3_000_000), y.headers.get("Content-Encoding"),
                                                    y.headers.get_content_charset())
    except urllib.error.HTTPError as e:
        try:
            govde = _coz_govde(e.read(400_000), e.headers.get("Content-Encoding"), e.headers.get_content_charset())
        except Exception:
            govde = ""
        return e.code, e.geturl() or url, govde


def _cffi_getir(url, zaman_asimi):
    """curl_cffi ile gerçek Chrome TLS/HTTP2 parmak izi. Yönlendirmeler elle izlenir: her adım internet adresi mi diye bakılır."""
    oturum = getattr(_ISPARCACIGI, "cffi", None)
    if oturum is None:
        oturum = _ISPARCACIGI.cffi = _cffi.Session(impersonate=CFFI_PROFIL)
    # UA ve istemci ipuçlarını curl_cffi profilin kendisi verir (TLS imzasıyla tutarlı olsun diye eklenmez)
    ek = {"Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7", "Referer": "https://www.google.com/"}
    for _ in range(6):
        if not guvenli_dis_adres(url):
            raise urllib.error.URLError("güvensiz yönlendirme")
        r = oturum.get(url, headers=ek, timeout=zaman_asimi, allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
            url = urllib.parse.urljoin(url, r.headers["location"])
            ek["Referer"] = r.url
            continue
        return r.status_code, str(r.url), r.text[:3_000_000]
    raise urllib.error.URLError("çok fazla yönlendirme")


def _tarayici_getir(url, zaman_asimi):
    """Son çare: sayfayı başsız Chromium'da açar (JavaScript ile oluşan fiyatlar için). Yavaş; tek tek çalışır."""
    with _TARAYICI_KILIT, _sync_playwright() as p:
        tarayici = p.chromium.launch(headless=True)
        try:
            ctx = tarayici.new_context(**p.devices["Pixel 7"], locale="tr-TR", timezone_id="Europe/Istanbul")
            sayfa = ctx.new_page()
            yanit = sayfa.goto(url, wait_until="domcontentloaded", timeout=zaman_asimi * 1000)
            sayfa.wait_for_timeout(2500)              # fiyatı JS ile yazan sayfalar için kısa bekleme
            son = sayfa.url
            if not guvenli_dis_adres(son):
                raise urllib.error.URLError("güvensiz yönlendirme")
            return (yanit.status if yanit else 200), son, sayfa.content()[:3_000_000]
        finally:
            tarayici.close()


def engel_mi(kod, metin):
    if kod in (403, 429, 503):
        return True
    return bool(ENGEL_RE.search(metin[:30000])) and not re.search(r'"@type"\s*:\s*"Product"', metin)


def magaza_sayfasi_getir(url, zaman_asimi=20, tarayici=None):
    """Sayfayı sırayla motorlarla dener. Dönen: {durum: ok|engellendi|hata, kod, url, html, motor, denenen}.
    Bir motor engellenirse sıradakine geçilir; hepsi engellenirse 'engellendi' döner (aşılmaya çalışılmaz)."""
    tarayici = (TARAYICI_IZIN and not GITHUB_ACTIONS) if tarayici is None else tarayici
    motorlar = ([("curl_cffi", _cffi_getir)] if _cffi else []) + [("urllib", _urllib_getir)] + \
        ([("tarayıcı", _tarayici_getir)] if tarayici and _sync_playwright else [])
    denenen, son_engel, son_hata = [], None, None
    for ad, f in motorlar:
        try:
            kod, son, metin = f(url, zaman_asimi)
        except Exception as e:
            denenen.append(f"{ad}: {hata_metni(e)}")
            son_hata = e
            continue
        if engel_mi(kod, metin):
            denenen.append(f"{ad}: engel (HTTP {kod})")
            son_engel = {"durum": "engellendi", "kod": kod, "url": son, "motor": ad}
            continue
        if kod >= 400:
            denenen.append(f"{ad}: HTTP {kod}")
            return {"durum": "hata", "kod": kod, "url": son, "html": metin, "motor": ad, "denenen": denenen}
        return {"durum": "ok", "kod": kod, "url": son, "html": metin, "motor": ad, "denenen": denenen}
    if son_engel:
        return dict(son_engel, html="", denenen=denenen)
    return {"durum": "hata", "kod": None, "url": url, "html": "", "motor": None, "denenen": denenen,
            "hata": hata_metni(son_hata) if son_hata else "okunamadı"}


# ---------------------------------------------------------------------------
# Gömülü JSON ayrıştırıcı
#   Pazar yerleri ürün verisini sayfaya JSON olarak gömer (Next.js __NEXT_DATA__, window.__..._STATE__,
#   JSON-LD). Görünen HTML'i kazımak yerine bu nesneler bulunur, ad/fiyat/stok buradan okunur.
#   Nesneyi bulmak için düzenli ifade, okumak için json.raw_decode kullanılır (iç içe parantezde şaşmaz).
# ---------------------------------------------------------------------------
_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.S | re.I)
# window.__STATE__ = {...}  ya da  window["__envoy__SHARED_PROPS"] = {...} (Trendyol)
_ATAMA_RE = re.compile(r"""(?:(?:window|self|globalThis)\[["']([^"'\]]{1,80})["']\]|(?:window\.|self\.|globalThis\.)?([A-Za-z_$][\w$.]*?))\s*=\s*(?=[{\[])""")
_JSON_PARSE_RE = re.compile(r'JSON\.parse\(\s*"((?:[^"\\]|\\.){20,})"\s*\)', re.S)
_JSON_COZ = json.JSONDecoder()
SATIS_ANAHTAR = ("discountedPrice", "finalPrice", "finalPriceOnSale", "salePrice", "sellingPrice", "currentPrice",
                 "priceToPay", "price", "amount", "lowPrice", "value")
LISTE_ANAHTAR = ("originalPrice", "listPrice", "oldPrice", "strikeThroughPrice", "marketPrice", "priceBeforeDiscount",
                 "highPrice", "regularPrice")
AD_ANAHTAR = ("name", "productName", "title", "displayName")
STOK_EVET = re.compile(r"instock|limitedavailability|preorder|onlineonly|instoreonly", re.I)
STOK_HAYIR = re.compile(r"outofstock|soldout|discontinued", re.I)


def gomulu_jsonlar(h, en_fazla=40):
    """Sayfadaki gömülü JSON nesnelerini (kaynak türüyle) döndürür: [(tür, nesne), ...]"""
    sonuc = []
    for m in _SCRIPT_RE.finditer(h):
        if len(sonuc) >= en_fazla:
            break
        nit, govde = m.group(1).lower(), m.group(2).strip()
        if not govde or len(govde) > 4_000_000:
            continue
        govde_ = re.sub(r"^\s*(?:<!--|//\s*<!\[CDATA\[)|(?:-->|//\s*\]\]>)\s*$", "", govde).strip()
        if "ld+json" in nit or "application/json" in nit or "__next_data__" in nit:
            try:
                sonuc.append(("ld" if "ld+json" in nit else "json", json.loads(govde_)))
            except ValueError:
                pass
            continue
        if "src=" in nit:
            continue
        # window.__PRODUCT_DETAIL_APP_INITIAL_STATE__ = {...};  /  window.__INITIAL_STATE__ = {...}
        for a in _ATAMA_RE.finditer(govde):
            ad = a.group(1) or a.group(2)
            if not re.search(r"state|data|props|product|initial|__", ad, re.I):
                continue
            try:
                nesne, _ = _JSON_COZ.raw_decode(govde, a.end())
            except ValueError:
                continue
            if isinstance(nesne, (dict, list)) and nesne:
                sonuc.append(("atama:" + ad[-40:], nesne))
        # JSON.parse("{\"product\":...}") biçimi
        for j in _JSON_PARSE_RE.finditer(govde):
            try:
                nesne = json.loads(json.loads('"' + j.group(1) + '"'))
            except ValueError:
                continue
            if isinstance(nesne, (dict, list)):
                sonuc.append(("jsonparse", nesne))
    return sonuc


def _sayi(v):
    """Fiyat alanı değeri: sayı, '1.299,90 TL' metni ya da {value/amount/price/text: ...} nesnesi olabilir."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, dict):
        for k in ("value", "amount", "discountedPrice", "price", "sellingPrice", "text"):
            if k in v:
                f = _sayi(v[k])
                if f:
                    return f
        return None
    f = _fiyat_metinden(v)
    return f if f and 0.5 <= f <= 20_000_000 else None


def _stok_coz(x):
    """Bir ürün nesnesinin stok durumu: True / False / None (bilinmiyor)."""
    for k in ("inStock", "isInStock", "hasStock", "isSellable", "available", "isAvailable", "buyable"):
        if isinstance(x.get(k), bool):
            return x[k]
    for k in ("soldOut", "isSoldOut", "outOfStock", "isOutOfStock"):
        if isinstance(x.get(k), bool):
            return not x[k]
    for k in ("stock", "stockCount", "stockQuantity", "quantity", "availableStock"):
        v = x.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v > 0
        if isinstance(v, dict) and isinstance(v.get("count", v.get("quantity")), (int, float)):
            return v.get("count", v.get("quantity")) > 0
    av = x.get("availability")
    if isinstance(av, str):
        if STOK_HAYIR.search(av):
            return False
        if STOK_EVET.search(av):
            return True
    return None


def _resim_coz(x):
    for k in ("image", "images", "imageUrl", "imageUrls", "thumbnail", "mainImage", "img"):
        r = x.get(k)
        if isinstance(r, list):
            r = r[0] if r else None
        if isinstance(r, dict):
            r = r.get("url") or r.get("contentUrl") or r.get("src")
        if isinstance(r, str) and r.startswith(("http://", "https://", "//")):
            return "https:" + r if r.startswith("//") else r
    return None


FIYAT_YOLU = ("price", "prices", "priceInfo", "pricing", "offers", "offer", "winnerVariant", "merchantListing", "listing",
              "selectedVariant", "priceSpecification")
LISTE_TURU = re.compile(r"ListPrice|Strikethrough|MSRP|SRP|RegularPrice", re.I)


def _fiyat_yerleri(x, derinlik=3, en_fazla=20):
    """Ürün nesnesi ve fiyat taşıyan alt nesneleri (ör. merchantListing → winnerVariant → price), yakından uzağa."""
    yerler, kuyruk, gorulen = [x], [(x, 0)], {id(x)}
    while kuyruk and len(yerler) < en_fazla:
        y, d = kuyruk.pop(0)
        if d >= derinlik:
            continue
        for k in FIYAT_YOLU:
            v = y.get(k)
            for z in (v[:5] if isinstance(v, list) else [v]):
                if isinstance(z, dict) and id(z) not in gorulen:
                    gorulen.add(id(z))
                    yerler.append(z)
                    kuyruk.append((z, d + 1))
    return yerler


def _liste_spec_mi(y):
    """schema.org UnitPriceSpecification(priceType=ListPrice/StrikethroughPrice): üstü çizili fiyatı taşır."""
    return bool(LISTE_TURU.search(str(y.get("priceType") or "")))


def _fiyatlar_coz(x):
    """Bir nesnenin (ve fiyat/teklif alt nesnelerinin) satış ve liste fiyatı: (satış, liste)."""
    yerler = _fiyat_yerleri(x)
    satis = liste = None
    # n11 uygulama verisi: displayPriceFloat = ödenen, priceFloat = üstü çizili fiyat
    if isinstance(x.get("displayPriceFloat"), (int, float)) and isinstance(x.get("priceFloat"), (int, float)):
        satis, liste = _sayi(x["displayPriceFloat"]), _sayi(x["priceFloat"])
    for anahtar in () if satis else SATIS_ANAHTAR:
        for y in yerler:
            if _liste_spec_mi(y):
                continue
            if anahtar in y and anahtar not in ("value", "amount") or (anahtar in ("value", "amount") and y is not x):
                f = _sayi(y.get(anahtar))
                if f:
                    satis = f
                    break
        if satis:
            break
    for anahtar in () if liste else LISTE_ANAHTAR:
        for y in yerler:
            f = _sayi(y.get(anahtar)) if anahtar in y else None
            if f:
                liste = f
                break
        if liste:
            break
    if not liste:
        liste = next((f for y in yerler if _liste_spec_mi(y) and (f := _sayi(y.get("price")))), None)
    # Liste fiyatı satıştan yüksek ve makul olmalı (8 katından fazlası çoğu zaman başka bir alanın değeridir)
    if satis and liste and not (satis * 1.001 < liste <= satis * 8):
        liste = None
    return satis, liste


def _benzerlik(a, b):
    if farkli_varyant(a, b):
        return 0.0
    ka, kb = set(anlamli_kelimeler(a or "")[:12]), set(anlamli_kelimeler(b or "")[:12])
    return len(ka & kb) / len(ka | kb) if ka and kb else 0.0


def urun_adaylari(nesneler, sayfa_basligi=None, en_fazla_dugum=60000):
    """Gömülü JSON'da ürünü anlatan nesneleri bulup puanlar. En iyi aday ilk sırada döner."""
    adaylar, yigin, sayac = [], [(t, n, 0) for t, n in nesneler], 0
    while yigin and sayac < en_fazla_dugum:
        tur, x, derinlik = yigin.pop()
        sayac += 1
        if isinstance(x, list):
            yigin.extend((tur, v, derinlik + 1) for v in x[:200] if isinstance(v, (dict, list)))
            continue
        if not isinstance(x, dict) or derinlik > 30:
            continue
        ad = next((x[k] for k in AD_ANAHTAR if isinstance(x.get(k), str) and 3 <= len(x[k]) <= 300), None)
        satis, liste = _fiyatlar_coz(x)
        tip = str(x.get("@type", "")).lower()
        if ad and satis:
            puan = 3 + 2 * (tip == "product") + (tur == "ld") + bool(liste)
            stok = _stok_coz(x)
            if stok is None and isinstance(x.get("offers"), (dict, list)):
                for o in (x["offers"] if isinstance(x["offers"], list) else [x["offers"]])[:5]:
                    if isinstance(o, dict) and (stok := _stok_coz(o)) is not None:
                        break
            puan += (stok is not None) + bool(_resim_coz(x)) + bool(gtin_coz(x))
            if sayfa_basligi:
                puan += 4 * _benzerlik(ad, sayfa_basligi)
            # Önerilen/benzer ürün listelerindeki kartlar ana ürünle karışmasın
            if re.search(r"recommend|similar|related|crosssell|benzer|oneri", tur + " " + " ".join(list(x)[:30]), re.I):
                puan -= 2
            adaylar.append({"puan": round(puan, 2), "baslik": re.sub(r"\s+", " ", html.unescape(ad)).strip()[:160],
                            "fiyat": satis, "liste_fiyati": liste, "stok": stok, "resim": _resim_coz(x),
                            "gtin": gtin_coz(x), "kaynak": tur})
        yigin.extend((tur, v, derinlik + 1) for v in x.values() if isinstance(v, (dict, list)))
    adaylar.sort(key=lambda a: -a["puan"])
    return adaylar


def amazon_liste_fiyati(h):
    """Amazon ana fiyat kutusundaki üstü çizili fiyat (yoksa None). Sayfadaki öneri kartlarının fiyatları alınmaz."""
    bas = max(h.find('id="corePriceDisplay_desktop_feature_div"'), h.find("apex-core-price-identifier"))
    if bas < 0:
        return None
    son = min([i for i in (h.find('id="oneTimePaymentPrice_feature_div"', bas), h.find('id="buybox"', bas)) if i > 0] or [bas + 8000])
    m = re.search(r'data-a-strike="true"[^>]*>\s*<span class="a-offscreen">([^<]*\d[^<]*)</span>', h[bas:min(son, bas + 8000)])
    return _fiyat_metinden(m.group(1)) if m else None


def urun_verisi_coz(h, host):
    """Ürün sayfasından {baslik, fiyat, liste_fiyati, stok, resim, gtin, yontem}. Bulunamayan alan None."""
    og = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', h, re.I) or \
        re.search(r'<meta[^>]+content="([^"]+)"[^>]+property="og:title"', h, re.I)
    sayfa_basligi = html.unescape(og.group(1)) if og else None
    adaylar = urun_adaylari(gomulu_jsonlar(h), sayfa_basligi)
    en = adaylar[0] if adaylar else None
    sonuc = {"baslik": None, "fiyat": None, "liste_fiyati": None, "stok": None, "resim": None, "gtin": None, "yontem": None}
    if en:
        sonuc.update({k: en[k] for k in ("baslik", "fiyat", "liste_fiyati", "stok", "resim", "gtin")})
        sonuc["yontem"] = "Sayfadaki ürün verisi (" + ("schema.org" if en["kaynak"] == "ld" else "gömülü JSON") + ")"
        # Aynı ürünün başka kaynaktaki kaydı eksik alanı tamamlar (ör. JSON-LD'de stok yok, uygulama verisinde var)
        for a in adaylar[1:8]:
            if _benzerlik(a["baslik"], en["baslik"]) < 0.6:
                continue
            for k in ("liste_fiyati", "stok", "resim", "gtin"):
                if sonuc[k] is None and a[k] is not None:
                    sonuc[k] = a[k]
            # JSON-LD çoğu zaman indirimsiz satış fiyatını yazar; uygulama verisindeki indirimli fiyat daha düşükse o geçerli
            if a["fiyat"] and a["kaynak"] != "ld" and en["kaynak"] == "ld" and 0.5 * sonuc["fiyat"] <= a["fiyat"] < sonuc["fiyat"]:
                sonuc["liste_fiyati"] = sonuc["liste_fiyati"] or sonuc["fiyat"]
                sonuc["fiyat"], sonuc["yontem"] = a["fiyat"], "Sayfadaki ürün verisi (gömülü JSON)"
    # Amazon: ödenecek fiyat kutusu JSON'dan güvenilir; eski yöntemler de yedek olarak kalır
    if "amazon." in host or not sonuc["fiyat"]:
        f, y = sayfadan_fiyat(h, host)
        if f and ("amazon." in host or not sonuc["fiyat"]):
            sonuc["fiyat"], sonuc["yontem"] = f, y
    if "amazon." in host and sonuc["fiyat"]:
        liste = amazon_liste_fiyati(h)
        sonuc["liste_fiyati"] = liste if liste and sonuc["fiyat"] * 1.001 < liste <= sonuc["fiyat"] * 8 else None
    if not sonuc["baslik"] or not sonuc["resim"] or not sonuc["gtin"]:
        b, r, g = sayfadan_baslik(h)
        sonuc["baslik"], sonuc["resim"], sonuc["gtin"] = sonuc["baslik"] or b, sonuc["resim"] or r, sonuc["gtin"] or g
    if sonuc["stok"] is None:
        if re.search(r'"availability"\s*:\s*"[^"]*(?:OutOfStock|SoldOut)|Şu anda mevcut değil|Currently unavailable|'
                     r'id="outOfStock"|>\s*(?:Stokta yok|Tükendi|Bu ürün tükendi)\s*<', h, re.I):
            sonuc["stok"] = False
        elif re.search(r'"availability"\s*:\s*"[^"]*InStock', h):
            sonuc["stok"] = True
    if sonuc["baslik"]:
        sonuc["baslik"] = re.sub(r"\s*[|\-–]\s*(?:Amazon\.com\.tr|Trendyol|Hepsiburada|n11\.com|n11|Teknosa|MediaMarkt|"
                                 r"Vatan Bilgisayar|Pazarama|Çiçeksepeti).*$", "", sonuc["baslik"], flags=re.I).strip() or None
    return sonuc


def sayfa_getir_guclu(url, zaman_asimi=25):
    """Liste/forum sayfaları için: mağaza okuma katmanıyla getirir, olmazsa anlaşılır hata fırlatır."""
    s = magaza_sayfasi_getir(url, zaman_asimi=zaman_asimi, tarayici=False)
    if s["durum"] == "ok":
        return s["html"]
    raise ValueError(("Site otomatik okumaya izin vermedi" if s["durum"] == "engellendi" else "Sayfa açılamadı")
                     + (f" (HTTP {s['kod']})" if s.get("kod") else "") + (" · " + "; ".join(s["denenen"]) if s.get("denenen") else ""))


URUN_SAYFASI_RE = re.compile(r"/dp/|/gp/product/|/gp/aw/d/|-p-\d|-pm-|/urun/|/product/|/p/|-p\d|\.html$|/[^/]*\d{5,}")


# Aracı fırsat siteleri (Onual vb.) kendi sayfalarına yönlendirir; gerçek mağaza linki sayfanın içindedir.
ARACI_URL_PARAM = ("url", "u", "target", "redirect_url", "to")


def _icten_magaza_linki(href):
    """'https://zxro.com/u/?redirect=1&url=https%3A%2F%2Fwww.pazarama.com%2F...' → içteki mağaza linki (yoksa None)."""
    try:
        parca = urllib.parse.urlparse(href)
    except ValueError:
        return None
    if magaza_alani(href):
        return href
    q = urllib.parse.parse_qs(parca.query)
    for ad in ARACI_URL_PARAM:
        for deger in q.get(ad, []):
            if deger.startswith("http") and magaza_alani(deger):
                return deger
    return None


def araci_sayfadan_magaza_linki(h):
    """Aracı sitenin ürün sayfasından mağazanın ÜRÜN sayfası linkini bulur. Önce 'Ürüne Git' düğmesi."""
    adaylar = []
    for m in re.finditer(r'<a\b[^>]*?href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>', h, re.I | re.S):
        href = html.unescape(m.group(1))
        ic = _icten_magaza_linki(href)
        if not ic:
            continue
        yol = urllib.parse.urlparse(ic).path.lower()
        if re.search(r"/arama|/magaza/|/sr$|/s$|/kampanya|/butik/|/liste/|/store/", yol) or not URUN_SAYFASI_RE.search(yol):
            continue
        metin = kucuk(re.sub(r"<[^>]+>", " ", m.group(2)))
        oncelik = 0 if re.search(r"ürüne git|urune git|satın al|mağazaya git|siteye git", metin) else 1
        adaylar.append((oncelik, len(adaylar), ic))
    return min(adaylar)[2] if adaylar else None


def canli_fiyat_getir(url, tarayici=None, _derinlik=0):
    """Mağaza sayfasını bir kez açar, güncel fiyatı ve stoğu okur.
    durum: ok / stok_yok / engellendi / urun_degil / bulunamadi / hata"""
    if not url or not guvenli_dis_adres(url):
        return {"durum": "hata", "mesaj": "Bu paylaşımda mağaza bağlantısı yok."}
    ic = _icten_magaza_linki(url)
    if ic and ic != url and _derinlik == 0:          # yönlendirme linkinin içinde mağaza linki açıkça duruyor
        return canli_fiyat_getir(ic, tarayici, _derinlik + 1)
    s = magaza_sayfasi_getir(url, tarayici=tarayici)
    son, motor = s.get("url") or url, s.get("motor")
    if s["durum"] == "engellendi":
        return {"durum": "engellendi", "son_url": son, "motor": motor, "denenen": s["denenen"],
                "mesaj": f"Mağaza otomatik kontrole izin vermedi (HTTP {s.get('kod')}); fiyatı sayfayı açarak görün."}
    if s["durum"] == "hata":
        return {"durum": "hata", "denenen": s["denenen"],
                "mesaj": f"Mağaza sayfası açılamadı ({'HTTP ' + str(s['kod']) if s.get('kod') else s.get('hata')})."}
    h = s["html"]
    if not magaza_alani(son):
        # Mağaza değil, aracı bir fırsat sitesine geldik: fiyat orada DOĞRULANMAZ, gerçek mağaza linki aranır
        hedef = araci_sayfadan_magaza_linki(h) if _derinlik < 2 else None
        if hedef:
            return canli_fiyat_getir(hedef, tarayici, _derinlik + 1)
        host_ = (urllib.parse.urlparse(son).hostname or "").lower()
        return {"durum": "urun_degil", "son_url": son, "motor": motor,
                "mesaj": f"Bağlantı mağazaya değil aracı bir siteye ({host_}) gidiyor; mağaza linki bulunamadı."}
    host = (urllib.parse.urlparse(son).hostname or "").lower()
    yol = urllib.parse.urlparse(son).path.lower()
    if re.search(r"/magaza/|/arama|/sr$|/s$|/kampanya|/butik/|/liste/|/store/", yol) or not URUN_SAYFASI_RE.search(yol):
        return {"durum": "urun_degil", "son_url": son, "motor": motor,
                "mesaj": "Paylaşımdaki bağlantı ürün sayfasına değil, mağaza/arama sayfasına gidiyor; fiyatı ürünü seçerek görün."}
    u = urun_verisi_coz(h, host)
    ortak = {"son_url": son, "magaza": magaza_alani(son), "motor": motor, "baslik": u["baslik"], "resim": u["resim"],
             "gtin": u["gtin"], "liste_fiyati": u["liste_fiyati"], "stok": u["stok"]}
    if not u["fiyat"]:
        if u["stok"] is False:
            return dict(ortak, durum="stok_yok", mesaj="Ürün şu anda stokta yok (mağaza sayfası öyle diyor).")
        if "amazon." in host:
            return dict(ortak, durum="bulunamadi",
                        mesaj="Amazon'da öne çıkan satıcı yok; ürün yalnızca diğer satıcılarda olabilir. Fiyatı sayfada görün.")
        return dict(ortak, durum="bulunamadi", mesaj="Sayfa açıldı ama fiyat okunamadı (fiyat sayfada JavaScript ile oluşuyor olabilir).")
    return dict(ortak, durum="ok", fiyat=u["fiyat"], yontem=u["yontem"], stok_yok=u["stok"] is False,
                kampanyalar=sayfa_kampanyalari(h, host))


SEPETTE_YUZDE_RE = re.compile(r"sepette\s*(?:ek\s*)?%\s*(\d{1,2})(?!\d)|sepette\s*(?:ek\s*)?(\d{1,2})\s*%", re.I)
SEPETTE_TUTAR_RE = re.compile(r"sepette\s*(?:ek\s*)?(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+(?:,\d{1,2})?)\s*(?:TL|₺)\s*(?:indirim|ind\.)", re.I)
SEPETTE_FIYAT_RE = re.compile(r"sepette\s*(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d{2,6}(?:,\d{1,2})?)\s*(?:TL|₺)(?!\s*(?:indirim|ind\.|ve üzeri|üzeri))", re.I)


def sepette_indirim_bul(h):
    """Ürün sayfasındaki 'Sepette %10 indirim' / 'Sepette 200 TL indirim' / 'Sepette 1.299 TL' etiketleri.
    Liste fiyatı ile ödenecek tutar farklı olduğunda bunu görmek yanlış fiyattan almayı önler.
    Dönen: {"yuzde": 10} ya da {"tutar": 200} ya da {"fiyat": 1299.0} (ilk bulunan); yoksa None."""
    # Sayfanın görünen metni ile sayfa içindeki JSON verisi birlikte taranır; script/style gürültüsü azaltılır
    metin = html.unescape(re.sub(r"<style.*?</style>", " ", h, flags=re.S | re.I))
    metin = re.sub(r"<[^>]+>", " ", metin)
    m = SEPETTE_YUZDE_RE.search(metin)
    if m:
        y = int(m.group(1) or m.group(2))
        if 1 <= y <= 90:
            return {"yuzde": y}
    m = SEPETTE_TUTAR_RE.search(metin)
    if m and (t := _fiyat_metinden(m.group(1))):
        return {"tutar": t}
    m = SEPETTE_FIYAT_RE.search(metin)
    if m and (t := _fiyat_metinden(m.group(1))):
        return {"fiyat": t}
    return None


def sayfa_kampanyalari(h, host):
    """Ürün sayfasında görünen adet/sepet kampanyaları: Amazon'un kampanya kutusu + tüm mağazalarda 'Sepette …' etiketi."""
    sonuc = []
    if "amazon." in host:
        for m in re.finditer(r'id="promoMessage[a-z0-9]+"[^>]*>(.{0,800}?)</(?:span|div)>', h, re.S):
            t = html.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))
            t = re.sub(r"\s+", " ", t).replace(" uygun ürünler", "").strip(" .")
            if t and t not in sonuc:
                sonuc.append(t[:140])
    s = sepette_indirim_bul(h)
    if s:
        if "yuzde" in s:
            sonuc.append(f"Sepette %{s['yuzde']} indirim")
        elif "tutar" in s:
            sonuc.append(f"Sepette {fiyat_yaz(s['tutar'])} indirim")
        else:
            sonuc.append(f"Sepette {fiyat_yaz(s['fiyat'])}")
    if not sonuc and "amazon." not in host:
        return None          # bu mağazanın sayfasında kampanya görünmüyor → bilinmiyor
    return sonuc


def sepet_fiyati_tahmini(sayfa, kampanyalar):
    """'Sepette …' etiketinden ödenecek tutarı hesaplar; yoksa None."""
    for k in kampanyalar or []:
        s = sepette_indirim_bul(k)
        if not s:
            continue
        if "yuzde" in s:
            return round(sayfa * (100 - s["yuzde"]) / 100, 2)
        if "tutar" in s and s["tutar"] < sayfa:
            return round(sayfa - s["tutar"], 2)
        if "fiyat" in s and s["fiyat"] < sayfa:
            return s["fiyat"]
    return None


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
    # Sayfada "Sepette %10 / Sepette 1.299 TL" etiketi varsa ödenecek tutar listedekinden düşüktür
    sepet_t = sepet_fiyati_tahmini(sayfa, (sonuc_kamp or {}).get("kampanyalar"))
    sonuc["sepet_tahmini"] = sepet_t
    if sepet_t:
        sonuc["sepet_metin"] = (f"Sayfada sepet indirimi etiketi var: liste {fiyat_yaz(sayfa)}, sepette yaklaşık {fiyat_yaz(sepet_t)}. "
                                f"Kesin tutar sepete ekleyince görünür.")
    if g.get("kanal") == TAKIBIM:
        # Kullanıcının kendi eklediği ürün: "paylaşılan fiyat" yok, önceki okumayla ve eklendiği günkü fiyatla kıyasla
        ilk = g.get("ilk_fiyat") or p
        if ilk:
            fark = (sayfa - ilk) / ilk * 100
            sonuc["fark"] = round(fark, 1)
            if fark <= -3:
                sonuc["karar"], sonuc["renk"] = ("gecerli", "yesil")
                sonuc["metin"] = f"Eklediğinizde {fiyat_yaz(ilk)} idi, şimdi %{int(round(-fark))} daha ucuz."
            elif fark >= 3:
                sonuc["karar"], sonuc["renk"] = ("sepette", "sari")
                sonuc["metin"] = f"Eklediğinizde {fiyat_yaz(ilk)} idi, şimdi %{int(round(fark))} daha pahalı."
            else:
                sonuc["karar"], sonuc["renk"] = ("gecerli", "gri")
                sonuc["metin"] = f"Eklediğinizden beri ({fiyat_yaz(ilk)}) fiyat değişmedi."
        p = None
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
        # Liste fiyatı paylaşılandan yüksek ama sayfadaki sepet indirimiyle paylaşılan fiyata iniyorsa fırsat sürüyor
        if sepet_t and sonuc.get("karar") in ("bitti", "sepette", "sepet_yok") and sepet_t <= p * 1.03:
            sonuc["karar"], sonuc["renk"] = ("kampanya", "yesil")
            sonuc["metin"] = (f"Sayfada {fiyat_yaz(sayfa)} görünüyor ama sepet indirimi etiketiyle sepette yaklaşık {fiyat_yaz(sepet_t)} — "
                              f"paylaşılan {fiyat_yaz(p)} hâlâ geçerli görünüyor; ödemeden önce sepet tutarını doğrulayın.")
    if gk:
        oz = gk["ozet"]
        sonuc["kayit"] = {"en_dusuk": oz["en_dusuk"], "ortalama": oz["ortalama"], "adet": oz["adet"], "ilk_ts": oz["ilk_ts"]}
        if sayfa <= oz["en_dusuk"] * 1.01:
            sonuc["gercek"] = "Mağaza fiyatı, kanalların kaydettiği en düşük fiyat seviyesinde — gerçek bir indirim."
        elif sayfa < oz["ortalama"] * 0.97:
            sonuc["gercek"] = f"Mağaza fiyatı kayıtlı ortalamanın %{int(round((oz['ortalama'] - sayfa) / oz['ortalama'] * 100))} altında."
        else:
            sonuc["gercek"] = f"Mağaza fiyatı kayıtlı ortalamanın altında değil ({fiyat_yaz(oz['ortalama'])}); acele etmeyin."
        # 30 gün ölçütü: yönetmeliğe göre "indirim" son 30 günün en düşük fiyatına göre anlamlıdır
        og = gk.get("otuz_gun")
        if og:
            if sayfa <= og["en_dusuk"] * 1.005:
                sonuc["otuz_gun"] = f"✔ Son 30 günde görülen en düşük fiyat ({og['adet']} kayıt) — indirim gerçek."
            else:
                sonuc["otuz_gun"] = (f"Son 30 günde {fiyat_yaz(og['en_dusuk'])} görüldü; mağazadaki fiyat bunun "
                                     f"%{int(round((sayfa - og['en_dusuk']) / og['en_dusuk'] * 100))} üstünde.")
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


AKAKCE_URUN_RE = re.compile(r'href="((?:https://www\.akakce\.com)?/[a-z0-9-]+/en-ucuz-[a-z0-9-]+-fiyati,(\d+)\.html)"', re.I)


def akakce_coz(h):
    """Akakçe liste sayfasındaki ürün kartları: ad, en ucuz fiyat, diğer satıcılara göre fark (TL ve %)."""
    eslesmeler = list(AKAKCE_URUN_RE.finditer(h))
    urunler, gorulen = [], set()
    for i, m in enumerate(eslesmeler):
        uid = m.group(2)
        if uid in gorulen:
            continue
        son = eslesmeler[i + 1].start() if i + 1 < len(eslesmeler) else min(len(h), m.end() + 4000)
        # Aynı ürünün ardışık bağlantıları (resim + ad) tek kart sayılır
        j = i + 1
        while j < len(eslesmeler) and eslesmeler[j].group(2) == uid:
            j += 1
        son = eslesmeler[j].start() if j < len(eslesmeler) else min(len(h), m.end() + 4000)
        parca = h[max(0, m.start() - 600):son]
        bas = h[m.start():son]
        ad = None
        for rx in (r'title="([^"]{6,200})"', r'alt="([^"]{6,200})"'):
            t = re.search(rx, bas)
            if t:
                ad = html.unescape(t.group(1)).strip()
                break
        bas = re.sub(r"<!--.*?-->", "", bas, flags=re.S)   # React yorumları (<!-- -->) fiyatın ortasına giriyor
        # Akakçe fiyatı "27.599<i>,00 TL</i>" diye böler: kuruş kısmını ana sayıya yapıştır (yoksa yalnız "00 TL" okunur)
        bas = re.sub(r"(\d)\s*<i\b[^>]*>\s*,\s*(\d{1,3})", r"\1,\2", bas)
        metin = html.unescape(re.sub(r"<[^>]+>", " \n ", bas)).replace("\xa0", " ")   # etiketler arası boşluk: sayılar birleşmesin
        metin = "\n".join(re.sub(r"[ \t]+", " ", x).strip() for x in metin.split("\n") if x.strip())
        if not ad:   # bağlantının kendi yazısı (ör. <a ...><b>Ürün adı</b></a>)
            t = re.match(r"[^>]*>(.*?)</a>", bas, re.S | re.I)
            t = html_metne(t.group(1)) if t else ""
            if len(t) >= 6 and not re.search(r"\d\s*TL", t):
                ad = t.split("\n")[0].strip()
        if not ad:
            satir = [x.strip() for x in metin.split("\n") if len(x.strip()) >= 6 and not re.search(r"\d\s*TL|%\s*\d|FİYAT", x)]
            ad = satir[0] if satir else None
        # Fiyat yalnızca fiyat kutusundan (pt_v8 / pt_v9) okunur. "3.400 TL fark" rozeti fiyat DEĞİLDİR:
        # eski sürüm kuruşu ayrı yazılan fiyatı okuyamayıp bu fark tutarını ürün fiyatı sanıyordu.
        fiyat = fark = None
        fk = re.search(r'class="pt_v\d+[^"]*"[^>]*>(.*?)(?:<span|</span>)', bas, re.S)
        if fk:
            f_ = re.search(r"(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,3}))?\s*TL", html_metne(fk.group(1)).replace("\n", " "))
            if f_:   # "1.325,995 TL" gibi 3 haneli kuruş da görülüyor: yuvarlanır
                fiyat = round(float(f_.group(1).replace(".", "") + "." + (f_.group(2) or "0")), 2)
        fr = re.search(r"(?<![\d.,])(" + SAYI + r")\s*TL\s*fark", metin, re.I)
        if fr:
            fark = tutar_(fr.group(1))
        if fiyat is None:   # yedek: kutu bulunamazsa, "fark" ve "FİYAT" sayılarını dışarıda bırakarak en büyük tutar
            tutarlar = [tutar_(m_.group(1)) for m_ in re.finditer(r"(?<![\d.,%])(" + SAYI + r")\s*TL(?!\s*fark)", metin, re.I)]
            tutarlar = [x for x in tutarlar if x and x >= 10]
            fiyat = max(tutarlar) if tutarlar else None
        if not ad or not fiyat:
            continue
        if fark is not None and not 0 < fark < fiyat:
            fark = None
        # Satıcılar arası fark oranı fark tutarından hesaplanır. Kartın "En Ucuz %X" yazısı başka bir ölçüdür.
        yuzde = round(fark / (fiyat + fark) * 100) if fark else None
        resim = re.search(r'(?:data-src|src)="(https://[^"]+\.(?:jpe?g|png|webp)[^"]*)"', parca)
        link = m.group(1)
        if link.startswith("/"):
            link = "https://www.akakce.com" + link
        gorulen.add(uid)
        urunler.append({"uid": uid, "ad": ad, "fiyat": fiyat, "fark": fark, "yuzde": yuzde, "link": link,
                        "resim": resim.group(1) if resim else None})
    return urunler


def akakce_cek(url, sayfa_sayisi):
    tum, gorulen = [], set()
    for i in range(sayfa_sayisi):
        bulunan = akakce_coz(sayfa_getir_guclu(url + (f"?p={i + 1}" if i else "")))
        if not bulunan:
            if i == 0:
                raise ValueError("Sayfada ürün bulunamadı (sayfa yapısı değişmiş ya da erişim engellenmiş olabilir)")
            break
        for u in bulunan:
            if u["uid"] not in gorulen:
                gorulen.add(u["uid"])
                tum.append(u)
        if i + 1 < sayfa_sayisi:
            time.sleep(1.5)
    return tum


def akakce_gonderi(u, listeler, tarih):
    """listeler: ürünün göründüğü Akakçe listelerinin kısa adları (fark, dip, brosur)."""
    metin = f"{u['ad']}\n{fiyat_yaz(u['fiyat'])}"
    g = gonderi_olustur(AKAKCE, int(u["uid"]), metin, [u["link"]], u["resim"], tarih, None)
    notlar = []
    if "dip" in listeler:
        notlar.append("Akakçe’nin fiyat kaydına göre son 6 ayın en ucuz fiyatında.")
    if "brosur" in listeler:
        notlar.append("Market broşürlerindeki fiyattan daha ucuza satılıyor.")
    if "takip" in listeler and u.get("takip_kelime"):
        notlar.append(f"Takip ettiğiniz “{u['takip_kelime']}” için Akakçe’deki bugünkü en ucuz fiyat (indirim olduğu anlamına gelmez).")
    if u["yuzde"] and u["fark"]:
        notlar.append(f"En ucuz satıcı diğerlerinden yaklaşık %{u['yuzde']} ({fiyat_yaz(u['fark'])}) ucuz; bu oran fiyat düşüşü değil, satıcılar arası farktır.")
    notlar.append("Satıcıyı ve kargo ücretini kontrol edin.")
    rozetler = [r for r in (g.get("rozetler") or []) if r != "dip"] + (["dip"] if "dip" in listeler else [])
    g.update(baslik=u["ad"], fiyat=u["fiyat"], eski_fiyat=None, yuzde=None, yuzde_varan=False, tur="urun",
             akakce_fark=u["yuzde"], akakce_fark_tl=u["fark"], akakce_listeler=sorted(listeler), rozetler=rozetler,
             metin=f"Akakçe ({', '.join([ad for k, _, ad, _ in AKAKCE_LISTELERI if k in listeler] + (['Takip araması'] if 'takip' in listeler else []))}). " + " ".join(notlar))
    return g


DH_KONU_RE = re.compile(r'<a\b[^>]*href="(?:https://forum\.donanimhaber\.com)?/([a-z0-9-]+)--(\d{6,})"[^>]*>(.*?)</a>', re.S | re.I)


def dhforum_coz(h):
    """Sıcak Fırsatlar konu listesi: başlığında TL fiyatı geçen konular (ana konular ve sorular hariç)."""
    konular, gorulen = [], set()
    for m in DH_KONU_RE.finditer(h):
        no = int(m.group(2))
        baslik = html_metne(m.group(3)).replace("\n", " ").strip()
        if no in gorulen or len(baslik) < 8 or not re.search(r"(?:" + SAYI + r")\s*(?:TL|₺)|₺\s*\d", baslik, re.I):
            continue
        if re.search(r"ana konu|\[ana|soru|yardım|tavsiye", baslik, re.I):
            continue
        gorulen.add(no)
        konular.append({"no": no, "baslik": baslik, "link": f"https://forum.donanimhaber.com/{m.group(1)}--{no}"})
    return konular


def dhforum_cek(sayfa_sayisi=DHFORUM_SAYFA):
    tum, gorulen = [], set()
    for i in range(sayfa_sayisi):
        bulunan = dhforum_coz(sayfa_getir_guclu(DHFORUM_URL + (f"?sayfa={i + 1}" if i else "")))
        if not bulunan and i == 0:
            raise ValueError("Forumda fiyatlı konu bulunamadı (sayfa yapısı değişmiş ya da erişim engellenmiş olabilir)")
        for k in bulunan:
            if k["no"] not in gorulen:
                gorulen.add(k["no"])
                tum.append(k)
        if i + 1 < sayfa_sayisi:
            time.sleep(1.5)
    return tum


def rss_coz(metin):
    """RSS 2.0 / Atom beslemesi → [{baslik, link, tarih, aciklama}]. DOCTYPE/ENTITY içeren besleme reddedilir
    (XML genişletme saldırısına karşı)."""
    if re.search(r"<!DOCTYPE|<!ENTITY", metin[:5000], re.I):
        raise ValueError("Besleme beklenmeyen biçimde (DOCTYPE)")
    kok = ET.fromstring(metin.encode("utf-8") if isinstance(metin, str) else metin)
    ad = lambda e: e.tag.rsplit("}", 1)[-1].lower()
    ogeler = [e for e in kok.iter() if ad(e) in ("item", "entry")]
    sonuc = []
    for e in ogeler:
        alan = {ad(c): c for c in e}
        link = (alan.get("link").text or alan.get("link").get("href") or "").strip() if "link" in alan else ""
        tarih = None
        for k in ("pubdate", "published", "updated", "date"):
            if k in alan and (alan[k].text or "").strip():
                t = alan[k].text.strip()
                try:
                    tarih = parsedate_to_datetime(t)
                except (TypeError, ValueError, IndexError):
                    tarih = zaman(t.replace("Z", "+00:00"))
                break
        aciklama = ""
        for k in ("encoded", "description", "summary", "content"):
            if k in alan and alan[k].text:
                aciklama = alan[k].text
                break
        sonuc.append({"baslik": html_metne(alan["title"].text or "").replace("\n", " ").strip() if "title" in alan else "",
                      "link": link, "tarih": tarih, "aciklama": aciklama})
    return sonuc


def dh_rss_cek():
    """DonanımHaber Sıcak Fırsatlar RSS'i. Her konudan mağaza bağlantısı (varsa) çıkarılır: doğrulayıcı onu açar."""
    hatalar = []
    for adres in DH_RSS_ADRESLERI:
        s = magaza_sayfasi_getir(adres, zaman_asimi=20, tarayici=False)
        if s["durum"] != "ok":
            hatalar.append(f"RSS {s['durum']} ({s.get('kod') or s.get('hata') or ''})")
            continue
        try:
            ogeler = rss_coz(s["html"])
        except (ET.ParseError, ValueError) as e:
            hatalar.append("RSS okunamadı: " + str(e)[:60])
            continue
        konular, gorulen = [], set()
        for o in ogeler:
            m = re.search(r"--(\d{5,})", o["link"]) or re.search(r"(\d{6,})", o["link"])
            if not m or len(o["baslik"]) < 6:
                continue
            no = int(m.group(1))
            if no in gorulen:
                continue
            gorulen.add(no)
            magaza_link = None
            for l in re.findall(r'href="(https?://[^"]+)"', html.unescape(o["aciklama"] or "")):
                if magaza_alani(l):
                    magaza_link = l
                    break
            konular.append({"no": no, "baslik": o["baslik"], "link": o["link"], "magaza_link": magaza_link,
                            "tarih": o["tarih"]})
        if konular:
            return konular
        hatalar.append("RSS boş")
    raise ValueError("; ".join(hatalar) or "RSS adresi yok")


def dh_cek():
    """Önce RSS, olmazsa forum sayfası. Hangisinden okunduğu kaynak notuna yazılır."""
    try:
        return dh_rss_cek(), None
    except Exception as e:
        rss_hata = hata_metni(e)
    return dhforum_cek(), f"RSS kullanılamadı, forum sayfası okundu ({rss_hata})"


def dhforum_gonderi(k, tarih):
    tarih = k.get("tarih") or tarih
    linkler = [k["magaza_link"]] if k.get("magaza_link") else []
    g = gonderi_olustur(DHFORUM, k["no"], k["baslik"], linkler, None, tarih, None)
    g["forum_link"] = k["link"]
    if not k.get("magaza_link"):
        g["urun_link"] = k["link"]
    g["metin"] = k["baslik"] + "\n\nDonanımHaber’de paylaşılan fırsat" + \
        ("; ürün bağlantısı konudan alındı, fiyat mağazada doğrulanır." if k.get("magaza_link") else
         "; ürün bağlantısı ve kullanıcı yorumları konunun içinde. Yorumlarda fiyatın hâlâ geçerli olup olmadığına bakın.")
    return g


def akakce_karsilastir(baslik, fiyat, gtin=None):
    """Ürünü Akakçe aramasında bulur, en ucuz satıcı fiyatını döndürür. Emin olunamazsa None (yanlış eşleşme yerine hiç).
    Eşleşme: başlıktaki model kodu Akakçe adında geçmeli; model kodu yoksa kelime benzerliği ≥ %60."""
    if not baslik or not fiyat:
        return None
    sorgu = arama_sorgusu(baslik)
    s = magaza_sayfasi_getir("https://www.akakce.com/arama/?q=" + urllib.parse.quote(sorgu), tarayici=False)
    if s["durum"] != "ok":
        return {"durum": s["durum"], "ts": int(time.time())}
    model = model_kodu(baslik)
    en, en_puan = None, 0
    for u in akakce_coz(s["html"])[:15]:
        if not 0.4 <= u["fiyat"] / fiyat <= 2.5:      # aksesuar / farklı paket gibi alakasız sonuçlar
            continue
        if farkli_varyant(baslik, u["ad"]):           # S26 FE ararken S26 / S26 Ultra / S25 FE eşleşmesin
            continue
        ad_ = (u["ad"] or "").upper().replace(" ", "")
        if model and model.replace("-", "") in ad_.replace("-", ""):
            puan = 2 + _benzerlik(baslik, u["ad"])
        elif not model and _benzerlik(baslik, u["ad"]) >= 0.6:
            puan = _benzerlik(baslik, u["ad"])
        else:
            continue
        if puan > en_puan:
            en, en_puan = u, puan
    if not en:
        return {"durum": "bulunamadi", "sorgu": sorgu, "ts": int(time.time())}
    fark = round((fiyat - en["fiyat"]) / en["fiyat"] * 100, 1)
    return {"durum": "ok", "en_ucuz": en["fiyat"], "ad": en["ad"], "link": en["link"], "fark": fark,
            "eslesme": "model kodu" if en_puan >= 2 else "başlık benzerliği", "ts": int(time.time())}


MARKET_API = "https://api.marketfiyati.org.tr/api/"
MARKET_YOLLAR = ("v2/nearest", "v2/search", "v3/searchByCategories", "v3/price-history")   # yalnızca bu iki uç noktaya aracılık edilir


def market_istegi(yol, govde):
    """Market sekmesi için Market Fiyatı (TÜBİTAK) aracılığı. Tarayıcı doğrudan ulaşamazsa (ör. ağ kısıtı)
    arayüz isteği buraya gönderir. Yalnızca izinli yollar ve sınırlı alanlar iletilir."""
    if yol not in MARKET_YOLLAR or not isinstance(govde, dict):
        raise ValueError("Geçersiz market isteği")
    izinli = {"latitude", "longitude", "distance", "pages", "size", "depots", "offer_discount", "menu_category",
              "main_category", "market_names", "uniqueId", "keywords"}
    temiz = {k: v for k, v in govde.items() if k in izinli}
    if "size" in temiz:
        temiz["size"] = max(1, min(int(temiz["size"]), 25))
    if isinstance(temiz.get("depots"), list):
        temiz["depots"] = [str(x)[:40] for x in temiz["depots"][:80]]
    istek = urllib.request.Request(MARKET_API + yol, data=json.dumps(temiz).encode("utf-8"), method="POST",
                                   headers={"Content-Type": "application/json", "Accept": "application/json",
                                            "User-Agent": "KasimFirsatlari/" + SURUM})
    with urllib.request.urlopen(istek, timeout=25) as yanit:
        return json.loads(yanit.read().decode("utf-8", "replace"))


def oyun_cek():
    istek = urllib.request.Request(OYUN_URL, headers={"User-Agent": "KasimFirsatlari/" + SURUM, "Accept": "application/json"})
    with urllib.request.urlopen(istek, timeout=25) as yanit:
        veri = json.loads(yanit.read().decode("utf-8", "replace"))
    if not isinstance(veri, list):
        raise ValueError("Oyun servisi beklenmeyen yanıt verdi")
    sonuc, gorulen = [], set()
    for d in veri:
        try:
            indirim = float(d.get("savings") or 0)
            satis, normal = float(d["salePrice"]), float(d["normalPrice"])
        except (KeyError, TypeError, ValueError):
            continue
        anahtar = str(d.get("gameID") or d.get("dealID"))
        if indirim < OYUN_EN_AZ_INDIRIM or not d.get("dealID") or anahtar in gorulen:
            continue
        gorulen.add(anahtar)
        sonuc.append({"no": int(d["gameID"]) if str(d.get("gameID", "")).isdigit() else abs(hash(anahtar)) % 10**9,
                      "baslik": str(d.get("title") or "").strip()[:150], "satis": satis, "normal": normal,
                      "indirim": round(indirim), "magaza": OYUN_MAGAZA.get(str(d.get("storeID")), "PC mağazası"),
                      "link": "https://www.cheapshark.com/redirect?dealID=" + urllib.parse.quote(str(d["dealID"])),
                      "resim": d.get("thumb") if str(d.get("thumb", "")).startswith("https://") else None})
    return sonuc


def oyun_gonderi(o, tarih):
    baslik = f"{o['baslik']} — {o['magaza']}’da %{o['indirim']} indirim"
    g = gonderi_olustur(OYUN, o["no"], baslik, [], o["resim"], tarih, None)
    g.update(baslik=baslik, tur="kampanya", fiyat=None, eski_fiyat=None, yuzde=float(o["indirim"]), yuzde_varan=False,
             urun_link=o["link"], kategori="oyun" if "oyun" in KATEGORI_AD else g.get("kategori"),
             metin=(f"{o['baslik']}: {o['magaza']} ABD fiyatı ${o['satis']:.2f} (normali ${o['normal']:.2f}). "
                    "Türkiye’deki fiyat farklı olabilir; indirim oranı aynı mağazanın kendi fiyatına göredir. Kaynak: CheapShark."))
    return g


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

KORUNAN_ALANLAR = ("dogrulama", "akakce_dogrulama", "magaza_basligi", "gtin")


def zayif_baslik(b):
    return len(b or "") < 12 or (b or "").startswith("(") or not re.search(r"[A-Za-zÇĞİÖŞÜçğıöşü]{3}", b or "")


def baglanti_farkli_urun(g, s_):
    """Paylaşımdaki bağlantı başlıkta yazandan farklı bir ürüne gidiyorsa açıklama metni, değilse None.
    (Ör. başlık "Galaxy S26 FE 256 GB", bağlantı "Galaxy S26 Ultra" sayfasını açıyor.)"""
    b, mb = g.get("baslik"), (s_ or {}).get("baslik")
    if not mb or zayif_baslik(b) or g.get("kanal") == TAKIBIM or not farkli_varyant(b, mb):
        return None
    return f"Bağlantı başka bir ürüne gidiyor: mağazada “{mb[:120]}” açılıyor. Paylaşılan fiyat bu ürün için geçerli değil."


def zenginlestirmeyi_koru(g, eski):
    """Kaynak yeniden okunduğunda paylaşım baştan oluşturulur; doğrulayıcının eklediği bilgiler kaybolmasın."""
    for k in KORUNAN_ALANLAR:
        if eski.get(k) and not g.get(k):
            g[k] = eski[k]
    if eski.get("resim") and not g.get("resim"):
        g["resim"] = eski["resim"]
    if g.get("magaza_basligi") and zayif_baslik(g.get("baslik")):
        g["baslik"] = g["magaza_basligi"]


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
        if [x.lower() for x in ayar["kanallar"]] in ESKI_VARSAYILAN_KANALLAR:
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
            "arama_arsivi": v.get("arama_arsivi") or {},   # id -> [ts, başlık, fiyat, mağaza, tür, kodlar]
            "derin": v.get("derin") or {},                 # kanal -> geriye doğru okumanın kaldığı yer
        }
        self.veri["canli"] = v.get("canli") or {}
        self.veri["canli_son"] = {k: x for k, x in (v.get("canli_son") or {}).items()
                                  if time.time() - x.get("ts", 0) < 6 * 3600 and x.get("surum") == SURUM}
        self.veri["kuyruk"] = v.get("kuyruk") or {}              # Aşama 2'yi bekleyen ürünler: id -> {t, n, s}
        self.veri["engel"] = {k: t for k, t in (v.get("engel") or {}).items() if time.time() - t < ENGEL_BEKLE_SN}
        self.veri["akakce_dogrulama"] = {k: x for k, x in (v.get("akakce_dogrulama") or {}).items()
                                         if time.time() - x.get("ts", 0) < AKAKCE_ONBELLEK_SN}
        self.veri["akakce_takip"] = v.get("akakce_takip") or {}   # takip kelimesi -> {ts, sorgu, urunler}
        self.veri["akakce_cozucu"] = v.get("akakce_cozucu")
        self._akakce_hatali_kayitlari_temizle()
        self.canli_son_istek = {}
        self.canli_kilit = threading.Lock()
        self.dogrulama_tetik = threading.Event()
        self.dogrulama_kilit = threading.Lock()
        self._eski_kayitlari_yeniden_coz()

    def _akakce_hatali_kayitlari_temizle(self):
        """3.4 ve öncesi Akakçe okuyucusu "3.400 TL fark" rozetini ürün fiyatı sanıyordu (ör. 48.400 TL'lik telefon 4.840 TL).
        O sürümün kaydettiği Akakçe ürünleri, fiyat geçmişi girdileri ve Akakçe karşılaştırmaları bir kez silinir;
        sonraki yenilemede doğru fiyatla yeniden okunur."""
        if self.veri.get("akakce_cozucu") == AKAKCE_COZUCU:
            return
        gon = self.veri["gonderiler"]
        for gid in [k for k, g in gon.items() if g.get("kanal") == AKAKCE]:
            del gon[gid]
        for anahtar in list(self.veri["gecmis"]):
            kayit = [x for x in self.veri["gecmis"][anahtar] if not (len(x) > 2 and str(x[2]).startswith(AKAKCE + "/"))]
            if kayit:
                self.veri["gecmis"][anahtar] = kayit
            else:
                del self.veri["gecmis"][anahtar]
        for k in [k for k in self.veri["arama_arsivi"] if k.startswith(AKAKCE + "/")]:
            del self.veri["arama_arsivi"][k]
        self.veri["akakce_dogrulama"] = {}   # eski okuyucu arama sonuçlarında fiyat bulamıyordu ("bulunamadı")
        for g in gon.values():
            if (g.get("akakce_dogrulama") or {}).get("durum") != "ok":
                g.pop("akakce_dogrulama", None)
        self.veri["akakce_takip"] = {}
        self.veri["akakce_cozucu"] = AKAKCE_COZUCU

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
                            else:
                                zenginlestirmeyi_koru(g, eski)
                            self.veri["gonderiler"][g["id"]] = g
                        self.veri["kaynaklar"][kanal] = {"ok": True, "ad": ad, "adet": len(gonderiler),
                                                         "zaman": zaman_s, "hata": None}
                except Exception as e:  # bir kanal hata verirse diğerleri devam etsin
                    with self.kilit:
                        onceki = self.veri["kaynaklar"].get(kanal) or {}
                        self.veri["kaynaklar"][kanal] = {"ok": False, "ad": onceki.get("ad"), "adet": 0,
                                                         "zaman": zaman_s, "hata": hata_metni(e)}
                time.sleep(0.8)
            yeni_idler += self._dis_kaynaklari_yenile()
            with self.kilit:
                self._kuyruga_al(yeni_idler)
                self._bayatlari_kuyruga_al()
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
        self.dogrulama_tetik.set()             # Aşama 2 (doğrulayıcı) yeni kuyruğu işlemeye başlasın
        takip_yeni = self._takipleri_ara()
        with self.kilit:
            self._kuyruga_al(takip_yeni)
        yeni_idler += takip_yeni
        self._takibimi_guncelle()
        self._bildir(set(yeni_idler), ilk_calisma)
        self._kacirilmazlari_dogrula()
        if self.ayarlar.get("gecmis_tamamla", True):
            self._arsivi_tamamla(ARSIV_OTO_ADET)
        self._geriye_tara()

    def _dis_kaynak_yaz(self, kanal, gonderiler, ok_adet, hata, tut=lambda g: False):
        """Bir dış kaynağın son okumasını kaydeder. Artık listede olmayan kayıtlar kaldırılır
        (tut(g) True dönenler hariç: ör. okunamayan bir Akakçe listesinden gelenler). Bildirim gönderilmez."""
        zaman_s = simdi().isoformat()
        ilk_kez = []
        with self.kilit:
            gon = self.veri["gonderiler"]
            if gonderiler is not None:
                yeni = set()
                for g in gonderiler:
                    eski = gon.get(g["id"])
                    if eski:   # ilk görüldüğü zaman ve doğrulama sonucu korunur
                        g["tarih"], g["ts"] = eski["tarih"], eski["ts"]
                        zenginlestirmeyi_koru(g, eski)
                    else:
                        ilk_kez.append(g["id"])
                    gon[g["id"]] = g
                    yeni.add(g["id"])
                for gid in [k for k, v in gon.items() if v.get("kanal") == kanal and k not in yeni and not tut(v)]:
                    del gon[gid]
            onceki = self.veri["kaynaklar"].get(kanal) or {}
            self.veri["kaynaklar"][kanal] = {"ok": gonderiler is not None, "ad": DIS_KAYNAKLAR[kanal],
                                             "adet": ok_adet if gonderiler is not None else onceki.get("adet", 0) if hata is None else 0,
                                             "zaman": zaman_s, "hata": hata}
        return ilk_kez

    def _dis_kaynaklari_yenile(self):
        """Akakçe listeleri, DonanımHaber, oyun indirimleri. İlk kez görülen paylaşımların id'lerini döndürür."""
        yeni = []
        if self.ayarlar.get("akakce", True):
            bulunan, hatalar, okunan = {}, [], set()
            for kisa, url, ad, sayfa in AKAKCE_LISTELERI:
                try:
                    for u in akakce_cek(url, sayfa):
                        b = bulunan.setdefault(u["uid"], {"u": u, "l": set()})
                        b["l"].add(kisa)
                        if u.get("fark") and not b["u"].get("fark"):
                            b["u"] = dict(u, fiyat=b["u"]["fiyat"])
                    okunan.add(kisa)
                except Exception as e:
                    hatalar.append(f"{ad}: {hata_metni(e)}")
                time.sleep(1)
            # Takip kelimeleri Akakçe'de de aranır: aranan ürünün bugünkü en ucuz fiyatları "Takibim" sekmesine gelir
            takip_bulunan = self._akakce_takip_ara(hatalar)
            for u in takip_bulunan:
                b_ = bulunan.setdefault(u["uid"], {"u": u, "l": set()})
                b_["l"].add("takip")
                b_["u"] = dict(b_["u"], takip_kelime=u.get("takip_kelime"))
            if self.ayarlar.get("takip"):
                okunan.add("takip")
            if okunan:
                gl = [akakce_gonderi(b["u"], b["l"], simdi()) for b in bulunan.values()]
                self._dis_kaynak_yaz(AKAKCE, gl, len(gl), "; ".join(hatalar) or None,
                                     tut=lambda g: bool(set(g.get("akakce_listeler") or []) - okunan))
            else:
                self._dis_kaynak_yaz(AKAKCE, None, 0, "; ".join(hatalar))
        if self.ayarlar.get("dhforum", VARSAYILAN_AYARLAR["dhforum"]):
            try:
                konular, not_ = dh_cek()
                gl = [dhforum_gonderi(x, simdi()) for x in konular]
                yeni += self._dis_kaynak_yaz(DHFORUM, gl, len(gl), not_)
                if not_:   # forum yedeğiyle okunduysa kaynak "tamam" ama notu görünsün
                    self.veri["kaynaklar"][DHFORUM]["ok"] = True
            except Exception as e:
                self._dis_kaynak_yaz(DHFORUM, None, 0, hata_metni(e))
        if self.ayarlar.get("oyun", True):
            try:
                gl = [oyun_gonderi(x, simdi()) for x in oyun_cek()]
                self._dis_kaynak_yaz(OYUN, gl, len(gl), None)
            except Exception as e:
                self._dis_kaynak_yaz(OYUN, None, 0, hata_metni(e))
        return yeni

    # --- Aşama 2: doğrulayıcı ve zenginleştirici ----------------------------
    # Aşama 1 (yenile) kanallardan ve dış kaynaklardan gelen ürünleri kuyruğa yazar. Doğrulayıcı kuyruktaki
    # her ürünün mağaza sayfasını açar: ad, anlık fiyat, görsel ve stok teyit edilir; fiyat tutmuyorsa ya da
    # stok bittiyse ürün "red" olur ve listeden çıkar. Okunamayan mağaza ürünü listeden atmaz, "doğrulanamadı" der.
    def _dogrulanabilir(self, g):
        link = g.get("urun_link") or ""
        return bool(g.get("tur") == "urun" and g.get("fiyat") and link and guvenli_dis_adres(link)
                    and g.get("kanal") not in (AKAKCE, OYUN, TAKIBIM)
                    and "donanimhaber.com" not in (urllib.parse.urlparse(link).hostname or "")
                    and "ikinci_el" not in (g.get("rozetler") or [])
                    and time.time() - g.get("ts", 0) < DOGRULAMA_YAS_SN)

    def _kuyruga_al(self, idler):
        """(kilit altında çağrılır) Doğrulanabilir yeni ürünleri kuyruğa ekler."""
        kuyruk, gon = self.veri["kuyruk"], self.veri["gonderiler"]
        for gid in idler:
            g = gon.get(gid)
            if g and gid not in kuyruk and not g.get("dogrulama") and self._dogrulanabilir(g):
                kuyruk[gid] = {"t": int(time.time()), "n": 0, "s": 0}

    def _bayatlari_kuyruga_al(self, en_fazla=20):
        """(kilit altında) Kanaldan okunup hiç kuyruğa girmemiş ürünler ile DOGRULAMA_TAZE_SN'den eski
        onayları yeniden kuyruğa alır: fiyat sonradan yükselmiş ya da stok bitmiş olabilir."""
        kuyruk, eklenen = self.veri["kuyruk"], 0
        for g in sorted(self.veri["gonderiler"].values(), key=lambda g: -g.get("ts", 0)):
            if eklenen >= en_fazla:
                break
            if g["id"] in kuyruk or not self._dogrulanabilir(g):
                continue
            d = g.get("dogrulama")
            if d is None or (d.get("durum") in ("onayli", "kosullu") and time.time() - d.get("ts", 0) > DOGRULAMA_TAZE_SN):
                kuyruk[g["id"]] = {"t": int(time.time()), "n": 0, "s": 0}
                eklenen += 1

    def _nazik_bekle(self, anahtar, aralik):
        """Aynı siteye art arda istek atmamak için: son istekten bu yana 'aralik' saniye geçene kadar bekler."""
        with self.canli_kilit:
            bekle = self.canli_son_istek.get(anahtar, 0) + aralik - time.time()
            if bekle > 0:
                time.sleep(bekle)
            self.canli_son_istek[anahtar] = time.time()

    def dogrulama_turu(self, sure_sn=None, en_fazla=None):
        """Kuyruktaki ürünleri sırayla doğrular (yüksek indirimli ve yeni olan önce). İşlenen ürün sayısını döndürür."""
        if not self.dogrulama_kilit.acquire(blocking=False):
            return 0                     # başka bir tur zaten çalışıyor
        bas, islenen = time.time(), 0
        try:
            while (not sure_sn or time.time() - bas < sure_sn) and (not en_fazla or islenen < en_fazla):
                with self.kilit:
                    kuyruk, gon, an = self.veri["kuyruk"], self.veri["gonderiler"], time.time()
                    for gid in [i for i, k in kuyruk.items() if i not in gon or not self._dogrulanabilir(gon[i])]:
                        del kuyruk[gid]
                    hazir = [i for i, k in kuyruk.items() if k.get("s", 0) <= an]
                    if not hazir:
                        break
                    gid = min(hazir, key=lambda i: (-int(gon[i].get("yuzde") or 0) // 10, -gon[i].get("ts", 0)))
                    zorla = kuyruk[gid]["n"] > 0 or bool(gon[gid].get("dogrulama"))
                try:
                    s_ = self.canli_kontrol(gid, zorla=zorla)
                except Exception as e:
                    s_ = {"durum": "hata", "mesaj": hata_metni(e)}
                if self._dogrulama_isle(gid, s_):
                    try:
                        self._akakce_dogrula(gid)
                    except Exception:
                        pass
                islenen += 1
        finally:
            self.dogrulama_kilit.release()
        if islenen:
            with self.kilit:
                try:
                    json_yaz(VERI_DOSYASI, self.veri)
                except OSError:
                    pass
        return islenen

    def _dogrulama_isle(self, gid, s_):
        """Mağaza okumasının sonucunu ürüne yazar, kuyruğu günceller. Akakçe karşılaştırması gerekiyorsa True."""
        with self.kilit:
            g, kuyruk = self.veri["gonderiler"].get(gid), self.veri["kuyruk"]
            k = kuyruk.get(gid) or {"n": 0}
            if not g:
                kuyruk.pop(gid, None)
                return False
            durum, an = s_.get("durum"), int(time.time())
            sonuc = {"ts": an, "motor": s_.get("motor"), "son_url": s_.get("son_url"),
                     "liste_fiyati": s_.get("liste_fiyati")}
            yeniden = None
            farkli = baglanti_farkli_urun(g, s_)
            if durum == "ok":
                sonuc["fiyat"] = s_.get("fiyat")
                if farkli:
                    sonuc.update(durum="red", neden=farkli)
                elif s_.get("stok_yok"):
                    sonuc.update(durum="red", neden="Mağazada stokta yok")
                elif s_.get("karar") in ("bitti", "sepet_yok"):
                    sonuc.update(durum="red", neden=s_.get("metin") or "Mağazadaki fiyat paylaşılanla uyuşmuyor")
                elif s_.get("karar") == "sepette":
                    sonuc.update(durum="kosullu", neden=s_.get("metin") or "Fiyat yalnızca sepette/kodla oluşuyor")
                else:
                    sonuc.update(durum="onayli", neden=s_.get("metin") or "Fiyat mağazada doğrulandı")
            elif durum == "stok_yok":
                sonuc.update(durum="red", neden="Mağazada stokta yok")
            elif durum == "engellendi":
                sonuc.update(durum="okunamadi", neden="Mağaza otomatik okumaya izin vermedi")
            else:
                sonuc.update(durum="okunamadi", neden=s_.get("mesaj") or "Mağaza sayfası okunamadı")
                if durum == "hata" and k["n"] + 1 < DOGRULAMA_DENEME:
                    yeniden = an + 20 * 60 * (k["n"] + 1)        # geçici ağ hatası: 20 / 40 dk sonra yeniden dene
            g["dogrulama"] = sonuc
            # Zenginleştirme: mağazadaki ad, görsel ve barkod
            if farkli:
                pass                                   # başka ürünün adı, görseli ve barkodu karta yazılmaz
            elif s_.get("baslik"):
                g["magaza_basligi"] = s_["baslik"]
                if zayif_baslik(g.get("baslik")):
                    g["baslik"] = s_["baslik"]
            if s_.get("resim") and not g.get("resim") and not farkli:
                g["resim"] = s_["resim"]
            if s_.get("gtin") and not g.get("gtin") and not farkli:
                g["gtin"] = s_["gtin"]
            if yeniden:
                kuyruk[gid] = {"t": k.get("t", an), "n": k["n"] + 1, "s": yeniden}
            else:
                kuyruk.pop(gid, None)
            return sonuc["durum"] in ("onayli", "kosullu") and bool(self.ayarlar.get("akakce_dogrula", True))

    def _akakce_dogrula(self, gid):
        """Doğrulanan ürünü Akakçe'deki en ucuz satıcı fiyatıyla karşılaştırır (ürün başına 12 saat önbellek)."""
        with self.kilit:
            g = self.veri["gonderiler"].get(gid)
            if not g:
                return
            anahtar = g.get("gtin") or g.get("anahtar") or gid
            on = self.veri["akakce_dogrulama"].get(anahtar)
            fiyat, baslik = g["fiyat"], g.get("magaza_basligi") or g["baslik"]
        # Eski sürümün yanlış eşleştirdiği kayıt (ör. S26 FE için S26 Ultra) önbellekten kullanılmaz
        if on and on.get("ad") and farkli_varyant(baslik, on["ad"]):
            on = None
        if not on or time.time() - on.get("ts", 0) > AKAKCE_ONBELLEK_SN:
            self._nazik_bekle("akakce", 2.0)
            on = akakce_karsilastir(baslik, fiyat, g.get("gtin"))
            if on is None:
                return
        with self.kilit:
            self.veri["akakce_dogrulama"][anahtar] = on
            g = self.veri["gonderiler"].get(gid)
            if g is not None:
                g["akakce_dogrulama"] = dict(on, fark=round((fiyat - on["en_ucuz"]) / on["en_ucuz"] * 100, 1)) \
                    if on.get("durum") == "ok" else on

    def dogrulayici_dongu(self):
        """Yerel modda ayrı iş parçacığı: yenileme bitince (ya da 2 dakikada bir) kuyruğu işler."""
        while True:
            self.dogrulama_tetik.wait(timeout=120)
            self.dogrulama_tetik.clear()
            try:
                self.dogrulama_turu()
            except Exception as e:
                print("Uyarı: doğrulama turu yarıda kaldı:", hata_metni(e), file=sys.stderr)

    def dogrulama_ozeti(self):
        say = {"onayli": 0, "kosullu": 0, "red": 0, "okunamadi": 0}
        for g in self.veri["gonderiler"].values():
            d = g.get("dogrulama")
            if d and d.get("durum") in say and time.time() - g.get("ts", 0) < DOGRULAMA_YAS_SN:
                say[d["durum"]] += 1
        engelli = sorted(MAGAZA_AD.get(k, k) for k, t in self.veri["engel"].items() if time.time() - t < ENGEL_BEKLE_SN)
        return dict(say, kuyruk=len(self.veri["kuyruk"]), motorlar=motor_ozeti(), engelli=engelli)

    # --- geriye doğru tarama ve arama arşivi ---------------------------------
    def _arsive_ekle(self, liste):
        ar = self.veri["arama_arsivi"]
        for g in liste:
            ar[g["id"]] = [g["ts"], g.get("baslik") or "", g.get("fiyat"), g.get("magaza"), g.get("tur"),
                           list(g.get("kodlar") or [])[:3]]

    def _eskileri_isle(self, liste):
        """Geriye doğru okunan paylaşımlar: saklama süresindekiler listeye, fiyatlılar fiyat geçmişine, hepsi arşive."""
        ssinir = int((simdi() - timedelta(days=self.ayarlar["saklama_gun"])).timestamp())
        gsinir = int((simdi() - timedelta(days=ARSIV_GUN)).timestamp())
        gonderiler, gecmis = self.veri["gonderiler"], self.veri["gecmis"]
        for g in liste:
            if g["ts"] >= ssinir:
                gonderiler.setdefault(g["id"], g)
            elif g.get("fiyat") and g.get("anahtar") and g["ts"] >= gsinir:
                kayit = gecmis.setdefault(g["anahtar"], [])
                if all(k[2] != g["id"] for k in kayit):
                    kayit.append([g["ts"], g["fiyat"], g["id"]])
                    kayit.sort()
        self._arsive_ekle(liste)

    def _geriye_tara(self, sure_sn=GERIYE_TARAMA_SN):
        """Her yenilemede kanalların daha eski sayfalarından biraz daha okur. Böylece liste saklama süresi
        boyunca dolar, aramada da son ARAMA_ARSIV_GUN güne ait paylaşımlar bulunur."""
        bas = time.time()
        asinir = int((simdi() - timedelta(days=ARAMA_ARSIV_GUN)).timestamp())
        with self.kilit:
            derin, sira = self.veri["derin"], []
            for k in self.ayarlar["kanallar"]:
                d = derin.get(k.lower())
                if d is None:
                    nolar = [g["no"] for g in self.veri["gonderiler"].values() if g["kanal"].lower() == k.lower()]
                    if not nolar:
                        continue
                    d = derin[k.lower()] = {"once": min(nolar), "bitti": False}
                if not d.get("bitti"):
                    sira.append(k)
        okunan = 0
        while sira and time.time() - bas < sure_sn:
            for k in list(sira):
                if time.time() - bas >= sure_sn:
                    break
                d = derin[k.lower()]
                try:
                    _, liste = sayfayi_coz(sayfa_getir(f"https://t.me/s/{k}?before={d['once']}", 15))
                except Exception:
                    sira.remove(k)          # bu sefer olmadı; sonraki yenilemede kaldığı yerden devam eder
                    continue
                okunan += 1
                with self.kilit:
                    en_kucuk = min((g["no"] for g in liste), default=None)
                    if en_kucuk is None or en_kucuk >= d["once"]:
                        d["bitti"] = True
                        sira.remove(k)
                        continue
                    d["once"] = en_kucuk
                    self._eskileri_isle(liste)
                    if min(g["ts"] for g in liste) < asinir:
                        d["bitti"] = True
                        sira.remove(k)
                time.sleep(1.2)
        if okunan:
            with self.kilit:
                self._gecmisi_guncelle()
                self._temizle()
                try:
                    json_yaz(VERI_DOSYASI, self.veri)
                except OSError:
                    pass

    def arsiv_listesi(self):
        """Ana listede olmayan eski paylaşımlar (yalnızca arama için, hafif biçimde)."""
        with self.kilit:
            kanallar = {k.lower() for k in self.ayarlar["kanallar"]}
            listede = set(self.veri["gonderiler"])
            satirlar = [[k, v[0], v[1], v[2], MAGAZA_AD.get(v[3], "Diğer"), v[4], v[5]]
                        for k, v in self.veri["arama_arsivi"].items()
                        if k not in listede and k.split("/")[0].lower() in kanallar]
        satirlar.sort(key=lambda x: -x[1])
        return {"a": satirlar}

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
        self._arsive_ekle(self.veri["gonderiler"].values())
        asinir = int((simdi() - timedelta(days=ARAMA_ARSIV_GUN)).timestamp())
        ar = {k: v for k, v in self.veri["arama_arsivi"].items() if v[0] >= asinir}
        if len(ar) > ARAMA_ARSIV_EN_FAZLA:
            ar = dict(sorted(ar.items(), key=lambda kv: kv[1][0], reverse=True)[:ARAMA_ARSIV_EN_FAZLA])
        self.veri["arama_arsivi"] = ar
        sinir = int((simdi() - timedelta(days=self.ayarlar["saklama_gun"])).timestamp())
        # Kullanıcının kendi eklediği ürünler silinene kadar kalır
        g = {k: v for k, v in self.veri["gonderiler"].items() if v.get("ts", 0) >= sinir or v.get("kanal") == TAKIBIM}
        if len(g) > EN_FAZLA_GONDERI:
            g = dict(sorted(g.items(), key=lambda kv: (kv[1].get("kanal") == TAKIBIM, kv[1].get("ts", 0)), reverse=True)[:EN_FAZLA_GONDERI])
        self.veri["gonderiler"] = g
        gsinir = int((simdi() - timedelta(days=ARSIV_GUN)).timestamp())
        # Fiyat geçmişine yanlışlıkla katılmış başka model/varyant kayıtlarını ayıkla (ör. S26 FE geçmişindeki S26 Ultra)
        basliklar = {gid: x.get("baslik") for gid, x in g.items()}
        anahtar_basligi = {}
        for x in g.values():
            if x.get("anahtar") and not zayif_baslik(x.get("baslik")):
                anahtar_basligi.setdefault(x["anahtar"], x["baslik"])
        yeni = {}
        for k, kayitlar in self.veri["gecmis"].items():
            ref = anahtar_basligi.get(k)
            if ref:
                kayitlar = [x for x in kayitlar if not farkli_varyant(
                    ref, basliklar.get(x[2]) or (self.veri["arama_arsivi"].get(x[2]) or [None, None])[1])]
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
        # Mağaza sayfasından doğrudan okunan fiyatlar da kayda girer (kanal paylaşımı olmadan görülen fiyatlar)
        magaza_kayit = sorted((int(k[0]), float(k[1])) for k in self.veri.get("canli", {}).get(g["anahtar"], [])
                              if g["ts"] - ARSIV_GUN * 86400 <= k[0] and k[1])
        # Son 30 gün (Fiyat Etiketi Yönetmeliği ölçütü): paylaşımdan önceki 30 günde görülen en düşük fiyat
        otuz = [p for t, p in tum + magaza_kayit if g["ts"] - 30 * 86400 <= t < g["ts"]]
        otuz_gun = {"en_dusuk": min(otuz), "adet": len(otuz)} if otuz else None
        tum = sorted(set(tum + [x for x in magaza_kayit if x[0] < g["ts"] - 1800]))
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
            "magaza_adet": len(magaza_kayit),
        }, "otuz_gun": otuz_gun}
        onceki = [x for x in tum if x[0] >= g["ts"] - GECMIS_GUN * 86400]
        # Paylaşımdaki "eski fiyat" (üstü çizili) son 60 günde hiç görülmediyse şişirilmiş liste fiyatı olabilir
        if g.get("eski_fiyat") and len(onceki) >= 2:
            en_yuksek_gorulen = max(p for _, p in onceki)
            if g["eski_fiyat"] > en_yuksek_gorulen * 1.15:
                sonuc["eski_fiyat_supheli"] = {"eski": g["eski_fiyat"], "gorulen": en_yuksek_gorulen,
                                               "gercek_yuzde": round((en_yuksek_gorulen - f) / en_yuksek_gorulen * 100, 1)}
        if not onceki:
            return sonuc
        sonuc["en_dusuk_onceki"] = min(p for _, p in onceki)
        # Karşılaştırma fiyatı: 3+ kayıtta ortanca (tek bir yanlış okunmuş/çoklu paket fiyatı oranı şişirmesin),
        # daha azında en yüksek kayıt. Az kayıtla %50+ "indirim" çoğunlukla farklı paket/varyanttır: oran verilmez.
        sirali = sorted(onceki, key=lambda x: x[1])
        en_buyuk = sirali[len(sirali) // 2] if len(sirali) >= 3 else sirali[-1]
        if en_buyuk[1] >= g["fiyat"] * 1.03:
            y_ = (en_buyuk[1] - g["fiyat"]) / en_buyuk[1] * 100
            if y_ >= 50 and len(onceki) < 3:
                sonuc["kayit_supheli"] = round(y_, 1)
            else:
                sonuc.update(yuzde=y_, fiyat=en_buyuk[1], ts=en_buyuk[0])
        if len(onceki) >= 2 and g["fiyat"] <= min(p for _, p in onceki) * 0.995:
            sonuc["en_dusuk"] = True
        return sonuc or None

    def firsatlar(self):
        kanallar = {k.lower() for k in self.ayarlar["kanallar"]}
        liste = []
        # Bir kanal "KASIM200 kodu bitti / çalışmıyor" diye yeni bir paylaşım yaparsa, aynı kodu içeren
        # daha eski paylaşımlar da (başka kanallardakiler dahil) geçersiz sayılır.
        olu_kodlar = {}
        for g in self.veri["gonderiler"].values():
            if g.get("bitti") and g.get("kodlar"):
                for k in g["kodlar"]:
                    olu_kodlar[k] = max(olu_kodlar.get(k, 0), g.get("ts", 0))
        for g in self.veri["gonderiler"].values():
            akk = g["kanal"] in DIS_KAYNAKLAR
            if akk and not self.ayarlar.get(g["kanal"], VARSAYILAN_AYARLAR.get(g["kanal"], True)):
                continue
            if g["kanal"].lower() not in kanallar and g["kanal"] != TAKIBIM and not akk:
                continue
            d = {k: v for k, v in g.items() if k not in ("anahtar_eski",)}
            d["link"] = g.get("urun_link") if g["kanal"] == TAKIBIM or akk else f"https://t.me/{g['id']}"
            d["magaza_ad"] = MAGAZA_AD.get(g["magaza"], "Diğer")
            d["rozetler"] = list(g.get("rozetler") or [])
            adaylar = []
            if g.get("yuzde"):
                adaylar.append((g["yuzde"], "Paylaşımda yazıyor"))
            gk = self._gecmis_karsilastir(g)
            if g.get("fiyat") and g.get("eski_fiyat"):
                if gk and gk.get("eski_fiyat_supheli"):
                    # Üstü çizili fiyat kayıtlarda hiç görülmedi: oranı gerçekten görülen en yüksek fiyata göre hesapla
                    adaylar.append((gk["eski_fiyat_supheli"]["gercek_yuzde"], "Gerçekten görülen en yüksek fiyata göre"))
                else:
                    adaylar.append(((g["eski_fiyat"] - g["fiyat"]) / g["eski_fiyat"] * 100, "Eski fiyata göre"))
            if g.get("xaly_yuzde"):
                adaylar.append((g["xaly_yuzde"], g["xaly"] + (" (paylaşılan fiyata dahil)" if g.get("sayfa_tahmini") else "")))
            if g.get("tur") == "kupon" and g.get("kupon_tutar") and g.get("kupon_limit"):
                adaylar.append((g["kupon_tutar"] / g["kupon_limit"] * 100,
                                f"Kupon: {fiyat_yaz(g['kupon_tutar'])} / {fiyat_yaz(g['kupon_limit'])} alışveriş"))
            d["onceki"] = None
            d["fiyat_gecmisi"] = gk["liste"] if gk else []
            d["fiyat_ozet"] = gk["ozet"] if gk else None
            d["otuz_gun"] = gk.get("otuz_gun") if gk else None
            d["eski_fiyat_supheli"] = gk.get("eski_fiyat_supheli") if gk else None
            # Ani %70+ düşüş: çoğu zaman yanlış okunan fiyat ya da farklı varyant → mağazada doğrulanana kadar "kontrol et"
            d["kontrol_et"] = None
            if g.get("tur") == "urun" and g.get("fiyat") and d["otuz_gun"] and d["otuz_gun"]["adet"] >= 2 \
                    and g["fiyat"] < d["otuz_gun"]["en_dusuk"] * 0.3:
                d["kontrol_et"] = (f"Fiyat son 30 günün en düşüğünden (%{int(round((1 - g['fiyat'] / d['otuz_gun']['en_dusuk']) * 100))}) "
                                   f"çok aşağıda; yanlış okunmuş ya da farklı varyant olabilir, mağazada doğrulayın.")
            if not d["kontrol_et"] and gk and gk.get("kayit_supheli") and g.get("tur") == "urun":
                d["kontrol_et"] = (f"Programın az sayıdaki fiyat kaydına göre %{int(gk['kayit_supheli'])} ucuz görünüyor; "
                                   f"bu genelde farklı paket/varyant demektir, mağazada doğrulayın.")
            d["arsiv_arandi"] = bool(g.get("anahtar") and g["anahtar"] in self.veri["arsiv_arama"])
            d["akakce"] = ("https://www.akakce.com/arama/?q=" + urllib.parse.quote(arama_sorgusu(g["baslik"]))) if g.get("fiyat") else None
            cs = self.veri["canli_son"].get(g["id"])
            d["dogrulama"] = g.get("dogrulama")
            akd = g.get("akakce_dogrulama") or {}
            d["akakce_dogrulama"] = akd if akd.get("durum") == "ok" and not farkli_varyant(
                g.get("magaza_basligi") or g["baslik"], akd.get("ad")) else None
            if (g.get("dogrulama") or {}).get("durum") == "red" and self.ayarlar.get("red_gizle", True):
                d["bitti"] = True
            if not d.get("bitti") and g.get("kodlar") and all(
                    k in olu_kodlar and olu_kodlar[k] >= g.get("ts", 0) for k in g["kodlar"]):
                d["bitti"] = True   # kodun bittiği/çalışmadığı sonradan paylaşıldı
            d["kosul"] = kosul_metni(g) if g.get("tur") == "urun" else None
            d["canli"] = cs if cs and time.time() - cs.get("ts", 0) < 6 * 3600 else None
            d["cimri"] = ("https://www.cimri.com/arama?q=" + urllib.parse.quote(arama_sorgusu(g["baslik"]))) if g.get("fiyat") else None
            d["model"] = model_kodu(g["baslik"]) if g.get("tur") == "urun" else None
            # Akakçe tarzı etiket: kayıtlı fiyatlara göre bu fiyat nerede?
            d["dusus_etiketi"] = None
            if gk and g.get("fiyat"):
                oz = gk["ozet"]
                og = gk.get("otuz_gun")
                gun = max(1, round((g["ts"] - oz["ilk_ts"]) / 86400))
                if oz["adet"] >= 2 and gun >= 7 and g["fiyat"] <= oz["en_dusuk"] * 1.001:
                    d["dusus_etiketi"] = f"Son {gun} günün en düşüğü"
                elif og and og["adet"] >= 2 and g["fiyat"] <= og["en_dusuk"] * 1.001 and not d["kontrol_et"]:
                    d["dusus_etiketi"] = "Son 30 günün en düşüğü"
                elif oz["adet"] >= 2 and oz["dusus_ort"] >= 5:
                    d["dusus_etiketi"] = f"Ortalamanın %{int(round(oz['dusus_ort']))} altında"
            # Kanal, 30 günün en düşüğünden pahalı bir fiyatı "indirim" diye paylaşmış olabilir
            d["otuz_gun_ustunde"] = None
            if g.get("tur") == "urun" and g.get("fiyat") and d["otuz_gun"] and d["otuz_gun"]["adet"] >= 2 \
                    and g["fiyat"] > d["otuz_gun"]["en_dusuk"] * 1.05 and not d["kontrol_et"]:
                d["otuz_gun_ustunde"] = d["otuz_gun"]["en_dusuk"]
            # Paylaşım "indirimli" diyor ama program bu ürünü yakın zamanda aynı ya da daha ucuz fiyata gördüyse uyar
            d["daha_once_ucuz"] = bool(gk and g.get("fiyat") and gk.get("en_dusuk_onceki") is not None
                                       and gk["en_dusuk_onceki"] <= g["fiyat"] * 1.005)
            d.update(fiyat_bilgisi(g, gk if gk and gk.get("fiyat") else None))
            # Kategori her gösterimde güncel kurallarla yeniden hesaplanır: kurallar düzelince eski kayıtlar da düzelir
            d["kategori"] = ("oyun" if g.get("kanal") == OYUN else
                             kategori_bul(g.get("magaza_basligi") or g.get("baslik") or "") if g.get("tur") == "urun"
                             else g.get("kategori") or "diger")
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
            if d["tur"] != "urun" or not d.get("anahtar") or d["kanal"] == TAKIBIM:
                # Kullanıcının kendi eklediği ürün kanal paylaşımıyla birleştirilmez; "Takibim"de hep kendi kartı kalır
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
        # Eşleştirme anahtarı: mağaza sayfasından okunan barkod (kesin) ya da başlıktaki model kodu
        sinir = time.time() - 14 * 86400
        kova = {}

        def es_anahtarlar(d):
            return [k for k in (("g:" + d["gtin"]) if d.get("gtin") else None, ("m:" + d["model"]) if d.get("model") else None) if k]
        for d in liste:
            if d["tur"] == "urun" and d.get("fiyat") and not d.get("bitti") and d["ts"] >= sinir \
                    and "ikinci_el" not in d["rozetler"]:
                for k in es_anahtarlar(d):
                    kova.setdefault(k, []).append(d)
        engelli = {k for k, t in self.veri["engel"].items() if time.time() - t < ENGEL_BEKLE_SN}
        for d in sonuc:
            d["urun_gibi"] = d["tur"] in ("kupon", "kampanya") and urun_gibi(d)
            d["teyit"] = kanal_teyidi(d) if d["tur"] == "urun" and d.get("fiyat") else None
            d["oto_okunamaz"] = d["tur"] == "urun" and d.get("magaza") in engelli
            d["kacirilmaz"] = kacirilmaz_bilgisi(d)
            d["magazalar"] = []
            esler, gorulen = [], set()
            for k in es_anahtarlar(d):
                for x in kova.get(k, []):
                    if x["id"] not in gorulen:
                        # Barkodu bilinen iki ürünün barkodu farklıysa model kodu uysa da aynı ürün değildir
                        if d.get("gtin") and x.get("gtin") and x["gtin"] != d["gtin"]:
                            continue
                        gorulen.add(x["id"])
                        esler.append(x)
            if len(esler) >= 2:
                esler.sort(key=lambda x: x["ts"], reverse=True)
                en_yeni = {}
                for x in esler:      # en yeniden eskiye
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

    # --- ürün kataloğu (mağaza / ortaklık ağı feed'leri) -----------------------
    feedler = None        # katalog.Feedler; katalog.py programın yanındaysa main() bağlar

    def katalog_ara(self, q, sirala="uygun"):
        if not self.feedler:
            return {"sonuc": [], "toplam": 0}
        sonuc, toplam = self.feedler.katalog.ara(re.sub(r"\s+", " ", q).strip()[:60], 60,
                                                 sirala if sirala in ("uygun", "ucuz", "indirim") else "uygun")
        alanlar = ("id", "baslik", "magaza_ad", "fiyat", "eski_fiyat", "yuzde", "link", "resim", "stok", "ortaklik",
                   "dip", "dusus", "izlenen_gun", "son")
        return {"sonuc": [{k: d[k] for k in alanlar} for d in sonuc], "toplam": toplam}

    def feed_islem(self, veri):
        """Ayarlar ekranından feed ekleme / kaldırma / hemen güncelleme."""
        if not self.feedler:
            raise ValueError("Katalog modülü (katalog.py) programın yanında bulunamadı")
        islem = veri.get("islem")
        if islem == "ekle":
            url = str(veri.get("url") or "").strip()
            if not guvenli_dis_adres(url):
                raise ValueError("Feed adresi https:// ile başlayan bir internet adresi olmalı")
            self.feedler.ekle(veri.get("ad"), url, bool(veri.get("ortaklik")), veri.get("sablon"))
        elif islem == "sil":
            if not self.feedler.sil(str(veri.get("kod") or "")):
                raise ValueError("Feed bulunamadı")
        elif islem == "guncelle":
            if self.feedler.calisiyor:
                raise ValueError("Güncelleme zaten sürüyor")
            threading.Thread(target=self.feedler.guncelle, kwargs={"zorla": True, "bildir": self.feed_bildir},
                             daemon=True).start()
            time.sleep(0.3)
        else:
            raise ValueError("Geçersiz istek")
        return self.feedler.ozet()

    def feed_bildir(self, f, s):
        """Feed güncellenince pencereye bir satır yazar."""
        if self.feedler_log:
            self.feedler_log(f, s)

    feedler_log = None    # katalog.log_satiri (main bağlar)

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

    def _akakce_takip_ara(self, hatalar):
        """Her takip kelimesi için Akakçe aramasındaki ilk ürünler (3 saatte bir yenilenir, arada önbellekten)."""
        with self.kilit:
            takip = list(self.ayarlar.get("takip") or [])
            onbellek = self.veri.setdefault("akakce_takip", {})
            for k in [k for k in onbellek if k not in {sade(t) for t in takip}]:
                del onbellek[k]
        sonuc = []
        for kelime in takip[:10]:
            sk = sade(kelime)
            kayit = onbellek.get(sk)
            if not kayit or time.time() - kayit.get("ts", 0) > AKAKCE_TAKIP_SN:
                sorgu = AKAKCE_TAKIP_SORGU.get(sk, kelime)
                try:
                    s_ = magaza_sayfasi_getir("https://www.akakce.com/arama/?q=" + urllib.parse.quote(sorgu), tarayici=False)
                    if s_["durum"] != "ok":
                        raise ValueError(f"Akakçe araması okunamadı ({s_['durum']})")
                    urunler = []
                    for u in akakce_coz(s_["html"]):
                        # Yalnızca gerçekten aranan ürün (ör. "Oyuncu Koltuğu Tekerleği" gibi aksesuarı ayıklamak tam olmaz,
                        # ama "Tablet Kılıfı" gibi alakasız sonuçlar elenir)
                        if takip_eslesen([kelime], {"baslik": u["ad"], "metin": ""}):
                            urunler.append(dict(u, takip_kelime=kelime))
                        if len(urunler) >= AKAKCE_TAKIP_ADET:
                            break
                    kayit = {"ts": int(time.time()), "sorgu": sorgu, "urunler": urunler}
                    with self.kilit:
                        onbellek[sk] = kayit
                    time.sleep(1)
                except Exception as e:
                    hatalar.append(f"Takip araması ({kelime}): {hata_metni(e)}")
                    if not kayit:
                        continue
            sonuc += kayit.get("urunler") or []
        return sonuc

    def _takipleri_ara(self, adet=TAKIP_ARAMA_ADET):
        """Takip edilen kelimeleri her yenilemede sırayla kanalların arama sayfasında arar; böylece kanalın
        ana akışında geride kalmış ya da okunmayan sayfalardaki ürünler de listeye girer. Yeni id'leri döndürür."""
        with self.kilit:
            takip = list(self.ayarlar.get("takip") or [])
            if not takip:
                return []
            bas = self.veri.get("takip_sira", 0) % len(takip)
            secilen = [takip[(bas + i) % len(takip)] for i in range(min(adet, len(takip)))]
            self.veri["takip_sira"] = (bas + len(secilen)) % len(takip)
            mevcut = set(self.veri["gonderiler"])
            sinir = int((simdi() - timedelta(days=self.ayarlar["saklama_gun"])).timestamp())
        yeni = []
        for q in secilen:
            try:
                idler = self.ara(q)
            except Exception:
                continue
            with self.kilit:
                yeni += [i for i in idler if i not in mevcut and (self.veri["gonderiler"].get(i) or {}).get("ts", 0) >= sinir]
                mevcut.update(idler)
            time.sleep(1.5)
        return yeni

    def durum(self):
        with self.kilit:
            kaynaklar = []
            for k in self.ayarlar["kanallar"]:
                b = self.veri["kaynaklar"].get(k) or {}
                kaynaklar.append({"kanal": k, "ad": b.get("ad") or k, "ok": b.get("ok"), "hata": b.get("hata"),
                                  "adet": b.get("adet", 0), "zaman": b.get("zaman")})
            for k, ad in DIS_KAYNAKLAR.items():
                if self.ayarlar.get(k, VARSAYILAN_AYARLAR.get(k, True)):
                    b = self.veri["kaynaklar"].get(k) or {}
                    kaynaklar.append({"kanal": k, "ad": ad, "ok": b.get("ok"), "hata": b.get("hata"),
                                      "adet": b.get("adet", 0), "zaman": b.get("zaman"), "dis": True})
            return {
                "surum": SURUM,
                "simdi": simdi().isoformat(),
                "son_guncelleme": self.veri.get("son_guncelleme"),
                "yenileniyor": self.yenileniyor,
                "ayarlar": self.ayarlar,
                "kaynaklar": kaynaklar,
                "bildirim_var": bool(shutil.which("termux-notification") or self.ayarlar.get("ntfy_konu")),
                "termux_bildirim": bool(shutil.which("termux-notification")),
                "dogrulama": self.dogrulama_ozeti(),
                "yayin": self.yayinci.ozet() if getattr(self, "yayinci", None) else None,
                "katalog": self.feedler.ozet() if self.feedler else None,
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
        # Otomatik kontrolü engelleyen mağazayı 6 saat boyunca tekrar deneme (mağazayı yormamak için).
        # Kayıt veri dosyasında tutulur: GitHub'da her 5 dakikalık çalıştırma da bu kuralı bilir.
        engel = self.veri["engel"].get(alan)
        if alan and engel and time.time() - engel < ENGEL_BEKLE_SN:
            return {"durum": "engellendi", "mesaj": "Bu mağaza otomatik kontrole izin vermiyor; fiyatı mağazada açarak görün.",
                    "ts": int(time.time()), "onbellek_engel": True}
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
            with self.kilit:
                self.veri["engel"][magaza_alani(son) or (urllib.parse.urlparse(son).hostname or "")] = time.time()
        farkli = baglanti_farkli_urun(g, s_) if s_["durum"] == "ok" else None
        if farkli:
            s_.update(karar="farkli_urun", renk="kirmizi", metin=farkli)
        elif s_["durum"] == "ok":
            s_.update(canli_karar(g, s_["fiyat"], gk, s_))
            with self.kilit:
                kayit = self.veri.setdefault("canli", {}).setdefault(g.get("anahtar") or g["id"], [])
                # Aynı fiyatı 1 saat içinde tekrar yazma; kayıt mağaza fiyat geçmişidir
                if not kayit or kayit[-1][1] != s_["fiyat"] or s_["ts"] - kayit[-1][0] > 3600:
                    kayit.append([s_["ts"], s_["fiyat"]])
                del kayit[:-200]
                # Mağaza sayfasında barkod okunduysa paylaşıma yaz: farklı mağazalardaki aynı ürün böyle eşleşir
                gg_ = self.veri["gonderiler"].get(g["id"])
                if gg_ is not None and s_.get("gtin") and not gg_.get("gtin"):
                    gg_["gtin"] = s_["gtin"]
                if g.get("kanal") == TAKIBIM:
                    # Kendi eklenen ürün: kart fiyatı her zaman mağazadaki son fiyattır
                    gg = self.veri["gonderiler"].get(g["id"])
                    if gg:
                        gg["fiyat"], gg["ts"], gg["tarih"] = s_["fiyat"], s_["ts"], simdi().isoformat()
                        gg["bitti"] = bool(s_.get("stok_yok"))
                        if not gg.get("resim") and s_.get("resim"):
                            gg["resim"] = s_["resim"]
                        if gg.get("baslik", "").startswith("(") and s_.get("baslik"):
                            gg["baslik"] = s_["baslik"]
                        self._gecmisi_guncelle()
        with self.kilit:
            self.veri["canli_son"][g["id"]] = s_
        return s_

    # --- kullanıcının kendi eklediği ürünler ("Takibim") ---------------------
    def urun_ekle(self, url):
        """Mağaza bağlantısını alır, sayfadan fiyat/başlık okur ve ürünü 'takibim' kanalına kart olarak ekler.
        Bundan sonra her yenilemede fiyatı mağazadan okunur; alarm kurulabilir."""
        url = (url or "").strip()
        m = re.search(r"https?://\S+", url)
        url = m.group(0).rstrip(".,)") if m else ""
        if not url or not guvenli_dis_adres(url):
            raise ValueError("Geçerli bir mağaza bağlantısı yapıştırın (https:// ile başlamalı)")
        with self.kilit:
            for g in self.veri["gonderiler"].values():
                if g.get("kanal") == TAKIBIM and g.get("urun_link") == url:
                    return {"ok": True, "id": g["id"], "zaten": True}
            if sum(1 for g in self.veri["gonderiler"].values() if g.get("kanal") == TAKIBIM) >= 60:
                raise ValueError("En fazla 60 ürün ekleyebilirsiniz; Takibim sekmesinden silerek yer açın")
        s_ = canli_fiyat_getir(url)
        s_["ts"], s_["surum"] = int(time.time()), SURUM
        baslik = s_.get("baslik") or "(başlık okunamadı)"
        fiyat = s_.get("fiyat") if s_["durum"] == "ok" else None
        metin = baslik + (f"\n{fiyat_yaz(fiyat)}" if fiyat else "")
        with self.kilit:
            no = max([g["no"] for g in self.veri["gonderiler"].values() if g.get("kanal") == TAKIBIM] or [0]) + 1
            g = gonderi_olustur(TAKIBIM, no, metin, [url], s_.get("resim"), simdi(), None)
            g.update(fiyat=fiyat, eski_fiyat=None, yuzde=None, kodlar=[], rozetler=[], sepet_fiyati=False, bitti=False,
                     tur="urun" if fiyat else "kampanya", ilk_fiyat=fiyat, baslik=baslik, gtin=s_.get("gtin"),
                     magaza=magaza_bul([s_.get("son_url") or url], ""),
                     anahtar=urun_anahtari(s_.get("son_url") or url, magaza_bul([s_.get("son_url") or url], ""), baslik, False),
                     kategori=kategori_bul(baslik))
            self.veri["gonderiler"][g["id"]] = g
            if fiyat:
                s_.update(canli_karar(g, fiyat, None, s_))
                kayit = self.veri.setdefault("canli", {}).setdefault(g["anahtar"] or g["id"], [])
                kayit.append([s_["ts"], fiyat])
                self.veri["canli_son"][g["id"]] = s_
                self._gecmisi_guncelle()
            try:
                json_yaz(VERI_DOSYASI, self.veri)
            except OSError:
                pass
        return {"ok": True, "id": g["id"], "fiyat": fiyat, "baslik": baslik, "durum": s_["durum"], "mesaj": s_.get("mesaj")}

    def urun_sil(self, gid):
        with self.kilit:
            g = self.veri["gonderiler"].get(gid)
            if not g or g.get("kanal") != TAKIBIM:
                raise ValueError("Ürün bulunamadı")
            del self.veri["gonderiler"][gid]
            self.veri["canli_son"].pop(gid, None)
            try:
                json_yaz(VERI_DOSYASI, self.veri)
            except OSError:
                pass
        return True

    def _takibimi_guncelle(self, en_fazla=30):
        """Kullanıcının eklediği ürünlerin fiyatını her yenilemede mağazadan okur (ürün başına 30 dk önbellek)."""
        with self.kilit:
            idler = [g["id"] for g in self.veri["gonderiler"].values() if g.get("kanal") == TAKIBIM and g.get("urun_link")]
        for gid in idler[:en_fazla]:
            try:
                self.canli_kontrol(gid)
            except Exception:
                pass

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
                a["kanallar"] = kl[:EN_FAZLA_KANAL]
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
            for k in DIS_KAYNAKLAR:
                if k in yeni:
                    a[k] = bool(yeni[k])
            for k in ("gecmis_tamamla", "akakce_dogrula", "red_gizle"):
                if k in yeni:
                    a[k] = bool(yeni[k])
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
    "/ikon-32.png": "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAABmJLR0QA/wD/AP+gvaeTAAAChklEQVRYhc2Xz0uUQRjHPzPrrqy9enBrlTTItLKiYKHANA1PWeuhwLRDJ29pCh46d6j8B5agiwhRB0E6lFHhSVNRSJCyi2SHSCLRSndt/bH7Ph1sF3+8uru2y9v39DLPPPP9zDwzvDOKDRquNLwR02xH8CvFUWAf6dESMKXgpdY6UDUSmo0FVOzj7Xn3dRG6QOWmyXQHSRChuWYs3BsHWDdXPRuBMixBpLFmLNyrhisNb9SMfsr8zLdpEacu0xHTbLfBHCCPiNmmFdTbYL4uoV4DpbYBIGUaMOwDULnaPvN1WQK49hfi6x7C1z2EuzizFbIEyK+swyj3YZT7OP3wVUYhLAF+jLxhZXYGgGxvUUII7cwGvbdqWmatzn3jQ+vlpCDyL1yh4vUXzvZMWMa9dTcovtmBznYnDwAQ/jqdEMJT7edk51McOQbu4tJt8dxT5zh+t4uS1vscbLyVGkAiCE+1nxMPnqCcrnj/rZAuT2E85vIUpA6wE8SZR/2bzBcmhlLaMykBWEG4PAVx81/vBpjsuGa9UoeOpAfACiJm/vFOA+byb8uVOtxyL30AMYj3LXX8HO3ne9/juPlOkEo7Eo6ZlQoAwPLMZyY7rm5qK2q6jfdSE6i/9xmRpMdLGWCrlNNFSXtnwtlK1LRs/+efkaytMj/Yt2uf6FKQ+YHnljE1WJGT/HrtoiwjD5T1fMzlMObainVeOswBIqHFPeX9D/cBCdrov6hF1LSNANNaK3bfwpmU8EJrrQM2lWEhGnEEdNVIaBahGUjLcUxSIkhz7XhwTgPUjIV7EWkE9naWUtOCIA0XR8PP2PoYHaw2DhAx2xD8wLE0vhlCwBRCXzTiCNSOB+digT+INA5UnhtTMAAAAABJRU5ErkJggg==",
    "/ikon-180.png": "iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAIAAACyr5FlAAAABmJLR0QA/wD/AP+gvaeTAAAHXElEQVR4nO3cW2wUZRiA4dllt6UnegQKSCHoUhFoUSMiwoWAQYJRDEoMaOIhgXhIxAsRuCCNISRyBSISTEg0AY0BQUnKMVBFlBAR5RAOhaBSKZTWtkBLu7tl6sUCQdpvmZ2dmX9m+z6X3el+f9qX2b+zw/r2jcvUgO74VS8A7kUcEBEHRMQBEXFARBwQEQdExAERcUBEHBARB0TEARFxQEQcEBEHRMQBEXFARBwQEQdExAERcUBEHBARB0TEARFxQEQcEBEHRMQBEXFARBwQEQdExAERcUBEHBARB0TEARFxQEQcEBEHRMQBEXFARBwQEQdExAERcUBEHBARB0TEARFxQEQcEBEHRMQBEXFARBwQEQdExAERcUBEHBARB0QB1Qv4P58vY/ADwdzC6JV/22rOap2dqhfUo7klDn8wfdCc9wbOnJdWVBz7SqThUu23ay9sWKlHw6pX10P59o3LVL0GLZhXOGrF1uzSMV0faqk+cvz9GdHGyyrW1dO5YM/h949YtqHbMjRNyx5eXrZ6R7Cgn+PLggvi6Dt5Zu7DE+MckDm0tHzNrrSiAQ4uCpor4ug/bfY9j8koCZWt3k4fDlMfR9bwciOHZZSERq+q5PXFSerj8KdnGDwyc2gp+w8nqY8jfOm88YPpw0nq42j8ZWdCx9OHY9THUbtxzY3Wawl9C304Q30ckYaL1cve1nQ9oe+iDweoj0PTtIa9m09VvNGp30jou7j+Ybder98XVL0GTdO06+dOhOtqCidM13w+498VzC0sGD+1oWqL3tZq08L8ab1zRjyaef/IQE5utKle60zsDOdprnhv5bb+018ZvniN5k/sfHb9r9NH33nG8vdfAjl5Q+YuKX72VX/vmz+ijmvNtZvW1nyxXI+0WzvLndwVh3v6yCgJjV65Nb24pOtDrWeOHZv/nLUt+gJBn8/vtvefXReHG/rIKAmVrd5x++YB+2YFcvIGvfxu3ykvZpSEYpd8Gn74/p+vPonU1yb5zJZwYxxq+7hnGVbNyh7xyMiPv0nrO/Cur3e0XDm15LWmA7tMP7NV3LIhvUvrmaNm9qd5RQVPTktmf2qwjORn9R4wpHztnmB+364P+dN6F016ofngHuXnD1f8Kdutusr11cvecvL6h/Eykp81bP7yQE6e9Kg/mB5auCqhfxh2cG8cDveRaBnJzEorGlA4cXr8Y7JCZTkjH0voaS3n6jgc68NcGeZmaZrWZ9RYI2eFPqMeN7EeC7k9Dgf6SKaMRGfFBPIKjRwWzC8yvSRLeCCOWB82XV+/dY+Z+TKMz7rN5zP4Y2fPYUz97o1nEn9/Lv79Y7fOGda8O5N696p5Jg7Lzx9WnTOMzPIoL8URO3+crngz0T663p9s3x3LqXQvtMfisKQPu39/KdOH9+JIcv+RO2aChfuM+LO8vv/wZBxJ/X27Zmei+ww90n7hm0974L1qXo3D9P40UXo0fHLxnHMrPuyB96p5OA7T+w/j9Gj45KLZjT/vsHAv7CHejsPWPu4sI5lZ3u3D83GY3p/Gp0faT3zw0p1lJDPLo/vTVIjD8v1HbJ/RdHCPhbO8uP9IkTgsfH3p+mpi1SzPvb6kThyW9GGkjGRm2XHN3j4uvU3QNHP//yVGj7SfWDCr6cBuW2cFcwv7lI838sbs1SMHmg9VGX9my6XUmSPG3PWxm2UI+wxrZ/n8vRI6XpUUjMPEnjH+DtTaWR6SmnEktCcwvs9Ifpa3pGwcBn9nyZdhfJbnpHIcsd9Z9dJ50u9MD7d1e6XL9CzLr8WpleJxaJp2efvXx+c/337x77u+3nr2+JF5U8ztMyTm9qeu5ZaPt7ZV869Vh2aV5497OrfsiUBOXqSp/srhfc2//WjHZ6vXVa7XNM3E/+V0oR4Rh6ZpnR3Rxv3bGvdvc2BWXeV6PRIurVjnlT9ZJT0ljqT4/fljJ+ePnZTWb5DP8PWuaONlD10p7xZx3EPWsIdKK9ZlhcpUL0QB4ognKzS67LOdgexc1QtRw/ObJvv4egUe/OjLHlsGccRT9NSMzKGlqlehEnGICsZPVb0ExYhDlF48WPUSFCMOkR5W/HmSHS3NahdAHKKW6iNqF9B6+g+1CyAOUV3leoXvkoQvX2j+fb+q6THEIWo7f6Z28+eqpp9bsaAzGlE1PYY44jm3cmHD3i1OT9X1P1ctaqj6zum5XaTaDcYW0/WGqi3hS+czhgwPGvsgryTHNR/+qXrp3Prdm2yfZYBLP8HYhdL7D04vHuxPS7fp+W9cb2mrOdtxtcmm5zeB91aMCtfVhOtqVK/CUew5ICIOiIgDIuKAiDggIg6IiAMi4oCIOCAiDoiIAyLigIg4ICIOiIgDIuKAiDggIg6IiAMi4oCIOCAiDoiIAyLigIg4ICIOiIgDIuKAiDggIg6IiAMi4oCIOCAiDoiIAyLigIg4ICIOiIgDIuKAiDggIg6IiAMi4oCIOCAiDoiIAyLigIg4ICIOiIgDIuKAiDggIg6IiAMi4oCIOCAiDoiIA6L/AFfBHHABsmNtAAAAAElFTkSuQmCC",
    "/ikon-192.png": "iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAIAAADdvvtQAAAABmJLR0QA/wD/AP+gvaeTAAAIQElEQVR4nO3df2zUdx3H8e/3e/1xlB5wpWOTwkKXrVsL2dSNFocks81ElDjHZGSgEfzDidNlLpZlbm46hyj8ockSs0liMPuRCP6IW5BEhy4dashUDJRuSJVRQ+lsSwvrj+u1vfrHtykIvXLf7+t79/1+756P/yjf730/fzzz/b6/d9/2zJYVZQbgluX3AhBuBAQJAUFCQJAQECQEBAkBQUJAkBAQJAQECQFBQkCQEBAkBAQJAUFCQJAQECQEBAkBQUJAkBAQJAQECQFBQkCQEBAkBAQJAUFCQJAQECQEBAkBQUJAkBAQJAQECQFBQkCQEBAkBAQJAUFCQJAQECQEBAkBQUJAkBAQJAQECQFBQkCQEBAkBAQJAUFCQJAQECQEBAkBQUJAkBAQJAQECQFBQkCQEBAkBAQJAUFS5PcC0orMjkUXVqdGhhNnTk2Mj/m9HEwveAGZZuXH7l30wNdiS5cbpmkYxvjQQM8bv+n46fcTZ/7t9+JwObNlRZnfa7jIKi6teXr3NU33XflfqcTQP7dv7X79F36sC2lFtiwq9nsNF9U8tXvB3eun/S+zqLjyrnsSZ08Pth/L+bqQVoCG6Io7Vy9YvWGmLSzr5idfWLBmY+7WhKsJUEAL12+9+kY0FDCBCciy5n5wZYZb0lBwBCWg4tg8K5rxOE9DgRGUgMaHh4yJCQc70FAwBCWgVDIx9O4JZ/vQUAAE6Da+KDZv3h13OdvHNCtXrR3p6hg8yb29P4JyBjIMo3Pvj0e6OhzvZlk1TzzPecgvAQpofGjg+LYNYwMXHO/Jtcw/AQrIMIzBk0ePfXXN2IU+x3ta1s1PvnDtJzdlZVlIL0AzkC3Z23X+yJuVjeusklJne5rm/FWfysFnHaYVidXeHq9vLK+51SouGe19z9n9Y34J1oepU2JLly/70atF5XMc75lKnXj2wf8eeCUryzLNheu+tHhzc0nlB6Z+luw5+589uzp/9ZPCzChwZyBbsrvz/N9b3J2HKletzcZ5yCqJ1m5/sWrjw5Gy2KU/j5TFKu5cXX7jst6W/QX43FJAz0C24JyHrJJo3c698YamGbbpO3ywbdv9qWTCkyPOuv6mheu3xhuaolXVqcTwYPux7td/2fXqntTIsCev75VABxSQhjKpx+ZVQ4s+/+iSB582I5c/7pc4c6rt8Y2DJ4+Kr++hgF7Cpvh+Lcu8HsMwZi26IVZ3R88ffq1cyxZv3la99TumNc0NctGc+DVN9/W2vDZ2/pzr1/dW0APytyFH9djEhsprbrvlmT2mmfbtFat0Vqz2w12v/czFi2dDsN4HSuf942+1PvLpHL/H6KIeW7yhqW7nXqsk6uKgVZseMa3IzNvEltXP/dAqFy+eDeEIKPcNua7H5rIh06xYcXcmG1Z85OPuFua50ASUy4bEemwuGiqaPadoTjyTLaNVS4SleSlMAdkNZfuzDqu4tPZ7L4v12OINTXW79lmlszLc3opmvKWr62M2hCwgwzAGTvzj2MNr3TVU88TzMzdkFZfW7nilYuUnpCVeIl7fWLdzb+YNhU74ArIban30XnfXshme/bBKonW79nlYjy1e31j3g58H55zhrVAGlI1rmYdXris5vZaFSFgD8vZa5vmV60r5ei0LcUBeNZSDemx52VC4A9Lnoes+88VszD3p5N88FPqAxPeHbnrsOXdzT9/hg32HD7rYUXmfOoDyISBppnal7/DBtsc2tDWvP/enAy52z6eZOk8CkuYhh+x6UiPDqdGRtx/f5LKhfJmH8ieg3DQ0VY/9TxrKq4CkmToDk8+L/f8zganRkbe/+TmX81D4Z+p8Cyh789DkuWe6pw1TyUTBzkN5GFA2rmWXXbmuVLDXsvwMyNuGrlqPrTAbytuAvJqHpp170hHnoVu+G5QHVTOXzwFJ7zEahrvfskglE23b7nfXUKz2dhd7+SvPA1Jm6hmm5pkpM3Xo5H9A7uahDOeedJR5KFwKIiCnDYn12AqkoUIJKPOGPKnHVggNFVBAF+/L3u9Pt0HfX36X+T1XJpT7slAorIDsmfrIlo/2v/XHy36eSgyd3v3s8W981qu/jnDxlYX7suAL+h9XyJ7ZNy6L1zdGq24YTwwPth89d+jADGcmnSe/azbl3KHfHm+e/ktFcix4X/eUK4PtrYPtrTk7nH1vX7vj5YqVa3J20Bwo3IBEVkk03tAUW7q8eO78zPcau9A3kRq/6m+/hwgBubFgzQPVD20vmX+t3wvxHwE5tuTL3178hWa/VxEUBXcXJqpsXEc9lyIgB0wrUv2VZ/xeRbAQkANzbl0Rrar2exXBQkAOzK65ze8lBA4BORDJ/Cvxsiw1Nur3EiYRkAPJnrN+L2FSsrvT7yVMIiAHzh85FJDvM+j/W4vfS5hEQA4kzp7ufXO/36swEp3vBucREQJy5l8/bB7t7/VxARPjYyd3PDTBDBRSI10drV+/x68RJJUYeuepLf1/fcOXo08rBH+pPmiSPV3v7X/JtKxoVXWkrDw3Bx0fGuj+/b53vrX5wtE/5+aIGSrc54E8UXrd9cXzHHwa78740EDizKlgfpcUH6ZKRro63HxRcB5hBoKEgCAhIEgICBICgoSAICEgSAgIEgKChIAgISBICAgSAoKEgCAhIEgICBICgoSAICEgSAgIEgKChIAgISBICAgSAoKEgCAhIEgICBICgoSAICEgSAgIEgKChIAgISBICAgSAoKEgCAhIEgICBICgoSAICEgSAgIEgKChIAgISBICAgSAoKEgCAhIEgICBICgoSAICEgSAgIEgKChIAgISBICAgSAoKEgCAhIEj+B8c/b6QF3+jPAAAAAElFTkSuQmCC",
    "/ikon-512.png": "iVBORw0KGgoAAAANSUhEUgAAAgAAAAIACAIAAAB7GkOtAAAABmJLR0QA/wD/AP+gvaeTAAAV2UlEQVR4nO3da5BcZZnA8e655TaZZHIHcgMJBExiiKiRiKKiy01llVJr0QLcLcwuKPIBRLcKY6m7aKq2EFFS4sqyrKWFIlAKKi6SJYsCRlASBYGFECCEyWQygSQkc90PTUkkQ+bWfd7T/fx+X82c8xRWvf9+3+4+Xbx7+fgCAPHUpR4AgDQEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAISgAAghIAgKAEACAoAQAIqiH1ABxM3ZhxY6YfWt/c0t/b272jraujrdDXl3oooEYIQO40Tpkx9cTTW99y8sRj3zhm5pz9/6f+nu7dj298YeN9Hetu63xgXX9Pd7oxgapXvHv5+NQz8LKJx7zxsLMvmnbSB4r1g4e5q33rczdfu+XGa3p27cxkOqDWCEAuNE2bdcSnr5h+8lmFYnFYf9i9s2PztV/e8uNvF/r7KzYdUJsEIL2pJ56+4PPXNE6eOuIrdK5f++dVn+ja/nxZ5wJqnE8BJTb77IuOveIHo1n9C4XC5ONPWvqdtROOOLZ8cwG1TwBSmnPupYdf+C+FujL8vzBm1twl19zRfPTScswFhCAAycw8/WPzP/mFMl6woaV18VU/1QBgiAQgjQkLFh95yZVlv6wGAEMnACnU1S249Kq6MeMqce2GltbFV98+8Zg3VuLiQC0RgARmve+ciYveXLnrNzRPWnTlrfYBwMEJQNaKDY1zzrmk0ndxFgQMSgCyNmXFqWMPmZfBjZwFAQcnAFmbccpHM7uXsyDgIAQgU8X6htY3vSvLO9oHAK9FADLVfPTS+gkTM76pfQAwIAHI1PjDFya5r30AcCAByNTYQ+enurV9APAqApCp+gktCe9uHwDsTwAyNZRfeqko+wDgLwQgU30v7U49gn0A8DIByFTX9q2pRyjYBwAlApCpPU89mnqEl3lWBCAAmdr18O8KfX2pp3iZsyAITgAy1b2zY9djD6We4hXOgiAyAcjatjtvSj3CX7EPgLAEIGttP/t+f3dX6in+in0AxCQAWetqf+752/4r9RSvZh8AAQlAApuvu6J3z67UU7yafQBEIwAJ7Gt7dtOaVamnGIDPhkIoApDGlh+tab/rltRTDMBZEMQhAIn09z/6lZW7Hnkw9RwDcBYEQQhAMr27X9x48Qf2PPFw6kEG4CwIIhCAlLo7tz904am7n/hT6kEG4CwIap4AJNa9Y9uGC0/LaQOaJy266icaALVKANLTACAJAcgFDQCyV7x7+fjUM/Cyxikzllz9s1Q/HH9wPS/s2PDpM3b9+fepB8mR+vHN4w8/pmnqrMYp0wuFQnfHtq7tW/c8+XAOv+UHAxKAfNGA/Gs+eun0d3+odcUpE+YvLNQdsIfu69u96ZEd9/x82503+W9FzglA7mhAbk1Zcerccy+duOjNQ/z3L268f/N/fK3jnp9VeC4YIQHIIw3Im7GHzl/wuW9OPv6kEfxt5/q1j/3rBXu3bKrAXDAq3gTOo+6OtocuPHXPk4+kHmQAAb8jNv3ks5bdcN/IVv9CoTD5+JOW3XDf9JPPKvdcMFr1581uTD0DA+h7aXf7r25ufet7m1qnp57l1erGjJt+8lk716/tan8u9SwVd9hHLlhw2dV1TWNGc5G6xqZp7zyzWCzufGBd+UaD0RKA/Orbu6f9rlty2oCmsdPe/cGab8C88y+f/49fLBSLZbhWsThp2Yn145o777+zDFeDcvAeQN55PyCV+StXzTnnkrJf9unrV+fzYeCj17zwuNa3nDzxmGXj5h7VNP3QhuaW/r7e7o62fW3P7n5swwsb7u245+fdne2px+QVAlAFNCB7FVr9S2qsAQ0trbM+cN4hZ/792EPnH/xf9vf1dv72ri0/vKbj178o9PdnNSCvSQCqgwZkqaKrf0ltNKDY0HjYR/5pzrmfbWieNKw/fPGPv33i65e9sOHeio3GkHgPoDr0vbS7/a5bppxwSmPrtNSzvFrdmHHT3/XBzt/+qmv71tSzlEEGq3+hUJi0dEVdY1Pn+rWVvlHljJ931KJ/u3nmaR+raxo73L8dM+OwWad/vH78xJ0Priv09VZmQAYnAFXD54IyMO/8y+eee2k295q0dEX1vic8+U3vXPT1nw565nMwxWLLkuWTjz+pY93tfXv3lHM4hkwAqknf3j3ta2/N6T6gaey0d55Z1fuA+StXZbb6l7QsWV6N+4Cpbz/j9V+7sX5sGU6Px8yc3br8PdvuvKlv395yjMbwCECVcRZUIdmc/Byo6s6CJi1dcezqH9Y1NpXrgk1TZkxedmLbz3/Q39tTrmsyRAJQfTSg7FKt/iVV1ICmabMWX3XbcN/yHdSYGYc1tU730KTsCUBV8n5AGWV57v9aquX9gKO/8J2JxyyrxJWbFx6365EHX3r68UpcnNfiWUDVqnvHtg2fOj2nzwtqnrToylur4nlB81eumnveZ1NPUSgUCrPPvmj+ylx/MHTKCX8z9e3vq9z1X3fx6mL5TpYYCjuAKmYfMEp5eO2/v5zvA47+4nVjph9aues3tLTue/6ZWvpCSf7ZAVQ3+4ARy89r//3ldh8waemKDH4W9LCPfqrSt2B/AlD1PDt6BNK+63twc865JIcNmHHq32Vwl/Hzj5547PEZ3IgSAagF3R1tD11wSk5/U76ldfHVt+fqN+XnnX95blf/kjnnXHL4hf+Seor9FItTVpyaza0yuxECUDucBQ1RPk9+DpSrs6Dxhy9smjozm3tNPv4d2dwIAagp9gGDyv9r//3lZx8w4XWLM7zXovL8AANDIAA1xT7gIKrltf/+crIPGDf78MzuVT9hYmP+PtVWqwSg1tgHDKi6XvvvLw/7gIaJkzO9Xbm/acxrEYAaZB/wKtX42n9/yfcBdeV47tvQ1Y+bkOXtIhOA2mQf8BfV+9p/f4n3ARkfynsPICsCULO6d2zbcOFpOW1A86RFV/0kgwbMO//yqn7tv7/ZZ1+U/CyIGiMAtSx4A2pp9S/RAMpLAGpc2AbU3upfogGUkQDUvoANqNXVv0QDKBcBCCFUA2p79S/RAMpCAKII0oAIq3+JBjB6AhBIzTcgzupfogGMkgDEUsMNiLb6l2gAoyEA4dRkA2Ku/iUawIgJQEQ11oDIq3+JBjAyAhBUzTTA6l+iAYyAAMRVAw2w+u9PAxguAQitqhtg9T+QBjAsAhBdlTbA6v9aNIChEwCqrwFW/4PTAIZIAChUVwOs/kOhAQyFAPCyqmiA1X/oNIBBCQCvyPlvSS5Zc4fVf1iS/5YkOScA/JU8/5ZkXdPY1CMM7JnvXbn5uq+mnmJgefhNeXKrIfUA5E7pLGjx1bdPOOLY1LNUgWe+d+WTV/9zoVAo9PfP/cRlqccZwOyzLyoUCk9e/fnUg5A7dgAMIM/vB+TKK6t/ofDUtV/a/N0rUk80MO8HMCABYGAaMKj9V/8SDaC6CACvSQMO4sDVv0QDqCICwMFowIBea/Uv0QCqhQAwCA14lYOv/iUaQFUQAAanAX8xlNW/RAPIPwFgSDRgWKt/iQaQcwLAUAVvwHBX/xINIM8EgGEI24CRrf4lGkBuCQDDE7ABo1n9SzSAfBIAhi3Pz4wru6evXz3K1b/kqWu/9PT1q8sxUfl5ZlxYAsBI5PmZcWX0zPeu3LSmbCvjpjWrcrsP8My4mASAEar5s6DRn/wcyFkQuSIAjFwNN6ASq3+JBpAfAsCo1GQDKrf6l2gAOSEAjFaNNaDSq3+JBpAHAkAZ1EwDsln9SzSA5ASA8qiBBmS5+pdoAGkJAGVT1Q3IfvUv0QASqj9vdmPqGagdfXv3tN91S+tb39vUOj31LMOQavUv2fnA3cW6+knHvS3VAAfRsmR5/bjm+qYxzQuPy+ymW2/9blf71sxuF5kAUGZ9e/e0r711ygmnNLZOSz3LkDx9/epN37o87Qw7H7i7rrFp0tIVaccYUMuS5WMPnV83ZlxmdxSAzDgCovyq6HvC5f2u72jk+XvCDS2tqUegIgSAiqiK9wPSnvwcKM/vB1CTBIBKyfkz48r1lLfyyvMz46g9AkAF5fYsKD8nPwfK81kQNUYAqKwcngXl7eTnQM6CyIYAUHG5akD+V/8SDSADAkAWctKAaln9SzSAShMAMpK8AdW1+pdoABUlAGQnYQOqcfUv0QAqRwDIVJIGVO/qX6IBVIgAkLWMG1Dtq3+JBlAJAkACmTWgNlb/Eg2g7ASANDJoQC2t/iUaQHkJAMlUtAG1t/qXaABlJACkVKEG1OrqX6IBlIsAkFipAbse/UO5LvjUd75Sw6t/iWfGURYCQHrdO7Y9tPK929fdNsrr9HXtffTLn9z87yF+xXDTmlUawCj5RTByob+na9t//6hnR/uk495W19g0givsevQPf7z4b3fcf2cFpsupzvVrc/s7YqPhF8EyIwDkyIsP/+75225omNAy/nXHFusbhvhX+7ZufvKqz/3f6ou7OtoqPGDu1GQDBCAzxbuXj089A7xa45QZs874+LSTzmxeeFyhWBzw3/S+tGvHb37ZdseNHetu6+/rzXzGHJm/ctWccy5JPUXZPHje23Y98mDqKUIQAHKtcfLU5oXLxs87qmnaIQ3Nk/r7ers62rrant312EO7H9/Y39OdesC8qKUGCEBmhrrLhiS6O7fvuPeXO+79ZepB8m7TmlX9vb1zP3FZ6kGoJj4FBDXC9wMYLgGA2qEBDIsAQE3RAIbOewBQJnV14+cc2Xz00jGHzGuaOrOucUzCWXpe2NHQ0ppwAKqCAMBotbzhhJmnnT31xNMbW6enngWGQQBg5Frf8u75K7/YvPC41IPASAgAjETjlBkLPvuNqW8/I/UgMHICAMPWsuStx17xfQc+VDufAoLhmfqO9y/+xk+t/tQAOwAYhtbl7znmS9cXR/S8UsgbOwAYqnFzjlz4pf+0+lMzBACGpFjfcMxXbmhobkk9CJSNAMCQHPbRCycsWJJ6CignAYDBNTS3zDn30tRTQJkJAAzukA99sqF5UuopoMwEAAY387SzU48A5ScAMIjmY5aNm7sg9RRQfgIAg2h907tSjwAVIQAwiImvPz71CFARAgCDGDf3qNQjQEUIAAyiadqs1CNARQgADKJ+3ITUI0BFCAAMor+3J/UIsfTt3ZN6hCgEAAbRs+uF1CPE0rP7xdQjRCEAMIh9WzalHiGQvq693dufTz1FFAIAg9j95MOpRwjkpace7e/rTT1FFAIAg9j54P+mHiGQzgfWpR4hEAGAQXTef6fXpJnZ8Zs7Uo8QiADAILq2P995/69STxFCV0db5/q1qacIRABgcFt+tCb1CCE8d9O3feg2SwIAg+v49S92PfxA6ilqXM+unVtu/FbqKWIRABiC/v7HV3/GOwEVtelbl/fs2pl6ilgEAIbkxYd/9+wPvpl6iprVuf5/nrv1u6mnCEcAYKg2XXP5zt/fk3qKGrSv7dlHvnBuoa8v9SDhCAAMVX9P958u/fDuxzakHqSmdHe2b/zM+7s72lIPEpEAwDD0vNi54dOnv7Dh3tSD1Ih9WzdvuODUPU8+knqQoAQAhqe7c/uGC0/feut1qQepep3r1/7+H07a/cSfUg8SV/Hu5eNTzwBVacqKU4646Kvj5hyZepDq072zY/O1X97y428X+vtTzxKaAMDIFRsaZ57x8cM+fMH4wxemnqU6dLVvfe7ma7fceI1PfOaBAMCoFYsTFy6b+o73tbzhhOaj31A/rjn1QPnS39O9+/GNL2y8r2PdbZ0PrOvv6U49ES8TACizxsnTmqbOLDY2pR4kvf7e3u4dbV0dbT7imU8NqQeAWtPd2d7d2Z56ChicTwEBBCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQAgAQlAAABCUAAEEJAEBQ/w9zMtbInXfqAgAAAABJRU5ErkJggg==",
}
MANIFEST = json.dumps({
    "name": "Düşero · Kasım Fırsatları", "short_name": "Düşero", "lang": "tr", "start_url": "/", "scope": "/",
    "display": "standalone", "background_color": "#f6f6f3", "theme_color": "#c2380a",
    "description": "Telegram fırsat kanallarındaki indirimler, kuponlar ve fiyat geçmişi",
    "icons": [{"src": "/ikon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
              {"src": "/ikon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}],
}, ensure_ascii=False)


YETKISIZ_HTML = """<!doctype html><html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Düşero · Kasım Fırsatları</title><style>body{font:16px/1.5 system-ui,sans-serif;margin:0;padding:40px 20px;background:#f6f6f3;color:#14161a}
main{max-width:420px;margin:0 auto}h1{font-size:20px}@media (prefers-color-scheme:dark){body{background:#13100e;color:#f4efe9}}</style></head>
<body><main><h1>Bu sayfa için erişim anahtarı gerekiyor</h1>
<p>Bilgisayardaki Kasım Fırsatları sayfasında <b>Ayarlar → Telefonda aç</b> bölümündeki QR kodu okutun ya da oradaki bağlantıyı açın.</p></main></body></html>"""

EKLE_HTML = """<!doctype html><html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ürün eklendi — Kasım Fırsatları</title><meta http-equiv="refresh" content="4;url=/?sekme=takip">
<style>body{font:16px/1.5 system-ui,sans-serif;margin:0;padding:40px 20px;background:#f6f3ef;color:#1c1917}
main{max-width:420px;margin:0 auto}h1{font-size:20px}.ok{color:#15803d}.hata{color:#b91c1c}a{color:inherit}
@media (prefers-color-scheme:dark){body{background:#13100e;color:#f4efe9}}</style></head>
<body><main><h1 class="{{SINIF}}">{{MESAJ}}</h1>
<p>Birkaç saniye içinde <a href="/?sekme=takip">Takibim</a> sekmesi açılacak.</p></main></body></html>"""


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
        elif yol == "/ekle":
            # Telefondan hızlı ekleme: "Paylaş" ile gelen mağaza linki (?url=...) eklenir, sonra Takibim sekmesi açılır.
            # (Android'de HTTP Shortcuts gibi bir uygulamayla paylaşım menüsüne bağlanabilir; KURULUM.txt'ye bakın.)
            u = urllib.parse.parse_qs(parca.query).get("url", [""])[0]
            m = re.search(r"https?://\S+", u)
            if not m:
                return self._gonder(400, EKLE_HTML.replace("{{MESAJ}}", "Bağlantı bulunamadı; mağaza linkini /ekle?url=… biçiminde verin.")
                                    .replace("{{SINIF}}", "hata"), "text/html; charset=utf-8")
            try:
                r = self.depo.urun_ekle(m.group(0))
                if r.get("zaten"):
                    mesaj = "Bu ürün zaten takip listenizde."
                elif r.get("fiyat"):
                    mesaj = f"Eklendi: {html.escape((r.get('baslik') or '')[:70])} — {fiyat_yaz(r['fiyat'])}"
                else:
                    mesaj = "Eklendi ama fiyat okunamadı: " + html.escape(r.get("mesaj") or "mağaza sayfası okunamadı")
                sinif = "ok"
            except ValueError as e:
                mesaj, sinif = html.escape(str(e)), "hata"
            self._gonder(200, EKLE_HTML.replace("{{MESAJ}}", mesaj).replace("{{SINIF}}", sinif), "text/html; charset=utf-8")
        elif yol == "/api/durum":
            d = self.depo.durum()
            d["ag"] = {"acik": self.ag_modu, "adres": self.ag_adresi if self._yerel_istek() else None,
                       "bu_cihaz": self._yerel_istek()}
            self._json(200, d)
        elif yol == "/api/ara":
            q = urllib.parse.parse_qs(parca.query).get("q", [""])[0]
            self._json(200, {"idler": self.depo.ara(q)})
        elif yol == "/api/arsiv":
            self._json(200, self.depo.arsiv_listesi())
        elif yol == "/api/katalog":
            sorgu = urllib.parse.parse_qs(parca.query)
            self._json(200, self.depo.katalog_ara(sorgu.get("q", [""])[0], sorgu.get("sirala", ["uygun"])[0]))
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
        if yol == "/api/ekle":
            try:
                veri = json.loads(govde or b"{}")
                if veri.get("islem") == "sil":
                    return self._json(200, {"ok": self.depo.urun_sil(str(veri.get("id", "")))})
                return self._json(200, self.depo.urun_ekle(str(veri.get("url", ""))))
            except ValueError as e:
                return self._json(400, {"hata": str(e) or "Geçersiz istek"})
        if yol == "/api/feed":
            try:
                veri = json.loads(govde or b"{}")
                if not isinstance(veri, dict):
                    raise ValueError("Geçersiz istek")
                return self._json(200, {"ok": True, "katalog": self.depo.feed_islem(veri)})
            except ValueError as e:
                return self._json(400, {"hata": str(e) or "Geçersiz istek"})
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
        if yol == "/api/market":
            try:
                veri = json.loads(govde or b"{}")
                if not isinstance(veri, dict):
                    raise ValueError("Geçersiz istek")
                return self._json(200, market_istegi(str(veri.get("yol", "")), veri.get("govde")))
            except ValueError as e:
                return self._json(400, {"hata": str(e) or "Geçersiz istek"})
            except Exception as e:
                return self._json(502, {"hata": "Market Fiyatı'na ulaşılamadı: " + hata_metni(e)})
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
    print("Mağazada doğrulanıyor (en fazla 3 dk)...")
    depo.dogrulama_turu(sure_sn=180)
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
  let son = null, sonSurum = null;
  const kopya = v => (typeof structuredClone === "function" ? structuredClone(v) : JSON.parse(JSON.stringify(v)));
  async function veriAl(){
    let surum = null;
    try { surum = (await (await asil("surum.json?t=" + Date.now(), {cache: "no-store"})).json()).s; } catch (e) {}
    if (son && surum && surum === sonSurum) return kopya(son);
    const r = await asil("veri.json?t=" + Date.now(), {cache: "no-store"});
    son = await r.json(); sonSurum = surum;
    return kopya(son);
  }
  const TAKIP_ES = {"oyuncu sandalyesi":["oyuncu koltugu","oyuncu sandalye","oyun koltugu","gaming chair"],"oyuncu koltugu":["oyuncu sandalyesi","gaming chair"],"evrak cantasi":["evrak canta","is cantasi","briefcase"],"is cantasi":["evrak cantasi","evrak canta"]};
  // "evrak çantası" ↔ "evrak çantaları", "evrak çanta": kelimeler art arda, Türkçe ekleriyle
  function kokEslesir(ifade, m){
    const kok = w => (w.length <= 4 || /\d/.test(w)) ? w : w.slice(0, Math.max(4, w.length - 3));
    const p = (ifade.match(/[a-z0-9]+/g) || []).map(kok), k = m.match(/[a-z0-9]+/g) || [];
    for (let i = 0; i + p.length <= k.length; i++) if (p.length && p.every((q, j) => k[i + j].startsWith(q))) return true;
    return false;
  }
  function kisisel(v){
    const takip = oku("kf_takip", []), alarmlar = oku("kf_alarmlar", []);
    v.ayarlar = Object.assign({}, v.ayarlar, {takip, alarmlar});
    for (const x of v.firsatlar) {
      const m = sade(x.baslik + " " + x.metin);
      x.takip = takip.filter(k => [sade(k)].concat(TAKIP_ES[sade(k)] || []).some(e => m.includes(e) || kokEslesir(e, m)));
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
    if (url.startsWith("/api/arsiv")) { try { return yanit(await (await asil("arsiv.json?t=" + Date.now(), {cache: "no-store"})).json()); } catch (e) { return yanit({a: []}); } }
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
    # Aşama 2: kuyruğu süre sınırıyla işle (GitHub işi 25 dakikada kesilir; site yine de yayınlansın)
    n = depo.dogrulama_turu(sure_sn=int(os.environ.get("KF_DOGRULAMA_SN", "300")))
    with depo.kilit:
        d = depo.durum()
    oz = d["dogrulama"]
    print(f"  doğrulama · {n} ürün işlendi · onaylı {oz['onayli']} · koşullu {oz['kosullu']} · red {oz['red']} · "
          f"okunamadı {oz['okunamadi']} · kuyrukta {oz['kuyruk']} · motor {'+'.join(oz['motorlar'])}"
          + (f" · engelleyen: {', '.join(oz['engelli'])}" if oz["engelli"] else ""))
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
    for x in d["kaynaklar"]:   # günlükte kaynak durumu (sorun teşhisi için)
        print(f"  kaynak · {x['kanal']}: {'tamam' if x.get('ok') else 'HATA'} · {x.get('adet', 0)} · {x.get('hata') or ''}")
    d["bildirim_var"] = bool(konu)
    d.pop("ag", None)
    d["ayarlar"] = {k: v for k, v in d["ayarlar"].items() if k not in ("ntfy_konu", "ntfy_sunucu", "alarmlar", "takip")}
    with open(os.path.join(cikis, "veri.json"), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
    arsiv = depo.arsiv_listesi()
    with open(os.path.join(cikis, "arsiv.json"), "w", encoding="utf-8") as f:
        json.dump(arsiv, f, ensure_ascii=False, separators=(",", ":"))
    # Telefon bu küçük dosyaya bakar; büyük veri.json yalnızca güncelleme olunca yeniden indirilir
    with open(os.path.join(cikis, "surum.json"), "w", encoding="utf-8") as f:
        json.dump({"s": d.get("son_guncelleme"), "n": len(d["firsatlar"])}, f)
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
    derin = depo.veri["derin"]
    print(f"Site hazır: {cikis} · {len(d['firsatlar'])} paylaşım · {sum(1 for x in d['firsatlar'] if x.get('kacirilmaz'))} kaçırılmaz"
          f" · arama arşivinde {len(arsiv['a'])} eski paylaşım"
          f" · geriye tarama biten kanal {sum(1 for x in derin.values() if x.get('bitti'))}/{len(derin)}")


def yayinci_olustur(depo):
    """yayin.py ve site/ programın yanındaysa yayıncıyı hazırlar (Termux gibi tek dosyalı kurulumda yoktur)."""
    dizin = os.path.dirname(os.path.abspath(__file__))
    if not os.path.isfile(os.path.join(dizin, "yayin.py")):
        return None
    sys.path.insert(0, dizin)
    import yayin
    depo.yayinci = yayin.Yayinci(depo, VERI_DIZINI, os.path.join(dizin, "site"), SURUM)
    return depo.yayinci


def katalog_olustur(depo):
    """katalog.py programın yanındaysa ürün kataloğunu (feed'ler) hazırlar."""
    dizin = os.path.dirname(os.path.abspath(__file__))
    if not os.path.isfile(os.path.join(dizin, "katalog.py")):
        return None
    if dizin not in sys.path:
        sys.path.insert(0, dizin)
    import katalog
    depo.feedler = katalog.Feedler(VERI_DIZINI)
    depo.feedler_log = katalog.log_satiri
    return depo.feedler


def katalog_komutu(a):
    """--feed-ekle / --feed-liste / --feed-sil / --feed-guncelle / --katalog-ara komutları."""
    import katalog
    fl = katalog.Feedler(VERI_DIZINI)
    if a.feed_ekle:
        if not a.feed_ad:
            print("Mağaza adını da verin:  --feed-ekle ADRES --feed-ad \"Mağaza Adı\"")
            return
        try:
            f = fl.ekle(a.feed_ad, a.feed_ekle, a.feed_ortaklik, a.feed_sablon, dosya_izin=True)
        except ValueError as e:
            print("Eklenemedi:", e)
            return
        print(f"Feed eklendi: {f['ad']} ({f['kod']}). İlk okuma yapılıyor...")
        katalog.log_satiri(f, fl.birini_guncelle(f))
    elif a.feed_sil:
        print("Feed ve ürünleri silindi." if fl.sil(a.feed_sil) else "Bu kodla feed yok; kodları görmek için: --feed-liste")
    elif a.feed_guncelle:
        if not fl.ayar["feedler"]:
            print("Henüz feed eklenmemiş. Ekleme: --feed-ekle ADRES --feed-ad \"Mağaza Adı\"")
        fl.guncelle(zorla=True, bildir=katalog.log_satiri)
    elif a.katalog_ara:
        sonuc, toplam = fl.katalog.ara(a.katalog_ara, 20)
        print(f"“{a.katalog_ara}” için katalogda {toplam} ürün" + (" (ilk 20):" if toplam > 20 else ":" if toplam else ""))
        for d in sonuc:
            print(f"  {fiyat_yaz(d['fiyat']):>12}  {d['magaza_ad']}: {d['baslik'][:80]}" + ("" if d["stok"] else "  (stokta yok)"))
        return
    oz = fl.ozet()
    print(f"\nKatalog: {oz['toplam']} ürün · {len(oz['feedler'])} feed · {oz['aralik_saat']} saatte bir güncellenir")
    for f in oz["feedler"]:
        durum = ("HATA: " + f["hata"]) if f["hata"] else (f"{f['adet']} ürün ({f['stokta']} stokta)" if f["son"] else "henüz okunmadı")
        print(f"  [{f['kod']}] {f['ad']} · {durum}" + (" · ortaklık linki" if f["ortaklik"] else ""))


def kilit_bekcisi(depo, bekle=180, aralik=60):
    """Teşhis: her dakika veri/nabiz.txt'ye zaman yazar; ana kilit `bekle` saniyeden uzun serbest kalmazsa
    tüm iş parçacıklarının o anki satırlarını veri/takilma.log'a yazar (programın nerede takıldığını görmek için)."""
    import traceback
    nabiz = os.path.join(VERI_DIZINI, "nabiz.txt")
    gunluk = os.path.join(VERI_DIZINI, "takilma.log")
    while True:
        time.sleep(aralik)
        try:
            with open(nabiz, "w", encoding="utf-8") as f:
                f.write(simdi().isoformat() + "\n")
        except OSError:
            pass
        if depo.kilit.acquire(timeout=bekle):
            depo.kilit.release()
            continue
        try:
            adlar = {t.ident: t.name for t in threading.enumerate()}
            if os.path.exists(gunluk) and os.path.getsize(gunluk) > 2_000_000:
                os.replace(gunluk, gunluk + ".eski")
            with open(gunluk, "a", encoding="utf-8") as f:
                f.write(f"\n=== {simdi().isoformat()} · ana kilit {bekle} sn'dir serbest değil (sürüm {SURUM}) ===\n")
                for tid, cerceve in sys._current_frames().items():
                    f.write(f"--- {adlar.get(tid, tid)}\n" + "".join(traceback.format_stack(cerceve)))
        except OSError:
            pass
        time.sleep(600)


def main():
    ap = argparse.ArgumentParser(description="Kasım Fırsatları — telefonda çalışan indirim takipçisi")
    ap.add_argument("--port", type=int, default=VARSAYILAN_PORT, help="yerel sayfanın portu (varsayılan 8765)")
    ap.add_argument("--tarayici-acma", action="store_true", help="tarayıcıyı otomatik açma")
    ap.add_argument("--kisayol", action="store_true", help="Termux:Widget için ana ekran kısayolu oluştur")
    ap.add_argument("--tek-sefer", action="store_true", help="bir kez çek, en iyi 15 fırsatı terminale yaz")
    ap.add_argument("--statik", metavar="KLASOR", help="bir kez çek ve internette yayınlanacak siteyi bu klasöre yaz")
    ap.add_argument("--dosyadan", metavar="HTML", help="(test) kaydedilmiş t.me/s sayfasını çöz")
    ap.add_argument("--yayin-kur", action="store_true", help="herkese açık site (GitHub Pages) ayarlarını yap")
    ap.add_argument("--yayin-onizle", metavar="KLASOR", help="herkese açık siteyi mevcut veriyle bu klasöre yaz (yüklemeden)")
    ap.add_argument("--yayin-simdi", action="store_true", help="bir kez yenile, doğrula ve herkese açık siteyi hemen yükle")
    ap.add_argument("--feed-ekle", metavar="ADRES", help="ürün feed'i ekle (XML/CSV adresi ya da indirilmiş dosya); --feed-ad ile birlikte")
    ap.add_argument("--feed-ad", metavar="AD", help="eklenen feed'in mağaza adı (ör. \"Teknosa\")")
    ap.add_argument("--feed-ortaklik", action="store_true", help="feed'deki linkler ortaklık (affiliate) linki")
    ap.add_argument("--feed-sablon", metavar="SABLON", help="düz linkleri ortaklık linkine çeviren şablon (içinde {url_kodlu})")
    ap.add_argument("--feed-liste", action="store_true", help="ekli feed'leri ve ürün sayılarını göster")
    ap.add_argument("--feed-sil", metavar="KOD", help="feed'i ve ürünlerini sil (kod: --feed-liste)")
    ap.add_argument("--feed-guncelle", action="store_true", help="tüm feed'leri şimdi indir ve kataloğu güncelle")
    ap.add_argument("--katalog-ara", metavar="KELIME", help="ürün kataloğunda ara (deneme için)")
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
    if a.yayin_kur:
        import yayin
        os.makedirs(VERI_DIZINI, exist_ok=True)
        return yayin.kurulum_sihirbazi(VERI_DIZINI)
    if a.feed_ekle or a.feed_liste or a.feed_sil or a.feed_guncelle or a.katalog_ara:
        os.makedirs(VERI_DIZINI, exist_ok=True)
        return katalog_komutu(a)
    if a.yayin_onizle or a.yayin_simdi:
        depo = Depo()
        katalog_olustur(depo)
        yy = yayinci_olustur(depo)
        if not yy:
            print("Yayın modülü (yayin.py ve site/ klasörü) programın yanında bulunamadı.")
            return
        if a.yayin_simdi and not yy.hazir():
            print("Önce kurulum yapın:  python kasim_firsat.py --yayin-kur  (Windows: Yayin-Kur.bat)")
            return
        if a.yayin_simdi:
            print("Kanallar okunuyor ve mağazada doğrulanıyor (birkaç dakika)...")
            depo.yenile()
            depo.dogrulama_turu(sure_sn=300)
        if a.yayin_onizle:
            n = yy.onizle(a.yayin_onizle)
            print(f"Önizleme yazıldı: {a.yayin_onizle} · {n} fırsat (klasörde 'python -m http.server' ile açabilirsiniz)")
        else:
            if not yy.hazir():
                print("Önce kurulum yapın:  python kasim_firsat.py --yayin-kur")
                return
            n = yy.yayinla(zorla=True)
            print(f"Yüklendi: {n} fırsat → {yy.ozet()['adres']}")
        return

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
    threading.Thread(target=depo.dongu, daemon=True, name="okuyucu").start()
    threading.Thread(target=kilit_bekcisi, args=(depo,), daemon=True, name="bekci").start()      # teşhis
    threading.Thread(target=depo.dogrulayici_dongu, daemon=True, name="dogrulayici").start()   # Aşama 2: doğrulayıcı
    fl = katalog_olustur(depo)
    if fl:
        threading.Thread(target=fl.dongu, kwargs={"bildir": depo.feed_bildir}, daemon=True, name="katalog").start()   # ürün kataloğu
        if fl.ayar["feedler"]:
            print(f"Ürün kataloğu: {len(fl.ayar['feedler'])} feed · {fl.ayar['aralik_saat']} saatte bir güncellenir")
    yy = yayinci_olustur(depo)
    if yy and yy.hazir():
        threading.Thread(target=yy.dongu, daemon=True, name="yayinci").start()               # Aşama 3: herkese açık site
        print(f"Herkese açık site: {yy.ozet()['adres']} (10 dakikada bir güncellenir)")
    print(f"Kasım Fırsatları {SURUM} çalışıyor → {url}")
    print(f"Mağaza okuma: {' → '.join(motor_ozeti())}" + ("" if _cffi else
          "  (daha iyi sonuç için: pip install curl_cffi)"))
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
<meta name="theme-color" content="#c2380a">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" type="image/png" sizes="32x32" href="/ikon-32.png">
<link rel="apple-touch-icon" href="/ikon-180.png">
<meta name="apple-mobile-web-app-title" content="Düşero">
<meta name="application-name" content="Düşero">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>Düşero · Kasım Fırsatları</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,700;12..96,800&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#f6f3ef; --yuzey:#ffffff; --yuzey2:#efe9e1; --metin:#1c1917; --soluk:#6b635b; --cizgi:#e3dbd1;
  --vurgu:#c2410c; --vurgu-ust:#ffffff; --vurgu-yumusak:#fde7d9;
  --yesil:#166534; --yesil-y:#dcfce7; --mavi:#1e40af; --mavi-y:#dbeafe; --sari:#854d0e; --sari-y:#fef3c7;
  --kirmizi:#b91c1c; --kirmizi-y:#fee2e2; --gri-y:#ece6de;
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
.kart-alt{margin-top:8px;display:flex;justify-content:flex-end}
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

/* Market sekmesi — Market Fiyatı'ndan ilham: beyaz kartlar, büyük görsel, mavi ana düğme, market renkleri */
.mf{--mf-mavi:#1681ff}
.mf-mod{display:flex;gap:4px;background:var(--yuzey2);padding:4px;border-radius:12px;margin-bottom:10px}
.mf-mod button{flex:1;border:0;background:transparent;padding:10px 8px;border-radius:9px;font-weight:700;font-size:14.5px;color:var(--soluk)}
.mf-mod button[aria-pressed=true]{background:var(--yuzey);color:var(--mf-mavi);box-shadow:0 1px 4px rgba(0,0,0,.12)}
.mf-konum{display:flex;gap:6px;margin-bottom:10px}
.mf-konum select{flex:1;min-width:0}
.mf-ara{display:flex;gap:6px;margin-bottom:10px}
.mf-ara input{flex:1;min-width:0;font-size:16px;padding:12px 14px;border-radius:10px;border:1.5px solid var(--cizgi);background:var(--yuzey);color:var(--metin)}
.mf-ara input:focus{outline:none;border-color:var(--mf-mavi)}
.mf-arac{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:10px;font-size:13px;color:var(--soluk)}
.mf-izgara{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}
.mf-kart{background:var(--yuzey);border:1px solid var(--cizgi);border-radius:12px;padding:12px;display:flex;flex-direction:column;gap:10px;transition:box-shadow .15s}
.mf-kart:hover{box-shadow:0 4px 14px rgba(0,0,0,.08)}
.mf-kart.acik{grid-column:1/-1}
.mf-kart.soluk{opacity:.7}
.mf-ust{display:flex;flex-direction:column;gap:10px}
.mf-kart.acik .mf-ust{flex-direction:row}
.mf-kart.acik .mf-gorsel{width:140px;flex:none}
.mf-gorsel{position:relative;background:#fff;border-radius:8px;aspect-ratio:1;display:flex;align-items:center;justify-content:center;overflow:hidden}
.mf-gorsel img{width:86%;height:86%;object-fit:contain}
.mf-oran{position:absolute;top:6px;left:6px;background:#e31e24;color:#fff;border-radius:8px;padding:4px 7px 3px;font-weight:800;font-size:16px;line-height:1;text-align:center}
.mf-oran small{display:block;font-size:9.5px;font-weight:600;opacity:.95}
.mf-oran.orta{background:#f59e0b}
.mf-bilgiler{display:flex;flex-direction:column;gap:5px;min-width:0;flex:1}
.mf-ad{font-weight:600;font-size:14.5px;line-height:1.3;color:var(--metin);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:2.6em}
.mf-zs{display:flex;align-items:center;gap:6px;min-width:0}
.mf-zs small{font-size:12px;color:var(--soluk);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.zchip{display:inline-block;flex:none;font-weight:800;font-size:11.5px;line-height:1;padding:4px 7px;border-radius:5px;letter-spacing:.02em;vertical-align:middle}
.cip .zchip{font-size:11px;padding:3px 6px}
.mf-fiyat{font-size:21px;font-weight:800;color:var(--metin);letter-spacing:-.01em}
.mf-fiyat s{font-size:13px;font-weight:500;color:var(--soluk);margin-left:7px}
.mf-fiyat .mf-aralik{font-size:13px;font-weight:600;color:var(--soluk);margin-left:5px}
.mf-birim{font-size:12px;color:var(--soluk)}
.mf .rozetler{margin-top:2px}
.mf-eylem{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:4px}
.mf-btn{background:var(--mf-mavi);color:#fff;border:0;border-radius:8px;padding:9px 12px;font-weight:700;font-size:13.5px;cursor:pointer}
.mf-btn:hover{filter:brightness(1.07)}
.mf-btn[aria-expanded=true]{background:var(--yuzey2);color:var(--metin)}
.mf-link{font-size:12.5px;color:var(--mf-mavi);text-decoration:none}
.mf-tablo{border:1px solid var(--cizgi);border-radius:10px;overflow:hidden}
.mf-tablo-ust{font-weight:700;font-size:13px;padding:9px 12px;background:var(--yuzey2)}
.mf-satir{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr) auto;gap:10px;align-items:center;padding:10px 12px;border-top:1px solid var(--cizgi)}
.mf-satir.ucuz{background:rgba(22,163,74,.07)}
.mf-satir.bu{box-shadow:inset 3px 0 0 var(--mf-mavi)}
.mf-z{display:flex;flex-direction:column;gap:4px;min-width:0}
.mf-z .zchip{align-self:flex-start}
.mf-z small{font-size:11.5px;color:var(--soluk);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mf-f{display:flex;flex-direction:column;line-height:1.2}
.mf-f b{font-size:17px;font-weight:800;color:var(--metin)}
.mf-f s{font-size:11.5px;color:var(--soluk)}
.mf-f small{font-size:11.5px;color:var(--soluk)}
.mf-e{display:flex;flex-direction:column;align-items:flex-end;gap:4px}
.mf-ucuz{background:#16a34a;color:#fff;border-radius:5px;font-size:11px;font-weight:700;padding:3px 7px}
.mf-pahali{background:#8b9096;color:#fff;border-radius:5px;font-size:11px;font-weight:700;padding:3px 7px}
.mf-ind{font-size:10.5px;color:#e31e24;font-weight:700}
.mf-ozet{font-size:12.5px;color:var(--soluk);padding:9px 12px;border-top:1px solid var(--cizgi)}
.mf-bilgi{font-size:13px;color:var(--soluk);background:var(--yuzey2);border-radius:8px;padding:10px 12px}
.mf-bos{text-align:center;color:var(--soluk);padding:34px 12px;line-height:1.6;background:var(--yuzey);border:1px dashed var(--cizgi);border-radius:14px}
.mf-bos b{color:var(--metin);font-size:16px}
.mf-bos-ikon{font-size:38px;margin-bottom:6px}
.mf-oneri{display:flex;flex-wrap:wrap;gap:6px;justify-content:center;margin-top:12px}
@media (max-width:520px){.mf-izgara{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.mf-kart{padding:9px}.mf-fiyat{font-size:18px}
  .mf-kart.acik .mf-ust{flex-direction:column}.mf-kart.acik .mf-gorsel{width:100%;max-width:220px;align-self:center}
  .mf-eylem .mf-btn{width:100%}.mf-satir{grid-template-columns:minmax(0,1fr) auto;}.mf-e{grid-column:2;grid-row:1/3}}

/* ---- Düşero: tasarım dosyasıyla (Main.dc.html / ProductDetail.dc.html) birebir aynı renk, yazı ve yerleşim ---- */
:root{--bg:#F6F6F3;--yuzey:#FFFFFF;--yuzey2:#F1F1EE;--metin:#14161A;--soluk:#5B6170;--soluk2:#4A505C;--cizgi:#E4E4E0;--cizgi2:#D9D9D4;
  --vurgu:#C2380A;--vurgu-koyu:#8F2905;--vurgu-ust:#FFFFFF;--vurgu-yumusak:#FFE8DD;--vurgu-yazi:#A12E06;--turuncu:#FF8A5B;
  --mavi:#1E3A9E;--mavi-cizgi:#1E40AF;--mavi-y:#E0E9FF;--gri-yazi:#3A4453;--gri-y:#ECEEF2;--gri-cizgi:#5B6170;
  --kirmizi:#9F1239;--kirmizi-cizgi:#BE123C;--kirmizi-y:#FFE4E8;--gorsel-zemin:#F1F1EE;--golge:0 18px 40px -24px rgba(20,22,26,.35);
  --yaz:Inter,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;--disp:"Bricolage Grotesque",Inter,system-ui,sans-serif;color-scheme:light}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--metin);font:16px/1.5 var(--yaz)}
button,input,select,textarea{font-family:var(--yaz)}
a{color:var(--vurgu)}a:hover{color:var(--vurgu-koyu)}
main{padding:0}
.s-kap{max-width:1240px;margin:0 auto;padding:0 24px;box-sizing:border-box;width:100%}
.disp,.s-disp{font-family:var(--disp)}
.s-sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.s-ico{fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round;flex-shrink:0}
.s-soluk{color:var(--soluk)}

/* Düğmeler ve alanlar (.btn / .btn-ghost / .field / .chip) */
.s-dugme{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:44px;padding:0 18px;border:0;border-radius:12px;
  background:var(--vurgu);color:#fff;font:600 14.5px/1 var(--yaz);text-decoration:none;cursor:pointer;white-space:nowrap;box-sizing:border-box}
.s-dugme:hover{background:var(--vurgu-koyu);color:#fff}
.s-dugme.s-ikincil{background:#fff;color:var(--metin);border:1px solid var(--cizgi2);padding:0 16px}
.s-dugme.s-ikincil:hover{border-color:var(--metin);background:#fff;color:var(--metin)}
.s-dugme.s-ikincil[aria-pressed=true]{color:var(--vurgu);border-color:var(--vurgu)}
.s-dugme.s-buyuk{min-height:52px;padding:0 26px;font-size:16px}
.s-dugme.s-kare{width:44px;padding:0;flex:none}
.s-dugme.s-tam{width:100%}
.s-dugme.s-turuncu{background:var(--turuncu);color:var(--metin)}
.s-dugme.s-turuncu:hover{background:#ff9b70;color:var(--metin)}
.s-dugme.don svg{animation:don 1s linear infinite}
.s-alan,.s-arac select,.s-arac input[type=search],.s-filtre select{min-height:46px;border:1px solid var(--cizgi2);border-radius:12px;
  background:#fff;padding:0 14px;font:15px var(--yaz);color:var(--metin);min-width:0;width:auto;box-sizing:border-box}
.s-alan.s-buyuk{min-height:52px;font-size:16px}
.s-dugme:focus-visible,.s-alan:focus,.s-cip:focus-visible,a:focus-visible,button:focus-visible,select:focus-visible{outline:3px solid #FF9B70;outline-offset:2px}
.s-link{background:none;border:0;padding:0;color:var(--vurgu);font:inherit;cursor:pointer;text-align:left}
.s-link:hover{color:var(--vurgu-koyu);text-decoration:underline}

/* Üst menü */
header.s-ust{position:static;background:#fff;border-bottom:1px solid var(--cizgi);padding:env(safe-area-inset-top) 0 0}
.s-ust-ic{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;padding-top:14px;padding-bottom:14px}
.s-marka{display:flex;align-items:center;gap:10px;text-decoration:none;color:var(--metin)}
.s-marka:hover{color:var(--metin)}
.s-marka-ad{font-family:var(--disp);font-weight:800;font-size:26px;letter-spacing:-.02em}
nav.s-menu{display:flex;align-items:center;gap:22px;flex-wrap:wrap;margin:0;overflow:visible}
.s-menu .s-menu-link{color:var(--metin);background:none;border:0;font:500 15px var(--yaz);padding:10px 4px;cursor:pointer;text-decoration:none}
.s-menu .s-menu-link:hover,.s-menu .s-menu-link[aria-current=page]{color:var(--vurgu)}
.s-menu .s-dugme{padding:0 16px;color:#fff}
@media (max-width:720px){nav.s-menu{gap:4px 14px}.s-menu .s-menu-link{font-size:14px;padding:8px 2px}.s-m-gizle{display:none}}

/* Kahraman */
.s-kahraman{background:#fff;border-bottom:1px solid var(--cizgi)}
.s-kahraman-ic{display:flex;flex-wrap:wrap;gap:48px;align-items:center;padding-top:64px;padding-bottom:72px}
.s-kahraman-metin{flex:1 1 480px;min-width:0;display:flex;flex-direction:column;gap:22px}
.s-etiket-ust{align-self:flex-start;background:var(--vurgu-yumusak);color:var(--vurgu-yazi);font-weight:600;font-size:13.5px;padding:7px 12px;border-radius:999px}
h1.s-h1{margin:0;font-family:var(--disp);font-size:clamp(40px,5.4vw,68px);line-height:1.02;letter-spacing:-.03em;font-weight:800}
h1.s-h1 span{color:var(--vurgu)}
.s-giris-p{margin:0;font-size:18px;line-height:1.55;color:var(--soluk2);max-width:560px}
.s-ara-kutu{display:flex;gap:10px;flex-wrap:wrap;max-width:600px}
.s-ara-kutu .s-alan{flex:1 1 280px}
.s-ozellikler{list-style:none;margin:0;padding:0;display:flex;gap:22px;flex-wrap:wrap;color:var(--soluk2);font-size:14.5px}
.s-ozellikler li{display:flex;align-items:center;gap:8px}
.s-ozellikler .s-ico{color:var(--vurgu)}
.s-durum{margin:0;font-size:13.5px;color:var(--soluk)}
.s-durum .hata{color:var(--vurgu-yazi);font-weight:600;text-decoration:underline;background:none;border:0;padding:0;font-size:inherit;cursor:pointer}
.s-sayac{display:flex;gap:8px;flex-wrap:wrap}
.s-sayac span{font-size:13px;background:var(--vurgu-yumusak);color:var(--vurgu-yazi);border-radius:999px;padding:5px 11px}
.s-sayac b{color:var(--vurgu)}

/* Günün düşüşü kartı */
.s-one{flex:1 1 420px;min-width:0;background:#fff;border:1px solid var(--cizgi);border-radius:22px;padding:24px;box-shadow:var(--golge);
  display:flex;flex-direction:column;gap:16px;box-sizing:border-box}
.s-one-ust{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.s-ust-yazi{font-size:13px;font-weight:600;color:var(--soluk);text-transform:uppercase;letter-spacing:.08em}
.s-one-magaza{font-size:14px;color:var(--soluk);margin-bottom:4px}
.s-one-baslik{margin:0;font-size:20px;font-weight:600;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.s-fiyat-satir{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.s-fiyat{font-family:var(--disp);font-weight:800;font-size:26px;letter-spacing:-.01em;font-variant-numeric:tabular-nums}
.s-fiyat.s-dev{font-size:44px;letter-spacing:-.02em}
.s-fiyat small{font:600 13px var(--yaz);color:var(--soluk)}
.s-eski{color:var(--soluk);font-size:14px}
.s-dev+.s-eski{font-size:17px}
.s-eski:empty{display:none}
.s-kutucuklar{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}
.s-kutucuk{background:var(--bg);border-radius:12px;padding:12px;min-width:0}
.s-kutucuk span{display:block;font-size:12.5px;color:var(--soluk)}
.s-kutucuk b{display:block;font-size:20px;font-weight:700;font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.s-kutucuk.s-mavi{background:var(--mavi-y)}
.s-kutucuk.s-mavi span,.s-kutucuk.s-mavi b{color:var(--mavi)}
.s-grafik{display:block;width:100%;height:120px;overflow:visible}
.s-grafik .alan-dolgu{fill:var(--vurgu-yumusak)}
.s-grafik .cizgi{fill:none;stroke:var(--vurgu);stroke-width:2.5;stroke-linejoin:round;vector-effect:non-scaling-stroke}
.s-grafik .normal-cizgi{stroke:#9AA0AA;stroke-width:1;stroke-dasharray:4 4;vector-effect:non-scaling-stroke}
.s-grafik-kutu{position:relative}
.s-grafik-buyuk{height:280px}
.s-grafik-etiket{position:absolute;right:8px;font-size:12.5px;color:var(--soluk);background:#fff;padding:2px 6px;border-radius:6px}
.s-grafik-etiket.ust-e{top:6px}.s-grafik-etiket.alt-e{bottom:6px}
.s-grafik-tarih{display:flex;justify-content:space-between;gap:8px;font-size:12.5px;color:var(--soluk)}
.s-grafik-yok{display:grid;place-items:center;min-height:200px;border:1px dashed #C9C9C3;border-radius:14px;color:var(--soluk);font-size:14px;padding:20px;text-align:center;gap:10px}

/* Bölümler */
.s-bolum{padding-top:56px;padding-bottom:24px;display:flex;flex-direction:column;gap:22px}
.s-bolum-bas{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap}
h2.s-bolum-baslik{margin:0 0 6px;font-family:var(--disp);font-size:36px;letter-spacing:-.02em;font-weight:800;line-height:1.15}
.s-bolum-bas p{margin:0;color:var(--soluk);font-size:15px}
.s-arac{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.s-arac #ara{width:240px;max-width:100%}
.s-arac select{padding-right:10px}
@media (max-width:720px){.s-arac{width:100%}.s-arac #ara{flex:1 1 100%;width:auto}.s-arac select{flex:1 1 45%}}
.s-anahtar{display:flex;align-items:center;gap:8px;min-height:46px;padding:0 14px;background:#fff;border:1px solid var(--cizgi2);border-radius:12px;
  font-size:14.5px;font-weight:500;cursor:pointer;white-space:nowrap;margin:0;box-sizing:border-box}
.s-anahtar input{width:18px;height:18px;accent-color:var(--mavi-cizgi);margin:0}
details.s-diger{position:relative}
details.s-diger>summary{list-style:none}
details.s-diger>summary::-webkit-details-marker{display:none}
.s-filtre{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:-8px}
.s-cipler{display:flex;gap:8px;flex-wrap:wrap}
.s-cip,.s-cipler .cip{min-height:40px;padding:0 16px;border-radius:999px;border:1px solid var(--cizgi2);background:#fff;color:var(--metin);
  font:500 14px var(--yaz);cursor:pointer}
.s-cip:hover,.s-cipler .cip:hover{border-color:var(--metin)}
.s-cip[aria-pressed=true],.s-cipler .cip[aria-pressed=true]{background:var(--metin);border-color:var(--metin);color:#fff}
.s-not{font-size:13.5px;color:var(--soluk);margin:0;line-height:1.5}

/* Kart ızgarası */
.s-izgara,#icerik .s-izgara{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:18px}
#icerik{padding-top:0}
@media (max-width:560px){.s-izgara{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}

/* Kart */
.s-kart{background:#fff;border:1px solid var(--cizgi);border-radius:18px;padding:16px;display:flex;flex-direction:column;gap:12px;min-width:0;box-sizing:border-box}
.s-kart:hover{border-color:#B9B9B2}
.s-kart.s-kac-kart{border:2px solid var(--vurgu);padding:15px}
.s-kart.s-bitti{opacity:.6}
.s-gorsel{position:relative;display:flex;align-items:center;justify-content:center;height:150px;border-radius:12px;overflow:hidden;background:var(--gorsel-zemin);color:#8A8F99;
  text-decoration:none;cursor:pointer}
.s-gorsel img{width:100%;height:100%;object-fit:contain;display:block;background:#fff}
.s-gorsel.yok::after{content:"";width:44px;height:44px;background:currentColor;
  -webkit-mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M21 8l-9-5-9 5 9 5 9-5z'/%3E%3Cpath d='M3 8v8l9 5 9-5V8'/%3E%3Cpath d='M12 13v8'/%3E%3C/svg%3E") center/contain no-repeat;
  mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M21 8l-9-5-9 5 9 5 9-5z'/%3E%3Cpath d='M3 8v8l9 5 9-5V8'/%3E%3Cpath d='M12 13v8'/%3E%3C/svg%3E") center/contain no-repeat}
.s-oran-rozet{position:absolute;top:10px;left:10px;background:var(--vurgu);color:#fff;font-weight:700;font-size:13px;padding:4px 9px;border-radius:8px}
.s-kac-etiket{position:absolute;top:10px;right:10px;background:var(--metin);color:#fff;font-weight:700;font-size:12px;padding:4px 9px;border-radius:8px}
.s-ust-satir{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:13px;color:var(--soluk)}
.s-ust-satir .s-magaza{font-weight:600;color:var(--metin);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.s-ust-satir .s-sag{display:flex;gap:6px;align-items:center;flex-shrink:0}
.s-yeni{background:var(--vurgu-yumusak);color:var(--vurgu-yazi);border-radius:6px;padding:1px 6px;font-size:12px;font-weight:700}
h3.s-baslik{margin:0;font-size:15.5px;line-height:1.35;font-weight:600;min-height:42px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
h3.s-baslik a{color:inherit;text-decoration:none}
h3.s-baslik a:hover{text-decoration:underline}
.s-karar-satir{display:flex;align-items:center;justify-content:space-between;gap:10px}
.s-mini{display:block;width:96px;height:32px;flex-shrink:0}
.s-mini svg{display:block;width:100%;height:100%;overflow:visible}
.s-mini polyline{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round;stroke:var(--cizgi2)}
.s-mini.k-dip polyline{stroke:var(--mavi-cizgi)}
.s-mini.k-iyi polyline{stroke:var(--gri-cizgi)}
.s-mini.k-sisirilmis polyline{stroke:var(--kirmizi-cizgi)}
.s-ozet-satir{margin:0;font-size:13px;line-height:1.45;color:var(--soluk)}
.s-ozet-satir:empty{display:none}
.s-ozet-satir b,.s-ozet-satir strong{color:var(--metin)}
.s-notlar{list-style:none;margin:0;padding:0;font-size:12.5px;color:var(--soluk);display:flex;flex-direction:column;gap:3px}
.s-notlar:empty{display:none}
.s-notlar li.uyari{color:var(--vurgu-yazi)}
.s-notlar li.iyi{color:var(--mavi)}
.s-notlar .kod{margin:2px 4px 0 0}
.s-kart-alt{display:flex;gap:8px;margin-top:auto}
.s-kart-alt .s-git{flex:1 1 auto}
@media (max-width:560px){.s-kart{padding:10px;border-radius:14px;gap:8px}.s-gorsel{height:120px}.s-fiyat{font-size:20px}.s-mini{display:none}.s-ozet-satir{font-size:12px}
  h3.s-baslik{font-size:14px}.s-kart-alt .s-git{padding:0 10px;font-size:14px}}

/* Kupon kartı */
.s-kupon-bas{font-family:var(--disp);font-size:24px;font-weight:800;letter-spacing:-.02em;overflow-wrap:anywhere}
.s-kodlar{display:flex;flex-wrap:wrap;gap:8px}

/* Rozetler */
.s-rozet{display:inline-flex;align-items:center;gap:6px;padding:5px 10px;border-radius:999px;font-size:12.5px;font-weight:600;white-space:nowrap}
.s-rozet.k-dip{background:var(--mavi-y);color:var(--mavi)}
.s-rozet.k-iyi{background:var(--gri-y);color:var(--gri-yazi)}
.s-rozet.k-sisirilmis{background:var(--kirmizi-y);color:var(--kirmizi)}
.s-rozet.k-yok{background:#fff;color:var(--soluk);box-shadow:inset 0 0 0 1px var(--cizgi2)}
.s-rozet.k-kac{background:var(--vurgu);color:#fff}
@media (max-width:560px){.s-rozet{font-size:11.5px;padding:4px 8px}}

/* Eski parçalar */
.bos-durum{display:flex;flex-direction:column;gap:6px;align-items:center;text-align:center;color:var(--soluk);background:#fff;border:1px dashed #C9C9C3;border-radius:18px;padding:40px 20px}
.bos-durum b{color:var(--metin);font-size:17px}
.daha{min-height:48px;border-radius:12px;border:1px solid var(--cizgi2);background:#fff;font-weight:600;width:100%}
.arama-ozet,.ipucu{margin:0}
.not{border-radius:12px;margin:0;background:var(--vurgu-yumusak);color:var(--metin)}
.kod{border-radius:8px}

/* Nasıl çalışır */
.s-nasil{padding-top:56px;padding-bottom:56px}
.s-adimlar{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:18px}
.s-adim{background:#fff;border:1px solid var(--cizgi);border-radius:18px;padding:26px;display:flex;flex-direction:column;gap:10px}
.s-adim-no{font-family:var(--disp);font-size:40px;font-weight:800;color:var(--vurgu);line-height:1.2}
.s-adim h3{margin:0;font-size:19px}
.s-adim p{margin:0;color:var(--soluk2);line-height:1.55}
.s-aciklama{display:flex;gap:12px;flex-wrap:wrap;margin-top:22px}
.s-aciklama .s-rozet{white-space:normal}
.s-nasil h2.s-bolum-baslik{margin-bottom:24px}

/* Telegram bandı */
.s-tg-bant{padding:0 24px 56px}
.s-tg-ic{max-width:1192px;margin:0 auto;background:var(--metin);color:#fff;border-radius:26px;padding:48px;display:flex;flex-wrap:wrap;gap:28px;align-items:center;justify-content:space-between;box-sizing:border-box}
.s-tg-ic h2{margin:0 0 10px;font-family:var(--disp);font-size:38px;letter-spacing:-.02em;font-weight:800;line-height:1.1}
.s-tg-ic h2 span{color:var(--turuncu)}
.s-tg-ic p{margin:0;color:#C9CCD2;font-size:17px;line-height:1.55;max-width:560px}
.s-tg-sag{display:flex;flex-direction:column;gap:10px;align-items:flex-start}
.s-tg-sag small{font-size:14px;color:#C9CCD2}
@media (max-width:560px){.s-tg-ic{padding:28px}.s-tg-ic h2{font-size:30px}}

/* Alt bilgi */
.s-alt{margin-top:auto;background:#fff;border-top:1px solid var(--cizgi);color:var(--soluk);padding-bottom:env(safe-area-inset-bottom)}
.s-alt-ic{display:flex;flex-wrap:wrap;gap:28px;justify-content:space-between;padding-top:36px;padding-bottom:36px}
.s-alt-ic>div{flex:1 1 420px;min-width:0;display:flex;flex-direction:column;gap:10px}
.s-alt-ic p{font-size:13.5px;line-height:1.6;margin:0;max-width:620px}
.s-kucuk-marka{font-size:22px;color:var(--metin)}
.s-alt-menu{display:flex;flex-direction:column;gap:8px;font-size:14.5px}
.s-alt-menu .s-link{font-size:14.5px}
.s-telif{border-top:1px solid var(--cizgi);padding:14px 24px;text-align:center;font-size:13px;margin:0}
body{display:flex;flex-direction:column;min-height:100vh}

/* Ürün sayfası (ProductDetail tasarımı) */
#detay{position:fixed;inset:0;z-index:50;background:var(--bg);overflow:auto;padding:0;display:flex;flex-direction:column}
#detay[hidden]{display:none}
.u-ana{max-width:1240px;width:100%;box-sizing:border-box;margin:0 auto;padding:22px 24px 56px;display:flex;flex-direction:column;gap:22px}
.u-yol{font-size:14px;color:var(--soluk);display:flex;gap:8px;flex-wrap:wrap}
.u-yol .s-link{font-size:14px}
.u-ust{display:flex;flex-wrap:wrap;gap:22px;align-items:stretch}
.u-kutu{background:#fff;border:1px solid var(--cizgi);border-radius:20px;padding:24px;display:flex;flex-direction:column;gap:16px;min-width:0;box-sizing:border-box}
.u-sol{flex:1 1 340px}
.u-sag{flex:2 1 560px;gap:18px}
.u-gorsel{height:240px;border-radius:14px;background:var(--gorsel-zemin);display:flex;align-items:center;justify-content:center;flex-direction:column;gap:8px;color:#6B707B;font-size:13px;overflow:hidden}
.u-gorsel img{width:100%;height:100%;object-fit:contain;background:#fff}
.u-gorsel.yok::after{content:"Ürün görseli yüklenemedi"}
.u-enucuz{font-size:14px;color:var(--soluk)}
.u-enucuz strong{color:var(--metin)}
.u-baslik{margin:0;font-size:24px;line-height:1.3;font-weight:700}
.u-fiyat{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
.u-fiyat .s-fiyat{font-size:46px;letter-spacing:-.02em}
.u-fiyat s{font-size:18px;color:var(--soluk)}
.u-oran{background:var(--vurgu);color:#fff;font-weight:700;font-size:14px;padding:4px 10px;border-radius:8px}
.u-karar{border-radius:14px;padding:16px;display:flex;flex-direction:column;gap:6px;background:var(--bg)}
.u-karar p{margin:0;font-size:15px;line-height:1.5}
.u-karar .s-rozet{align-self:flex-start;background:#fff}
.u-karar.k-dip{background:var(--mavi-y)}.u-karar.k-dip p{color:var(--mavi)}
.u-karar.k-iyi{background:var(--gri-y)}.u-karar.k-iyi p{color:var(--gri-yazi)}
.u-karar.k-sisirilmis{background:var(--kirmizi-y)}.u-karar.k-sisirilmis p{color:var(--kirmizi)}
.u-uyari{background:#FEF3C7;color:#854D0E;border-radius:12px;padding:10px 12px;font-size:13.5px;line-height:1.45}
.u-dugmeler{display:flex;flex-wrap:wrap;gap:8px}
.u-dugmeler .s-dugme.s-ikincil{min-height:40px;padding:0 14px;border-radius:10px;font-size:14px}
.u-gecmis-bas{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.u-h2{margin:0;font-family:var(--disp);font-size:26px;font-weight:800;letter-spacing:-.01em}
.u-sekmeler{display:flex;gap:4px;background:var(--gorsel-zemin);padding:4px;border-radius:12px}
.u-sekmeler button{min-height:40px;padding:0 16px;border-radius:10px;border:0;background:transparent;color:var(--soluk2);font:600 14px var(--yaz);cursor:pointer}
.u-sekmeler button[aria-pressed=true]{background:var(--metin);color:#fff}
.u-istat{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
.u-istat div{background:var(--bg);border-radius:12px;padding:14px;min-width:0}
.u-istat span{display:block;font-size:12.5px;color:var(--soluk)}
.u-istat b{display:block;font-size:21px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
@media (max-width:560px){.u-istat{grid-template-columns:repeat(2,minmax(0,1fr))}.u-fiyat .s-fiyat{font-size:38px}}
.u-tablo-kutu{background:#fff;border:1px solid var(--cizgi);border-radius:20px;padding:24px 0 8px}
.u-tablo-kutu .u-h2{margin:0 24px 14px}
.u-tablo-kaydir{overflow-x:auto}
table.u-tablo{width:100%;border-collapse:collapse}
.u-tablo th{font-size:12.5px;font-weight:600;color:var(--soluk);text-align:left;padding:12px 16px;text-transform:uppercase;letter-spacing:.06em}
.u-tablo td{padding:14px 16px;border-top:1px solid #ECECE8;font-size:15px;white-space:nowrap}
.u-tablo th:first-child,.u-tablo td:first-child{padding-left:24px}
.u-tablo th:last-child,.u-tablo td:last-child{padding-right:24px}
.u-tablo .dus{color:var(--mavi)}.u-tablo .art{color:var(--kirmizi)}
.u-alt-iki{display:flex;flex-wrap:wrap;gap:22px}
.u-alarm{flex:1 1 420px;padding:26px;gap:14px}
.u-alarm p{margin:0;color:var(--soluk2);line-height:1.55}
.u-alarm-form{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}
.u-alarm-form label{font-size:14px;font-weight:600}
.u-alarm-form .u-alan-kutu{display:flex;flex-direction:column;gap:6px;flex:1 1 200px;min-width:0}
.u-alarm-form input{min-height:48px;border:1px solid var(--cizgi2);border-radius:12px;background:#fff;padding:0 14px;font:16px var(--yaz);color:var(--metin)}
.u-durum{margin:0;background:var(--mavi-y);color:var(--mavi);border-radius:12px;padding:12px 14px;font-size:14.5px}
.u-tg{flex:1 1 420px;min-width:0;background:var(--metin);color:#fff;border-radius:20px;padding:26px;display:flex;flex-direction:column;gap:14px;justify-content:center;box-sizing:border-box}
.u-tg h2{margin:0;font-family:var(--disp);font-size:28px;font-weight:800;letter-spacing:-.01em}
.u-tg h2 span{color:var(--turuncu)}
.u-tg p{margin:0;color:#C9CCD2;line-height:1.55}
.u-tg .s-dugme{align-self:flex-start}
.u-alt{margin-top:auto;background:#fff;border-top:1px solid var(--cizgi)}
.u-alt div{max-width:1240px;margin:0 auto;padding:24px;font-size:13px;line-height:1.6;color:var(--soluk);box-sizing:border-box}
#detay .canli{margin:0}
#detay details.u-ayrinti summary{cursor:pointer;font-weight:600;font-size:14px}
#detay .fiyatlar{margin-top:8px}

/* Diğer bölümler (Takibim, Market, Ayarlar, Takvim) */
.s-sayfa{padding-top:40px;padding-bottom:56px}
h1.s-sayfa-baslik{margin:0 0 22px;font-family:var(--disp);font-size:36px;letter-spacing:-.02em;font-weight:800;line-height:1.15}
.s-sayfa input:not([type=checkbox]):not([type=radio]):not([type=range]){min-height:46px;border:1px solid var(--cizgi2);border-radius:12px;padding:0 14px;font:15px var(--yaz);background:#fff;color:var(--metin);box-sizing:border-box}
.s-sayfa select{min-height:46px;border:1px solid var(--cizgi2);border-radius:12px;padding:0 10px;font:15px var(--yaz);background:#fff;color:var(--metin)}
.ayar,.takvim{max-width:860px}
.telefon-panel{border-radius:18px}
.toast{border-radius:12px}
</style>
</head>
<body>
<header class="s-ust"><div class="s-kap s-ust-ic">
  <a class="s-marka" href="./" data-git="tum" aria-label="Düşero ana sayfa"><svg width="36" height="36" viewBox="0 0 36 36" aria-hidden="true"><rect x="0" y="0" width="36" height="36" rx="10" fill="#C2380A"/><path d="M18 9v15m0 0l-6-6m6 6l6-6" fill="none" stroke="#FFFFFF" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/><path d="M10 28h16" stroke="#FFFFFF" stroke-width="3" stroke-linecap="round"/></svg><span class="s-marka-ad">düşero</span></a>
  <nav class="s-menu" aria-label="Ana menü">
    <button type="button" class="s-menu-link" data-git="tum">Fırsatlar</button>
    <button type="button" class="s-menu-link" data-git="kupon">Kuponlar</button>
    <button type="button" class="s-menu-link s-m-gizle" data-kaydir="nasil">Nasıl çalışır?</button>
    <button type="button" class="s-menu-link" data-git="takip">Fiyat alarmı</button>
    <button type="button" class="s-menu-link" data-git="ayar">Ayarlar</button>
    <a class="s-dugme" id="tgBtn" href="https://t.me/" target="_blank" rel="noopener noreferrer"><svg width="18" height="18" viewBox="0 0 24 24" class="s-ico" aria-hidden="true"><path d="M21 4L3 11l6 2 2 6 3-4 5 4 2-15z"/><path d="M9 13l8-6"/></svg>Telegram</a>
  </nav>
</div></header>

<section class="s-kahraman" id="kahraman">
  <div class="s-kap s-kahraman-ic">
    <div class="s-kahraman-metin">
      <span class="s-etiket-ust">Etikete değil, fiyat geçmişine bak</span>
      <h1 class="s-h1">Fiyat gerçekten düştü mü?<br><span>Düşero söyler.</span></h1>
      <p class="s-giris-p">Fırsat kanallarındaki fiyatları sürekli izliyoruz. Her indirimi ürünün kayıtlı fiyatlarıyla karşılaştırıyor, şişirilmiş etiketleri ayıklıyor, sadece gerçek düşüşleri öne çıkarıyoruz.</p>
      <form class="s-ara-kutu" role="search" id="ustAraForm">
        <label class="s-sr" for="ustAra">Ürün ara</label>
        <input id="ustAra" class="s-alan s-buyuk" type="search" placeholder="Ürün, marka ya da model ara…" autocomplete="off">
        <button class="s-dugme s-buyuk" type="submit">Ara</button>
      </form>
      <ul class="s-ozellikler">
        <li><svg width="20" height="20" viewBox="0 0 24 24" class="s-ico" aria-hidden="true"><path d="M3 17l6-6 4 4 8-8"/><path d="M15 7h6v6"/></svg>Fiyat geçmişi</li>
        <li><svg width="20" height="20" viewBox="0 0 24 24" class="s-ico" aria-hidden="true"><path d="M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7l8-4z"/><path d="M9 12l2 2 4-4"/></svg>Gerçek indirim kontrolü</li>
        <li><svg width="20" height="20" viewBox="0 0 24 24" class="s-ico" aria-hidden="true"><path d="M6 8a6 6 0 1112 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10 21h4"/></svg>Anlık bildirim</li>
      </ul>
      <p class="s-durum" id="durum" role="status">Yükleniyor…</p>
      <div class="s-sayac" id="sayac"></div>
    </div>
    <aside id="one" class="s-one" hidden aria-label="Günün düşüşü"></aside>
  </div>
</section>

<main><div id="icerik"></div></main>

<section id="nasil" class="s-kap s-nasil">
  <h2 class="s-bolum-baslik">Nasıl çalışır?</h2>
  <div class="s-adimlar">
    <div class="s-adim"><span class="s-adim-no">1</span><h3>Takip ederiz</h3><p>Telegram fırsat kanallarını ve Akakçe listelerini gün boyu okur, her paylaşımı ve fiyatını kaydederiz.</p></div>
    <div class="s-adim"><span class="s-adim-no">2</span><h3>Karşılaştırırız</h3><p>Yeni fiyatı ürünün kayıtlı en düşük fiyatı ve ortalamasıyla kıyaslar, indirimin gerçek olup olmadığını etiketleriz.</p></div>
    <div class="s-adim"><span class="s-adim-no">3</span><h3>Haber veririz</h3><p>Gerçek düşüşler burada ve Telegram kanalında yayınlanır. İstediğin ürüne fiyat alarmı da kurabilirsin.</p></div>
  </div>
  <div class="s-aciklama">
    <span class="s-rozet k-dip">Gerçek indirim — kayıtlardaki en düşük fiyat</span>
    <span class="s-rozet k-iyi">İyi fiyat — ortalamanın altında</span>
    <span class="s-rozet k-sisirilmis">Şişirilmiş — etiket yüksek, fiyat ortalamada</span>
  </div>
</section>

<section id="telegram" class="s-tg-bant">
  <div class="s-tg-ic">
    <div style="flex:1 1 420px;min-width:0">
      <h2>Düşünce <span>haber verelim.</span></h2>
      <p>Gerçek indirimler düştüğü anda Düşero Telegram kanalına gelir. Uygulama indirmeden, ücretsiz.</p>
    </div>
    <div class="s-tg-sag">
      <a class="s-dugme s-buyuk s-turuncu" id="tgBantBtn" href="https://t.me/" target="_blank" rel="noopener noreferrer"><svg width="20" height="20" viewBox="0 0 24 24" class="s-ico" aria-hidden="true"><path d="M21 4L3 11l6 2 2 6 3-4 5 4 2-15z"/><path d="M9 13l8-6"/></svg>Telegram kanalına katıl</a>
      <small id="tgAd">t.me/</small>
    </div>
  </div>
</section>

<footer class="s-alt">
  <div class="s-kap s-alt-ic">
    <div>
      <span class="s-marka-ad s-kucuk-marka">düşero</span>
      <p>Düşero bağımsız bir fiyat takip aracıdır; satış yapmaz. Sitedeki bazı bağlantılar satış ortaklığı (reklam) bağlantısı olabilir; ödediğin fiyat değişmez. Fiyat ve stoklar mağazalarca anlık değişebilir; satın almadan önce mağazadaki son fiyatı kontrol et.</p>
    </div>
    <nav aria-label="Alt menü" class="s-alt-menu">
      <button type="button" class="s-link" data-git="tum">Fırsatlar</button>
      <button type="button" class="s-link" data-git="kupon">Kupon &amp; kampanya</button>
      <button type="button" class="s-link" data-git="market">Market fiyatları</button>
      <button type="button" class="s-link" data-git="takvim">Kampanya takvimi</button>
      <button type="button" class="s-link" data-git="takip">Fiyat alarmı ve kayıtlılar</button>
      <button type="button" class="s-link" data-git="ayar">Ayarlar</button>
    </nav>
  </div>
  <p class="s-telif">© 2026 Düşero</p>
</footer>
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
const tl = v => v == null ? "" : "₺" + nf.format(v);

const SEKMELER = [["kac","🔥 Kaçırılmaz"],["one","Öne çıkanlar"],["ucuz","Ucuzlayanlar"],["tum","Tüm ürünler"],["kupon","Kupon & kampanya"],["market","🛒 Market"],["takip","Takibim"],["takvim","Takvim"],["ayar","Ayarlar"]];
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

const S = {veri:null, sekme:"tum", limit:60, aralik:90, f:{ara:"", magaza:"hepsi", min:0, ikinciEl:true, sirala:"gercek", kategori:"hepsi", sure:{one:72, ucuz:72, tum:72, kupon:72, takip:0}, sureSurum:2, kuponTur:"hepsi", gecmisGizle:true}, zamanlayici:null, ayarTaslak:null};
try {
  const k = JSON.parse(localStorage.getItem("kf-filtre") || "null"); if (k) { const sure = Object.assign({}, S.f.sure, k.sureSurum === 2 ? (k.sure || {}) : {}); k.sureSurum = 2; Object.assign(S.f, k); S.f.sure = sure; if (!["hepsi", "kod", "kampanya"].includes(S.f.kuponTur)) S.f.kuponTur = "hepsi"; }
  const s = localStorage.getItem("kf-sekme"); if (s && SEKMELER.some(x => x[0] === s)) S.sekme = s;
} catch (e) {}
// Tasarımdaki tek sayfa: eski Kaçırılmaz / Öne çıkanlar / Ucuzlayanlar sekmeleri ana sayfada birleşti
if (["kac", "one", "ucuz"].includes(S.sekme)) S.sekme = "tum";
if (!["gercek", "indirim", "yeni", "ucuz"].includes(S.f.sirala)) S.f.sirala = "gercek";
try {
  // /ekle sayfasından ya da bir bağlantıdan belirli sekmeyle açılma: /?sekme=takip
  const qs = new URLSearchParams(location.search).get("sekme");
  if (qs && SEKMELER.some(x => x[0] === qs)) { S.sekme = ["kac", "one", "ucuz"].includes(qs) ? "tum" : qs; history.replaceState(null, "", location.pathname); }
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
const TAKIBIM = "takibim";
function kanalAd(k){ if (k === TAKIBIM) return "Kendi eklediğim"; const x = (S.veri?.kaynaklar || []).find(z => z.kanal.toLowerCase() === String(k).toLowerCase()); return x ? x.ad : k; }

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
  const v = S.veri;
  if (!v) return;
  const tg = String((v.yayin && v.yayin.telegram) || "").replace(/^@/, "").trim();
  ["#tgBtn", "#tgBantBtn"].forEach(q => { const e = $(q); if (e) e.href = tg ? "https://t.me/" + encodeURIComponent(tg) : "https://t.me/"; });
  const ad = $("#tgAd"); if (ad) ad.textContent = tg ? "t.me/" + tg : "Kanal adı Ayarlar → Herkese açık site bölümünden girilir";
  const hatali = v.kaynaklar.filter(k => k.ok === false).length;
  const urun = v.firsatlar.filter(x => x.tur === "urun").length;
  let m = v.son_guncelleme ? `Son güncelleme ${saat(v.son_guncelleme)} · ${urun} ürün` : (v.yenileniyor ? "İlk veriler çekiliyor…" : "Henüz veri yok");
  $("#durum").innerHTML = esc(m) + (hatali ? ` · <button class="hata" type="button" data-git="ayar">${hatali} kaynakta hata</button>` : "")
    + ` · <button class="s-link" type="button" id="yenileBtn">${v.yenileniyor ? "Yenileniyor…" : "Şimdi yenile"}</button>`;
  const sy = SAYAC.map(([a, t]) => { const g = gunFarki(t); return g < 0 ? "" : `<span>${esc(a)} · <b>${g === 0 ? "bugün" : g + " gün"}</b></span>`; }).join("");
  $("#sayac").innerHTML = sy;
  $("#sayac").style.display = sy ? "" : "none";
}

const KATEGORI = [["elektronik","Telefon, TV & elektronik"],["bilgisayar","Bilgisayar, oyun & donanım"],["beyaz","Beyaz eşya & ev aletleri"],["ev","Ev, mutfak & mobilya"],["gida","Gıda & içecek"],["temizlik","Temizlik & kâğıt ürünleri"],["kozmetik","Kişisel bakım & kozmetik"],["giyim","Giyim, ayakkabı & aksesuar"],["bebek","Anne, bebek & oyuncak"],["spor","Spor & outdoor"],["yapi","Yapı market & bahçe"],["oto","Oto, motosiklet & scooter"],["evcil","Evcil hayvan"],["oyun","Dijital oyunlar"],["diger","Diğer"]];
function kayitlilar(){ try { return JSON.parse(localStorage.getItem("kf-kayitli") || "{}") || {}; } catch (e) { return S.kayitliYedek || {}; } }
function kayitliYaz(k){ S.kayitliYedek = k; try { localStorage.setItem("kf-kayitli", JSON.stringify(k)); } catch (e) {} }

function sekmeVerisi(sekme){
  const d = S.veri ? S.veri.firsatlar.filter(x => !x.bitti) : [];
  if (sekme === "kac") return d.filter(x => x.kacirilmaz && !bittiMi(x));
  if (sekme === "one") return d.filter(x => x.tur === "urun" && !bittiMi(x) && !x.kontrol_et && !(dogrulanamadi(x) && Date.now() / 1000 - x.ts > 86400) && (oneIndirim(x) >= 15 || (x.akakce_fark || 0) >= 15 || x.rozetler.includes("dip") || x.rozetler.includes("kayit_dusuk")));
  if (sekme === "ucuz") return d.filter(x => x.tur === "urun" && x.fiyat_ozet && (x.fiyat_ozet.dusus_son >= 3 || x.fiyat_ozet.dusus_ort >= 5));
  if (sekme === "tum") return d.filter(x => x.tur === "urun" && !sadeceTakipAramasi(x));
  if (sekme === "kupon") return d.filter(x => (x.tur === "kupon" || x.tur === "kampanya") && !x.urun_gibi);
  if (sekme === "takip") return d.filter(x => x.takip && x.takip.length);
  return [];
}
const SAAT = 3600;
// Akakçe'de yalnızca takip kelimesi için aranan ürün (indirim listesinden gelmedi): yalnızca Takibim sekmesinde
// Öne çıkanlar için indirim: "3 al 2 öde" gibi çoklu alım kampanyası tek başına indirim sayılmaz
// (ürünün kendi fiyatı düşmemiştir); mağazada fiyatı yükselmiş ürün de girmez.
function oneIndirim(x){
  if (x.xaly && String(x.indirim_kaynak || "").startsWith(x.xaly)) return 0;
  const c = x.canli;
  if (c && c.durum === "ok" && c.fiyat && x.fiyat && c.fiyat > x.fiyat * 1.05 && c.karar !== "kampanya" && c.karar !== "sepette") return 0;
  return x.indirim || 0;
}
function sadeceTakipAramasi(x){ const l = x.akakce_listeler || []; return l.length === 1 && l[0] === "takip"; }
// Bitiş tarihi yazılmayan kupon/kampanya kodları genelde 1-3 gün içinde biter ya da limiti dolar
const KUPON_OMUR_SAAT = 72;
function kuponEski(x){ return (x.tur === "kupon" || x.tur === "kampanya") && !x.son_tarih && Date.now() / 1000 - x.ts > KUPON_OMUR_SAAT * SAAT; }
function suresiGecti(x){ return (!!x.son_tarih && new Date(x.son_tarih).getTime() < Date.now()) || kuponEski(x); }
function sonGunYazi(x){
  if (kuponEski(x)) return ["3 günden eski · büyük olasılıkla geçersiz", "r-kirmizi"];
  if (!x.son_tarih && x.tur === "kupon") return ["Bitiş tarihi yazılmamış", "r-sari"];
  if (!x.son_tarih) return null;
  const d = new Date(x.son_tarih);
  if (d.getTime() < Date.now()) return ["Süresi geçti", "r-kirmizi"];
  const g = gunFarki(x.son_tarih.slice(0, 10));
  if (g <= 0) return ["Bugün bitiyor", "r-kirmizi"];
  if (g === 1) return ["Yarın bitiyor", "r-sari"];
  return ["Son gün: " + d.toLocaleDateString("tr-TR", {day:"numeric", month:"long"}), "r-mavi"];
}

function navCiz(){
  document.querySelectorAll(".s-menu [data-git]").forEach(b => { if (b.dataset.git === S.sekme) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current"); });
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
// "telefonlar", "laptopu", "kulaklığı" gibi ekli yazımlarda kökü de dene
const EKLER = ["larini","lerini","larin","lerin","lari","leri","lar","ler","sini","sunu","si","su","yi","yu","ni","nu","i","u","e","a"];
function kok(p){
  if (p.length < 5 || /\d/.test(p)) return null;
  for (const e of EKLER) if (p.endsWith(e) && p.length - e.length >= (e.length === 1 ? 6 : 4)) return p.slice(0, -e.length);
  return null;
}
function secenekler(p){ const a = ESANLAM[p] || [p], k = kok(p); return k && !a.includes(k) ? a.concat([k]) : a; }
function aramaEslesir(x, parca){
  const m = sadeJS(x.baslik + " " + (x.metin || "") + " " + (x.kodlar || []).join(" ") + " " + (x.magaza_ad || "")), mb = bitisik(m);
  return parca.every(p => secenekler(p).some(a => m.includes(a) || (bitisik(a).length >= 3 && mb.includes(bitisik(a)))));
}
function aramaPuani(x, q, parca){
  const b = sadeJS(x.baslik);
  if (b.includes(q) || bitisik(b).includes(bitisik(q))) return 0;
  if (parca.every(p => secenekler(p).some(a => b.includes(a) || bitisik(b).includes(bitisik(a))))) return 1;
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
    if (f.gercek && S.sekme !== "kupon") d = d.filter(x => x.tur === "urun" && ["dip", "iyi"].includes(kararHesap(x).kod));
  }
  if (q && arama) { const parca = aramaParcalari(q); d = d.filter(x => aramaEslesir(x, parca)); d.forEach(x => x._puan = aramaPuani(x, q, parca)); }
  const sure = arama ? 0 : ((f.sure || {})[S.sekme] || 0);
  if (sure > 0) { const sinir = Date.now() / 1000 - sure * SAAT; d = d.filter(x => x.ts >= sinir); }
  if (S.sekme === "kupon" && !arama) {
    if (f.kuponTur === "kod") d = d.filter(x => x.tur === "kupon");
    else if (f.kuponTur === "kampanya") d = d.filter(x => x.tur === "kampanya");
    if (f.gecmisGizle) d = d.filter(x => !suresiGecti(x));
  }
  const dip = x => x.rozetler.includes("dip") || x.rozetler.includes("kayit_dusuk") ? 1 : 0;
  const dusus = x => x.fiyat_ozet ? Math.max(x.fiyat_ozet.dusus_son, x.fiyat_ozet.dusus_ort) : -1;
  if (S.sekme === "kac" && !arama) return d.slice().sort((a, b) => ((b.kacirilmaz || {}).guc || 0) - ((a.kacirilmaz || {}).guc || 0) || (b.indirim || 0) - (a.indirim || 0) || b.ts - a.ts);
  if (S.sekme === "ucuz" && f.sirala === "indirim" && !arama) return d.slice().sort((a, b) => (dusus(b) - dusus(a)) || (b.ts - a.ts));
  const gd = x => { const k = kararHesap(x); return k.gercek != null ? k.gercek : (["dip", "iyi"].includes(k.kod) ? 0 : -999); };
  const s = {
    gercek: (a, b) => (gd(b) - gd(a)) || (dip(b) - dip(a)) || ((b.indirim ?? -1) - (a.indirim ?? -1)) || (b.ts - a.ts),
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
function bittiMi(x){ const c = canliSonuc(x); return !!c && ((c.durum === "ok" && (c.karar === "bitti" || c.karar === "sepet_yok" || c.karar === "farkli_urun")) || c.durum === "stok_yok"); }
function canliSonuc(x){ const c = (S.canli || {})[x.id] || x.canli; return c && !c.yukleniyor ? c : null; }
function fiyatBlok(x){
  const c = canliSonuc(x);
  if (x.fiyat != null && c && c.durum === "ok" && c.karar === "farkli_urun")
    return `<div class="fiyatlar"><div class="fsatir eski"><span class="fet">Paylaşılan fiyat</span><span class="fdeger"><s>${tl(x.fiyat)}</s></span></div><div class="fdurum c-kirmizi">${esc("❌ " + (c.metin || "Bağlantı başka bir ürüne gidiyor"))}</div></div>`;
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

// ---- Düşero kartları (herkese açık siteyle aynı düzen) ----
function kararHesap(x){
  const o = x.fiyat_ozet, sup = x.eski_fiyat_supheli, r = x.rozetler || [];
  const gercek = o ? Math.round(o.dusus_ort) : (sup ? Math.round(sup.gercek_yuzde) : null);
  if (sup) return {kod: "sisirilmis", ad: "Şişirilmiş indirim", gercek, etiket: `Üstü çizili ${tl(sup.eski)} kayıtlarda hiç görülmedi`};
  if (o && o.en_dusuge_gore <= 0.5 && o.dusus_ort >= 3) return {kod: "dip", ad: "Gerçek indirim", gercek, etiket: "Kayıtlardaki en düşük fiyat"};
  if (x.dusus_etiketi || r.includes("dip") || r.includes("kayit_dusuk")) return {kod: "dip", ad: "Gerçek indirim", gercek, etiket: x.dusus_etiketi || "Dip fiyat"};
  if (o && o.dusus_ort >= 5) return {kod: "iyi", ad: "İyi fiyat", gercek, etiket: `Normal fiyatın %${Math.round(o.dusus_ort)} altında`};
  if (o && x.indirim) return {kod: "sisirilmis", ad: "Şişirilmiş indirim", gercek, etiket: `Çoğunlukla ${tl(o.ortalama)} civarında paylaşıldı`};
  return {kod: "yok", ad: "Geçmiş az", gercek, etiket: null};
}
function miniGrafik(x, buyuk){
  const n = (x.fiyat_gecmisi || []).map(([t, f]) => [t, f]).concat([[x.ts, x.indirimli_fiyat ?? x.fiyat]]).filter(z => z[1] != null);
  if (n.length < 2) return "";
  const G = buyuk ? 400 : 96, Y = buyuk ? 120 : 32, P = buyuk ? 10 : 4;
  const normal = buyuk && x.fiyat_ozet ? x.fiyat_ozet.ortalama : null;
  const fs = n.map(z => z[1]).concat(normal ? [normal] : []);
  const mn = Math.min(...fs), mx = Math.max(...fs), ar = (mx - mn) || 1, t0 = n[0][0], ta = (n[n.length - 1][0] - t0) || 1;
  const xy = (t, f) => [((t - t0) / ta * G).toFixed(1), (mx === mn ? Y / 2 : P + (1 - (f - mn) / ar) * (Y - 2 * P)).toFixed(1)];
  const c = n.map(([t, f]) => xy(t, f).join(",")).join(" ");
  const alan = buyuk ? `<polygon class="alan-dolgu" points="0,${Y} ${c} ${G},${Y}"/>` : "";
  const ny = normal ? (y => `<line class="normal-cizgi" x1="0" x2="${G}" y1="${y}" y2="${y}"/>`)(xy(t0, normal)[1]) : "";
  return `<svg ${buyuk ? 'class="s-grafik" ' : ""}viewBox="0 0 ${G} ${Y}" preserveAspectRatio="none" aria-hidden="true">${alan}${ny}<polyline ${buyuk ? 'class="cizgi" ' : ""}points="${c}"/></svg>`;
}
const saatYaz = ts => new Date(ts * 1000).toLocaleTimeString("tr-TR", {hour: "2-digit", minute: "2-digit"});
const IKON_GRAFIK = `<svg width="20" height="20" viewBox="0 0 24 24" class="s-ico" aria-hidden="true"><path d="m3 17 6-6 4 4 8-8"/></svg>`;
const IKON_PAYLAS = `<svg width="20" height="20" viewBox="0 0 24 24" class="s-ico" aria-hidden="true"><path d="M4 12v7a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-7"/><path d="M12 3v12"/><path d="m7 8 5-5 5 5"/></svg>`;

function kart(x){
  const id = esc(x.id), res = link(x.resim), hedef = link(x.urun_link) || link(x.link);
  const k = kararHesap(x), cs = canliSonuc(x), bitti = bittiMi(x);
  const fiyat = x.indirimli_fiyat ?? x.fiyat;
  const birim = (x.sayfa_tahmini || (x.indirimli_not && /adet fiyatı/.test(x.indirimli_not))) ? "<small> / adet</small>" : "";
  const eski = x.satis_fiyati != null && fiyat != null && x.satis_fiyati > fiyat * 1.01 ? tl(x.satis_fiyati) : "";
  const yas = Date.now() / 1000 - x.ts;
  const notlar = [];
  if (x.kacirilmaz && !bitti && (x.kacirilmaz.neden || []).length) notlar.push(["iyi", "Neden kaçırılmaz: " + x.kacirilmaz.neden.join(", ")]);
  if (bitti) notlar.push(["uyari", cs && cs.durum === "stok_yok" ? "Ürün şu anda stokta yok" : "Fırsat bitmiş görünüyor"]);
  else if (cs && cs.durum === "ok") notlar.push([cs.renk === "yesil" ? "iyi" : cs.renk === "kirmizi" ? "uyari" : "", `Mağazada ${cs.ts ? saatYaz(cs.ts) : "şu an"}: ${tl(cs.fiyat)}${cs.metin ? " · " + cs.metin : ""}`]);
  else if (dogrulanamadi(x)) notlar.push(["uyari", `Fiyat mağazada doğrulanamadı · ${sureMetni(yas)} önce paylaşıldı`]);
  else if (x.dogrulama && x.dogrulama.durum === "onayli") notlar.push(["iyi", `Mağazada doğrulandı · ${saatYaz(x.dogrulama.ts)}`]);
  if (x.kosul) notlar.push(["", "İndirimli fiyat: " + x.kosul]);
  else if (x.indirimli_not) notlar.push(["", x.indirimli_not]);
  else if (x.sepet_fiyati) notlar.push(["", "Fiyat sepette oluşur; ürün sayfasında daha yüksek görünür"]);
  const akd = x.akakce_dogrulama;
  if (akd && !bitti) notlar.push(["", akd.fark <= 1 ? "Akakçe’deki en ucuz fiyat seviyesinde" : `Akakçe’de en ucuzu ${tl(akd.en_ucuz)}`]);
  else if (x.akakce_fark) notlar.push(["", `Akakçe’de diğer satıcılardan %${x.akakce_fark} ucuz`]);
  if (x.kontrol_et) notlar.push(["uyari", "Fiyat şüpheli derecede düşük; kontrol et"]);
  if (yas > 3 * 86400) notlar.push(["uyari", `${tarihKisa(x.ts)} tarihli fiyat · bugün geçerli olmayabilir`]);
  (x.takip || []).forEach(t => notlar.push(["iyi", "Takip: " + t]));
  if (x.alarm_kurulu) notlar.push(["iyi", "Hedef fiyat: " + tl(x.alarm_kurulu.hedef)]);
  const kodlar = (x.kodlar || []).map(z => `<button type="button" class="kod" data-kod="${esc(z)}" title="Kopyala">${esc(z)} ⧉</button>`).join("");
  const ozet = k.gercek != null && (k.gercek > 0 || !["dip", "iyi"].includes(k.kod))
    ? `Gerçek düşüş: <strong>${k.gercek > 0 ? "%" + k.gercek : "yok"}</strong>${x.fiyat_ozet ? ` · kayıtlı en düşük: ${tl(x.fiyat_ozet.en_dusuk)}` : ""}`
    : (k.etiket ? esc(k.etiket) : (x.tur === "urun" ? "Fiyat geçmişi birikiyor." : ""));
  const oto = x.tur === "urun" && link(x.urun_link) && !cs ? ` data-oto="${id}"` : "";
  const ust = yas < 3600 ? `<span class="s-yeni">Yeni</span>` : "";
  const kac = x.kacirilmaz && !bitti;
  return `<article class="s-kart${bitti ? " s-bitti" : ""}${kac ? " s-kac-kart" : ""}"${oto}>
    <div class="s-gorsel${res ? "" : " yok"}" data-detay="${id}" role="button" tabindex="0" aria-label="${esc(x.baslik)}">${res ? `<img src="${esc(res)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.parentNode.classList.add('yok');this.remove()">` : ""}${x.indirim != null && !bitti ? `<span class="s-oran-rozet">%${x.indirim}${x.varan ? "’e varan" : ""}</span>` : ""}${kac ? `<span class="s-kac-etiket">🔥 Kaçırılmaz</span>` : ""}</div>
    <div class="s-ust-satir"><span class="s-magaza">${esc(x.magaza_ad)}</span><span class="s-sag">${ust}<span>${once(x.ts)}</span></span></div>
    <h3 class="s-baslik"><a href="#" data-detay="${id}">${esc(x.baslik)}</a></h3>
    <div class="s-fiyat-satir"><span class="s-fiyat">${fiyat != null ? tl(fiyat) + birim : "—"}</span>${eski ? `<s class="s-eski">${eski}</s>` : ""}</div>
    <div class="s-karar-satir"><span class="s-rozet k-${k.kod}"${k.etiket ? ` title="${esc(k.etiket)}"` : ""}>${k.ad}</span><span class="s-mini k-${k.kod}">${miniGrafik(x)}</span></div>
    <p class="s-ozet-satir">${ozet}</p>
    <ul class="s-notlar">${notlar.map(([c, m]) => `<li${c ? ` class="${c}"` : ""}>${esc(m)}</li>`).join("")}${kodlar ? `<li>${kodlar}</li>` : ""}</ul>
    <div class="s-kart-alt">${hedef ? `<a class="s-dugme s-git" href="${esc(hedef)}" target="_blank" rel="noopener noreferrer">Fırsata git</a>` : ""}<button type="button" class="s-dugme s-ikincil s-kare" data-detay="${id}" aria-label="Fiyat geçmişi ve ayrıntılar">${IKON_GRAFIK}</button></div>
  </article>`;
}

function oneCikan(){
  // Önce kaçırılmaz fırsatlar (en güçlüsü), yoksa son 2 günün en güçlü gerçek indirimi
  if (!S.veri) return null;
  const simdi = Date.now() / 1000;
  const kac = sekmeVerisi("kac").filter(x => x.fiyat != null).sort((a, b) => ((b.kacirilmaz || {}).guc || 0) - ((a.kacirilmaz || {}).guc || 0) || b.ts - a.ts);
  if (kac.length) return kac[0];
  const d = S.veri.firsatlar.filter(x => x.tur === "urun" && !x.bitti && !bittiMi(x) && x.fiyat != null && (x.fiyat_gecmisi || []).length && simdi - x.ts < 2 * 86400);
  const puan = x => { const k = kararHesap(x); return (k.kod === "dip" ? 1000 : 500) + (k.gercek || 0); };
  return d.filter(x => ["dip", "iyi"].includes(kararHesap(x).kod)).sort((a, b) => puan(b) - puan(a))[0] || null;
}
function vitrinCiz(){
  const kutu = $("#one"); if (!kutu) return;
  const aday = oneCikan();
  kutu.hidden = !aday;
  if (!aday) { kutu.innerHTML = ""; return; }
  const k = kararHesap(aday), o = aday.fiyat_ozet, fiyat = aday.indirimli_fiyat ?? aday.fiyat;
  const eski = aday.satis_fiyati != null && aday.satis_fiyati > fiyat * 1.01 ? `<s class="s-eski">${tl(aday.satis_fiyati)}</s>` : "";
  const grafik = miniGrafik(aday, true);
  kutu.innerHTML = `<div class="s-one-ust"><span class="s-ust-yazi">${aday.kacirilmaz ? "🔥 Kaçırılmaz fırsat" : "Günün düşüşü"}</span><span class="s-rozet k-${k.kod}">${k.ad}</span></div>
    <div><div class="s-one-magaza">${esc(aday.magaza_ad)} · ${once(aday.ts)}</div><h2 class="s-one-baslik">${esc(aday.baslik)}</h2></div>
    <div class="s-fiyat-satir"><span class="s-fiyat s-dev">${tl(fiyat)}</span>${eski}</div>
    ${grafik || `<div class="s-grafik-yok" style="min-height:120px">Fiyat geçmişi birikiyor</div>`}
    <div class="s-kutucuklar">
      <div class="s-kutucuk"><span>Mağazanın dediği</span><b>${aday.indirim != null ? "%" + aday.indirim : "—"}</b></div>
      <div class="s-kutucuk s-mavi"><span>Gerçek düşüş</span><b>${k.gercek != null && k.gercek > 0 ? "%" + k.gercek : (["dip", "iyi"].includes(k.kod) ? "dip fiyat" : k.gercek != null ? "yok" : "—")}</b></div>
      <div class="s-kutucuk"><span>Ortalama fiyat</span><b>${o ? tl(o.ortalama) : "—"}</b></div>
    </div>
    <button type="button" class="s-dugme s-ikincil" data-detay="${esc(aday.id)}">Fiyat geçmişini incele</button>`;
}
function buyukGrafik(x){
  const hepsi = (x.fiyat_gecmisi || []).map(([t, f]) => [t, f]).concat([[x.ts, x.indirimli_fiyat ?? x.fiyat]]).filter(z => z[1] != null);
  const sinir = Date.now() / 1000 - (S.aralik || 90) * 86400;
  let n = hepsi.filter(z => z[0] >= sinir);
  if (n.length < 2 && hepsi.length >= 2) n = hepsi.slice(-2);
  if (n.length < 2) return {html: `<div class="s-grafik-yok"><span>Bu ürün için henüz fiyat geçmişi yok. Kayıtlar biriktikçe grafik burada görünecek.</span>${x.arsiv_arandi ? "" : `<button type="button" class="s-dugme s-ikincil gecmis-btn" data-gecmis="${esc(x.id)}">Kanal arşivinden geçmiş fiyatları getir</button>`}</div>`, n};
  const W = 800, H = 280, P = 24, fs = n.map(z => z[1]);
  const lo = Math.min(...fs), hi = Math.max(...fs), ar = (hi - lo) || 1, t0 = n[0][0], ta = (n[n.length - 1][0] - t0) || 1;
  const srt = fs.slice().sort((a, b) => a - b), m = Math.floor(srt.length / 2), med = srt.length % 2 ? srt[m] : (srt[m - 1] + srt[m]) / 2;
  const y = f => (hi === lo ? H / 2 : P + (1 - (f - lo) / ar) * (H - 2 * P)).toFixed(1);
  const c = n.map(([t, f]) => ((t - t0) / ta * W).toFixed(1) + "," + y(f)).join(" ");
  const gun = Math.max(1, Math.round((Date.now() / 1000 - t0) / 86400));
  return {n, lo, hi, med, html: `<div class="s-grafik-kutu"><svg class="s-grafik s-grafik-buyuk" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(`Son ${gun} günün fiyat grafiği; en düşük ${tl(lo)}, en yüksek ${tl(hi)}`)}">
      <line class="normal-cizgi" x1="0" y1="${y(med)}" x2="${W}" y2="${y(med)}"/><polygon class="alan-dolgu" points="0,${H} ${c} ${W},${H}"/><polyline class="cizgi" points="${c}"/></svg>
      ${hi === lo ? `<span class="s-grafik-etiket ust-e">Fiyat değişmedi: ${tl(hi)}</span>` : `<span class="s-grafik-etiket ust-e">En yüksek ${tl(hi)}</span><span class="s-grafik-etiket alt-e">En düşük ${tl(lo)}</span>`}</div>
    <div class="s-grafik-tarih"><span>${gun} gün önce · ${tarihKisa(t0)}</span><span>Bugün</span></div>`};
}

function kararAciklama(x, k){
  const o = x.fiyat_ozet, ind = x.indirim != null ? `Mağaza %${x.indirim} indirim gösteriyor` : "Mağaza bir indirim oranı göstermiyor";
  if (k.kod === "sisirilmis") return `${ind} ama ürün çoğunlukla <strong>${o ? tl(o.ortalama) : esc(k.etiket || "")}</strong> civarında paylaşıldı. Gerçek düşüş <strong>${k.gercek > 0 ? "%" + k.gercek : "yok"}</strong>.`;
  if (k.kod === "dip") return `${ind}${o ? `; ürün kayıtlarda çoğunlukla <strong>${tl(o.ortalama)}</strong> civarındaydı` : ""}. ${k.gercek > 0 ? `Gerçek düşüş <strong>%${k.gercek}</strong> — ` : ""}kayıtlardaki en düşük fiyat.`;
  if (k.kod === "iyi") return `${ind}. Fiyat kayıtlı ortalamanın <strong>%${k.gercek}</strong> altında; en düşük fiyat değil ama iyi bir fiyat.`;
  return `${ind}. Bu ürünün henüz yeterli fiyat kaydı yok; indirimin gerçek olup olmadığını söylemek için kayıtların birikmesi gerekiyor.`;
}

function detayHTML(x){
  const o = x.fiyat_ozet, res = link(x.resim), hedef = link(x.urun_link) || link(x.link);
  const k = kararHesap(x), cs = canliSonuc(x), fiyat = x.indirimli_fiyat ?? x.fiyat;
  const eski = x.satis_fiyati != null && fiyat != null && x.satis_fiyati > fiyat * 1.01 ? tl(x.satis_fiyati) : "";
  const kayitli = !!kayitlilar()[x.id];
  const tg = String((S.veri && S.veri.yayin && S.veri.yayin.telegram) || "").replace(/^@/, "");
  const tgLink = tg ? "https://t.me/" + encodeURIComponent(tg) : "https://t.me/";
  const kontrol = cs && cs.durum === "ok" && cs.ts ? `mağazada ${saatYaz(cs.ts)}’te kontrol edildi` : `${once(x.ts)} paylaşıldı`;
  const uyarilar = [];
  if (Date.now() / 1000 - x.ts > 3 * 86400) uyarilar.push(`Bu fiyat ${tarihKisa(x.ts)} tarihli bir paylaşımdan; bugünkü fiyat farklı olabilir.`);
  if (dogrulanamadi(x)) uyarilar.push(`Fiyat mağazada doğrulanamadı · ${sureMetni(Date.now() / 1000 - x.ts)} önce paylaşıldı. Sepetteki tutara bakın.`);
  if (x.kosul) uyarilar.push("İndirimli fiyat: " + x.kosul);
  else if (x.indirimli_not) uyarilar.push(x.indirimli_not);
  const kodlar = (x.kodlar || []).map(z => `<button type="button" class="kod" data-kod="${esc(z)}" title="Kopyala">${esc(z)} ⧉</button>`).join("");
  const g = buyukGrafik(x);
  const ort = g.n && g.n.length >= 2 ? g.n.reduce((t, z) => t + z[1], 0) / g.n.length : null;
  const sekmeler = [30, 90, 180].map(v => `<button type="button" data-aralik="${v}" aria-pressed="${S.aralik === v}">${v} gün</button>`).join("");
  const istat = g.lo != null ? `<div class="u-istat">
      <div><span>Şu an</span><b>${tl(fiyat)}</b></div>
      <div><span>Dönem en düşük</span><b>${tl(g.lo)}</b></div>
      <div><span>Dönem ortalaması</span><b>${tl(ort)}</b></div>
      <div><span>Dönem en yüksek</span><b>${tl(g.hi)}</b></div></div>` : "";
  const mg = (x.magazalar || []).length ? x.magazalar : [{magaza_ad: x.magaza_ad, fiyat, ts: x.ts, link: hedef}];
  const enUcuz = Math.min(...mg.map(m => m.fiyat ?? 1e12));
  const magazaSatir = mg.map(m => `<tr><td style="font-weight:600">${esc(m.magaza_ad)}</td><td style="font-weight:700">${tl(m.fiyat)}${m.sepette ? " <small>sepette</small>" : ""}</td>
      <td style="color:var(--soluk)">${m.fiyat === enUcuz ? "En ucuz" : "+" + tl(m.fiyat - enUcuz)}</td><td style="color:var(--soluk)">${once(m.ts)}</td>
      <td style="text-align:right">${link(m.link) ? `<a class="s-dugme s-ikincil" style="min-height:40px;border-radius:10px;font-size:14px" href="${esc(link(m.link))}" target="_blank" rel="noopener noreferrer">Mağazaya git</a>` : ""}</td></tr>`).join("");
  const kayitlar = (x.fiyat_gecmisi || []).map(([ts, f]) => ({ts, f})).concat([{ts: x.ts, f: x.fiyat, simdi: true}]);
  const kayitSatir = kayitlar.slice().reverse().map((z, i, a) => { const onceki = a[i + 1]; const deg = onceki ? (z.f - onceki.f) / onceki.f * 100 : null;
    return `<tr><td>${new Date(z.ts * 1000).toLocaleString("tr-TR", {day:"numeric", month:"short", year:"numeric", hour:"2-digit", minute:"2-digit"})}${z.simdi ? " · bu paylaşım" : ""}</td><td>${tl(z.f)}</td><td>${deg == null || Math.abs(deg) < 0.5 ? "–" : `<span class="${deg < 0 ? "dus" : "art"}">${deg < 0 ? "▼" : "▲"} %${nf.format(Math.abs(Math.round(deg * 10) / 10))}</span>`}</td></tr>`; }).join("");
  const alarm = x.alarm_kurulu;
  return `<header class="s-ust"><div class="s-kap s-ust-ic">
      <a class="s-marka" href="#" data-detaykapat="1" aria-label="Düşero ana sayfa"><svg width="36" height="36" viewBox="0 0 36 36" aria-hidden="true"><rect width="36" height="36" rx="10" fill="#C2380A"/><path d="M18 9v15m0 0l-6-6m6 6l6-6" fill="none" stroke="#FFFFFF" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/><path d="M10 28h16" stroke="#FFFFFF" stroke-width="3" stroke-linecap="round"/></svg><span class="s-marka-ad">düşero</span></a>
      <nav class="s-menu" aria-label="Ana menü"><button type="button" class="s-menu-link" data-detaykapat="1">Fırsatlar</button><button type="button" class="s-menu-link" data-kaydir-detay="alarm">Fiyat alarmı</button>
      <a class="s-dugme" href="${esc(tgLink)}" target="_blank" rel="noopener noreferrer">Telegram</a></nav></div></header>
    <main class="u-ana">
      <nav class="u-yol" aria-label="Konum"><button type="button" class="s-link" data-detaykapat="1">Ana sayfa</button><span aria-hidden="true">›</span><span>${esc(x.kategori_ad || "Ürün")}</span><span aria-hidden="true">›</span><span>${esc(x.magaza_ad || "")}</span></nav>
      <section class="u-ust">
        <div class="u-kutu u-sol">
          <div class="u-gorsel">${res ? `<img src="${esc(res)}" alt="" referrerpolicy="no-referrer" onerror="this.parentNode.classList.add('yok');this.remove()">` : `<svg width="52" height="52" viewBox="0 0 24 24" class="s-ico" aria-hidden="true"><path d="M21 8l-9-5-9 5 9 5 9-5z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/></svg>Ürün görseli yok`}</div>
          <div class="u-enucuz">${mg.length > 1 ? "En ucuz" : "Mağaza"}: <strong>${esc(mg.length > 1 ? mg.find(m => m.fiyat === enUcuz).magaza_ad : x.magaza_ad)}</strong> · ${kontrol}</div>
          <h1 class="u-baslik">${esc(x.baslik)}</h1>
          <div class="u-fiyat"><span class="s-fiyat">${tl(fiyat)}</span>${eski ? `<s>${eski}</s>` : ""}${x.indirim != null ? `<span class="u-oran">%${x.indirim}</span>` : ""}</div>
          <div class="u-karar k-${k.kod}"><span class="s-rozet k-${k.kod}">${k.ad}</span><p>${kararAciklama(x, k)}</p></div>
          ${x.kacirilmaz ? `<div class="u-karar k-dip"><span class="s-rozet k-kac">🔥 Kaçırılmaz</span><p>${esc((x.kacirilmaz.neden || []).join(" · "))}</p></div>` : ""}
          ${uyarilar.map(u => `<div class="u-uyari">⚠️ ${esc(u)}</div>`).join("")}
          ${kodlar ? `<div class="s-kodlar">${kodlar}</div>` : ""}
          ${hedef ? `<a class="s-dugme s-buyuk" href="${esc(hedef)}" target="_blank" rel="noopener noreferrer">${esc(x.magaza_ad || "Mağaza")}’da gör</a>` : ""}
          ${canliBlok(x)}
          <div class="u-dugmeler">
            ${link(x.akakce) ? `<a class="s-dugme s-ikincil" href="${esc(x.akakce)}" target="_blank" rel="noopener noreferrer">Akakçe</a>` : ""}
            ${link(x.cimri) ? `<a class="s-dugme s-ikincil" href="${esc(x.cimri)}" target="_blank" rel="noopener noreferrer">Cimri</a>` : ""}
            <button type="button" class="s-dugme s-ikincil" data-kaydet="${esc(x.id)}" aria-pressed="${kayitli}">${kayitli ? "★ Kaydedildi" : "☆ Kaydet"}</button>
            <button type="button" class="s-dugme s-ikincil" data-paylas="${esc(x.id)}">Paylaş</button>
            ${link(x.link) ? `<a class="s-dugme s-ikincil" href="${esc(x.link)}" target="_blank" rel="noopener noreferrer">Kaynak paylaşım</a>` : ""}
          </div>
          <details class="u-ayrinti"><summary>Fiyat ayrıntısı</summary>${fiyatBlok(x)}</details>
        </div>
        <div class="u-kutu u-sag">
          <div class="u-gecmis-bas"><h2 class="u-h2">Fiyat geçmişi</h2><div class="u-sekmeler" role="group" aria-label="Dönem">${sekmeler}</div></div>
          ${g.html}
          ${istat}
          ${o ? `<p class="s-not">Kaynak: fırsat kanallarının ${tarihKisa(o.ilk_ts)} tarihinden beri bu ürün için paylaştığı fiyatlar. Kesikli çizgi dönemin ortanca fiyatıdır.</p>` : ""}
        </div>
      </section>
      <section class="u-tablo-kutu">
        <h2 class="u-h2">Mağaza karşılaştırması</h2>
        <div class="u-tablo-kaydir"><table class="u-tablo"><thead><tr><th scope="col">Mağaza</th><th scope="col">Fiyat</th><th scope="col">Fark</th><th scope="col">Son kontrol</th><th scope="col"><span class="s-sr">Bağlantı</span></th></tr></thead><tbody>${magazaSatir}</tbody></table></div>
      </section>
      <section id="alarm" class="u-alt-iki">
        <div class="u-kutu u-alarm">
          <h2 class="u-h2">Fiyat alarmı kur</h2>
          <p>Hedef fiyatını yaz; ürün o fiyata ya da altına düştüğünde ${S.veri && S.veri.bildirim_var ? "telefona bildirim gelir" : "Fiyat alarmı sayfasında işaretlenir (telefona bildirim için Ayarlar → Telefona bildirim)"}.</p>
          <div class="u-alarm-form">
            <div class="u-alan-kutu"><label for="hedefTL-${esc(x.id)}">Hedef fiyat (₺)</label><input id="hedefTL-${esc(x.id)}" type="number" inputmode="numeric" min="1" step="1" value="${alarm ? alarm.hedef : Math.floor((x.fiyat || 0) * 0.9)}"></div>
            <button class="s-dugme" type="button" data-hedefkur="${esc(x.id)}" style="min-height:48px">${alarm ? "Alarmı güncelle" : "Alarm kur"}</button>
            ${alarm ? `<button class="s-dugme s-ikincil" type="button" data-alarmsil="${esc(alarm.id)}" style="min-height:48px">Alarmı kaldır</button>` : ""}
          </div>
          ${alarm ? `<p class="u-durum" role="status">Alarm kuruldu: fiyat ${tl(alarm.hedef)} ve altına inince bildirim gelecek.</p>` : ""}
        </div>
        <div class="u-tg">
          <h2>Benzer düşüşleri <span>kaçırma</span></h2>
          <p>${esc(x.kategori_ad || "Bu kategori")} ve diğer kategorilerdeki gerçek indirimler Düşero Telegram kanalına anında düşer.</p>
          <a class="s-dugme s-turuncu" href="${esc(tgLink)}" target="_blank" rel="noopener noreferrer">Telegram kanalına katıl</a>
        </div>
      </section>
      ${kayitlar.length >= 2 ? `<section class="u-tablo-kutu"><h2 class="u-h2">Tüm fiyat kayıtları</h2><div class="u-tablo-kaydir"><table class="u-tablo"><thead><tr><th scope="col">Tarih</th><th scope="col">Fiyat</th><th scope="col">Değişim</th></tr></thead><tbody>${kayitSatir}</tbody></table></div></section>` : ""}
      <details class="metin u-kutu"><summary style="cursor:pointer;font-weight:600">Paylaşım metni · ${esc(kanalAd(x.kanal))}</summary><div>${esc(x.metin)}</div></details>
    </main>
    <footer class="u-alt"><div>Bazı bağlantılar satış ortaklığı (reklam) bağlantısı olabilir; ödediğin fiyat değişmez. Fiyatlar mağazalarca anlık değişebilir. © 2026 Düşero</div></footer>`;
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
  const yasDk = c.ts ? Math.round((Date.now() / 1000 - c.ts) / 60) : 0;
  const bayat = yasDk >= 90 ? ` · <b>${yasDk >= 120 ? Math.round(yasDk / 60) + " saat" : yasDk + " dk"} önce okundu, değişmiş olabilir</b>` : "";
  return `<div class="canli c-${c.renk || "gri"}">
      <div class="c-ust"><span>Mağazada ${yasDk >= 90 ? "son okunan" : "şu an"}</span><b>${tl(c.fiyat)}</b></div>
      <div class="c-alt">${zaman} itibarıyla · ${esc(c.yontem || "")}${c.stok_yok ? " · <b>stokta yok görünüyor</b>" : ""}${bayat}</div>
      ${c.metin ? `<div class="c-karar">${ikon} ${esc(c.metin)}</div>` : ""}
      ${c.gercek ? `<div class="c-karar">📊 ${esc(c.gercek)}</div>` : ""}
      ${c.otuz_gun ? `<div class="c-karar">📅 ${esc(c.otuz_gun)}</div>` : ""}
      ${c.sepet_metin ? `<div class="c-karar">🛒 ${esc(c.sepet_metin)}</div>` : ""}
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
  if (!el) { el = document.createElement("div"); el.id = "detay"; el.hidden = true; el.setAttribute("role", "dialog"); el.setAttribute("aria-modal", "true"); el.setAttribute("aria-label", "Ürün sayfası"); document.body.appendChild(el); }
  const x = S.detay && S.veri ? S.veri.firsatlar.find(z => z.id === S.detay) : null;
  if (!x) { el.hidden = true; el.innerHTML = ""; document.body.style.overflow = ""; return; }
  if (document.activeElement && el.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return;
  const ilk = el.hidden;
  el.innerHTML = detayHTML(x); el.hidden = false; document.body.style.overflow = "hidden";
  if (ilk) { el.scrollTop = 0; const b = el.querySelector("[data-detaykapat]"); if (b) b.focus({preventScroll: true}); }
}
function detayAc(id){ S.detay = id; S.hedefAcik = null; S.aralik = 90; try { history.pushState({detay: id}, ""); } catch (e) {} detayCiz();
  const x = S.veri && S.veri.firsatlar.find(z => z.id === id);
  if (x && x.tur === "urun" && link(x.urun_link) && !(S.veri && (S.veri.onizleme || S.veri.statik))) canliKontrol(id); }
function detayKapat(){ if (!S.detay) return; if (history.state && history.state.detay) history.back(); else { S.detay = null; detayCiz(); } }
window.addEventListener("popstate", () => { if (S.detay) { S.detay = null; S.hedefAcik = null; detayCiz(); ciz(); } });
document.addEventListener("click", e => {
  const d = e.target.closest("[data-detay]"); if (d) { e.preventDefault(); detayAc(d.dataset.detay); return; }
  if (e.target.closest("[data-detaykapat]")) { e.preventDefault(); detayKapat(); return; }
  const kd = e.target.closest("[data-kaydir-detay]"); if (kd) { const h = document.getElementById(kd.dataset.kaydirDetay); if (h) h.scrollIntoView({behavior: "smooth"}); return; }
  const mk = e.target.closest("a.s-marka[data-git]"); if (mk) { e.preventDefault(); S.sekme = "tum"; sakla(); ciz(); window.scrollTo(0, 0); }
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && S.detay) detayKapat();
  if ((e.key === "Enter" || e.key === " ") && e.target.matches && e.target.matches(".kbaslik[data-detay],.s-gorsel[data-detay]")) { e.preventDefault(); detayAc(e.target.dataset.detay); }
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
  const kodlar = (x.kodlar || []).map(z => `<button type="button" class="kod buyuk" data-kod="${esc(z)}" title="Kopyala">${esc(z)} <span aria-hidden="true">⧉</span></button>`).join("");
  const bas = x.kupon_tutar ? `${tl(x.kupon_tutar)} indirim` : (x.indirim ? `%${x.indirim} indirim` : esc(x.baslik));
  const alt = [];
  if (x.kupon_limit) alt.push(`${tl(x.kupon_limit)} ve üzeri alışverişte`);
  if (x.kupon_tutar && x.kupon_limit) alt.push(`sepete oranı %${Math.round(x.kupon_tutar / x.kupon_limit * 100)}`);
  const notlar = [];
  (x.takip || []).forEach(t => notlar.push(["iyi", "Takip: " + t]));
  const sg = sonGunYazi(x); if (sg) notlar.push([sg[1] === "r-kirmizi" ? "uyari" : "", sg[0]]);
  if (x.uygulama) notlar.push(["", "Yalnızca mobil uygulamada"]);
  if (x.ilk_siparis) notlar.push(["", "İlk siparişe özel"]);
  const kanallar = [...new Set([kanalAd(x.kanal)].concat((x.diger || []).map(z => kanalAd(z.kanal))))];
  if (kanallar.length > 1) notlar.push(["iyi", `${kanallar.length} kanalda paylaşıldı`]);
  const kayitli = !!kayitlilar()[x.id];
  return `<article class="s-kart s-kupon">
    <div class="s-ust-satir"><span class="s-magaza">${esc(x.magaza_ad)}</span><span class="s-sag"><span>${once(x.ts)}</span></span></div>
    <div class="s-kupon-bas">${bas}</div>
    ${alt.length ? `<p class="s-ozet-satir">${esc(alt.join(" · "))}</p>` : ""}
    ${x.kupon_tutar || x.indirim ? `<h3 class="s-baslik">${esc(x.baslik)}</h3>` : ""}
    ${kodlar ? `<div class="s-kodlar">${kodlar}</div>` : ""}
    <ul class="s-notlar">${notlar.map(([c, m]) => `<li${c ? ` class="${c}"` : ""}>${esc(m)}</li>`).join("")}</ul>
    <div class="s-kart-alt">${hedef ? `<a class="s-dugme s-git" href="${esc(hedef)}" target="_blank" rel="noopener noreferrer">Mağazaya git</a>` : `<a class="s-dugme s-git" href="${esc(x.link)}" target="_blank" rel="noopener noreferrer">Paylaşımı aç</a>`}<button type="button" class="s-dugme s-ikincil s-kare" data-kaydet="${esc(x.id)}" aria-pressed="${kayitli}" aria-label="${kayitli ? "Kayıttan çıkar" : "Kaydet"}">${kayitli ? "★" : "☆"}</button><button type="button" class="s-dugme s-ikincil s-kare" data-paylas="${esc(x.id)}" aria-label="Paylaş">${IKON_PAYLAS}</button></div>
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
  const benim = v.firsatlar.filter(x => x.kanal === TAKIBIM).sort((a, b) => b.ts - a.ts);
  const statik = !!v.statik;
  $("#icerik").innerHTML = `
    <h2 class="bolum">Kendi eklediğim ürünler <span class="say">${benim.length}</span></h2>
    ${statik ? `<div class="ipucu">Ürün ekleme yalnızca bilgisayarda çalışan programda yapılır (sitede değil).</div>` : `<div class="filtre"><div class="satir"><input type="url" id="urunLink" placeholder="Mağaza ürün linkini yapıştır (Trendyol, Hepsiburada, Amazon, n11…)" autocomplete="off" inputmode="url"><button type="button" class="cip" id="urunEkle" style="flex:none">Ekle</button></div>
      <div class="ipucu">Program sayfadan fiyatı okur, her yenilemede yeniden kontrol eder; ürün sayfasındaki “Fiyat alarmı kur” bölümünden hedef fiyat alarmı kurabilirsin. Telefonda: mağaza uygulamasında “Paylaş → Bağlantıyı kopyala”, sonra buraya yapıştır.</div></div>`}
    ${benim.length ? benim.map(x => kartSec(x).replace("</article>", `<div class="kart-alt"><button type="button" class="cip" data-urunsil="${esc(x.id)}">Takipten çıkar</button></div></article>`)).join("") : ""}
    <h2 class="bolum">Hedef fiyat alarmları <span class="say">${alarmlar.length}</span></h2>
    ${alarmlar.length ? alarmlar.map(alarmSatir).join("") + `<div class="ipucu" style="margin-top:6px">${v.bildirim_var ? "Hedefe inen yeni paylaşımda telefona bildirim gelir." : "Telefona bildirim almak için Ayarlar → Telefona bildirim."}</div>` : `<div class="bos-durum">Bir ürünün fiyat geçmişi sayfasındaki “Fiyat alarmı kur” bölümüne istediğin fiyatı yaz; ürün o fiyata inince haber verelim.</div>`}
    <div class="filtre" style="margin-top:14px">
      <div class="satir"><input type="text" id="takipKelime" placeholder="Takip et: ör. airfryer, ps5, kahve makinesi" autocomplete="off" maxlength="40"><button type="button" class="cip" id="takipEkle" style="flex:none">Ekle</button></div>
      ${cipler ? `<div class="cipler sar">${cipler}</div>` : ""}
      <div class="ipucu">${v.bildirim_var ? "Bu kelimelere uyan yeni bir fırsat çıkınca indirim oranına bakılmadan bildirim gelir." : "Bu kelimelere uyan fırsatlar burada toplanır. Telefona bildirim için Ayarlar → Telefona bildirim."}</div>
    </div>
    <h2 class="bolum">Kelimelerine uyan fırsatlar <span class="say">${eslesen.length}</span></h2>
    ${kelimeler.length ? (eslesen.length ? eslesen.slice(0, S.limit).map(kartSec).join("") : `<div class="bos-durum">Şu an kelimelerine uyan fırsat yok. Yeni paylaşımlar her yenilemede kontrol edilir.</div>`) : `<div class="bos-durum">Henüz kelime eklemedin. Yukarıdan bir ürün adı ekle.</div>`}
    <h2 class="bolum">Kaydettiklerin <span class="say">${kayitli.length}</span></h2>
    ${kayitli.length ? kayitli.map(k => { const g = guncel(k.id); return g ? kartSec(g) : kartSec(Object.assign({}, k, {rozetler: (k.rozetler || []), _eski: true})).replace('<article class="s-kart', '<article class="s-kart s-bitti'); }).join("") : `<div class="bos-durum">Kartlardaki “☆ Kaydet” ile fırsatları buraya ekleyebilirsin.</div>`}`;
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
  const kacListe = (!arama && S.sekme === "tum") ? sekmeVerisi("kac").sort((a, b) => ((b.kacirilmaz || {}).guc || 0) - ((a.kacirilmaz || {}).guc || 0) || b.ts - a.ts) : [];
  const kacId = new Set(kacListe.map(x => x.id));
  const d = filtreUygula(ham).filter(x => !kacId.has(x.id));
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
    ? `<div class="not">%15 ve üstü indirimler, dip fiyat işaretli ürünler ve Akakçe’de diğer satıcılardan %15+ ucuz olanlar (“Akakçe” etiketli; bu oran fiyat düşüşü değil, satıcılar arası farktır). Satış fiyatı paylaşımdan ya da programın fiyat kaydından gelir; “≈” işaretli olanlar tahmindir. Almadan önce ürün sayfasında kontrol edin.${bitenler ? ` Kanalın “bitti” diye güncellediği ${bitenler} fırsat gizlendi.` : ""}</div>`
    : S.sekme === "kupon" ? `<div class="not">Kodlar kanalların paylaştığı haliyle gösterilir; koda dokununca kopyalanır. Bitiş tarihi geçen, kanalda “bitti / çalışmıyor” diye güncellenen ve tarihi yazılmayıp 3 günden eski olan kodlar “Süresi geçenleri gizle” açıkken gösterilmez. Program bir kodun hâlâ geçerli olduğunu doğrulayamaz: sepette kabul edilmezse süresi dolmuş, stok bitmiş ya da kategori/limit koşulu tutmuyor olabilir. “Paylaşım metni ve koşullar”daki hariç tutulan ürünlere bakın.</div>`
    : `<div class="not">Fiyatlar kanalın paylaştığı andaki fiyattır. Turuncu notlu ürünlerde fiyat sepette ya da birden fazla adet alınca oluşur; ürün sayfasında daha yüksek görünür.${bitenler ? ` Kanalın “bitti” diye güncellediği ${bitenler} fırsat gizlendi.` : ""}</div>`;
  let govde;
  if (arama && !S.arsiv) arsivYukle();
  const arsivBulunan = arama ? arsivSonuclari(new Set(d.map(x => x.id))) : [];
  const tgLinkleri = arama && S.veri ? (S.veri.ayarlar.kanallar || []).map(k => `<a href="https://t.me/s/${encodeURIComponent(k)}?q=${encodeURIComponent(S.f.ara.trim())}" target="_blank" rel="noopener noreferrer">${esc(kanalAd(k))}</a>`).join(" · ") : "";
  const arsivBolum = !arama ? "" : (arsivBulunan.length ? `<section class="eski-liste"><h2 class="bolum">Arşivde bulunanlar <span class="say">${arsivBulunan.length}</span></h2>
      <div class="not">Son ${S.veri && S.veri.arsiv_gun || 90} günde paylaşılmış, ana listede olmayan paylaşımlar. Yazan fiyat paylaşıldığı günün fiyatıdır; <b>bugün geçerli olmayabilir</b>. Paylaşımı açmak için ürüne dokunun.</div>
      <table class="d-tablo"><thead><tr><th>Ürün</th><th>Tarih</th><th class="sag">O günkü fiyat</th></tr></thead><tbody>
      ${arsivBulunan.slice(0, 60).map(x => `<tr><td><a href="https://t.me/${esc(x.id)}" target="_blank" rel="noopener noreferrer">${esc(x.baslik || "Paylaşım")}</a><br><small>${esc(x.magaza_ad)} · ${esc(kanalAd(x.id.split("/")[0]))}${x.kodlar.length ? " · kod: " + esc(x.kodlar.join(", ")) : ""}</small></td><td>${tarihKisa(x.ts)}</td><td class="sag">${x.fiyat != null ? tl(x.fiyat) : "–"}</td></tr>`).join("")}
      </tbody></table>${arsivBulunan.length > 60 ? `<div class="ipucu">İlk 60 gösteriliyor; aramayı daraltın (ör. model adı ekleyin).</div>` : ""}</section>`
      : (S.arsivYukleniyor ? `<div class="ipucu">Arşivde aranıyor…</div>` : ""))
    + `<div class="ipucu">Bulamadığınız ürünü doğrudan Telegram kanallarında arayın: ${tgLinkleri}</div>`;
  const aramaBas = arama ? `<div class="arama-ozet"><b>“${esc(S.f.ara.trim())}”</b> için ${d.length + arsivBulunan.length} sonuç${S.araDurum === "araniyor" ? " · kanallarda da aranıyor…" : S.araDurum === "bitti" ? " · kanallar da tarandı" : S.arsiv ? " · arşiv de tarandı" : ""}
      · <a href="https://www.akakce.com/arama/?q=${encodeURIComponent(S.f.ara.trim())}" target="_blank" rel="noopener noreferrer">Akakçe’de fiyatları karşılaştır</a></div>
      <div class="ipucu" style="margin:-4px 0 10px">Arama tüm sekmelerde ve tüm kayıtlarda yapılır; mağaza, kategori ve oran filtreleri arama sırasında uygulanmaz. Adında geçenler önce gelir.</div>${S.araDurum === "onizleme" ? `<div class="not">Ön izleme kanallarda arama yapamaz; yalnızca bu sayfadaki paylaşımlarda arar. Telefondaki program aynı aramayı Telegram kanallarında da yapar ve daha eski paylaşımları da getirir.</div>` : ""}` : "";
  if (!S.veri) govde = `<div class="bos-durum">Yükleniyor…</div>`;
  else if (arama && !d.length) govde = (arsivBulunan.length ? `<div class="bos-durum">Son paylaşımlarda yok; aşağıda arşivde bulunanlar var.</div>` : `<div class="bos-durum">${S.araDurum === "araniyor" ? "Kanallarda aranıyor…" : S.araDurum === "onizleme" ? "Bu sayfadaki paylaşımlarda bulunamadı." : S.araDurum === "bitti" ? "Kayıtlı fırsatlarda ve kanalların aramasında bulunamadı. Marka ya da model adıyla deneyin." : S.araDurum === "statik" ? (S.arsivYukleniyor ? "Son paylaşımlarda yok; arşivde aranıyor…" : "Son paylaşımlarda ve arşivde bulunamadı. Marka ya da model adıyla deneyin ya da aşağıdan Telegram’da arayın.") : "Kayıtlı fırsatlarda yok; kanallarda aranıyor…"}</div>`) + arsivBolum;
  else if (!ham.length) govde = `<div class="bos-durum">${S.veri.yenileniyor ? "Fırsatlar çekiliyor, birkaç saniye sürebilir…" : "Bu sekmede henüz fırsat yok. “Yenile”ye dokunun ya da “Tüm ürünler” sekmesine bakın."}</div>`;
  else if (!d.length) govde = `<div class="bos-durum"><b>Bu filtrelerle fırsat yok</b>Aramayı değiştir ya da “Sadece gerçek indirimler” seçimini kaldır; “Diğer filtreler”den zaman aralığını genişletebilirsin.</div>`;
  else if (arama) {
    const sinir = Date.now() / 1000 - ESKI_GUN * 86400;
    const guncel = d.filter(x => x.ts >= sinir), eski = d.filter(x => x.ts < sinir).sort((a, b) => b.ts - a.ts);
    govde = (guncel.length ? `<div class="s-izgara">${guncel.slice(0, S.limit).map(kartSec).join("")}</div>` : `<div class="bos-durum">Son ${ESKI_GUN} günde “${esc(S.f.ara.trim())}” için güncel fırsat paylaşılmadı.</div>`)
      + (eski.length ? `<section class="eski-liste"><h2 class="bolum">Eski paylaşımlar <span class="say">${eski.length}</span></h2>
          <div class="not">Bunlar ${ESKI_GUN} günden eski paylaşımlar; yazan fiyat o tarihteki fiyattır, <b>bugün geçerli değildir</b>. Yalnızca geçmiş fiyat fikri vermek için listelenir.</div>
          <table class="d-tablo"><thead><tr><th>Ürün</th><th>Tarih</th><th class="sag">O günkü fiyat</th></tr></thead><tbody>
          ${eski.slice(0, 40).map(x => `<tr><td>${x.tur === "urun" ? `<a href="#" data-detay="${esc(x.id)}">${esc(x.baslik)}</a>` : esc(x.baslik)}<br><small>${esc(x.magaza_ad)} · ${esc(kanalAd(x.kanal))}</small></td><td>${tarihKisa(x.ts)}</td><td class="sag">${x.fiyat != null ? tl(x.fiyat) : "–"}</td></tr>`).join("")}
          </tbody></table></section>` : "") + arsivBolum;
  }
  else govde = `<div class="s-izgara">${d.slice(0, S.limit).map(kartSec).join("")}</div>` + (d.length > S.limit ? `<button type="button" class="daha" id="daha">Daha fazla göster (${d.length - S.limit})</button>` : "");
  const kupon = S.sekme === "kupon";
  const magSec = `<select id="magazaSec" aria-label="Mağaza"><option value="hepsi">Tüm mağazalar</option>${Object.entries(magazalar).sort((a, b) => b[1].n - a[1].n).map(([k, v]) =>
    `<option value="${esc(k)}" ${S.f.magaza === k ? "selected" : ""}>${esc(v.ad)} (${v.n})</option>`).join("")}</select>`;
  const minSec = `<select id="min" aria-label="En az indirim">${[[0,"Tüm oranlar"],[-1,"Oranı bilinenler"],[10,"%10 ve üstü"],[20,"%20 ve üstü"],[30,"%30 ve üstü"],[50,"%50 ve üstü"]].map(([v,a]) => `<option value="${v}" ${S.f.min === v ? "selected" : ""}>${a}</option>`).join("")}</select>`;
  const siraSec = `<select id="sirala" aria-label="Sırala">${[["gercek","Gerçek düşüşe göre"],["indirim","Mağaza indirimine göre"],["yeni","En yeni"],["ucuz","En düşük fiyat"]].map(([v,a]) => `<option value="${v}" ${S.f.sirala === v ? "selected" : ""}>${a}</option>`).join("")}</select>`;
  const sureSec = `<select id="sure" aria-label="Paylaşım zamanı">${[[6,"Son 6 saat"],[24,"Son 24 saat"],[72,"Son 3 gün"],[0,"Tüm kayıtlar"]].map(([v,a]) => `<option value="${v}" ${((S.f.sure || {})[S.sekme] ?? 72) === v ? "selected" : ""}>${a}</option>`).join("")}</select>`;
  const anahtar = kupon
    ? `<label class="s-anahtar"><input type="checkbox" id="gecmisGizle" ${S.f.gecmisGizle ? "checked" : ""}> Süresi geçenleri gizle</label>`
    : `<label class="s-anahtar"><input type="checkbox" id="gercekSec" ${S.f.gercek ? "checked" : ""}> Sadece gerçek indirimler</label>`;
  const filtreSay = (S.f.magaza !== "hepsi") + (S.f.min !== 0) + (((S.f.sure || {})[S.sekme] ?? 72) !== 72) + (!S.f.ikinciEl && !kupon);
  const digerBtn = arama ? "" : `<button type="button" class="s-dugme s-ikincil" id="digerFiltre" aria-expanded="${!!S.filtreAcik}">Diğer filtreler${filtreSay ? ` (${filtreSay})` : ""}</button>`;
  const filtreSatir = S.filtreAcik && !arama ? `<div class="s-filtre">${magSec}${minSec}${sureSec}${kupon ? "" : `<label class="s-anahtar"><input type="checkbox" id="ikinciEl" ${S.f.ikinciEl ? "checked" : ""}> 2. el ürünleri göster</label>`}</div>` : "";
  const katCip = kupon ? "" : [`<button type="button" class="cip" data-kategori="hepsi" aria-pressed="${S.f.kategori === "hepsi"}">Tümü</button>`]
    .concat(KATEGORI.filter(([k]) => kategoriler[k]).map(([k, a]) => `<button type="button" class="cip" data-kategori="${k}" aria-pressed="${S.f.kategori === k}">${esc(a)}</button>`)).join("");
  const baslik = arama ? "Arama sonuçları" : kupon ? "Kupon &amp; kampanya" : "Son düşenler";
  const altYazi = arama ? "" : kupon ? `${d.length} kod ve kampanya` : `${d.length} fırsat · fiyatlar kayıtlı fiyatlarla karşılaştırıldı`;
  const kacBolum = kacListe.length || (!arama && S.sekme === "tum" && S.veri) ? `<section class="s-kap s-bolum" id="kacirilmaz">
      <div class="s-bolum-bas"><div><h2 class="s-bolum-baslik">Kaçırılmaz fırsatlar</h2><p>${kacListe.length ? `${kacListe.length} fırsat · fiyatı mağazada doğrulanmış, kayıtlara göre gerçekten ucuz` : "Şu an mağazada doğrulanmış kaçırılmaz fırsat yok; yenisi çıkınca en üstte burada görünür ve telefona bildirim gelir."}</p></div></div>
      ${kacListe.length ? `<div class="s-izgara">${kacListe.slice(0, 12).map(kart).join("")}</div>` : ""}
    </section>` : "";
  $("#icerik").innerHTML = `${kacBolum}
    <section class="s-kap s-bolum" id="firsatlar">
      <div class="s-bolum-bas">
        <div><h2 class="s-bolum-baslik">${baslik}</h2>${altYazi ? `<p>${altYazi}</p>` : ""}</div>
        <div class="s-arac">
          <label class="s-sr" for="ara">Fırsatlarda ara</label>
          <input type="search" id="ara" class="s-alan" placeholder="Fırsatlarda ara" value="${esc(S.f.ara)}" autocomplete="off">
          ${siraSec}${arama ? "" : anahtar}${digerBtn}
        </div>
      </div>
      ${filtreSatir}
      ${katCip && !arama ? `<div class="s-cipler" role="group" aria-label="Kategoriler">${katCip}</div>` : ""}
      ${kupon && !arama ? `<div class="s-cipler">${[["hepsi","Hepsi"],["kod","Kupon kodları"],["kampanya","Kampanyalar"]].map(([v,a]) => `<button type="button" class="cip" data-kupontur="${v}" aria-pressed="${S.f.kuponTur === v}">${a}</button>`).join("")}</div>` : ""}
      ${arama ? aramaBas : kupon ? not.replace('<div class="not">', '<p class="s-not">').replace(/<\/div>\s*$/, "</p>") : ""}${arama && !d.length ? katalogBolumu() : ""}${govde}${arama && d.length ? katalogBolumu() : ""}
    </section>`;
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
  const kt = v.katalog;
  const feedDurum = f => f.hata ? "Hata: " + f.hata : f.son ? `${nf.format(f.adet)} ürün (${nf.format(f.stokta)} stokta) · son okuma ${saat(new Date(f.son * 1000).toISOString())}` : "Henüz okunmadı; birkaç dakika içinde okunur";
  const feedPanel = kt ? `<section class="telefon-panel"><h2>Ürün kataloğu (mağaza feed’leri)</h2>
    <div class="ipucu" style="margin-top:0">Bir mağazanın ya da satış ortaklığı ağının (ör. Admitad) verdiği <b>ürün listesi adresini</b> (XML ya da CSV feed) ekleyin. Program listeyi ${esc(kt.aralik_saat)} saatte bir indirir; ürünler buradaki ve herkese açık sitedeki aramada çıkar. Mağaza sayfaları taranmaz. Katalogda şu an <b>${nf.format(kt.toplam)}</b> ürün var${kt.calisiyor ? " · <b>güncelleniyor…</b>" : ""}.</div>
    ${kt.feedler.map(f => `<div class="kanal"><span class="nokta ${f.hata ? "hata" : f.son ? "ok" : ""}"></span><div class="ad"><b>${esc(f.ad)}</b>${f.ortaklik ? " · ortaklık linki" : ""}<div>${esc(feedDurum(f))}</div></div><button type="button" data-feedsil="${esc(f.kod)}">Kaldır</button></div>`).join("")}
    <label for="feedAd">Mağaza adı</label><input type="text" id="feedAd" placeholder="ör. Teknosa" autocomplete="off" maxlength="40">
    <label for="feedUrl">Feed adresi</label><input type="text" inputmode="url" id="feedUrl" placeholder="https://… (XML ya da CSV)" autocomplete="off">
    <label class="anahtar" style="margin-top:10px"><input type="checkbox" id="feedOrtak"> Bu listedeki linkler ortaklık (affiliate) linki</label>
    <div class="ipucu">Ortaklık ağından aldığınız feed’de linkler zaten sizin kodunuzu taşır; kutuyu işaretleyin ki sitede “iş birliği” olarak belirtilsin.</div>
    <div class="satir" style="margin-top:10px"><button type="button" class="cip" id="feedEkle" style="flex:none">Feed ekle</button>${kt.feedler.length ? `<button type="button" class="cip" id="feedGuncelle" style="flex:none"${kt.calisiyor ? " disabled" : ""}>Şimdi güncelle</button>` : ""}</div>
  </section>` : "";
  $("#icerik").innerHTML = `<div class="ayar">
    ${telefonPanel}
    ${bildirimPanel}
    ${feedPanel}
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
    ${[["akakce","Akakçe listeleri (fark atan, son 6 ayın en ucuzu, broşür)"],["dhforum","DonanımHaber Sıcak Fırsatlar forumu"],["oyun","Steam / Epic / GOG oyun indirimleri (%50+)"],["akakce_dogrula","Doğrulanan ürünü Akakçe’deki en ucuz fiyatla karşılaştır"],["red_gizle","Mağazada fiyatı yükselmiş ya da stoğu bitmiş ürünleri listeden çıkar"]].map(([id, et]) => { const b = v.kaynaklar.find(x => x.kanal === id); const d = b && b.ok != null ? ` <small>(${b.ok ? b.adet + " okundu" : "Hata: " + esc(b.hata)})</small>` : (b && b.hata ? ` <small>(Hata: ${esc(b.hata)})</small>` : ""); return `<label class="anahtar" style="margin-top:10px"><input type="checkbox" class="dis-kaynak" id="${id}" ${a[id] !== false ? "checked" : ""}> ${et}${d}</label>`; }).join("")}
    ${v.dogrulama ? `<div class="ipucu" style="margin-top:8px">Mağaza doğrulaması (son 48 saat): ✔ ${v.dogrulama.onayli} onaylı · ${v.dogrulama.kosullu} koşullu · ✖ ${v.dogrulama.red} elendi · ${v.dogrulama.okunamadi} okunamadı · ${v.dogrulama.kuyruk} sırada. Okuma: ${esc(v.dogrulama.motorlar.join(" → "))}${v.dogrulama.engelli.length ? ` · şu an izin vermeyen: ${esc(v.dogrulama.engelli.join(", "))}` : ""}</div>` : ""}
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

// Ürün kataloğu: mağaza feed'lerinden gelen ürünlerde arama (kanallardaki fırsatlardan ayrı gösterilir)
function katalogAra(q){
  const kt = S.veri && S.veri.katalog;
  if (!kt || !kt.toplam || S.veri.statik || S.veri.onizleme) { S.kat = null; return; }
  const sira = S.f.sirala === "ucuz" ? "ucuz" : S.f.sirala === "indirim" ? "indirim" : "uygun";
  const k = sadeJS(q) + "|" + sira;
  if (S.kat && S.kat.anahtar === k) return;
  S.kat = {anahtar: k, yukleniyor: true};
  fetch("/api/katalog?q=" + encodeURIComponent(q) + "&sirala=" + sira, {cache: "no-store"}).then(r => r.json())
    .then(j => { if (!S.kat || S.kat.anahtar !== k) return; S.kat = {anahtar: k, sonuc: j.sonuc || [], toplam: j.toplam || 0}; if (aramaVar()) cizGovde(); })
    .catch(() => { if (S.kat && S.kat.anahtar === k) S.kat = {anahtar: k, sonuc: [], toplam: 0}; });
}
function katalogBolumu(){
  const k = S.kat;
  if (!k || !aramaVar()) return "";
  if (k.yukleniyor) return `<div class="ipucu">Ürün kataloğunda aranıyor…</div>`;
  if (!k.sonuc.length) return "";
  const satir = x => {
    const not = [x.stok ? "" : "stokta yok", x.yuzde ? `%${x.yuzde} indirimli (mağazanın beyanı)` : "", x.dip ? "📉 izlenen dönemin en düşüğü" : (x.dusus >= 3 ? `son günlerde %${x.dusus} ucuzladı` : ""), x.ortaklik ? "ortaklık linki" : ""].filter(Boolean).join(" · ");
    return `<tr${x.stok ? "" : ' style="opacity:.55"'}><td>${link(x.link) ? `<a href="${esc(x.link)}" target="_blank" rel="noopener noreferrer nofollow">${esc(x.baslik)}</a>` : esc(x.baslik)}<br><small>${esc(x.magaza_ad)}${not ? " · " + esc(not) : ""}</small></td><td class="sag">${tl(x.fiyat)}${x.eski_fiyat ? `<br><small><s>${tl(x.eski_fiyat)}</s></small>` : ""}</td></tr>`;
  };
  return `<section class="eski-liste"><h2 class="bolum">Ürün kataloğunda <span class="say">${k.toplam}</span></h2>
    <div class="not">Mağazaların verdiği ürün listelerinden (feed) gelen <b>güncel liste fiyatları</b>. Bunlar fırsat paylaşımı değildir ve mağaza sayfasında tek tek doğrulanmamıştır; üstü çizili fiyat mağazanın beyanıdır. Sıralama kutusu bu listeyi de sıralar.</div>
    <table class="d-tablo"><thead><tr><th>Ürün</th><th class="sag">Fiyat</th></tr></thead><tbody>${k.sonuc.map(satir).join("")}</tbody></table>
    ${k.toplam > k.sonuc.length ? `<div class="ipucu">İlk ${k.sonuc.length} gösteriliyor; aramayı daraltın (ör. model adı ekleyin).</div>` : ""}</section>`;
}

async function arsivYukle(){
  if (S.arsivYukleniyor || (S.arsiv && Date.now() - S.arsivZaman < 15 * 60000)) return;
  S.arsivYukleniyor = true;
  try {
    const j = await (await fetch("/api/arsiv", {cache: "no-store"})).json();
    S.arsiv = (j.a || []).map(r => ({id: r[0], ts: r[1], baslik: r[2], fiyat: r[3], magaza_ad: r[4], tur: r[5], kodlar: r[6] || [], metin: ""}));
    S.arsivZaman = Date.now();
  } catch (e) { S.arsiv = S.arsiv || []; S.arsivZaman = Date.now() - 14 * 60000; }
  S.arsivYukleniyor = false;
  if (aramaVar()) cizGovde();
}
function arsivSonuclari(gorulen){
  if (!S.arsiv || !aramaVar()) return [];
  const q = sadeJS(S.f.ara.trim()), parca = aramaParcalari(q);
  return S.arsiv.filter(x => !gorulen.has(x.id) && aramaEslesir(x, parca))
    .map(x => (x._puan = aramaPuani(x, q, parca), x)).sort((a, b) => (a._puan - b._puan) || (b.ts - a.ts));
}
async function kanallardaAra(q){
  S.araOnbellek = S.araOnbellek || {};
  if (S.veri && S.veri.onizleme) { S.araDurum = "onizleme"; ciz(); return; }
  arsivYukle();
  if (S.veri && S.veri.statik) { S.araDurum = "statik"; ciz(); return; }
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

// ---------------------------------------------------------------------------
// 🛒 Market sekmesi: zincir marketlerin indirimli ürünleri (kaynak: Market Fiyatı · TÜBİTAK, marketfiyati.org.tr)
// Bu sekmenin ürünleri diğer sekmelere karışmaz; kendi kategorileri, kendi şehir seçimi vardır.
// ---------------------------------------------------------------------------
const MF_API = "https://api.marketfiyati.org.tr/api/";
const ILLER = [[1,"Adana",37.0000,35.3213],[2,"Adıyaman",37.7648,38.2786],[3,"Afyonkarahisar",38.7507,30.5567],[4,"Ağrı",39.7191,43.0503],[5,"Amasya",40.6499,35.8353],[6,"Ankara",39.9334,32.8597],[7,"Antalya",36.8969,30.7133],[8,"Artvin",41.1828,41.8183],[9,"Aydın",37.8560,27.8416],[10,"Balıkesir",39.6484,27.8826],[11,"Bilecik",40.1451,29.9799],[12,"Bingöl",38.8847,40.4939],[13,"Bitlis",38.4006,42.1095],[14,"Bolu",40.7350,31.6061],[15,"Burdur",37.7203,30.2906],[16,"Bursa",40.1885,29.0610],[17,"Çanakkale",40.1553,26.4142],[18,"Çankırı",40.6013,33.6134],[19,"Çorum",40.5506,34.9556],[20,"Denizli",37.7765,29.0864],[21,"Diyarbakır",37.9144,40.2306],[22,"Edirne",41.6771,26.5557],[23,"Elazığ",38.6810,39.2264],[24,"Erzincan",39.7500,39.5000],[25,"Erzurum",39.9043,41.2679],[26,"Eskişehir",39.7767,30.5206],[27,"Gaziantep",37.0662,37.3833],[28,"Giresun",40.9128,38.3895],[29,"Gümüşhane",40.4603,39.4814],[30,"Hakkari",37.5744,43.7408],[31,"Hatay",36.2023,36.1613],[32,"Isparta",37.7648,30.5566],[33,"Mersin",36.8000,34.6333],[34,"İstanbul",41.0082,28.9784],[35,"İzmir",38.4237,27.1428],[36,"Kars",40.6013,43.0975],[37,"Kastamonu",41.3887,33.7827],[38,"Kayseri",38.7312,35.4787],[39,"Kırklareli",41.7333,27.2167],[40,"Kırşehir",39.1425,34.1709],[41,"Kocaeli",40.7654,29.9408],[42,"Konya",37.8746,32.4932],[43,"Kütahya",39.4167,29.9833],[44,"Malatya",38.3552,38.3095],[45,"Manisa",38.6191,27.4289],[46,"Kahramanmaraş",37.5858,36.9371],[47,"Mardin",37.3212,40.7245],[48,"Muğla",37.2153,28.3636],[49,"Muş",38.9462,41.7539],[50,"Nevşehir",38.6939,34.6857],[51,"Niğde",37.9667,34.6833],[52,"Ordu",40.9839,37.8764],[53,"Rize",41.0201,40.5234],[54,"Sakarya",40.7569,30.3781],[55,"Samsun",41.2928,36.3313],[56,"Siirt",37.9333,41.9500],[57,"Sinop",42.0231,35.1531],[58,"Sivas",39.7477,37.0179],[59,"Tekirdağ",40.9833,27.5167],[60,"Tokat",40.3167,36.5500],[61,"Trabzon",41.0015,39.7178],[62,"Tunceli",39.1079,39.5401],[63,"Şanlıurfa",37.1591,38.7969],[64,"Uşak",38.6823,29.4082],[65,"Van",38.4891,43.4089],[66,"Yozgat",39.8181,34.8147],[67,"Zonguldak",41.4564,31.7987],[68,"Aksaray",38.3687,34.0370],[69,"Bayburt",40.2552,40.2249],[70,"Karaman",37.1759,33.2287],[71,"Kırıkkale",39.8468,33.5153],[72,"Batman",37.8812,41.1351],[73,"Şırnak",37.5164,42.4611],[74,"Bartın",41.6344,32.3375],[75,"Ardahan",41.1105,42.7022],[76,"Iğdır",39.9237,44.0450],[77,"Yalova",40.6500,29.2667],[78,"Karabük",41.2061,32.6204],[79,"Kilis",36.7184,37.1212],[80,"Osmaniye",37.0742,36.2464],[81,"Düzce",40.8438,31.1565]];
const M_KATEGORI = [["Meyve ve Sebze","Meyve & sebze"],["Et, Tavuk ve Balık","Et, tavuk & balık"],["Süt Ürünleri ve Kahvaltılık","Süt & kahvaltılık"],["Temel Gıda","Temel gıda"],["İçecek","İçecek"],["Atıştırmalık ve Tatlı","Atıştırmalık & tatlı"],["Temizlik Ürünleri","Temizlik"],["Kişisel Bakım","Kişisel bakım"],["Diğer Ürünler","Diğer"]];
const M_ZINCIR = {migros:"Migros", carrefour:"CarrefourSA", bim:"BİM", a101:"A101", sok:"ŞOK", tarim_kredi:"Tarım Kredi", hakmar:"Hakmar"};
const M_ONBELLEK_MS = 30 * 60 * 1000;
S.m = {il:42, yaricap:10, kat:"hepsi", zincir:"hepsi", sirala:"gercek", min:0, ara:"", konum:null, sisikGizle:true, mod:"indirim", kq:"", ksirala:"akilli"};
try { const k = JSON.parse(localStorage.getItem("kf-market") || "null"); if (k) Object.assign(S.m, k); } catch (e) {}
if (!ILLER.some(x => x[0] === S.m.il)) S.m.il = 42;
if (S.m.surum !== 2) { S.m.surum = 2; S.m.sirala = "gercek"; if (S.m.sisikGizle == null) S.m.sisikGizle = true; }
S.mv = {liste:{}, depo:{}, yuk:null, gecmis:{}, kuyruk:[], calisan:0, limit:60, kars:{}, acik:{}, ara:null, sonGorunen:null};
function mSakla(){ try { localStorage.setItem("kf-market", JSON.stringify(S.m)); } catch (e) {} }
function mKonum(){
  if (S.m.konum) return {lat:S.m.konum.lat, lon:S.m.konum.lon, ad:"Konumum", anahtar:`k${S.m.konum.lat.toFixed(3)},${S.m.konum.lon.toFixed(3)}`};
  const il = ILLER.find(x => x[0] === S.m.il) || ILLER[41];
  return {lat:il[2], lon:il[3], ad:il[1], anahtar:"il" + il[0]};
}
function mAnahtar(){ const k = mKonum(); return [k.anahtar, S.m.yaricap, S.m.kat, S.m.zincir].join("|"); }

// Market Fiyatı'nı yormamak için: aynı anda en çok 2 istek, istekler arası en az 250 ms; engellenirse 2 dk beklenir.
const M_SIRA = {aktif:0, bekleyen:[], son:0, durdur:0};
function mSiraAl(){
  return new Promise(coz => { M_SIRA.bekleyen.push(coz); mSiraIlerle(); });
}
function mSiraIlerle(){
  if (M_SIRA.aktif >= 2 || !M_SIRA.bekleyen.length) return;
  const bekle = Math.max(0, M_SIRA.son + 250 - Date.now());
  if (bekle > 0) { clearTimeout(M_SIRA.z); M_SIRA.z = setTimeout(mSiraIlerle, bekle); return; }
  M_SIRA.aktif++; M_SIRA.son = Date.now();
  M_SIRA.bekleyen.shift()();
  mSiraIlerle();
}
function mSiraBirak(){ M_SIRA.aktif--; mSiraIlerle(); }
function mDurduMu(){ return Date.now() < M_SIRA.durdur; }
async function mfIstek(yol, govde){
  if (mDurduMu()) throw new Error("Market Fiyatı çok sık istek nedeniyle kısa süreliğine yanıt vermiyor; " + Math.ceil((M_SIRA.durdur - Date.now()) / 60000) + " dk sonra tekrar deneyin.");
  await mSiraAl();
  try { return await mfIstekHam(yol, govde); }
  catch (e) {
    // Ağ hatası ya da 429: muhtemelen geçici engel — tüm isteklere 2 dakika ara ver
    if (!(e && e.name === "AbortError") && /Failed to fetch|NetworkError|429|Load failed/i.test(String(e && e.message || e))) {
      M_SIRA.durdur = Date.now() + 120000;
      throw new Error("Market Fiyatı’na ulaşılamadı: internet bağlantısı yok ya da çok sık istek yüzünden geçici olarak bekletiliyor. 2 dakika sonra tekrar deneyin.");
    }
    throw e;
  } finally { mSiraBirak(); }
}
async function mfIstekHam(yol, govde){
  // Önce doğrudan (Market Fiyatı herkese açık istek kabul ediyor); olmazsa bilgisayardaki program aracılığıyla.
  const zaman = (ms) => { const c = new AbortController(); setTimeout(() => c.abort(), ms); return c.signal; };
  try {
    const r = await fetch(MF_API + yol, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(govde), signal:zaman(20000)});
    if (r.ok) return await r.json();
    if (r.status === 429) throw new Error("429 · Market Fiyatı çok sık istek nedeniyle bekletiyor");
    if (r.status >= 400 && r.status < 500) throw new Error("Market Fiyatı isteği kabul etmedi (" + r.status + ")");
  } catch (e) {
    if (S.veri && (S.veri.statik || S.veri.onizleme)) throw e;
  }
  const r = await fetch("/api/market", {method:"POST", headers:{"Content-Type":"application/json", "X-Kasim":"1"}, body:JSON.stringify({yol, govde}), signal:zaman(40000)});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.hata || ("Market Fiyatı'na ulaşılamadı (" + r.status + ")"));
  return j;
}

function mUrun(c){
  const depolar = (c.productDepotInfoList || []).filter(d => d && d.price != null);
  if (!depolar.length) return null;
  // İndirimli şubeler arasından en ucuzu; indirimli yoksa en ucuzu
  const ind = depolar.filter(d => d.discount);
  const d = (ind.length ? ind : depolar).slice().sort((a, b) => a.price - b.price)[0];
  const eski = d.discountlessPrice && d.discountlessPrice > d.price ? d.discountlessPrice : null;
  let oran = d.discountRatio != null ? Math.round(d.discountRatio * 100) : null;
  if (oran == null && eski) oran = Math.round((1 - d.price / eski) * 100);
  if (!d.discount && !eski) return null;
  return {id:String(c.id), ad:c.title || "", marka:c.brand || "", resim:c.imageUrl || "", birim:c.refinedVolumeOrWeight || c.refinedQuantityUnit || "",
          menu:c.menu_category || "", ana:c.main_category || "", fiyat:d.price, eski, oran, birimFiyat:d.unitPrice || "", birimDeger:d.unitPriceValue,
          zincir:d.marketAdi || "", depo:d.depotId || "", sube:d.depotName || "", zaman:d.indexTime || "", promo:d.promotionText || "", subeSayisi:depolar.length};
}

async function mDepolar(){
  const k = mKonum(), a = k.anahtar + "|" + S.m.yaricap;
  const c = S.mv.depo[a];
  if (c && Date.now() - c.t < 6 * 3600 * 1000) return c.idler;
  const n = await mfIstek("v2/nearest", {latitude:k.lat, longitude:k.lon, distance:S.m.yaricap});
  const idler = (Array.isArray(n) ? n : []).map(x => x.id).filter(Boolean);
  S.mv.depo[a] = {t:Date.now(), idler, zincirler:[...new Set((Array.isArray(n) ? n : []).map(x => x.marketName))]};
  return idler;
}

async function mYukle(devam){
  const anahtar = mAnahtar();
  let L = S.mv.liste[anahtar];
  if (!devam && L && !L.hata && Date.now() - L.t < M_ONBELLEK_MS) return;
  if (S.mv.yuk === anahtar) return;
  if (!L || !devam) L = S.mv.liste[anahtar] = {t:Date.now(), urunler:[], idler:{}, sayfa:0, toplam:null, zincirSay:null, hata:null};
  S.mv.yuk = anahtar; L.hata = null; if (S.sekme === "market") { try { marketCiz(); } catch (e) { console.error(e); } }
  try {
    const depots = await mDepolar();
    if (!depots.length) throw new Error("Bu konumun " + S.m.yaricap + " km çevresinde kayıtlı market şubesi bulunamadı. Yarıçapı büyütün.");
    const hedef = L.sayfa + (S.m.kat === "hepsi" && S.m.zincir === "hepsi" ? 8 : 20);   // ~200 / ~500 ürün
    while (L.sayfa < hedef && (L.toplam == null || L.sayfa * 25 < L.toplam)) {
      const grup = [];
      for (let i = 0; i < 4 && L.sayfa + i < hedef && (L.toplam == null || (L.sayfa + i) * 25 < L.toplam); i++) {
        const g = {pages:L.sayfa + i, size:25, offer_discount:["true"], depots};
        if (S.m.kat !== "hepsi") g.menu_category = [S.m.kat];
        if (S.m.zincir !== "hepsi") g.market_names = [S.m.zincir];
        grup.push(mfIstek("v3/searchByCategories", g));
        if (L.toplam == null) break;   // ilk sayfa toplamı öğrenmek için tek başına
      }
      const sonuclar = await Promise.all(grup);
      for (const r of sonuclar) {
        if (L.toplam == null) {
          L.toplam = r.numberOfFound || 0;
          const f = (r.facetMap || {}).market_names || (r.facetMap || {}).offer_market;
          if (Array.isArray(f) && S.m.zincir === "hepsi") L.zincirSay = f;
        }
        (r.content || []).forEach(c => { const u = mUrun(c); if (u && !L.idler[u.id]) { L.idler[u.id] = 1; L.urunler.push(u); } });
        L.sayfa++;
      }
      if (S.mv.liste[anahtar] !== L) return;
      if (S.sekme === "market" && mAnahtar() === anahtar) marketCiz();
    }
    L.t = Date.now();
  } catch (e) {
    L.hata = (e && e.name === "AbortError") ? "Market Fiyatı yanıt vermedi (zaman aşımı). Biraz sonra tekrar deneyin." : String((e && e.message) || e);
  } finally {
    if (S.mv.yuk === anahtar) S.mv.yuk = null;
    if (S.sekme === "market") marketCiz();
  }
}

// --- Gerçek indirim kontrolü: ürünün aynı marketteki son 90 günlük fiyatı (Market Fiyatı fiyat geçmişi) ---
function mGecmisAnahtar(u){ return u.id + "|" + u.depo; }
function mGecmisOzet(u, h){
  const seri = (Array.isArray(h) ? h : []);
  const s = seri.find(x => x.name === u.zincir) || seri[0];
  const v = s && Array.isArray(s.series) ? s.series.filter(p => p && typeof p.value === "number" && p.value > 0).map(p => ({t:p.name, v:p.value})) : [];
  if (v.length < 7) return {durum:"az", gun:v.length};
  // Şu anki fiyatın başladığı gün: sondan geriye aynı fiyat sürdükçe
  let k = v.length - 1;
  while (k > 0 && Math.abs(v[k - 1].v - v[v.length - 1].v) < 0.01) k--;
  const once = v.slice(0, k);
  if (once.length < 7) return {durum:"az", gun:v.length};
  const sirali = once.map(x => x.v).sort((a, b) => a - b);
  const medyan = sirali[Math.floor(sirali.length / 2)];
  let min = once[0]; once.forEach(x => { if (x.v < min.v) min = x; });
  return {durum:"ok", medyan, min:min.v, minTarih:min.t, max:sirali[sirali.length - 1], gun:once.length, bas:v[k].t};
}
function mKarar(u){
  const g = S.mv.gecmis[mGecmisAnahtar(u)];
  if (!g) return {durum:"bekliyor"};
  if (g.durum !== "ok") return g;
  const gercek = Math.round((1 - u.fiyat / g.medyan) * 100);
  const karar = u.fiyat <= g.min + 0.01 ? "dip" : gercek >= 5 ? "gercek" : "sisik";
  return Object.assign({}, g, {gercek, karar, eskiSisik: !!(u.eski && u.eski > g.max * 1.02)});
}
function mTarih(t){ try { return new Date(t + "T12:00:00").toLocaleDateString("tr-TR", {day:"numeric", month:"long"}); } catch (e) { return t; } }
// Fiyat geçmişi sonuçları bu cihazda 12 saat saklanır (aynı ürün için tekrar istek atılmaz)
const M_GECMIS_SURE = 12 * 3600 * 1000;
try { const k = JSON.parse(localStorage.getItem("kf-mgecmis") || "{}"); const n = Date.now(); for (const [a, v] of Object.entries(k)) if (v && n - v.t < M_GECMIS_SURE) S.mv.gecmis[a] = v; } catch (e) {}
function mGecmisSakla(){
  clearTimeout(S.mv.gsZaman);
  S.mv.gsZaman = setTimeout(() => { try {
    const n = Date.now(), e = Object.entries(S.mv.gecmis).filter(([, v]) => v && v.t && v.durum !== "hata" && n - v.t < M_GECMIS_SURE).sort((a, b) => b[1].t - a[1].t).slice(0, 1500);
    localStorage.setItem("kf-mgecmis", JSON.stringify(Object.fromEntries(e)));
  } catch (x) {} }, 2000);
}
function mKuyrugaAl(liste){
  // Yalnızca ekranda görünen ürünler; sıra her çizimde yenilenir (kaydırılıp geçilenler beklemez)
  S.mv.kuyruk = liste.filter(u => u.depo && !S.mv.gecmis[mGecmisAnahtar(u)]);
  if (!S.mv.calisan) mIsci();
}
async function mIsci(){
  if (!S.mv.kuyruk.length) return;
  S.mv.calisan++;
  while (S.mv.kuyruk.length) {
    const u = S.mv.kuyruk.shift(), a = mGecmisAnahtar(u);
    if (S.mv.gecmis[a]) continue;
    if (mDurduMu()) break;
    try { S.mv.gecmis[a] = Object.assign(mGecmisOzet(u, await mfIstek("v3/price-history", {uniqueId:u.id, depots:[u.depo]})), {t:Date.now()}); mGecmisSakla(); }
    catch (e) { if (!mDurduMu()) S.mv.gecmis[a] = {durum:"hata", t:Date.now()}; }
    mCizErtele();
  }
  S.mv.calisan--;
  if (mDurduMu() && S.mv.kuyruk.length) setTimeout(() => { if (!S.mv.calisan) mIsci(); }, M_SIRA.durdur - Date.now() + 1000);
}
function mCizErtele(){ if (S.mv.cizZaman) return; S.mv.cizZaman = setTimeout(() => { S.mv.cizZaman = null; const ae = document.activeElement; if (S.sekme === "market" && !(ae && ae.tagName === "SELECT" && ae.id && ae.id.startsWith("m"))) marketCiz(); }, 500); }

function mSlug(s){ return sadeJS(s).replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 80); }
// Market zincirlerinin kendi renkleri (Market Fiyatı'ndaki gibi); logo yerine renkli ad etiketi kullanılır
const M_RENK = {sok:["#ffe000","#1d1d1b"], tarim_kredi:["#0c6937","#fff"], carrefour:["#01509d","#fff"], a101:["#2bcae0","#06343b"],
                migros:["#ee7623","#fff"], bim:["#e31e24","#fff"], hakmar:["#4db848","#fff"]};
function zEtiket(z){ const r = M_RENK[z] || ["#6b7280","#fff"]; return `<span class="zchip" style="background:${r[0]};color:${r[1]}">${esc(M_ZINCIR[z] || z)}</span>`; }
function mDetayLink(id, ad){ return `https://marketfiyati.org.tr/detay/${encodeURIComponent(id)}/${mSlug(ad)}`; }

// --- Fiyat karşılaştırma: aynı ürünün çevredeki tüm marketlerdeki fiyatı ---
function mDepoSatirlari(liste){
  // Her zincirden en ucuz şube; fiyata göre sıralı
  const enIyi = {};
  (liste || []).filter(d => d && d.price != null).forEach(d => { const z = d.marketAdi || "?"; if (!enIyi[z] || d.price < enIyi[z].price) enIyi[z] = d; });
  return Object.values(enIyi).sort((a, b) => a.price - b.price);
}
function mTablo(liste, buZincir){
  const sat = mDepoSatirlari(liste);
  if (!sat.length) return `<div class="mf-bilgi">Bu ürün çevrendeki başka markette bulunamadı.</div>`;
  const min = sat[0].price, max = sat[sat.length - 1].price;
  const ozet = sat.length < 2
    ? `Bu ürün çevrende yalnızca ${esc(M_ZINCIR[sat[0].marketAdi] || sat[0].marketAdi)}’de satılıyor.`
    : max - min < 0.01 ? `${sat.length} markette de aynı fiyat.`
    : `En ucuz <b>${esc(M_ZINCIR[sat[0].marketAdi] || sat[0].marketAdi)}</b> · en pahalıyla arasında <b>${tl(Math.round((max - min) * 100) / 100)}</b> fark (%${Math.round((max / min - 1) * 100)}).`;
  return `<div class="mf-tablo">
    <div class="mf-tablo-ust">⚖️ Fiyat karşılaştırma · ${sat.length} market</div>
    ${sat.map((d, i) => {
      const fark = Math.round((d.price / min - 1) * 1000) / 10;
      const etiket = d.price - min < 0.01 ? `<span class="mf-ucuz">En ucuz</span>` : `<span class="mf-pahali" title="En ucuzdan bu kadar pahalı">+%${fark.toLocaleString("tr-TR")}</span>`;
      return `<div class="mf-satir${d.price - min < 0.01 ? " ucuz" : ""}${d.marketAdi === buZincir ? " bu" : ""}">
        <div class="mf-z">${zEtiket(d.marketAdi)}<small>${esc(d.depotName || "")}</small></div>
        <div class="mf-f"><b>${tl(d.price)}</b>${d.discountlessPrice && d.discountlessPrice > d.price ? `<s>${tl(d.discountlessPrice)}</s>` : ""}<small>${esc(d.unitPrice || "")}</small></div>
        <div class="mf-e">${etiket}${d.discount ? `<span class="mf-ind">indirimde</span>` : ""}</div>
      </div>`; }).join("")}
    <div class="mf-ozet">${ozet}</div>
  </div>`;
}
async function mKarsilastirGetir(u){
  const a = u.id;
  if (S.mv.kars[a] && S.mv.kars[a].durum !== "hata") return;
  S.mv.kars[a] = {durum:"yukleniyor"}; marketCiz();
  try {
    const depots = await mDepolar();
    // Arama, ürünün çevredeki tüm marketlerdeki fiyatlarını döndürür; aynı ürünü kimliğiyle bulunur
    const r = await mfIstek("v2/search", {keywords:u.ad, pages:0, size:24, depots});
    const c = (r.content || []).find(x => String(x.id) === u.id);
    S.mv.kars[a] = c ? {durum:"ok", liste:c.productDepotInfoList || []} : {durum:"yok"};
  } catch (e) { S.mv.kars[a] = {durum:"hata", hata:String((e && e.message) || e)}; }
  marketCiz();
}
function mKarsBlok(u){
  const k = S.mv.kars[u.id];
  if (!k || !S.mv.acik[u.id]) return "";
  if (k.durum === "yukleniyor") return `<div class="mf-bilgi">Çevredeki marketlerin fiyatları getiriliyor…</div>`;
  if (k.durum === "hata") return `<div class="mf-bilgi">Karşılaştırma alınamadı: ${esc(k.hata)} <button type="button" class="ikincil" data-mkars="${esc(u.id)}">Tekrar dene</button></div>`;
  if (k.durum === "yok") return `<div class="mf-bilgi">Bu ürün çevrendeki başka markette bulunamadı.</div>`;
  return mTablo(k.liste, u.zincir);
}

// İndirim kartı (Market Fiyatı tarzı ızgara kartı)
function mKart(u){
  const res = link(u.resim);
  const k = mKarar(u);
  const kontrollu = k.durum === "ok";
  const gosterOran = kontrollu ? Math.max(0, k.gercek) : u.oran;
  const oran = gosterOran != null && !(kontrollu && k.karar === "sisik")
    ? `<div class="mf-oran${gosterOran >= 30 ? "" : " orta"}" title="${kontrollu ? "Son 90 günde çoğunlukla satıldığı fiyata göre" : "Marketin ilan ettiği oran"}">%${gosterOran}<small>${kontrollu ? "gerçek" : "indirim"}</small></div>` : "";
  const rozet = [];
  if (kontrollu) {
    if (k.karar === "dip") rozet.push(`<span class="rozet r-yesil">✅ Son ${k.gun} günün en düşüğü · normalde ~${tl(k.medyan)}</span>`);
    else if (k.karar === "gercek") {
      rozet.push(`<span class="rozet r-yesil">✅ Gerçek indirim · normalde ~${tl(k.medyan)}</span>`);
      rozet.push(`<span class="rozet r-sari">Daha önce ${tl(k.min)}’ye düştü (${esc(mTarih(k.minTarih))})</span>`);
    } else rozet.push(`<span class="rozet r-kirmizi">⚠️ Şişirilmiş indirim: son ${k.gun} günde çoğunlukla ${tl(k.medyan)}</span>`);
    if (k.eskiSisik) rozet.push(`<span class="rozet r-sari">Üstü çizili ${tl(u.eski)} son ${k.gun} günde hiç görülmedi</span>`);
    if (u.oran != null && Math.abs(u.oran - k.gercek) >= 3) rozet.push(`<span class="rozet">Marketin ilan ettiği: %${u.oran}</span>`);
  } else if (k.durum === "bekliyor") rozet.push(mDurduMu() ? `<span class="rozet r-sari">⏸ Fiyat geçmişi birazdan kontrol edilecek</span>` : `<span class="rozet">⏳ Fiyat geçmişi kontrol ediliyor…</span>`);
  else if (k.durum === "az") rozet.push(`<span class="rozet r-sari">Fiyat geçmişi yetersiz · oran marketin beyanı</span>`);
  else rozet.push(`<span class="rozet r-sari">Fiyat geçmişi okunamadı · oran marketin beyanı</span>`);
  if (u.promo) rozet.push(`<span class="rozet r-mavi">${esc(u.promo)}</span>`);
  if ((u.oran || 0) >= 70 && !kontrollu) rozet.push(`<span class="rozet r-kirmizi">🧐 Kontrol et: oran çok yüksek</span>`);
  const acik = !!S.mv.acik[u.id];
  return `<article class="mf-kart${acik ? " acik" : ""}${kontrollu && k.karar === "sisik" ? " soluk" : ""}">
    <div class="mf-ust">
      <div class="mf-gorsel">${res ? `<img src="${esc(res)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">` : ""}${oran}</div>
      <div class="mf-bilgiler">
        <div class="mf-ad" title="${esc(u.ad)}">${esc(u.ad)}</div>
        <div class="mf-zs">${zEtiket(u.zincir)}<small>${esc(u.sube)}</small></div>
        <div class="mf-fiyat">${tl(u.fiyat)}${u.eski ? `<s>${tl(u.eski)}</s>` : ""}</div>
        <div class="mf-birim">${esc(u.birimFiyat || "")}${u.zaman ? ` · ${esc(u.zaman)}` : ""}</div>
        <div class="rozetler">${rozet.join("")}</div>
        <div class="mf-eylem">
          <button type="button" class="mf-btn" data-mkars="${esc(u.id)}" aria-expanded="${acik}">${acik ? "Karşılaştırmayı kapat" : "⚖️ Fiyatları karşılaştır"}</button>
          <a class="mf-link" href="${mDetayLink(u.id, u.ad)}" target="_blank" rel="noopener noreferrer">Fiyat geçmişi ↗</a>
        </div>
      </div>
    </div>
    ${mKarsBlok(u)}
  </article>`;
}

// Arama sonucu kartı: ürünün çevredeki marketlerdeki fiyatları kartın içinde gelir
function mAraKart(c){
  const sat = mDepoSatirlari(c.productDepotInfoList);
  if (!sat.length) return "";
  const en = sat[0], acik = !!S.mv.acik["a:" + c.id], res = link(c.imageUrl);
  const enPahali = sat[sat.length - 1].price;
  return `<article class="mf-kart${acik ? " acik" : ""}">
    <div class="mf-ust">
      <div class="mf-gorsel">${res ? `<img src="${esc(res)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">` : ""}${sat.length > 1 && enPahali - en.price >= 0.01 ? `<div class="mf-oran orta" title="En ucuz ile en pahalı market arasındaki fark">%${Math.round((enPahali / en.price - 1) * 100)}<small>fark</small></div>` : ""}</div>
      <div class="mf-bilgiler">
        <div class="mf-ad" title="${esc(c.title)}">${esc(c.title)}</div>
        <div class="mf-zs">${zEtiket(en.marketAdi)}${sat.length > 1 ? `<small>+${sat.length - 1} market</small>` : `<small>${esc(en.depotName || "")}</small>`}</div>
        <div class="mf-fiyat">${tl(en.price)}${sat.length > 1 && enPahali - en.price >= 0.01 ? `<small class="mf-aralik">– ${tl(enPahali)}</small>` : ""}</div>
        <div class="mf-birim">${esc(en.unitPrice || "")}${c.menu_category ? ` · ${esc(c.menu_category)}` : ""}</div>
        <div class="mf-eylem">
          <button type="button" class="mf-btn" data-maac="${esc(c.id)}" aria-expanded="${acik}">${acik ? "Kapat" : `⚖️ Fiyatları karşılaştır${sat.length > 1 ? ` (${sat.length})` : ""}`}</button>
          <a class="mf-link" href="${mDetayLink(c.id, c.title)}" target="_blank" rel="noopener noreferrer">Fiyat geçmişi ↗</a>
        </div>
      </div>
    </div>
    ${acik ? mTablo(c.productDepotInfoList) : ""}
  </article>`;
}

async function mAraYukle(devam){
  const q = S.m.kq.trim();
  const anahtar = [mKonum().anahtar, S.m.yaricap, q].join("|");
  if (q.length < 2) { S.mv.ara = null; marketCiz(); return; }
  let A = S.mv.ara;
  if (!devam || !A || A.anahtar !== anahtar) A = S.mv.ara = {anahtar, q, sonuclar:[], idler:{}, sayfa:0, toplam:null, yuk:false, hata:null};
  if (A.yuk) return;
  A.yuk = true; A.hata = null; marketCiz();
  try {
    const depots = await mDepolar();
    if (!depots.length) throw new Error("Bu konumun " + S.m.yaricap + " km çevresinde kayıtlı market şubesi bulunamadı. Yarıçapı büyütün.");
    const r = await mfIstek("v2/search", {keywords:q, pages:A.sayfa, size:24, depots});
    if (S.mv.ara !== A) return;
    A.toplam = r.numberOfFound || 0; A.sayfa++;
    (r.content || []).forEach(c => { if (c && !A.idler[c.id] && (c.productDepotInfoList || []).length) { A.idler[c.id] = 1; A.sonuclar.push(c); } });
  } catch (e) { A.hata = String((e && e.message) || e); }
  A.yuk = false;
  if (S.sekme === "market") marketCiz();
}

function marketCiz(){
  if (S.sekme !== "market") return;
  const k = mKonum();
  const ilSecenek = ILLER.map(([no, ad]) => `<option value="${no}" ${!S.m.konum && S.m.il === no ? "selected" : ""}>${String(no).padStart(2, "0")} · ${esc(ad)}</option>`).join("");
  const ustBar = `
    <div class="mf-mod" role="tablist">
      <button type="button" data-mmod="indirim" aria-pressed="${S.m.mod !== "kars"}">🏷️ İndirimler</button>
      <button type="button" data-mmod="kars" aria-pressed="${S.m.mod === "kars"}">⚖️ Fiyat karşılaştır</button>
    </div>
    <div class="mf-konum">
      <select id="mIl" aria-label="Şehir">${S.m.konum ? `<option value="konum" selected>📍 Konumum</option>` : ""}${ilSecenek}</select>
      <select id="mYaricap" aria-label="Arama yarıçapı">${[3,5,10,20].map(v => `<option value="${v}" ${S.m.yaricap === v ? "selected" : ""}>${v} km çevre</option>`).join("")}</select>
      <button type="button" class="ikincil" id="mKonum">📍 Konumum</button>
    </div>`;
  const odakId = document.activeElement && ["mAra", "mKq"].includes(document.activeElement.id) ? document.activeElement.id : null;
  const pos = odakId ? document.activeElement.selectionStart : null;
  $("#icerik").innerHTML = `<div class="mf">${ustBar}${S.m.mod === "kars" ? mKarsGorunum(k) : mIndirimGorunum(k)}</div>`;
  if (odakId) { const a = $("#" + odakId); if (a) { a.focus(); try { a.setSelectionRange(pos, pos); } catch (e) {} } }
  if (S.m.mod !== "kars" && S.mv.sonGorunen) mKuyrugaAl(S.mv.sonGorunen);
}

function mKarsGorunum(k){
  const A = S.mv.ara, q = S.m.kq.trim();
  if (q.length >= 2 && (!A || A.anahtar !== [k.anahtar, S.m.yaricap, q].join("|")) && !(A && A.yuk)) setTimeout(() => mAraYukle(false), 0);
  let sonuc = (A && A.sonuclar) || [];
  if (S.m.ksirala === "ucuz") sonuc = sonuc.slice().sort((a, b) => mDepoSatirlari(a.productDepotInfoList)[0].price - mDepoSatirlari(b.productDepotInfoList)[0].price);
  else if (S.m.ksirala === "birim") sonuc = sonuc.slice().sort((a, b) => (mDepoSatirlari(a.productDepotInfoList)[0].unitPriceValue ?? 1e12) - (mDepoSatirlari(b.productDepotInfoList)[0].unitPriceValue ?? 1e12));
  else if (S.m.ksirala === "fark") { const f = c => { const s = mDepoSatirlari(c.productDepotInfoList); return s.length > 1 ? s[s.length - 1].price / s[0].price : 1; }; sonuc = sonuc.slice().sort((a, b) => f(b) - f(a)); }
  let govde;
  if (q.length < 2) govde = `<div class="mf-bos"><div class="mf-bos-ikon">⚖️</div><b>Hangi ürünün fiyatını karşılaştıralım?</b><br>Ürün adını yaz; ${esc(k.ad)} çevresindeki BİM, A101, ŞOK, Migros, CarrefourSA, Tarım Kredi ve Hakmar fiyatlarını yan yana gösteririm.
      <div class="mf-oneri">${["çaykur tiryaki 1 kg","ayçiçek yağı 5 lt","toz şeker 5 kg","pınar süt 1 lt","yumurta 30","tuvalet kağıdı 32"].map(x => `<button type="button" class="cip" data-mone="${esc(x)}">${esc(x)}</button>`).join("")}</div></div>`;
  else if (A && A.hata && !sonuc.length) govde = `<div class="bos-durum">${esc(A.hata)}<br><br><button type="button" class="ikincil" id="mAraYenile">Tekrar dene</button></div>`;
  else if (!sonuc.length) govde = `<div class="bos-durum">${!A || A.yuk ? "Fiyatlar getiriliyor…" : "Çevrede bu adla ürün bulunamadı. Daha kısa yazmayı deneyin (ör. “tiryaki”)."}</div>`;
  else govde = `<div class="mf-izgara">${sonuc.map(mAraKart).join("")}</div>`
    + (A && A.yuk ? `<div class="bos-durum">Yükleniyor…</div>` : A && A.toplam > A.sayfa * 24 ? `<button type="button" class="daha" id="mAraDaha">Daha fazla ürün (${A.toplam - A.sayfa * 24})</button>` : "");
  return `
    <form class="mf-ara" id="mKqForm"><input type="search" id="mKq" placeholder="Ürün ara ve karşılaştır (ör. tiryaki çay 1 kg)" value="${esc(S.m.kq)}" autocomplete="off"><button type="submit" class="mf-btn">Ara</button></form>
    ${sonuc.length ? `<div class="mf-arac"><span>${A.toplam} ürün · ${esc(k.ad)} ${S.m.yaricap} km</span>
      <select id="mKsirala" aria-label="Sıralama">${[["akilli","Akıllı sıralama"],["ucuz","En düşük fiyat"],["birim","En düşük birim fiyat"],["fark","Marketler arası en büyük fark"]].map(([v,a]) => `<option value="${v}" ${S.m.ksirala === v ? "selected" : ""}>${a}</option>`).join("")}</select></div>` : ""}
    ${govde}
    <div class="not" style="margin-top:12px">Kaynak: Market Fiyatı (TÜBİTAK). Her market için çevredeki en ucuz şubesi gösterilir; fiyatlar marketlerin bildirdiği şube fiyatlarıdır, kasada farklı olabilir.</div>`;
}

function mIndirimGorunum(k){
  const anahtar = mAnahtar(), L = S.mv.liste[anahtar];
  if (!L || (!L.hata && Date.now() - L.t > M_ONBELLEK_MS && S.mv.yuk !== anahtar)) { setTimeout(() => mYukle(false), 0); }
  const yukleniyor = S.mv.yuk === anahtar;
  let d = L ? L.urunler.slice() : [];
  const q = sadeJS(S.m.ara.trim());
  if (q) d = d.filter(u => sadeJS(u.ad + " " + u.marka + " " + u.ana).includes(q));
  const ko = u => { const x = mKarar(u); return x.durum === "ok" ? x.gercek : (u.oran || 0); };
  if (S.m.min > 0) d = d.filter(u => ko(u) >= S.m.min);
  const sisikSay = d.filter(u => mKarar(u).karar === "sisik").length;
  if (S.m.sisikGizle) d = d.filter(u => mKarar(u).karar !== "sisik");
  const srt = {gercek:(a, b) => ko(b) - ko(a), indirim:(a, b) => (b.oran || 0) - (a.oran || 0), kazanc:(a, b) => ((b.eski || b.fiyat) - b.fiyat) - ((a.eski || a.fiyat) - a.fiyat),
               ucuz:(a, b) => a.fiyat - b.fiyat, birim:(a, b) => (a.birimDeger ?? 1e12) - (b.birimDeger ?? 1e12)}[S.m.sirala] || (() => 0);
  d.sort(srt);
  const katCip = [`<button type="button" class="cip kat" data-mkat="hepsi" aria-pressed="${S.m.kat === "hepsi"}">Tüm kategoriler</button>`]
    .concat(M_KATEGORI.map(([v, a]) => `<button type="button" class="cip kat" data-mkat="${esc(v)}" aria-pressed="${S.m.kat === v}">${esc(a)}</button>`)).join("");
  const zs = (L && L.zincirSay) || [];
  const zincirCip = [`<button type="button" class="cip" data-mzincir="hepsi" aria-pressed="${S.m.zincir === "hepsi"}">Tüm marketler</button>`]
    .concat(Object.entries(M_ZINCIR).map(([v, a]) => { const z = zs.find(x => x.name === v); return `<button type="button" class="cip" data-mzincir="${v}" aria-pressed="${S.m.zincir === v}">${zEtiket(v)}${z ? " " + z.count : ""}</button>`; })).join("");
  const kalan = L && L.toplam != null ? Math.max(0, L.toplam - L.sayfa * 25) : 0;
  const gorunen = d.slice(0, S.mv.limit);
  S.mv.sonGorunen = L ? gorunen : null;
  let govde;
  if (L && L.hata && !d.length) govde = `<div class="bos-durum">${esc(L.hata)}<br><br><button type="button" class="ikincil" id="mYenile">Tekrar dene</button></div>`;
  else if (!d.length) govde = `<div class="bos-durum">${yukleniyor || !L ? "Market indirimleri getiriliyor…" : q || S.m.min ? "Bu filtreye uyan indirimli ürün yok." : "Bu konumda şu an indirimli ürün bulunamadı. Yarıçapı büyütmeyi ya da başka kategoriyi deneyin."}</div>`;
  else govde = `<div class="mf-izgara">${gorunen.map(mKart).join("")}</div>`
    + (d.length > S.mv.limit ? `<button type="button" class="daha" id="mGoster">Daha fazla göster (${d.length - S.mv.limit})</button>` : "")
    + (d.length > S.mv.limit ? "" : yukleniyor ? `<div class="bos-durum">Yükleniyor…</div>` : kalan > 0 ? `<button type="button" class="daha" id="mDaha">Daha fazla yükle (${kalan} ürün daha var)</button>` : "")
    + (L && L.hata ? `<div class="not">${esc(L.hata)}</div>` : "");
  return `
    <div class="filtre">
      <input type="search" id="mAra" placeholder="İndirimli ürünlerde ara (ör. çay, deterjan)" value="${esc(S.m.ara)}" autocomplete="off">
      <div class="cipler">${katCip}</div>
      <div class="cipler">${zincirCip}</div>
      <div class="satir">
        <select id="mMin" aria-label="En az indirim">${[[0,"Tüm oranlar"],[10,"%10 ve üstü"],[20,"%20 ve üstü"],[30,"%30 ve üstü"],[50,"%50 ve üstü"]].map(([v,a]) => `<option value="${v}" ${S.m.min === v ? "selected" : ""}>${a}</option>`).join("")}</select>
        <select id="mSirala" aria-label="Sıralama">${[["gercek","En yüksek gerçek indirim"],["indirim","Marketin ilan ettiği indirim %"],["kazanc","En çok TL kazanç"],["ucuz","En ucuz"],["birim","En ucuz birim fiyat"]].map(([v,a]) => `<option value="${v}" ${S.m.sirala === v ? "selected" : ""}>${a}</option>`).join("")}</select>
      </div>
      <label class="anahtar"><input type="checkbox" id="mSisik" ${S.m.sisikGizle ? "checked" : ""}> Şişirilmiş indirimleri gizle${sisikSay && S.m.sisikGizle ? ` (${sisikSay} gizlendi)` : ""}</label>
    </div>
    <div class="not">${esc(k.ad)} · ${S.m.yaricap} km çevresindeki şubeler${L && L.toplam != null ? ` · ${L.toplam} indirimli ürün, ${L.urunler.length} tanesi yüklendi${d.length !== L.urunler.length ? `, ${d.length} tanesi gösteriliyor` : ""}` : ""}${yukleniyor ? " · yükleniyor…" : ""}.
      “Gerçek indirim” ürünün aynı markette son 90 günde çoğunlukla satıldığı fiyata göre hesaplanır. “Fiyatları karşılaştır” aynı ürünün çevredeki diğer marketlerdeki fiyatını gösterir.
      Kaynak: Market Fiyatı (TÜBİTAK); fiyatlar marketlerin bildirdiği şube fiyatlarıdır. Bu sekmedeki ürünler diğer sekmelere karışmaz.</div>
    ${govde}`;
}

function mDegisti(){ mSakla(); S.mv.limit = 60; S.mv.kuyruk = []; window.scrollTo(0, 0); marketCiz(); }
document.addEventListener("click", e => {
  const t = e.target.closest("button"); if (!t || S.sekme !== "market") return;
  if (t.dataset.mmod) { S.m.mod = t.dataset.mmod; mSakla(); window.scrollTo(0, 0); marketCiz(); if (S.m.mod === "kars") { const a = $("#mKq"); if (a && !S.m.kq) a.focus(); } }
  else if (t.dataset.mkars) { const id = t.dataset.mkars; const u = Object.values(S.mv.liste).flatMap(L => L.urunler).find(x => x.id === id);
    if (S.mv.acik[id] && !(S.mv.kars[id] && S.mv.kars[id].durum === "hata")) { delete S.mv.acik[id]; marketCiz(); } else if (u) { S.mv.acik[id] = 1; if (S.mv.kars[id] && S.mv.kars[id].durum === "hata") delete S.mv.kars[id]; S.mv.kars[id] ? marketCiz() : mKarsilastirGetir(u); } }
  else if (t.dataset.maac) { const id = "a:" + t.dataset.maac; if (S.mv.acik[id]) delete S.mv.acik[id]; else S.mv.acik[id] = 1; marketCiz(); }
  else if (t.dataset.mone) { S.m.kq = t.dataset.mone; mSakla(); mAraYukle(false); }
  else if (t.id === "mAraDaha") mAraYukle(true);
  else if (t.id === "mAraYenile") { S.mv.ara = null; mAraYukle(false); }
  else if (t.dataset.mkat) { S.m.kat = t.dataset.mkat; mDegisti(); }
  else if (t.dataset.mzincir) { S.m.zincir = t.dataset.mzincir; mDegisti(); }
  else if (t.id === "mDaha") mYukle(true);
  else if (t.id === "mGoster") { S.mv.limit += 60; marketCiz(); }
  else if (t.id === "mYenile") { delete S.mv.liste[mAnahtar()]; marketCiz(); }
  else if (t.id === "mKonum") {
    if (!navigator.geolocation) return toast("Bu tarayıcı konum vermiyor; listeden şehir seçin");
    toast("Konum alınıyor…");
    navigator.geolocation.getCurrentPosition(p => { S.m.konum = {lat:p.coords.latitude, lon:p.coords.longitude}; mDegisti(); },
      () => toast("Konum alınamadı (izin verilmedi ya da bağlantı güvenli değil); listeden şehir seçin"), {timeout:15000, maximumAge:600000});
  }
});
document.addEventListener("change", e => {
  const t = e.target; if (S.sekme !== "market") return;
  if (t.id === "mIl") { if (t.value !== "konum") { S.m.il = +t.value; S.m.konum = null; } mDegisti(); }
  else if (t.id === "mYaricap") { S.m.yaricap = +t.value; mDegisti(); }
  else if (t.id === "mMin") { S.m.min = +t.value; mSakla(); marketCiz(); }
  else if (t.id === "mSirala") { S.m.sirala = t.value; mSakla(); marketCiz(); }
  else if (t.id === "mSisik") { S.m.sisikGizle = t.checked; mSakla(); marketCiz(); }
  else if (t.id === "mKsirala") { S.m.ksirala = t.value; mSakla(); marketCiz(); }
});
document.addEventListener("input", e => {
  if (e.target.id === "mAra") { S.m.ara = e.target.value; mSakla(); marketCiz(); }
  if (e.target.id === "mKq") { S.m.kq = e.target.value; mSakla(); clearTimeout(S.mv.kqZaman); S.mv.kqZaman = setTimeout(() => { if (S.m.kq.trim().length >= 2) mAraYukle(false); }, 700); }
});
document.addEventListener("submit", e => {
  if (e.target.id === "mKqForm") { e.preventDefault(); clearTimeout(S.mv.kqZaman); const a = $("#mKq"); if (a) a.blur(); S.mv.ara = null; mAraYukle(false); }
});

function ciz(){ cizGovde(); detayCiz(); canliGozle(); }
function cizGovde(){
  ust(); navCiz();
  const listeSekme = ["kac", "one", "ucuz", "tum", "kupon"].includes(S.sekme);
  const anaSayfa = S.sekme === "tum";
  $("#kahraman").hidden = !anaSayfa; $("#nasil").hidden = !anaSayfa; $("#telegram").hidden = !anaSayfa;
  $("#icerik").className = listeSekme ? "" : "s-kap s-sayfa";
  if (anaSayfa) vitrinCiz();
  const ua = $("#ustAra"); if (ua && document.activeElement !== ua) ua.value = S.f.ara;
  const SAYFA_BASLIK = {takip: "Fiyat alarmı", ayar: "Ayarlar", takvim: "Kampanya takvimi", market: "Market fiyatları"};
  const baslikEkle = () => { const b = SAYFA_BASLIK[S.sekme]; if (b && !$("#icerik > .s-sayfa-baslik")) $("#icerik").insertAdjacentHTML("afterbegin", `<h1 class="s-bolum-baslik s-sayfa-baslik">${b}</h1>`); };
  if (S.sekme === "takvim") { takvimCiz(); baslikEkle(); }
  else if (S.sekme === "takip") { if (document.activeElement && ["takipKelime", "urunLink"].includes(document.activeElement.id)) return; takipCiz(); baslikEkle(); }
  else if (S.sekme === "market") { const ae = document.activeElement; if (ae && ae.tagName === "SELECT" && ae.id && ae.id.startsWith("m")) return; marketCiz(); }
  else if (S.sekme === "ayar") { if (document.activeElement && document.activeElement.closest && document.activeElement.closest(".ayar")) return; ayarCiz(); qrCiz(); baslikEkle(); }
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
  if (t.id === "digerFiltre") { S.filtreAcik = !S.filtreAcik; ciz(); return; }
  if (t.dataset.kaydir) { if (S.sekme !== "tum") { S.sekme = "tum"; sakla(); ciz(); } const h = document.getElementById(t.dataset.kaydir); if (h) h.scrollIntoView({behavior: "smooth"}); return; }
  if (t.dataset.aralik) { S.aralik = +t.dataset.aralik; detayCiz(); return; }
  if (t.dataset.sekme || t.dataset.git) { if (S.detay) { S.detay = null; S.hedefAcik = null; } S.sekme = t.dataset.sekme || t.dataset.git; S.limit = 60; S.ayarTaslak = null; sakla(); ciz(); window.scrollTo(0, 0); return; }
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
  if (t.id === "urunEkle") {
    const url = ($("#urunLink").value || "").trim(); if (!/^https?:\/\//i.test(url)) { toast("Mağaza linkini https:// ile yapıştırın"); return; }
    t.disabled = true; t.textContent = "Fiyat okunuyor…";
    try {
      const r = await fetch("/api/ekle", {method:"POST", headers:{"Content-Type":"application/json","X-Kasim":"1"}, body: JSON.stringify({url})});
      const j = await r.json(); if (!r.ok) throw new Error(j.hata || "Eklenemedi");
      if (j.zaten) toast("Bu ürün zaten listende");
      else if (j.fiyat) toast(`Eklendi: ${tl(j.fiyat)} · ${(j.baslik || "").slice(0, 40)}`);
      else toast("Eklendi ama fiyat okunamadı: " + (j.mesaj || "mağaza sayfası okunamadı") + ". Mağazada görmek için karttaki bağlantıyı kullanın.");
      $("#urunLink").value = ""; if (document.activeElement) document.activeElement.blur(); await yukle();
    } catch (err) { toast(err.message); t.disabled = false; t.textContent = "Ekle"; }
    return;
  }
  if (t.dataset.urunsil) {
    try {
      const r = await fetch("/api/ekle", {method:"POST", headers:{"Content-Type":"application/json","X-Kasim":"1"}, body: JSON.stringify({islem:"sil", id: t.dataset.urunsil})});
      if (!r.ok) throw new Error("Çıkarılamadı");
      toast("Takipten çıkarıldı"); await yukle();
    } catch (err) { toast(err.message); }
    return;
  }
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
  if (t.id === "feedEkle" || t.id === "feedGuncelle" || t.dataset.feedsil) {
    const govde = t.id === "feedEkle" ? {islem: "ekle", ad: $("#feedAd").value.trim(), url: $("#feedUrl").value.trim(), ortaklik: $("#feedOrtak").checked}
      : t.id === "feedGuncelle" ? {islem: "guncelle"} : {islem: "sil", kod: t.dataset.feedsil};
    if (govde.islem === "ekle" && (govde.ad.length < 2 || !/^https?:\/\//i.test(govde.url))) { toast("Mağaza adını ve https:// ile başlayan feed adresini yazın"); return; }
    if (govde.islem === "sil" && !confirm("Bu feed ve katalogdaki ürünleri kaldırılsın mı?")) return;
    t.disabled = true;
    try {
      const r = await fetch("/api/feed", {method:"POST", headers:{"Content-Type":"application/json","X-Kasim":"1"}, body: JSON.stringify(govde)});
      const j = await r.json(); if (!r.ok) throw new Error(j.hata || "İşlem yapılamadı");
      toast(govde.islem === "ekle" ? "Feed eklendi · birkaç dakika içinde okunur" : govde.islem === "sil" ? "Feed kaldırıldı" : "Güncelleme başladı");
      if (document.activeElement) document.activeElement.blur();
      S.veri.katalog = j.katalog; S.kat = null; ayarCiz(); qrCiz(); setTimeout(yukle, 4000);
    } catch (err) { toast(err.message); t.disabled = false; }
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
  if (e.target.id === "ara" || e.target.id === "ustAra") {
    if (e.target.id === "ustAra" && !["one", "ucuz", "tum", "kupon"].includes(S.sekme)) S.sekme = "tum";
    const diger = $(e.target.id === "ara" ? "#ustAra" : "#ara"); if (diger) diger.value = e.target.value;
    S.f.ara = e.target.value; S.limit = 60; S.araDurum = null; sakla(); ciz();
    clearTimeout(S.araZaman);
    const q = S.f.ara.trim();
    if (q.length >= 3) S.araZaman = setTimeout(() => kanallardaAra(q), 700);
    clearTimeout(S.katZaman);
    if (q.length >= 2) S.katZaman = setTimeout(() => katalogAra(q), 300); else S.kat = null;
  }
});
document.addEventListener("keydown", e => {
  if (e.key === "Enter" && e.target.id === "takipKelime") { e.preventDefault(); $("#takipEkle").click(); }
  if (e.key === "Enter" && e.target.id === "urunLink") { e.preventDefault(); $("#urunEkle").click(); }
  if (e.key === "Enter" && e.target.id === "ara") { e.target.blur(); const q = S.f.ara.trim(); if (q.length >= 2) { clearTimeout(S.araZaman); kanallardaAra(q); } }
});
document.addEventListener("change", e => {
  const t = e.target;
  if (t.id === "min") { S.f.min = +t.value; sakla(); ciz(); }
  else if (t.id === "sirala") { S.f.sirala = t.value; sakla(); ciz(); if (aramaVar()) katalogAra(S.f.ara.trim()); }
  else if (t.id === "ikinciEl") { S.f.ikinciEl = t.checked; sakla(); ciz(); }
  else if (t.id === "gercekSec") { S.f.gercek = t.checked; S.limit = 60; sakla(); ciz(); }
  else if (t.id === "magazaSec") { S.f.magaza = t.value; S.limit = 60; sakla(); ciz(); }
  else if (t.id === "gecmisGizle") { S.f.gecmisGizle = t.checked; sakla(); ciz(); }
  else if (t.id === "sure") { S.f.sure = Object.assign({}, S.f.sure, {[S.sekme]: +t.value}); sakla(); ciz(); }
  else if (t.id === "kupon_ara" && S.ayarTaslak) { S.ayarTaslak.kupon_ara = t.checked; }
  else if (t.classList && t.classList.contains("dis-kaynak") && S.ayarTaslak) { S.ayarTaslak[t.id] = t.checked; }
  else if (t.id === "gecmis_tamamla" && S.ayarTaslak) { S.ayarTaslak.gecmis_tamamla = t.checked; }
  else if (S.ayarTaslak && ["aralik_dk","sayfa","bildirim_esik","saklama_gun"].includes(t.id)) { S.ayarTaslak[t.id] = +t.value; }
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) yukle(); });
document.addEventListener("submit", e => {
  if (e.target.id !== "ustAraForm") return;
  e.preventDefault();
  const q = S.f.ara.trim();
  if (q.length >= 2) { clearTimeout(S.araZaman); kanallardaAra(q); }
  $("#icerik").scrollIntoView({behavior: "smooth"});
});
yukle();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
