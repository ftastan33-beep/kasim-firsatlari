# Kasım Fırsatları

Telegram fırsat kanallarındaki indirimleri, kuponları ve fiyat geçmişini gösteren kişisel site.
GitHub Actions yaklaşık 5 dakikada bir kanalları okur ve siteyi GitHub Pages'te günceller.

- Site: depo **Settings → Pages** bölümünde yazan adres
- Elle güncelleme: **Actions → Fırsatları güncelle → Run workflow**
- Telefona bildirim (isteğe bağlı): **Settings → Secrets and variables → Actions → New repository secret**,
  ad `NTFY_KONU`, değer ntfy uygulamasında abone olunan konu adı.

Bilgisayarda çalıştırmak için: `Kasim-Firsatlari-Baslat.bat` (ayrıntılar `KURULUM.txt`).

## Doğru fiyat kontrolleri (2.8)

- **30 gün kuralı:** mağazadan ve kanallardan kaydedilen fiyatlara göre "indirim" son 30 günün en düşüğüyle kıyaslanır.
- **Şişirilmiş eski fiyat:** üstü çizili fiyat 60 günde hiç görülmediyse uyarılır; gerçek oran görülen en yüksek fiyata göre yazılır.
- **Şüpheli düşüş:** 30 günün en düşüğünden %70+ aşağıdaki fiyatlar "kontrol et" olarak işaretlenir, doğrulanmadan Kaçırılmaz'a girmez.
- **Sepette indirim:** ürün sayfasındaki "Sepette %X / Sepette N TL" etiketi okunur, ödenecek yaklaşık tutar gösterilir.
- **Barkod (GTIN/EAN) eşleştirme:** mağaza sayfasından okunan barkodla aynı ürün farklı mağazalarda eşleştirilir.
- **Bayat veri uyarısı:** mağaza fiyatı 1,5 saatten eski okunduysa kartta belirtilir.

## Daha çok ürün

- **Kendi ürününü ekle:** Takibim sekmesine mağaza linki yapıştır; fiyat her yenilemede mağazadan okunur, alarm kurulabilir.
  Telefondan hızlı ekleme: `http://BILGISAYAR-IP:8765/ekle?url=LİNK` (bkz. `KURULUM.txt` D bölümü).
- **Takip kelimeleri kanallarda otomatik aranır:** her yenilemede sırayla 3 kelime kanal arşivinde aranır.
- Kanal sınırı 25'e çıkarıldı; geriye doğru tarama ve 90 günlük arama arşivi sürüyor.

## 2.9

- **8 yeni Telegram kanalı** (toplam 14): firsatdolu, indirimalarmi_tr, amazonindirimtr, ucuzkenaldim, firsatzade,
  gunlukfirsatbulteni, uygunfiyatdedektifi, onual_ekstra. Varsayılan listeyi hiç değiştirmediyseniz otomatik eklenir.
- **Öne çıkanlar eşiği %25 → %15.**
- **Akakçe "Fark Atan Fiyatlar"** (ilk 3 sayfa): en ucuz satıcının diğerlerinden belirgin ucuz olduğu ürünler.
  Kartta "⚖️ Akakçe: diğer satıcılardan %X ucuz" yazar; bu oran **fiyat düşüşü değil, satıcılar arası farktır**.
  Bu ürünler için bildirim gönderilmez. Ayarlar'dan kapatılabilir.

## 3.0

