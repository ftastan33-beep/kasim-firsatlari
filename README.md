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

## 3.2.1 — Yanlış ürün eşleşmesi düzeltmesi

- **Model/varyant/hafıza ayrımı:** "Galaxy S26 FE 256 GB" artık "Galaxy S26", "S26 Ultra", "S25 FE" ya da
  "S26 FE 128 GB" ile aynı ürün sayılmaz (önceden başlık kelimelerinin çoğu ortak olduğu için eşleşiyordu).
  Akakçe karşılaştırması, fiyat geçmişi, alarm ve "aynı ürün başka mağazada" eşleşmeleri bu kuralı kullanır.
- **Bağlantı başka ürüne gidiyorsa:** doğrulayıcı ve "kontrol et" mağaza sayfasındaki ürün adını başlıkla karşılaştırır;
  farklı modelse kart "❌ Bağlantı başka bir ürüne gidiyor" olarak işaretlenir, o ürünün görseli/barkodu karta yazılmaz.
- Eski sürümün yanlış eşleştirdiği Akakçe kayıtları ve fiyat geçmişi girdileri otomatik ayıklanır.

## 3.3 — Market indirimleri ve ayrı kategoriler

**🛒 Market sekmesi:** BİM, A101, ŞOK, Migros, CarrefourSA, Tarım Kredi ve Hakmar şubelerindeki indirimli ürünler.
Kaynak: Market Fiyatı (TÜBİTAK, marketfiyati.org.tr) — marketlerin kendi bildirdiği şube fiyatları.

- **81 il:** listeden il seçilir (il merkezi çevresi) ya da “📍 Konumumu kullan” ile bulunduğunuz yer; yarıçap 3–20 km.
- Market kategorileri ayrıdır: meyve-sebze, et-tavuk-balık, süt-kahvaltılık, temel gıda, içecek, atıştırmalık, temizlik,
  kişisel bakım, diğer. Market zinciri filtresi, arama, en az indirim ve sıralama (indirim %, TL kazanç, fiyat, birim fiyat).
- Bu sekmenin ürünleri diğer sekmelere **karışmaz**; Telegram/Akakçe fırsatları da Market sekmesine girmez.
- Liste açıldığında canlı çekilir, 30 dakika önbellekte tutulur. Tarayıcı doğrudan ulaşamazsa program aracılık eder (`/api/market`).
- %70 ve üstü indirimler “Kontrol et” olarak işaretlenir. Şu an indirim bilgisini daha çok Migros, CarrefourSA ve BİM paylaşıyor.

**Kategoriler yeniden düzenlendi:** “Market & gıda” ve “Elektronik” gibi karışan gruplar ayrıldı:
Telefon-TV & elektronik · Bilgisayar, oyun & donanım · Beyaz eşya & ev aletleri · Ev, mutfak & mobilya · Gıda & içecek ·
Temizlik & kâğıt ürünleri · Kişisel bakım & kozmetik · Giyim · Anne-bebek & oyuncak · Spor · Yapı market & bahçe ·
Oto, motosiklet & scooter · Evcil hayvan · Dijital oyunlar. Örnek düzeltmeler: bulaşık makinesi tableti artık “Elektronik” değil
“Temizlik”; çay makineli su sebili “Gıda” değil “Beyaz eşya”; kağıt havlu “Ev” değil “Temizlik”; anakart, RAM, işlemci,
yazıcı “Diğer” değil “Bilgisayar”. Kategori her gösterimde yeniden hesaplandığı için eski kayıtlar da kendiliğinden düzelir.

### Gerçek indirim kontrolü (Market sekmesi)

Her ürün için Market Fiyatı'nın **son 90 günlük fiyat geçmişi** (aynı market) okunur; marketin üstü çizili fiyatı yerine
ürünün bu dönemde **çoğunlukla satıldığı fiyat** (medyan) esas alınır:

