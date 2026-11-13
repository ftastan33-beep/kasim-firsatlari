# -*- coding: utf-8 -*-
"""Kasım Fırsatları — herkese açık site yayıncısı (Düşero).

Bilgisayardaki program mağazada doğruladığı fırsatları buradan herkese açık siteye gönderir:
  1. Seçim     : yalnızca mağazada fiyatı teyit edilmiş, gerçekten indirimli, taze ürünler
  2. Link      : başka kanalların ortaklık/izleme kodları temizlenir, varsa bizim ortaklık kodumuz eklenir
  3. Site      : site/ klasöründeki şablon + veri.json üretilir
  4. Yükleme   : GitHub deposuna TEK commit (Git Data API); GitHub Pages siteyi yayınlar
  5. Telegram  : (isteğe bağlı) yeni fırsatlar kanala gönderilir

Her ürün için programın kaydettiği fiyat geçmişine bakılır ve indirim değerlendirilir
(fiyat_karari): "dip" = dönemin en düşüğü, "iyi" = normal fiyatın altında, "sisirilmis" = üstü çizili
fiyat var ama ürün zaten çoğunlukla bu fiyata satılıyor. Geçmiş azsa karar verilmez.

Kanal paylaşımlarının metni, kanal adı ve görselleri siteye KONMAZ: kart mağaza verisinden ve kendi
metnimizle oluşturulur. Yalnızca Python standart kütüphanesi kullanılır.
"""
import base64
import getpass
import hashlib
import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

YAYIN_ARALIK_SN = 10 * 60        # GitHub Pages saatte ~10 yayın sınırı var; 10 dakikada bir en fazla
NABIZ_SN = 30 * 60               # liste değişmese de "son kontrol" saati bu aralıkla güncellenir
TAZE_SN = 6 * 3600               # mağazadaki teyidi bundan eski ürün yayından düşer
EN_FAZLA_URUN = 150
EN_DUSUK_INDIRIM = 10            # %10'dan az indirim "fırsat" sayılmaz (dip fiyat / Akakçe en ucuzu hariç)
KATALOG_PARCA = 1500             # herkese açık katalog bu büyüklükte dosyalara bölünür
BUYUK_DOSYA = 200 * 1024         # bundan büyük dosyalar GitHub'a ayrı (blob) yüklenir
TELEGRAM_TUR_BASINA = 8
GECMIS_PENCERE_GUN = 90          # indirim değerlendirmesinde bakılan dönem
GECMIS_NOKTA = 30                # sitede grafik için gönderilen en fazla geçmiş kaydı
TELEGRAM_ILK_TUR = 5          # ilk açılışta kanala gönderilecek en iyi fırsat sayısı
# Kanal siteden daha seçicidir: yalnızca "hemen bakılmalı" denecek düşüşler gider.
TELEGRAM_EN_DUSUK_INDIRIM = 20   # üstü çizili fiyata göre en az bu kadar indirim (geçmişle çelişmiyorsa)
TELEGRAM_EN_DUSUK_GERCEK = 10    # ya da kayıtlı geçmişteki normal fiyatın en az bu kadar altı
TELEGRAM_BITIS_TAKIP_SN = 7 * 86400   # gönderilen mesajın "sona erdi" diye düzenlenebileceği süre
TELEGRAM_FIYAT_ARTIS = 1.03      # mağazadaki fiyat paylaşılandan bu oranda yükselirse fırsat bitmiş sayılır
VARSAYILAN = {
    "site_adi": "Düşero",
    "slogan": "Gerçekten düşen fiyatlar",
    "github": {"depo": "", "token": "", "dal": "main"},
    "iletisim": "",               # yasal sayfada görünen e-posta (içerik kaldırma talepleri için)
    "alan_adi": "",               # özel alan adı (ör. teyitlifirsat.com); boşsa github.io adresi
    "telegram": {"bot_token": "", "kanal": ""},
    # Mağaza başına ortaklık ayarı. İki biçim:
    #   {"param": {"tag": "ornek-21"}}                       → linke parametre eklenir
    #   {"sablon": "https://ortak.example/?url={url_kodlu}"} → link şablona yerleştirilir
    "ortaklik": {},
    # Ürün kataloğu (feed'lerden): sitedeki aramada çıkar. en_fazla = siteye konacak ürün sayısı (0 = katalog yayınlanmaz),
    # aralik_saat = katalog dosyalarının en sık kaç saatte bir yenileneceği (depo gereksiz büyümesin diye)
    "katalog": {"en_fazla": 10000, "aralik_saat": 24},
}
IZLEME_PARAMLARI = re.compile(
    r"^(utm_.*|tag|ref|ref_|refid|aff.*|afid|affiliate.*|partner.*|clickid|click_id|gclid|fbclid|yclid|msclkid|"
    r"adjust_.*|adj_.*|wt_.*|sc_.*|campaign.*|cmp.*|boutiqueid|linkcode|creative.*|camp|psc|th|smid|social_share|"
    r"sr|qid|sprefix|crid|keywords|dib.*|content-id|pd_rd_.*|pf_rd_.*|channel|source|medium|share.*|kid|kupon)$", re.I)
SAKLANAN_PARAMLAR = {"trendyol": {"merchantid"}, "hepsiburada": {"magaza"}}
# Eski sürümün varsayılan adı/sloganı kayıtlı ayarlarda kaldıysa Düşero'ya geçilir (elle yazılmış ad korunur)
ESKI_VARSAYILAN = {"site_adi": "Teyitli Fırsat", "slogan": "Fiyatı mağazada teyit edilmiş indirimler"}