- **5 kanal daha** (toplam 19): indirimseri, amazonozel, firsatlarco, umutlufirsatlar, yurticifirsat.
- **Akakçe listeleri:** "Fark Atan Fiyatlar", "Son 6 Ayın En Ucuzu" (kartta *Dip fiyat* rozeti; Öne çıkanlar'a girer)
  ve "Broşürlerdeki Daha Ucuz Fiyatlar". Bir liste okunamazsa diğerleri çalışmaya devam eder.
- **DonanımHaber Sıcak Fırsatlar forumu:** başlığında fiyat geçen konular; kart forum konusuna gider.
- **Oyun indirimleri (CheapShark):** Steam, Epic ve GOG'da %50+ indirimler, "Kupon & kampanya" sekmesinde.
  Fiyatlar ABD mağaza fiyatıdır (Türkiye fiyatı farklı olabilir); oran aynı mağazanın kendi fiyatına göredir.
- Dış kaynaklar için bildirim gönderilmez; her biri Ayarlar'dan ayrı ayrı kapatılabilir. Durumları GitHub
  Actions günlüğünde "kaynak ·" satırlarında görünür.

## 3.1 — İki aşamalı doğrulama hattı

**Aşama 1 · Toplayıcı:** Telegram kanalları, DonanımHaber (önce RSS, olmazsa forum sayfası), Akakçe listeleri ve
oyun indirimleri okunur; mağaza bağlantılı her yeni ürün **doğrulama kuyruğuna** yazılır (`veri.json` → `kuyruk`).

**Aşama 2 · Doğrulayıcı:** Kuyruktaki ürünün mağaza sayfası açılır; ad, anlık fiyat, görsel, barkod ve stok teyit edilir.

| Sonuç | Ne olur |
|---|---|
| ✔ Onaylı | Fiyat paylaşılanla uyuşuyor; kartta “Mağazada doğrulandı” yazar, Akakçe’deki en ucuz fiyatla karşılaştırılır |
| Koşullu | Fiyat yalnızca sepette/kodla oluşuyor; kart kalır, uyarı gösterilir |
| ✖ Elendi | Fiyat yükselmiş ya da stok bitmiş → listeden çıkar (Ayarlar’dan kapatılabilir) |
| Okunamadı | Mağaza izin vermedi / sayfa açılmadı → kart kalır, güven tazelik ve kanal sayısından hesaplanır |

Onaylı ürünler 3 saatte bir yeniden kontrol edilir. Geçici ağ hatası 20 ve 40 dakika sonra tekrar denenir.

**Mağaza okuma:** Sayfanın görünen HTML’i yerine içine gömülü ürün verisi okunur (`__NEXT_DATA__`, `window.__…STATE__`,
`JSON.parse(...)`, JSON-LD). Okuma sırası: `curl_cffi` (kuruluysa; gerçek Chrome imzası) → standart kütüphane
(Android Chrome başlıkları, çerez, gzip) → başsız tarayıcı (yalnızca bilgisayarda, `KF_TARAYICI=1` ve Playwright kuruluysa).
Bot koruması **aşılmaya çalışılmaz**: CAPTCHA çözülmez, vekil sunucu kullanılmaz; engelleyen mağaza 6 saat denenmez
(kayıt veri dosyasında tutulur).

**Önemli:** GitHub Actions sunucuları veri merkezi IP’si kullandığı için bazı mağazalar (özellikle Hepsiburada) orada
başlıklardan bağımsız olarak engelleyebilir. Doğrulama en iyi sonucu **bilgisayardaki kurulumda** (ev internetiyle) verir.

Ortam değişkenleri: `KF_DOGRULAMA_SN` (sitede doğrulamaya ayrılan süre, varsayılan 300), `KF_DH_RSS` (başka RSS adresi),
`KF_CFFI_PROFIL` (curl_cffi taklit profili, varsayılan `chrome`), `KF_TARAYICI=1` (tarayıcı yedeği).

Testler: `python -m unittest discover -s tests -v`

## 3.2 — Herkese açık site ve uygulama (Teyitli Fırsat)

Bilgisayardaki program doğruladığı fırsatları 10 dakikada bir herkese açık siteye gönderir (`yayin.py`, şablon `site/`).
Site GitHub Pages'te ücretsiz yayınlanır ve telefona uygulama olarak (PWA) kurulabilir.

- **Seçim:** yalnızca mağazada son 6 saat içinde teyit edilmiş, en az %10 indirimli ya da dip fiyattaki ürünler.
  Akakçe'de %15'ten fazla ucuzu olan ürün yayına girmez.
- **İçerik:** kart mağaza verisinden ve kendi metnimizle oluşturulur. Telegram kanallarının metni ve adı kullanılmaz.
- **Link:** başka kanalların ortaklık ve izleme kodları temizlenir; ortaklık hesabı tanımlanınca bizim kodumuz eklenir.
- **Yükleme:** değişen dosyalar tek commit'le gönderilir (Git Data API); liste değişmemişse yükleme yapılmaz.
- **Telegram:** isteğe bağlı; yeni fırsatlar kanala otomatik paylaşılır.

Kurulum: `YAYIN-KURULUM.txt` → `Yayin-Kur.bat`. Komutlar: `--yayin-kur`, `--yayin-onizle KLASOR`, `--yayin-simdi`.