| Rozet | Anlamı |
|---|---|
| ✅ Son N günün en düşük fiyatı | Şu anki fiyat dönemin en düşüğü veya ona eşit |
| ✅ Gerçek indirim | Normal fiyatın en az %5 altında; daha önce daha ucuza düştüyse tarihiyle yazılır |
| ⚠️ Şişirilmiş indirim | Normal fiyattan %5'ten az ucuz; varsayılan olarak gizlenir (“Şişirilmiş indirimleri gizle”) |
| Üstü çizili fiyat görülmedi | Marketin eski fiyatı son 90 günde hiç uygulanmamış |

Büyük kutudaki oran kontrol bitince **gerçek oran** olur; marketin ilan ettiği oran ayrıca yazılır. Varsayılan sıralama
“En yüksek gerçek indirim”. Kontrol ekrandaki ürünler için yapılır (aynı anda 4 istek), sonuçlar oturum boyunca saklanır.

## 3.4 — Fiyat karşılaştırma ve yeni Market görünümü

- **⚖️ Fiyat karşılaştır:** Market sekmesinde ikinci bölüm. Ürün adını yaz (ör. "tiryaki çay 1 kg"); çevredeki BİM, A101, ŞOK,
  Migros, CarrefourSA, Tarım Kredi ve Hakmar fiyatları yan yana gelir. Her market için en ucuz şube, en ucuza göre
  "+%X" farkı, en ucuz ile en pahalı arasındaki TL farkı. Sıralama: akıllı, en düşük fiyat, birim fiyat, marketler arası en büyük fark.
- **İndirim kartlarında "Fiyatları karşılaştır":** indirimdeki ürünün başka marketlerde kaça satıldığını kartın içinde gösterir.
- **Görünüm** Market Fiyatı'ndan ilham aldı: ızgara kartlar, büyük ürün görseli, mavi ana düğme, marketlerin kendi
  renklerinde etiketler; telefonda iki sütun.
- **Market Fiyatı'nı yormama:** aynı anda en çok 2 istek, istekler arası 250 ms; fiyat geçmişi yalnızca ekrandaki ürünler için
  ve bu cihazda 12 saat saklanır. Bağlantı kesilir ya da çok sık istek uyarısı gelirse 2 dakika istek atılmaz.

## 3.5 — Fiyat ve kupon düzeltmeleri, takip araması