def eski_varsayilani_guncelle(ayar):
    for k, eski in ESKI_VARSAYILAN.items():
        if ayar.get(k) == eski:
            ayar[k] = VARSAYILAN[k]
    return ayar


# ---------------------------------------------------------------------------
# Link: temizle + ortaklık
# ---------------------------------------------------------------------------
def link_temizle(url, magaza=None):
    """Ürünü belirleyen kısmı bırakır; başka kanalların ortaklık ve izleme parametrelerini atar."""
    p = urllib.parse.urlparse(url)
    host = (p.hostname or "").lower()
    if "amazon." in host:
        m = re.search(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", p.path)
        if m:
            return f"https://www.amazon.com.tr/dp/{m.group(1)}"
    sakla = SAKLANAN_PARAMLAR.get(magaza or "", set())
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=False)
         if k.lower() in sakla or (not sakla and not IZLEME_PARAMLARI.match(k))]
    return urllib.parse.urlunparse(("https", p.netloc, p.path, "", urllib.parse.urlencode(q), ""))


def ortaklik_linki(url, magaza, ayar):
    """(link, ortaklik_mi). Ortaklık ayarı yoksa temiz link döner."""
    temiz = link_temizle(url, magaza)
    o = (ayar.get("ortaklik") or {}).get(magaza or "")
    if not o:
        return temiz, False
    if o.get("sablon"):
        return o["sablon"].replace("{url_kodlu}", urllib.parse.quote(temiz, safe="")).replace("{url}", temiz), True
    if o.get("param"):
        p = urllib.parse.urlparse(temiz)
        q = urllib.parse.parse_qsl(p.query) + list(o["param"].items())
        return urllib.parse.urlunparse(p._replace(query=urllib.parse.urlencode(q))), True
    return temiz, False


# ---------------------------------------------------------------------------
# Seçim
# ---------------------------------------------------------------------------
def _ortanca(degerler):
    s_ = sorted(degerler)
    m = len(s_) // 2
    return s_[m] if len(s_) % 2 else (s_[m - 1] + s_[m]) / 2


