# Kasım Fırsatları

Telegram fırsat kanallarındaki indirimleri, kuponları ve fiyat geçmişini gösteren kişisel site.
GitHub Actions her 30 dakikada bir kanalları okur ve siteyi GitHub Pages'te günceller.

- Site: depo **Settings → Pages** bölümünde yazan adres
- Elle güncelleme: **Actions → Fırsatları güncelle → Run workflow**
- Telefona bildirim (isteğe bağlı): **Settings → Secrets and variables → Actions → New repository secret**,
  ad `NTFY_KONU`, değer ntfy uygulamasında abone olunan konu adı.

Bilgisayarda çalıştırmak için: `Kasim-Firsatlari-Baslat.bat` (ayrıntılar `KURULUM.txt`).