**Akakçe fiyat hatası düzeltildi.** Akakçe fiyatı kuruşunu ayrı etikette yazıyor (`27.599<i>,00 TL</i>`). Eski okuyucu bu
fiyatı okuyamayıp kartın köşesindeki **“3.400 TL fark”** rozetini ürün fiyatı sanıyordu (ör. 48.000 TL'lik telefon 4.840 TL,
3.599 TL'lik oyuncu koltuğu 800 TL görünüyordu). Artık fiyat yalnızca fiyat kutusundan okunur; fark rozeti ayrı okunur.
Satıcılar arası fark oranı da fark tutarından hesaplanır. Aynı hata Akakçe karşılaştırmasını da bozuyordu (arama sonuçlarında
fiyat bulunamadığı için hep “bulunamadı”); o da düzeldi. İlk açılışta eski sürümün kaydettiği hatalı Akakçe ürünleri,
fiyat geçmişi girdileri ve karşılaştırmalar bir kez silinir, sonraki yenilemede doğru fiyatla yeniden gelir.

**Kuponlar:**
- Büyük harfle yazılan `KOD:`, `KODU:`, `KUPON KODU:` ve “Bu kod uygulamada geçerlidir: APP150” gibi kalıplardaki kodlar artık okunur.
- “kod çalışmıyor”, “kupon bitti”, “limit doldu”, “tükenmiştir” gibi ifadeler paylaşımı **bitti** sayar.
- Bir kanal bir kodun bittiğini sonradan paylaşırsa, aynı kodu içeren eski paylaşımlar da (başka kanallarda olsa bile) gizlenir.
- Bitiş tarihi okunuyor: “12-14 Kasım”, “11.11'e özel”, “10/11 - 12/11 arası”.
- Bitiş tarihi yazılmayan kodlar 3 günden eskiyse “büyük olasılıkla geçersiz” sayılır ve “Süresi geçenleri gizle” açıkken gösterilmez.

**Takip kelimeleri:**
- Eş anlamlılar ve Türkçe ekler eşleşir: “oyuncu sandalyesi” = “oyuncu koltuğu / gaming chair”; “evrak çantası” = “evrak çanta / iş çantası”.
- Her takip kelimesi 3 saatte bir **Akakçe'de de aranır**; bulunan ürünlerin bugünkü en ucuz fiyatı Takibim sekmesinde görünür
  (bu kartlar indirim değil, güncel en ucuz fiyattır; “Tüm ürünler” sekmesine karışmaz).
- Varsayılan takip listesi: oyuncu sandalyesi, evrak çantası.

**Öne çıkanlar (3.5):**
- “3 al 2 öde”, “2 al 1 öde” gibi çoklu alım kampanyaları tek başına indirim sayılmaz; ürünün kendi fiyatı düşmemiştir.
- Mağazada fiyatı paylaşılandan yükselmiş ürünler ve “kontrol et” işaretli şüpheli fiyatlar Öne çıkanlar'a girmez.
- Programın fiyat kaydına göre indirim artık tek bir uç değere değil, kayıtların ortancasına göre hesaplanır. Az kayıtla
  %50+ çıkan oranlar (genelde farklı paket/varyant) indirim sayılmaz, “kontrol et” olarak işaretlenir.
- Başlığı yalnızca bağlantı (“🛒 https://…”) olan paylaşımlarda ürün adı doğru satırdan alınır.

## 3.6 — Ürün kataloğu (mağaza feed'leri)

Arama artık yalnızca fırsat paylaşımlarında değil, **mağazaların kendi verdiği ürün listelerinde** (feed) de yapılır.
Feed; mağazanın ya da satış ortaklığı ağının (ör. Admitad) hazırladığı XML/CSV dosyasıdır. Mağaza sayfaları taranmaz.

- **Ekleme:** Ayarlar → *Ürün kataloğu* → mağaza adı + feed adresi → *Feed ekle*. Komutla:
  `py kasim_firsat.py --feed-ekle ADRES --feed-ad "Mağaza"` (indirilmiş dosya yolu da olur). Ayrıntı: `KATALOG-KURULUM.txt`.
- **Biçimler:** Google Alışveriş XML, YML (Admitad), Türkiye'deki e-ticaret altyapılarının XML'leri, CSV; `.gz` / `.zip`.
- **Güncelleme:** 6 saatte bir. Okuma yarıda kesilir ya da feed boş gelirse katalog eski halinde kalır.
- **Fiyat geçmişi:** her fiyat değişikliği kaydedilir. En az 14 gündür izlenen ürün kayıtlı en düşük fiyatına inerse
  "izlenen dönemin en düşüğü" olarak işaretlenir. Mağazanın üstü çizili fiyatı "mağazanın beyanı" olarak gösterilir.
- **Programda:** arama sonuçlarının altında "Ürün kataloğunda" bölümü (fırsatlardan ayrı).
- **Herkese açık sitede:** arama yapılınca katalog indirilir ve "Katalogda N ürün" bölümünde gösterilir; kartta
  "Mağazanın bildirdiği fiyat" yazar (teyitli fırsatlardan ayrı). Varsayılan en fazla 10.000 ürün, günde bir yenilenir
  (`yayin.json` → `"katalog": {"en_fazla": 10000, "aralik_saat": 24}`; `en_fazla: 0` kataloğu siteye koymaz).
- **Ortaklık:** ağdan gelen feed'in linkleri zaten ortaklık linkidir (kutuyu işaretleyin). Mağazanın düz feed'inde
  linkler `yayin.json`daki `ortaklik` ayarıyla ya da `--feed-sablon "https://…{url_kodlu}"` ile çevrilir.
- Komutlar: `--feed-liste`, `--feed-sil KOD`, `--feed-guncelle`, `--katalog-ara "kelime"`.