def fiyat_karari(fiyat, eski_fiyat, gecmis, an=None, eski_supheli=False):
    """Fiyatı programın kaydettiği geçmişle karşılaştırır.

    gecmis: [[ts, fiyat], ...] (şimdiki fiyat hariç). Dönen sözlük:
      karar        : "dip" | "iyi" | "sisirilmis" | None (geçmiş yetersiz)
      etiket       : kartta yazacak kısa açıklama
      normal       : dönemde çoğunlukla görülen fiyat (ortanca; 3+ kayıtta)
      gercek_yuzde : normal fiyata göre gerçek düşüş (%)
      en_dusuk/en_yuksek/gun : dönem özeti
      gecmis       : grafik için sadeleştirilmiş kayıtlar
    """
    an = an or time.time()
    bas = an - GECMIS_PENCERE_GUN * 86400
    h = sorted((int(t), float(p)) for t, p, *_ in (gecmis or []) if p and p > 0 and bas <= t <= an)
    sonuc = {"karar": None, "etiket": None, "normal": None, "gercek_yuzde": None,
             "en_dusuk": None, "en_yuksek": None, "gun": None, "gecmis": []}
    if h:
        adim = max(1, -(-len(h) // GECMIS_NOKTA))
        secilen = h[::-1][::adim][::-1]                     # en yeni kayıt her zaman kalsın
        sonuc["gecmis"] = [[t, round(p, 2)] for t, p in secilen]
    if not fiyat or len(h) < 2:
        if eski_fiyat and eski_supheli:
            sonuc.update(karar="sisirilmis", etiket="Üstü çizili fiyat kayıtlarda hiç görülmedi")
        return sonuc
    fiyatlar = [p for _, p in h]
    gun = max(1, round((an - h[0][0]) / 86400))
    lo, hi = min(fiyatlar), max(fiyatlar)
    sonuc.update(en_dusuk=lo, en_yuksek=hi, gun=gun)
    normal = _ortanca(fiyatlar) if len(h) >= 3 else None
    if normal:
        sonuc.update(normal=round(normal, 2), gercek_yuzde=int(round((normal - fiyat) / normal * 100)))
    # Fiyat hep aynıysa "en düşük" sayılmaz: gerçekten düşmüş olmalı (ortancanın / en yükseğin %3 altı)
    referans = normal or hi
    if gun >= 7 and fiyat <= lo * 1.001 and fiyat <= referans * 0.97:
        sonuc.update(karar="dip", etiket=f"Son {gun} günün en düşük fiyatı")
    elif normal and sonuc["gercek_yuzde"] >= 5:
        sonuc.update(karar="iyi", etiket=f"Normal fiyatın %{sonuc['gercek_yuzde']} altında")
    elif normal and eski_fiyat:
        sonuc.update(karar="sisirilmis", etiket=f"Son {gun} günde çoğunlukla {_tl(normal)}")
    elif eski_fiyat and eski_supheli:
        sonuc.update(karar="sisirilmis", etiket="Üstü çizili fiyat kayıtlarda hiç görülmedi")
    return sonuc


def _magaza_linki_mi(url):
    """Link bilinen bir mağazanın kendi alan adına mı gidiyor? (kısaltılmış mağaza linkleri dahil)"""
    import sys
    for ad in ("__main__", "kasim_firsat"):
        m = sys.modules.get(ad)
        if m is not None and hasattr(m, "magaza_alani"):
            return bool(m.magaza_alani(url))
    return True


def kisa_id(metin):
    return hashlib.sha1(metin.encode("utf-8")).hexdigest()[:12]


# Mağaza sayfası başlıklarına eklenen site/kategori kuyrukları: "...: Amazon.com.tr: Otomotiv",
# "... Fiyatları ve Özellikleri", "... - Fiyatı", "... | Boyner" gibi.
_BASLIK_KUYRUK_RE = re.compile(
    r"(?:\s*[:|\-–]\s*(?:amazon\.com\.tr|trendyol(?:\.com)?|hepsiburada(?:\.com)?|n11(?:\.com)?|boyner|pazarama|"
    r"teknosa|vatan(?:\s*bilgisayar)?|mediamarkt|morhipo|çiçeksepeti|ciceksepeti|koçtaş|koctas|a101|migros)"
    r"(?:\s*:\s*[^:|]{1,40})?\s*$)"
    r"|(?:\s*[-–|:]?\s*(?:en ucuz )?fiyat(?:ı|lari|ları)(?: ve (?:özellikleri|modelleri|yorumları))?\s*$)"
    r"|(?:\s*[-–|]\s*\d{6,}\s*$)",
    re.I)


def baslik_temizle(baslik):
    """Mağaza başlığındaki site adı, kategori ve "Fiyatları ve Özellikleri" gibi kuyrukları atar."""
    b = re.sub(r"\s+", " ", baslik or "").strip()
    for _ in range(3):                       # birden çok kuyruk üst üste olabilir
        yeni = _BASLIK_KUYRUK_RE.sub("", b).strip(" -–|:")
        if yeni == b or len(yeni) < 6:
            break
        b = yeni
    return b


def kanal_uygun(o):
    """Bu fırsat Telegram kanalına gidecek kadar güçlü mü? Site daha geniş listeyi gösterir; kanal yalnızca
    gerçek ve belirgin düşüşleri paylaşır (az ama seçilmiş paylaşım takipçiyi kanalda tutar)."""
    # (Aracı site linkleri yayin_listesi'nde zaten elenir; ortaklık linki başka alan adında olabilir.)
    if o.get("karar") == "sisirilmis":
        return False
    if o.get("karar") == "dip":
        return True
    if o.get("karar") == "iyi" and (o.get("gercek_yuzde") or 0) >= TELEGRAM_EN_DUSUK_GERCEK:
        return True
    return (o.get("yuzde") or 0) >= TELEGRAM_EN_DUSUK_INDIRIM


def biten_firsatlar(firsatlar):
    """{id: neden}: mağazada stoku biten, fiyatı tutmayan ya da kanalda "bitti" denen ürünler.
    Kimlik yayin_listesi ile aynı yoldan (temiz mağaza linki) üretilir."""
    sonuc = {}
    for d in firsatlar:
        if d.get("tur") != "urun":
            continue
        dg = d.get("dogrulama") or {}
        if not (d.get("bitti") or dg.get("durum") == "red"):
            continue
        kaynak = dg.get("son_url") or d.get("urun_link")
        if not kaynak or not kaynak.startswith("http"):
            continue
        neden = dg.get("neden") if dg.get("durum") == "red" else "Fırsat sona erdi"
        sonuc[kisa_id(link_temizle(kaynak, d.get("magaza")))] = (neden or "Fırsat sona erdi", int(dg.get("ts") or d.get("ts") or 0))
    return sonuc


def yayin_listesi(firsatlar, ayar, an=None):
    """Depo.firsatlar() çıktısından herkese açık listeyi üretir. Kanal metni/adı alınmaz."""
    an = an or time.time()
    secilen = {}
    for d in firsatlar:
        dg = d.get("dogrulama") or {}
        if d.get("tur") != "urun" or d.get("bitti") or dg.get("durum") not in ("onayli", "kosullu"):
            continue
        if an - dg.get("ts", 0) > TAZE_SN:
            continue
        kaynak_link = dg.get("son_url") or d.get("urun_link")
        if not kaynak_link or not kaynak_link.startswith("http"):
            continue
        if not _magaza_linki_mi(kaynak_link):
            continue                                 # aracı site (Onual vb.): ziyaretçi mağazaya gitmeli
        fiyat = dg.get("fiyat") or d.get("fiyat")
        if not fiyat:
            continue
        eski = None
        for aday in (dg.get("liste_fiyati"), None if d.get("eski_fiyat_supheli") else d.get("eski_fiyat")):
            if aday and aday > fiyat * 1.01:
                eski = aday
                break
        yuzde = int(round((eski - fiyat) / eski * 100)) if eski else None
        akk = d.get("akakce_dogrulama")
        akk = {"en_ucuz": akk["en_ucuz"], "fark": akk["fark"]} if akk and akk.get("durum") == "ok" else None
        if akk and akk["fark"] > 15:
            continue                                 # başka yerde belirgin şekilde daha ucuz: fırsat değil
        dip = d.get("dusus_etiketi")
        if not ((yuzde or 0) >= EN_DUSUK_INDIRIM or dip or (akk and akk["fark"] <= 1)):
            continue
        link, ortak = ortaklik_linki(kaynak_link, d.get("magaza"), ayar)
        baslik = baslik_temizle(d.get("magaza_basligi") or d.get("baslik") or "")
        if len(baslik) < 6:
            continue
        anahtar = link_temizle(kaynak_link, d.get("magaza"))
        kr = fiyat_karari(fiyat, eski, d.get("fiyat_gecmisi"), an, bool(d.get("eski_fiyat_supheli")))
        resim = next((r for r in (dg.get("resim"), d.get("resim")) if (r or "").startswith("https://")), None)
        oge = {
            "id": kisa_id(anahtar), "baslik": baslik[:140], "magaza": d.get("magaza") or "diger",
            "magaza_ad": d.get("magaza_ad") or "Mağaza", "fiyat": round(fiyat, 2), "eski_fiyat": eski, "yuzde": yuzde,
            "resim": resim,
            "link": link, "ortaklik": ortak, "teyit": int(dg["ts"]),
            "kosul": (dg.get("neden") if dg["durum"] == "kosullu" else None),
            "dip": dip, "akakce": akk, "ilk": int(d.get("ts") or dg["ts"]),
            "kategori": d.get("kategori") or "diger", "kategori_ad": d.get("kategori_ad") or "Diğer",
            **kr,
        }
        eski_oge = secilen.get(anahtar)
        if not eski_oge or oge["fiyat"] < eski_oge["fiyat"] or \
                (oge["fiyat"] == eski_oge["fiyat"] and oge["teyit"] > eski_oge["teyit"]):
            if eski_oge:
                oge["ilk"] = min(oge["ilk"], eski_oge["ilk"])
            secilen[anahtar] = oge
    liste = sorted(secilen.values(), key=lambda o: -o["ilk"])
    return liste[:EN_FAZLA_URUN]


# ---------------------------------------------------------------------------
# Site dosyaları
# ---------------------------------------------------------------------------
def site_dosyalari(sablon_dizini, ayar, liste, surum):
    """{yol: bytes}. Şablondaki {{SITE_ADI}} gibi yer tutucular doldurulur."""
    tg_ayar = ayar.get("telegram") or {}
    # Sitedeki Telegram düğmesi kanalın herkese açık kullanıcı adına gider; "kanal" sayısal kimlik olabilir.
    tg = (tg_ayar.get("kullanici") or ("" if tg_ayar.get("kanal", "").startswith("-") else tg_ayar.get("kanal", ""))).lstrip("@")
    yerine = {
        "{{SITE_ADI}}": html.escape(ayar.get("site_adi") or VARSAYILAN["site_adi"]),
        "{{SLOGAN}}": html.escape(ayar.get("slogan") or VARSAYILAN["slogan"]),
        "{{TELEGRAM}}": html.escape(tg),
        "{{SURUM}}": surum,
        "{{YIL}}": time.strftime("%Y"),
        "{{ILETISIM}}": (f'E-posta: <a href="mailto:{html.escape(ayar["iletisim"])}">{html.escape(ayar["iletisim"])}</a>'
                         if ayar.get("iletisim") else "İletişim bilgisi eklenecek."),
    }
    dosyalar = {}
    for kok, _, adlar in os.walk(sablon_dizini):
        for ad in adlar:
            yol = os.path.join(kok, ad)
            goreli = os.path.relpath(yol, sablon_dizini).replace(os.sep, "/")
            with open(yol, "rb") as f:
                icerik = f.read()
            if goreli.endswith((".html", ".js", ".css", ".webmanifest", ".svg")):
                metin = icerik.decode("utf-8")
                for a, b in yerine.items():
                    metin = metin.replace(a, b)
                icerik = metin.encode("utf-8")
            dosyalar[goreli] = icerik
    dosyalar[".nojekyll"] = b""
    if ayar.get("alan_adi"):
        dosyalar["CNAME"] = ayar["alan_adi"].strip().encode()
    dosyalar["veri.json"] = json.dumps({"guncelleme": int(time.time()), "urunler": liste},
                                       ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return dosyalar


def katalog_dosyalari(urunler, guncelleme, ayar=None):
    """Katalog ürünlerini (katalog.Katalog.disa_aktar çıktısı) sitenin okuyacağı küçük dosyalara böler.
    Ürün hangi dosyaya gideceğini kimliğinden alır; böylece bir fiyat değişince yalnızca o dosya yeniden yüklenir.
    Feed'den gelen link ortaklık linki değilse ve mağaza için ortaklık ayarı varsa bizim kodumuz eklenir."""
    import zlib
    if not urunler:
        return {}
    parca = max(1, -(-len(urunler) // KATALOG_PARCA))
    kovalar = [[] for _ in range(parca)]
    for u in urunler:
        link, ortak = u["link"], bool(u.get("ortaklik"))
        if not ortak and ayar:
            link, ortak = ortaklik_linki(link, magaza_kodu(link), ayar)
        kovalar[zlib.crc32(u["id"].encode("utf-8")) % parca].append(
            [kisa_id(u["id"]), u["baslik"][:140], u.get("magaza_ad") or "Mağaza", u["fiyat"], u.get("eski_fiyat") or 0,
             link, u.get("resim") or "", int(ortak), int(bool(u.get("dip"))), u.get("dusus") or 0])
    yaz = lambda v: json.dumps(v, ensure_ascii=False, separators=(",", ":")).encode("utf-8")   # noqa: E731
    dosyalar = {f"katalog/{i}.json": yaz(k) for i, k in enumerate(kovalar)}
    dosyalar["katalog/ozet.json"] = yaz({"adet": len(urunler), "parca": parca, "guncelleme": int(guncelleme)})
    return dosyalar


def magaza_kodu(link):
    """Ortaklık ayarındaki mağaza anahtarı (amazon, trendyol ...) için linkten kaba mağaza adı."""
    host = (urllib.parse.urlparse(link).hostname or "").lower()
    parcalar = [p for p in host.split(".") if p not in ("www", "m", "com", "tr", "net", "org")]
    return parcalar[-1] if parcalar else ""


def liste_ozeti(liste):
    """Değişiklik algılamak için: zaman damgaları hariç içerik özeti."""
    return hashlib.sha1(json.dumps([{k: v for k, v in o.items() if k != "teyit"} for o in liste],
                                   sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# ---------------------------------------------------------------------------
# GitHub: tek commit ile yükleme
# ---------------------------------------------------------------------------
class GitHubHata(Exception):
    pass


def _gh(token, yontem, yol, govde=None, zaman_asimi=30):
    istek = urllib.request.Request(
        "https://api.github.com" + yol, method=yontem,
        data=json.dumps(govde).encode() if govde is not None else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "kasim-firsatlari-yayin",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(istek, timeout=zaman_asimi) as y:
            ham = y.read()
            return json.loads(ham) if ham else {}
    except urllib.error.HTTPError as e:
        try:
            mesaj = json.loads(e.read()).get("message", "")
        except Exception:
            mesaj = ""
        if e.code == 401:
            raise GitHubHata("GitHub anahtarı (token) geçersiz ya da süresi dolmuş") from None
        if e.code == 403:
            raise GitHubHata("GitHub anahtarının bu depoya yazma izni yok (Contents: Read and write) " + mesaj) from None
        if e.code == 404 and yontem == "GET" and "/git/ref" in yol:
            return None
        if e.code == 404:
            raise GitHubHata(f"Depo bulunamadı ya da anahtar bu depoyu göremiyor ({yol.split('/')[3] if yol.count('/') > 3 else yol})") from None
        if e.code == 409 and "/git/ref" in yol:          # boş depo
            return None
        raise GitHubHata(f"GitHub HTTP {e.code}: {mesaj}") from None


def github_yukle(gh, dosyalar, mesaj):
    """Değişen dosyaları tek commit'te yükler. gh = {depo, token, dal}."""
    depo, token, dal = gh["depo"], gh["token"], gh.get("dal") or "main"
    ref = _gh(token, "GET", f"/repos/{depo}/git/ref/heads/{dal}")
    if ref is None:   # boş depo: ilk dosyayı içerik API'siyle oluştur, sonra normal akış
        _gh(token, "PUT", f"/repos/{depo}/contents/.nojekyll",
            {"message": "Siteyi başlat", "content": "", "branch": dal})
        ref = _gh(token, "GET", f"/repos/{depo}/git/ref/heads/{dal}")
    ust = ref["object"]["sha"]
    taban_agac = _gh(token, "GET", f"/repos/{depo}/git/commits/{ust}")["tree"]["sha"]
    girdiler = []
    for yol, icerik in dosyalar.items():
        try:
            if len(icerik) > BUYUK_DOSYA:               # büyük dosya tek istekte gitmesin
                raise UnicodeDecodeError("utf-8", b"", 0, 1, "büyük")
            metin = icerik.decode("utf-8")
            if "\x00" in metin:
                raise UnicodeDecodeError("utf-8", b"", 0, 1, "ikili")
            girdiler.append({"path": yol, "mode": "100644", "type": "blob", "content": metin})
        except UnicodeDecodeError:
            blob = _gh(token, "POST", f"/repos/{depo}/git/blobs",
                       {"content": base64.b64encode(icerik).decode(), "encoding": "base64"})
            girdiler.append({"path": yol, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    agac = _gh(token, "POST", f"/repos/{depo}/git/trees", {"base_tree": taban_agac, "tree": girdiler})
    commit = _gh(token, "POST", f"/repos/{depo}/git/commits", {"message": mesaj, "tree": agac["sha"], "parents": [ust]})
    _gh(token, "PATCH", f"/repos/{depo}/git/refs/heads/{dal}", {"sha": commit["sha"]})
    return commit["sha"]


# ---------------------------------------------------------------------------
# Telegram kanalı
# ---------------------------------------------------------------------------
def _tl(x):
    s = f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return (s[:-3] if s.endswith(",00") else s) + " TL"


def telegram_metni(o, site_adi, bitis=None):
    """Kanal mesajı. bitis verilirse (neden metni) mesaj "sona erdi" olarak düzenlenmiş hâliyle üretilir."""
    satirlar = []
    if bitis:
        satirlar += [f"❌ <b>FIRSAT SONA ERDİ</b> · {html.escape(bitis)}", ""]
    satirlar += [f"<b>{html.escape(o['baslik'])}</b>", ""]
    fiyat = f"💰 <b>{_tl(o['fiyat'])}</b>"
    if o.get("eski_fiyat"):
        fiyat += f"  <s>{_tl(o['eski_fiyat'])}</s>  (%{o['yuzde']} indirim)"
    elif o.get("normal") and (o.get("gercek_yuzde") or 0) > 0:
        # Mağazada üstü çizili fiyat yoksa kendi kayıtlarımızdaki normal fiyatı gösteririz
        fiyat += f"  <s>{_tl(o['normal'])}</s>  (normal fiyatın %{o['gercek_yuzde']} altında)"
    satirlar.append(fiyat)
    if o.get("karar") in ("dip", "iyi"):
        satirlar.append(f"📉 {'Gerçek indirim' if o['karar'] == 'dip' else 'İyi fiyat'} · {html.escape(o['etiket'])}")
    elif o.get("dip"):
        satirlar.append(f"📉 {html.escape(o['dip'])}")
    if o.get("en_dusuk") and o.get("en_yuksek") and o["en_yuksek"] > o["en_dusuk"] and o.get("gun"):
        satirlar.append(f"📊 Son {o['gun']} gün: en düşük {_tl(o['en_dusuk'])} · en yüksek {_tl(o['en_yuksek'])}")
    if o.get("akakce"):
        satirlar.append("⚖️ Akakçe'deki en ucuz fiyat seviyesinde" if o["akakce"]["fark"] <= 1 else
                        f"⚖️ Akakçe'de en ucuzu {_tl(o['akakce']['en_ucuz'])}")
    if o.get("kosul"):
        satirlar.append(f"⚠️ {html.escape(o['kosul'])}")
    satirlar.append(f"🏬 {html.escape(o['magaza_ad'])} · ✔ {time.strftime('%H:%M', time.localtime(o['teyit']))} itibarıyla mağazada teyit edildi")
    if not bitis:
        satirlar += ["", f'<a href="{html.escape(o["link"])}">👉 Mağazada gör</a>']
    satirlar.append("<i>Fiyat ve stok anlık değişebilir.</i>" + (" #işbirliği" if o.get("ortaklik") else ""))
    return "\n".join(satirlar)


def _tg_istek(tg, yontem, govde):
    istek = urllib.request.Request(f"https://api.telegram.org/bot{tg['bot_token']}/{yontem}",
                                   data=json.dumps(govde).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(istek, timeout=20) as y:
        return json.loads(y.read())


def _tg_sohbet(tg):
    return tg["kanal"] if tg["kanal"].startswith(("@", "-")) else "@" + tg["kanal"]


def telegram_gonder(tg, o, site_adi):
    """Mesajı gönderir. Başarılıysa {"mid": mesaj no, "tur": "foto"|"yazi"} döner (doğruluk değeri True)."""
    if o.get("resim"):
        yontem, govde = "sendPhoto", {"photo": o["resim"], "caption": telegram_metni(o, site_adi)}
    else:
        # Önizleme her zaman kendi mağaza linkimizden üretilsin (başka sitenin kartı görünmesin)
        yontem, govde = "sendMessage", {"text": telegram_metni(o, site_adi),
                                        "link_preview_options": {"url": o["link"], "prefer_large_media": True}}
    govde.update(chat_id=_tg_sohbet(tg), parse_mode="HTML")
    try:
        cevap = _tg_istek(tg, yontem, govde)
    except urllib.error.HTTPError as e:
        if yontem == "sendPhoto" and e.code == 400:    # görsel indirilemedi: yazıyla dene
            return telegram_gonder(tg, dict(o, resim=None), site_adi)
        raise
    if not cevap.get("ok"):
        return False
    return {"mid": (cevap.get("result") or {}).get("message_id"), "tur": "foto" if yontem == "sendPhoto" else "yazi"}


def telegram_bitti_isaretle(tg, kayit, neden, site_adi):
    """Gönderilmiş mesajı "sona erdi" diye düzenler. kayit = {"mid", "tur", "o"}."""
    metin = telegram_metni(kayit["o"], site_adi, bitis=neden)
    govde = {"chat_id": _tg_sohbet(tg), "message_id": kayit["mid"], "parse_mode": "HTML"}
    if kayit.get("tur") == "foto":
        yontem = "editMessageCaption"
        govde["caption"] = metin
    else:
        yontem = "editMessageText"
        govde.update(text=metin, link_preview_options={"is_disabled": True})
    try:
        return bool(_tg_istek(tg, yontem, govde).get("ok"))
    except urllib.error.HTTPError as e:
        if e.code == 400:      # mesaj silinmiş ya da zaten aynı: tekrar denenmesin
            return True
        raise


# ---------------------------------------------------------------------------
# Yayıncı
# ---------------------------------------------------------------------------
class Yayinci:
    def __init__(self, depo, veri_dizini, sablon_dizini, surum):
        self.depo, self.sablon, self.surum = depo, sablon_dizini, surum
        self.ayar_yolu = os.path.join(veri_dizini, "yayin.json")
        self.durum_yolu = os.path.join(veri_dizini, "yayin_durum.json")
        self.ayar = self._oku(self.ayar_yolu, {})
        for k, v in VARSAYILAN.items():
            self.ayar.setdefault(k, json.loads(json.dumps(v)))
        eski_varsayilani_guncelle(self.ayar)
        self.durum = self._oku(self.durum_yolu, {"hashler": {}, "ozet": None, "son": 0, "gonderilen": {}})
        self.son_hata, self.son_sayi = None, 0
        self.tetik = threading.Event()
        self.kilit = threading.Lock()

    @staticmethod
    def _oku(yol, varsayilan):
        try:
            with open(yol, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return varsayilan

    def _yaz(self, yol, veri):
        gecici = yol + ".tmp"
        with open(gecici, "w", encoding="utf-8") as f:
            json.dump(veri, f, ensure_ascii=False, indent=1)
        os.replace(gecici, yol)

    def ayarlari_kaydet(self):
        self._yaz(self.ayar_yolu, self.ayar)

    def hazir(self):
        gh = self.ayar.get("github") or {}
        return bool(gh.get("depo") and gh.get("token") and os.path.isdir(self.sablon))

    def liste(self):
        with self.depo.kilit:
            f = self.depo.firsatlar()
        self._son_firsatlar = f
        return yayin_listesi(f, self.ayar)

    def katalog_dosyalari(self, zorla=False):
        """Feed kataloğunun site dosyaları. Sık yeniden üretilmez (varsayılan günde bir): depo şişmesin."""
        feedler = getattr(self.depo, "feedler", None)
        ka = self.ayar.get("katalog") or {}
        en_fazla = int(ka.get("en_fazla") or 0)
        if not feedler or en_fazla <= 0:
            return {}
        an = time.time()
        onbellek = getattr(self, "_katalog_onbellek", None)
        if onbellek and not zorla and an - onbellek[0] < max(1, float(ka.get("aralik_saat") or 24)) * 3600:
            return onbellek[1]
        dosyalar = katalog_dosyalari(feedler.katalog.disa_aktar(en_fazla), feedler.son_guncelleme() or an, self.ayar)
        if not dosyalar and self.durum.get("katalog_parca"):
            # Katalog boşaldı (feed'ler silindi): sitedeki arama eski ürünleri göstermesin
            dosyalar = {"katalog/ozet.json": b'{"adet":0,"parca":0,"guncelleme":%d}' % int(an)}
        self._katalog_onbellek = (an, dosyalar)
        return dosyalar

    def tum_dosyalar(self, liste, katalog_zorla=False):
        dosyalar = site_dosyalari(self.sablon, self.ayar, liste, self.surum)
        dosyalar.update(self.katalog_dosyalari(katalog_zorla))
        return dosyalar

    def onizle(self, hedef):
        liste = self.liste()
        for yol, icerik in self.tum_dosyalar(liste, True).items():
            tam = os.path.join(hedef, *yol.split("/"))
            os.makedirs(os.path.dirname(tam), exist_ok=True)
            with open(tam, "wb") as f:
                f.write(icerik)
        return len(liste)

    def yayinla(self, zorla=False):
        """Liste değiştiyse (ya da nabız süresi dolduysa) siteyi yükler. Dönen: yayınlanan ürün sayısı ya da None."""
        with self.kilit:
            liste = self.liste()
            ozet = liste_ozeti(liste)
            if not zorla and ozet == self.durum.get("ozet") and time.time() - self.durum.get("son", 0) < NABIZ_SN:
                return None
            dosyalar = self.tum_dosyalar(liste, zorla)
            self.durum["katalog_parca"] = sum(1 for y in dosyalar if y.startswith("katalog/") and y != "katalog/ozet.json")
            hashler = self.durum.setdefault("hashler", {})
            degisen = {y: i for y, i in dosyalar.items()
                       if zorla or y == "veri.json" or hashler.get(y) != hashlib.sha1(i).hexdigest()}
            github_yukle(self.ayar["github"], degisen,
                         f"Fırsatlar güncellendi · {len(liste)} ürün · {time.strftime('%d.%m %H:%M')}")
            for y, i in degisen.items():
                hashler[y] = hashlib.sha1(i).hexdigest()
            self.durum.update(ozet=ozet, son=int(time.time()))
            self.son_sayi, self.son_hata = len(liste), None
            self._telegram(liste)
            self._yaz(self.durum_yolu, self.durum)
            return len(liste)

    def _telegram(self, liste):
        tg = self.ayar.get("telegram") or {}
        if not (tg.get("bot_token") and tg.get("kanal")):
            return
        gon = self.durum.setdefault("gonderilen", {})
        an = time.time()
        for k in [k for k, t in gon.items() if an - t > 7 * 86400]:
            del gon[k]
        mesajlar = self.durum.setdefault("tg_mesajlar", {})
        for k in [k for k, m in mesajlar.items() if an - m.get("t", 0) > TELEGRAM_BITIS_TAKIP_SN]:
            del mesajlar[k]
        self._telegram_bitenler(tg, liste, mesajlar)
        # Kanal yalnızca gerçek ve belirgin düşüşleri paylaşır (kanal_uygun); diğerleri sitede durur
        uygun = [o for o in liste if kanal_uygun(o)]
        yeni = [o for o in sorted(uygun, key=lambda o: o["ilk"]) if o["id"] not in gon][:TELEGRAM_TUR_BASINA]
        if not self.durum.get("telegram_basladi"):
            # İlk çalıştırmada tüm liste kanala dökülmesin: yalnızca en iyi birkaç fırsat paylaşılır,
            # kalanlar "gönderildi" sayılır. Sonraki turlarda sadece yeni fırsatlar gider.
            sira = {"dip": 0, "iyi": 1}
            en_iyi = sorted(uygun, key=lambda o: (sira.get(o.get("karar"), 2), -(o.get("gercek_yuzde") or 0),
                                                  -(o.get("yuzde") or 0), -o["teyit"]))[:TELEGRAM_ILK_TUR]
            secili = {o["id"] for o in en_iyi}
            for o in liste:
                if o["id"] not in secili:
                    gon[o["id"]] = an
            self.durum["telegram_basladi"] = True
            yeni = en_iyi
        for o in yeni:
            try:
                sonuc = telegram_gonder(tg, o, self.ayar["site_adi"])
                if sonuc:
                    gon[o["id"]] = time.time()
                    if isinstance(sonuc, dict) and sonuc.get("mid"):
                        mesajlar[o["id"]] = {"mid": sonuc["mid"], "tur": sonuc.get("tur"), "t": int(time.time()), "o": o}
            except Exception as e:
                self.son_hata = "Telegram: " + str(e)[:120]
                break
            time.sleep(3)

    def _telegram_bitenler(self, tg, liste, mesajlar):
        """Kanalda paylaşılmış ama artık geçerli olmayan fırsatların mesajını "sona erdi" diye düzenler.
        Yalnızca kesin durumlar: mağazada stok bitti / fiyat tutmadı / kanalda bitti denildi ya da fiyat yükseldi.
        Sadece teyidi eskidiği için listeden düşen ürüne dokunulmaz (fırsat hâlâ sürüyor olabilir)."""
        if not mesajlar:
            return
        aktif = {o["id"]: o for o in liste}
        biten = biten_firsatlar(getattr(self, "_son_firsatlar", None) or [])
        for oid, m in list(mesajlar.items()):
            if m.get("bitti"):
                continue
            neden = None
            simdi = aktif.get(oid)
            if simdi:
                if simdi["fiyat"] > m["o"]["fiyat"] * TELEGRAM_FIYAT_ARTIS:
                    neden = f"Fiyat {_tl(simdi['fiyat'])} oldu"
            elif oid in biten and biten[oid][1] >= m.get("t", 0):
                neden = biten[oid][0]
            if not neden:
                continue
            try:
                if telegram_bitti_isaretle(tg, m, neden, self.ayar["site_adi"]):
                    m["bitti"] = int(time.time())
            except Exception as e:
                self.son_hata = "Telegram: " + str(e)[:120]
                return
            time.sleep(1)

    def ozet(self):
        gh = self.ayar.get("github") or {}
        return {"hazir": self.hazir(), "depo": gh.get("depo"), "son": self.durum.get("son"),
                "urun": self.son_sayi, "hata": self.son_hata,
                "adres": site_adresi(self.ayar)}

    def dongu(self):
        """Doğrulama turları bittikçe (en sık 10 dakikada bir) yayınlar."""
        time.sleep(90)                       # ilk okuma ve doğrulama biraz ilerlesin
        while True:
            try:
                n = self.yayinla()
                if n is not None:
                    print(f"[yayın] {time.strftime('%H:%M')} · {n} fırsat yayında → {site_adresi(self.ayar)}")
            except Exception as e:
                self.son_hata = str(e)[:200]
                print("[yayın] Hata:", self.son_hata)
            self.tetik.wait(timeout=YAYIN_ARALIK_SN)
            self.tetik.clear()
            time.sleep(5)


def site_adresi(ayar):
    if ayar.get("alan_adi"):
        return "https://" + ayar["alan_adi"].strip()
    depo = (ayar.get("github") or {}).get("depo") or ""
    if "/" not in depo:
        return ""
    sahip, ad = depo.split("/", 1)
    return f"https://{sahip.lower()}.github.io/" + ("" if ad.lower() == f"{sahip.lower()}.github.io" else ad + "/")


def kurulum_sihirbazi(veri_dizini):
    """Konsoldan soru-cevapla yayin.json oluşturur ve bağlantıyı dener."""
    yol = os.path.join(veri_dizini, "yayin.json")
    ayar = Yayinci._oku(yol, {})
    for k, v in VARSAYILAN.items():
        ayar.setdefault(k, json.loads(json.dumps(v)))
    eski_varsayilani_guncelle(ayar)

    def sor(soru, mevcut="", gizli=False):
        ipucu = f" [{'(kayıtlı)' if gizli and mevcut else mevcut}]" if mevcut else ""
        cevap = (getpass.getpass if gizli else input)(f"{soru}{ipucu}: ").strip()
        return cevap or mevcut

    print("\n=== Herkese açık site kurulumu ===  (boş bırakırsanız köşeli parantezdeki değer kalır)\n")
    ayar["site_adi"] = sor("Site adı", ayar["site_adi"])
    ayar["github"]["depo"] = sor("GitHub deposu (kullanıcı/depo, ör. kullaniciadi/teyitli-firsat)", ayar["github"]["depo"])
    ayar["github"]["token"] = sor("GitHub anahtarı (yapıştırın, ekranda görünmez)", ayar["github"]["token"], gizli=True)
    ayar["iletisim"] = sor("Sitede görünecek iletişim e-postası", ayar.get("iletisim", ""))
    ayar["alan_adi"] = sor("Özel alan adı (yoksa boş geçin)", ayar.get("alan_adi", ""))
    if input("Telegram kanalına da otomatik paylaşılsın mı? (e/h) [h]: ").strip().lower().startswith("e"):
        ayar["telegram"]["bot_token"] = sor("Telegram bot anahtarı (BotFather'dan)", ayar["telegram"]["bot_token"], gizli=True)
        ayar["telegram"]["kanal"] = sor("Kanal kullanıcı adı (ör. @dusero)", ayar["telegram"]["kanal"])
    print("\nGitHub bağlantısı deneniyor...")
    try:
        d = _gh(ayar["github"]["token"], "GET", f"/repos/{ayar['github']['depo']}")
        if not d.get("permissions", {}).get("push", True):
            print("! Anahtar depoyu görüyor ama yazamıyor. Contents iznini 'Read and write' yapın.")
        else:
            print(f"✔ Depo bulundu: {d.get('full_name')} ({'herkese açık' if not d.get('private') else 'gizli'})")
    except Exception as e:
        print("! Bağlantı kurulamadı:", e)
    with open(yol, "w", encoding="utf-8") as f:
        json.dump(ayar, f, ensure_ascii=False, indent=1)
    print(f"\nAyarlar kaydedildi: {yol}")
    print(f"Site adresi (GitHub Pages açıldıktan sonra): {site_adresi(ayar)}")
    print("Programı her zamanki gibi başlatın; doğrulanan fırsatlar 10 dakikada bir siteye gönderilir.\n")
