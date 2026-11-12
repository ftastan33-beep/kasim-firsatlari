# -*- coding: utf-8 -*-
"""Kasım Fırsatları — herkese açık site yayıncısı (Düşero).

Bilgisayardaki program mağazada doğruladığı fırsatları buradan herkese açık siteye gönderir:
  1. Seçim     : yalnızca mağazada fiyatı teyit edilmiş, gerçekten indirimli, taze ürünler
  2. Link      : başka kanalların ortaklık/izleme kodları temizlenir, varsa bizim ortaklık kodumuz eklenir
  3. Site      : site/ klasöründeki şablon + veri.json üretilir
  4. Yükleme   : GitHub deposuna TEK commit (Git Data API); GitHub Pages siteyi yayınlar
  5. Telegram  : (isteğe bağlı) yeni fırsatlar kanala gönderilir

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
TELEGRAM_TUR_BASINA = 8
ESKI_SITE_ADLARI = {"Teyitli Fırsat"}   # eski varsayılan ad; kayıtlı ayarda buysa Düşero'ya geçilir
VARSAYILAN = {
    "site_adi": "Düşero",
    "slogan": "Fiyatı mağazada teyit edilmiş indirimler",
    "github": {"depo": "", "token": "", "dal": "main"},
    "iletisim": "",               # yasal sayfada görünen e-posta (içerik kaldırma talepleri için)
    "alan_adi": "",               # özel alan adı (ör. dusero.com); boşsa github.io adresi
    "telegram": {"bot_token": "", "kanal": ""},
    # Mağaza başına ortaklık ayarı. İki biçim:
    #   {"param": {"tag": "ornek-21"}}                       → linke parametre eklenir
    #   {"sablon": "https://ortak.example/?url={url_kodlu}"} → link şablona yerleştirilir
    "ortaklik": {},
}
IZLEME_PARAMLARI = re.compile(
    r"^(utm_.*|tag|ref|ref_|refid|aff.*|afid|affiliate.*|partner.*|clickid|click_id|gclid|fbclid|yclid|msclkid|"
    r"adjust_.*|adj_.*|wt_.*|sc_.*|campaign.*|cmp.*|boutiqueid|linkcode|creative.*|camp|psc|th|smid|social_share|"
    r"sr|qid|sprefix|crid|keywords|dib.*|content-id|pd_rd_.*|pf_rd_.*|channel|source|medium|share.*|kid|kupon)$", re.I)
SAKLANAN_PARAMLAR = {"trendyol": {"merchantid"}, "hepsiburada": {"magaza"}}


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
def kisa_id(metin):
    return hashlib.sha1(metin.encode("utf-8")).hexdigest()[:12]


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
        baslik = re.sub(r"\s+", " ", d.get("magaza_basligi") or d.get("baslik") or "").strip()
        if len(baslik) < 6:
            continue
        anahtar = link_temizle(kaynak_link, d.get("magaza"))
        oge = {
            "id": kisa_id(anahtar), "baslik": baslik[:140], "magaza": d.get("magaza") or "diger",
            "magaza_ad": d.get("magaza_ad") or "Mağaza", "fiyat": round(fiyat, 2), "eski_fiyat": eski, "yuzde": yuzde,
            "resim": d.get("resim") if (d.get("resim") or "").startswith("https://") else None,
            "link": link, "ortaklik": ortak, "teyit": int(dg["ts"]),
            "kosul": (dg.get("neden") if dg["durum"] == "kosullu" else None),
            "dip": dip, "akakce": akk, "ilk": int(d.get("ts") or dg["ts"]),
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
    tg = (ayar.get("telegram") or {}).get("kanal", "").lstrip("@")
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


def telegram_metni(o, site_adi):
    satirlar = [f"<b>{html.escape(o['baslik'])}</b>", ""]
    fiyat = f"💰 <b>{_tl(o['fiyat'])}</b>"
    if o.get("eski_fiyat"):
        fiyat += f"  <s>{_tl(o['eski_fiyat'])}</s>  (%{o['yuzde']} indirim)"
    satirlar.append(fiyat)
    satirlar.append(f"🏬 {html.escape(o['magaza_ad'])} · ✔ {time.strftime('%H:%M', time.localtime(o['teyit']))} itibarıyla mağazada teyit edildi")
    if o.get("dip"):
        satirlar.append(f"📉 {html.escape(o['dip'])}")
    if o.get("akakce"):
        satirlar.append("⚖️ Akakçe'deki en ucuz fiyat seviyesinde" if o["akakce"]["fark"] <= 1 else
                        f"⚖️ Akakçe'de en ucuzu {_tl(o['akakce']['en_ucuz'])}")
    if o.get("kosul"):
        satirlar.append(f"⚠️ {html.escape(o['kosul'])}")
    satirlar += ["", f'<a href="{html.escape(o["link"])}">👉 Mağazada gör</a>']
    satirlar.append(f"🔻 <b>{html.escape(site_adi or VARSAYILAN['site_adi'])}</b> · <i>Fiyat ve stok anlık değişebilir.</i>"
                    + (" #işbirliği" if o.get("ortaklik") else ""))
    return "\n".join(satirlar)


def telegram_gonder(tg, o, site_adi):
    yontem, govde = ("sendPhoto", {"photo": o["resim"], "caption": telegram_metni(o, site_adi)}) if o.get("resim") else \
        ("sendMessage", {"text": telegram_metni(o, site_adi), "disable_web_page_preview": False})
    govde.update(chat_id=tg["kanal"] if tg["kanal"].startswith(("@", "-")) else "@" + tg["kanal"], parse_mode="HTML")
    istek = urllib.request.Request(f"https://api.telegram.org/bot{tg['bot_token']}/{yontem}",
                                   data=json.dumps(govde).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(istek, timeout=20) as y:
            return json.loads(y.read()).get("ok", False)
    except urllib.error.HTTPError as e:
        if yontem == "sendPhoto" and e.code == 400:    # görsel indirilemedi: yazıyla dene
            o2 = dict(o, resim=None)
            return telegram_gonder(tg, o2, site_adi)
        raise


# ---------------------------------------------------------------------------
# Yayıncı
# ---------------------------------------------------------------------------
def marka_guncelle(ayar):
    """Eski varsayılan site adını (Teyitli Fırsat) Düşero yapar. Kullanıcının kendi koyduğu ada dokunmaz."""
    if (ayar.get("site_adi") or "").strip() in ESKI_SITE_ADLARI:
        ayar["site_adi"] = VARSAYILAN["site_adi"]
        return True
    return False


class Yayinci:
    def __init__(self, depo, veri_dizini, sablon_dizini, surum):
        self.depo, self.sablon, self.surum = depo, sablon_dizini, surum
        self.ayar_yolu = os.path.join(veri_dizini, "yayin.json")
        self.durum_yolu = os.path.join(veri_dizini, "yayin_durum.json")
        self.ayar = self._oku(self.ayar_yolu, {})
        for k, v in VARSAYILAN.items():
            self.ayar.setdefault(k, json.loads(json.dumps(v)))
        if marka_guncelle(self.ayar) and os.path.exists(self.ayar_yolu):
            self.ayarlari_kaydet()
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
        return yayin_listesi(f, self.ayar)

    def onizle(self, hedef):
        liste = self.liste()
        for yol, icerik in site_dosyalari(self.sablon, self.ayar, liste, self.surum).items():
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
            dosyalar = site_dosyalari(self.sablon, self.ayar, liste, self.surum)
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
        yeni = [o for o in sorted(liste, key=lambda o: o["ilk"]) if o["id"] not in gon][:TELEGRAM_TUR_BASINA]
        if not self.durum.get("telegram_basladi"):
            # İlk çalıştırmada eldeki tüm liste kanala dökülmesin: mevcutlar "gönderildi" sayılır
            for o in liste:
                gon[o["id"]] = an
            self.durum["telegram_basladi"] = True
            return
        for o in yeni:
            try:
                if telegram_gonder(tg, o, self.ayar["site_adi"]):
                    gon[o["id"]] = time.time()
            except Exception as e:
                self.son_hata = "Telegram: " + str(e)[:120]
                break
            time.sleep(3)

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
    marka_guncelle(ayar)

    def sor(soru, mevcut="", gizli=False):
        ipucu = f" [{'(kayıtlı)' if gizli and mevcut else mevcut}]" if mevcut else ""
        cevap = (getpass.getpass if gizli else input)(f"{soru}{ipucu}: ").strip()
        return cevap or mevcut

    print("\n=== Herkese açık site kurulumu ===  (boş bırakırsanız köşeli parantezdeki değer kalır)\n")
    ayar["site_adi"] = sor("Site adı", ayar["site_adi"])
    ayar["github"]["depo"] = sor("GitHub deposu (kullanıcı/depo, ör. ftastan33-beep/dusero)", ayar["github"]["depo"])
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
