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
