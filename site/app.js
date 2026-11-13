"use strict";
// {{SITE_ADI}} — istemci. Veri: veri.json (bilgisayardaki doğrulayıcı 10 dakikada bir günceller).
const ESKI_TEYIT_SN = 12 * 3600;   // teyidi bundan eski ürün gösterilmez
const YENILE_MS = 3 * 60 * 1000;

const $ = (s) => document.querySelector(s);
const KATALOG_SAYFA = 30;          // katalog sonuçları bu kadar bu kadar gösterilir
const durum = { urunler: [], guncelleme: 0, magaza: "", ara: "", sirala: "yeni", sonZiyaret: 0 };
// Katalog: mağazaların ürün listelerinden gelen ürünler. Yalnızca arama yapılınca indirilir.
const katalog = { ozet: null, urunler: null, yukleniyor: false, yok: false, limit: KATALOG_SAYFA };

function sakla(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
function oku(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }

const tamFmt = new Intl.NumberFormat("tr-TR", { maximumFractionDigits: 0 });
const kurusFmt = new Intl.NumberFormat("tr-TR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const tl = (x) => (Number.isInteger(x) ? tamFmt : kurusFmt).format(x) + " TL";
function once(ts) {
  const sn = Math.max(0, Date.now() / 1000 - ts);
  if (sn < 90) return "az önce";
  if (sn < 3600) return Math.round(sn / 60) + " dk önce";
  if (sn < 86400) return Math.round(sn / 3600) + " sa önce";
  return Math.round(sn / 86400) + " gün önce";
}
function sade(s) {
  return (s || "").toLocaleLowerCase("tr-TR").normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/ı/g, "i");
}

async function veriAl() {
  try {
    const r = await fetch("veri.json?t=" + Date.now(), { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    const v = await r.json();
    durum.urunler = (v.urunler || []).filter((u) => Date.now() / 1000 - u.teyit < ESKI_TEYIT_SN);
    durum.guncelleme = v.guncelleme || 0;
    durumYaz();
  } catch (e) {
    if (!durum.urunler.length) $("#durum").textContent = "Fırsatlar şu an yüklenemedi. İnternet bağlantınızı kontrol edip sayfayı yenileyin.";
  }
  ciz();
}

function durumYaz() {
  const el = $("#durum");
  const yas = Date.now() / 1000 - durum.guncelleme;
  el.classList.toggle("uyari", yas > 2 * 3600);
  el.textContent = yas > 2 * 3600
    ? `Liste ${once(durum.guncelleme)} güncellendi; fiyatlar değişmiş olabilir, mağazada kontrol edin.`
    : `${durum.urunler.length} doğrulanmış fırsat · son kontrol ${once(durum.guncelleme)}`;
}

function magazaCipleri() {
  const say = {};
  durum.urunler.forEach((u) => { say[u.magaza] = say[u.magaza] || { ad: u.magaza_ad, n: 0 }; say[u.magaza].n++; });
  const kutu = $("#magazalar");
  kutu.textContent = "";
  const ekle = (kod, ad, n) => {
    const b = document.createElement("button");
    b.className = "cip"; b.type = "button";
    b.setAttribute("aria-pressed", String(durum.magaza === kod));
    b.textContent = ad;
    if (n != null) { const s = document.createElement("span"); s.textContent = n; b.append(s); }
    b.onclick = () => { durum.magaza = durum.magaza === kod ? "" : kod; ciz(); };
    kutu.append(b);
  };
  ekle("", "Tümü", durum.urunler.length);
  Object.entries(say).sort((a, b) => b[1].n - a[1].n).forEach(([k, v]) => ekle(k, v.ad, v.n));
}

function not(ul, ikon, metin, uyari) {
  const li = document.createElement("li");
  li.dataset.i = ikon; li.textContent = metin;
  if (uyari) li.className = "uyari";
  ul.append(li);
}

function kart(u) {
  const k = $("#kart-sablon").content.firstElementChild.cloneNode(true);
  k.querySelectorAll("a").forEach((a) => (a.href = u.link));
  const img = k.querySelector(".gorsel img");
  if (u.resim) { img.src = u.resim; img.onerror = () => { img.remove(); k.querySelector(".gorsel").classList.add("yok"); }; }
  else { img.remove(); k.querySelector(".gorsel").classList.add("yok"); }
  k.querySelector(".gorsel").setAttribute("aria-label", u.baslik);
  k.querySelector(".magaza").textContent = u.magaza_ad;
  k.querySelector(".yeni").hidden = !(durum.sonZiyaret && u.ilk > durum.sonZiyaret);
  k.querySelector(".baslik a").textContent = u.baslik;
  k.querySelector(".fiyat").textContent = tl(u.fiyat);
  const eski = k.querySelector(".eski"), oran = k.querySelector(".oran");
  if (u.eski_fiyat) { eski.textContent = tl(u.eski_fiyat); oran.textContent = "%" + u.yuzde; } else { eski.remove(); oran.remove(); }
  const ul = k.querySelector(".notlar");
  if (u.dip) not(ul, "📉", u.dip);
  if (u.akakce) not(ul, "⚖️", u.akakce.fark <= 1 ? "Akakçe'deki en ucuz fiyat seviyesinde" : `Akakçe'de en ucuzu ${tl(u.akakce.en_ucuz)}`);
  if (u.kosul) not(ul, "⚠️", u.kosul, true);
  if (!ul.children.length) ul.remove();
  const t = k.querySelector(".teyit");
  t.textContent = "✔ " + once(u.teyit) + " teyit edildi";
  t.title = "Mağaza sayfasında kontrol: " + new Date(u.teyit * 1000).toLocaleString("tr-TR");
  return k;
}

async function katalogYukle() {
  if (katalog.urunler || katalog.yukleniyor || katalog.yok) return;
  katalog.yukleniyor = true;
  katalogCiz();
  try {
    const r = await fetch("katalog/ozet.json?t=" + Date.now(), { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    const oz = await r.json();
    if (!oz.parca) throw new Error("boş");
    const parcalar = new Array(oz.parca);
    let sira = 0;
    const isci = async () => {
      while (sira < oz.parca) {
        const i = sira++;
        const p = await fetch("katalog/" + i + ".json?v=" + oz.guncelleme);
        if (!p.ok) throw new Error(p.status);
        parcalar[i] = await p.json();
      }
    };
    await Promise.all([isci(), isci(), isci(), isci()]);
    katalog.urunler = parcalar.flat().map((k) => ({
      id: k[0], baslik: k[1], magaza_ad: k[2], fiyat: k[3], eski_fiyat: k[4] || null, link: k[5], resim: k[6] || null,
      ortaklik: !!k[7], dip: !!k[8], dusus: k[9] || 0, s: sade(k[1] + " " + k[2]),
      yuzde: k[4] > k[3] * 1.01 ? Math.round((k[4] - k[3]) / k[4] * 100) : null,
    }));
    katalog.ozet = oz;
  } catch (e) {
    katalog.yok = true;                 // katalog yayınlanmamış ya da indirilemedi: site eskisi gibi çalışır
    setTimeout(() => { katalog.yok = false; }, 5 * 60 * 1000);
  }
  katalog.yukleniyor = false;
  ciz();
}

function katalogKart(u) {
  const k = kart({ ...u, kosul: null, akakce: null, ilk: 0, teyit: 0,
    dip: u.dip ? "İzlediğimiz dönemin en düşük fiyatı" : (u.dusus >= 3 ? `Son günlerde %${u.dusus} ucuzladı` : null) });
  k.classList.add("katalog");
  k.querySelector(".yeni").hidden = true;
  const t = k.querySelector(".teyit");
  t.textContent = "Mağazanın bildirdiği fiyat";
  t.title = "Bu fiyat mağazanın ürün listesinden alındı; mağaza sayfasında tek tek kontrol edilmedi.";
  return k;
}

// Arama sonuçlarını katalogdan tamamlar. firsatLinkleri: yukarıdaki doğrulanmış listede zaten gösterilenler.
function katalogCiz(q, firsatSayisi, firsatLinkleri) {
  const bolum = $("#katalog-bolum");
  if (!q || !q.length || (!katalog.urunler && !katalog.yukleniyor)) { bolum.hidden = true; return 0; }
  const liste = $("#katalog"), daha = $("#katalog-daha");
  liste.textContent = "";
  bolum.hidden = false;
  if (!katalog.urunler) {
    $("#katalog-baslik").textContent = "Katalogda aranıyor…";
    $("#katalog-not").textContent = "";
    daha.hidden = true;
    return 0;
  }
  let l = katalog.urunler.filter((u) => q.every((p) => u.s.includes(p)) && !firsatLinkleri.has(u.link));
  const tam = " " + q.join(" ") + " ";
  const puan = (u) => ((" " + u.s + " ").includes(tam) ? 0 : 2) + ((" " + u.s).includes(" " + q[0]) ? 0 : 1);
  if (durum.sirala === "indirim") l.sort((a, b) => (b.yuzde || 0) - (a.yuzde || 0) || a.fiyat - b.fiyat);
  else if (durum.sirala === "ucuz") l.sort((a, b) => a.fiyat - b.fiyat);
  else l.sort((a, b) => puan(a) - puan(b) || a.s.length - b.s.length || a.fiyat - b.fiyat);
  if (!l.length) { bolum.hidden = true; return 0; }
  $("#katalog-baslik").textContent = `Katalogda ${l.length} ürün`;
  $("#katalog-not").textContent = "Bu ürünler mağazaların bize ilettiği ürün listelerinden gelir (son güncelleme " +
    once(katalog.ozet.guncelleme) + "). Fiyat mağazanın bildirdiği fiyattır; yukarıdaki fırsatlar gibi tek tek teyit edilmemiştir.";
  const parca = document.createDocumentFragment();
  l.slice(0, katalog.limit).forEach((u) => parca.append(katalogKart(u)));
  liste.append(parca);
  daha.hidden = l.length <= katalog.limit;
  daha.textContent = `Daha fazla göster (${l.length - katalog.limit})`;
  return l.length;
}

function ciz() {
  magazaCipleri();
  const q = sade(durum.ara).split(/\s+/).filter(Boolean);
  let l = durum.urunler.filter((u) => (!durum.magaza || u.magaza === durum.magaza) &&
    q.every((p) => sade(u.baslik + " " + u.magaza_ad).includes(p)));
  if (durum.sirala === "indirim") l.sort((a, b) => (b.yuzde || 0) - (a.yuzde || 0) || b.ilk - a.ilk);
  else if (durum.sirala === "ucuz") l.sort((a, b) => a.fiyat - b.fiyat);
  else l.sort((a, b) => b.ilk - a.ilk);
  const liste = $("#liste");
  liste.textContent = "";
  const parca = document.createDocumentFragment();
  l.forEach((u) => parca.append(kart(u)));
  liste.append(parca);
  const katalogSayisi = katalogCiz(q, l.length, new Set(l.map((u) => u.link)));
  const bos = $("#bos");
  bos.hidden = l.length > 0 || !durum.guncelleme;
  if (!bos.hidden && q.length) {
    bos.textContent = katalog.yukleniyor ? "Doğrulanmış fırsatlarda yok; katalogda aranıyor…"
      : katalogSayisi ? "Bu ürün için şu an doğrulanmış bir fırsat yok. Aşağıda katalogdaki güncel fiyatları görebilirsiniz."
      : "Aradığınız ürün şu an listemizde yok. Daha kısa yazmayı (ör. yalnızca marka ya da model) deneyin.";
  } else if (!bos.hidden) bos.textContent = "Bu filtreye uyan fırsat yok. Filtreyi temizleyin ya da biraz sonra tekrar bakın.";
}

// --- arama / sıralama ---
let araZaman;
$("#ara").addEventListener("input", (e) => {
  clearTimeout(araZaman);
  araZaman = setTimeout(() => {
    durum.ara = e.target.value;
    katalog.limit = KATALOG_SAYFA;
    if (sade(durum.ara).trim().length >= 2) katalogYukle();
    ciz();
  }, 150);
});
$("#katalog-daha").addEventListener("click", () => { katalog.limit += KATALOG_SAYFA; ciz(); });
$("#sirala").addEventListener("change", (e) => { durum.sirala = e.target.value; sakla("sirala", durum.sirala); ciz(); });
durum.sirala = oku("sirala") || "yeni";
$("#sirala").value = durum.sirala;

// "Yeni" rozeti: son ziyaretten sonra eklenenler
durum.sonZiyaret = Number(oku("son_ziyaret")) || 0;
window.addEventListener("pagehide", () => sakla("son_ziyaret", String(Math.floor(Date.now() / 1000))));

// Telegram düğmesi (kanal ayarlıysa)
if ($("#tg").getAttribute("href").length > "https://t.me/".length) $("#tg").hidden = false;

// --- uygulama olarak yükleme (PWA) ---
let yuklemeOlayi = null;
window.addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); yuklemeOlayi = e; $("#yukle").hidden = false; });
$("#yukle").addEventListener("click", async () => {
  if (!yuklemeOlayi) return;
  yuklemeOlayi.prompt();
  await yuklemeOlayi.userChoice;
  yuklemeOlayi = null; $("#yukle").hidden = true;
});
const ios = /iphone|ipad|ipod/i.test(navigator.userAgent) && !navigator.standalone;
if (ios && !oku("ios_ipucu")) { $("#ios").hidden = false; sakla("ios_ipucu", "1"); }
if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});

veriAl();
setInterval(() => { if (!document.hidden) veriAl(); }, YENILE_MS);
document.addEventListener("visibilitychange", () => { if (!document.hidden) veriAl(); });
setInterval(durumYaz, 60 * 1000);
