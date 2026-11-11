"use strict";
// {{SITE_ADI}} — istemci. Veri: veri.json (bilgisayardaki doğrulayıcı 10 dakikada bir günceller).
const ESKI_TEYIT_SN = 12 * 3600;   // teyidi bundan eski ürün gösterilmez
const YENILE_MS = 3 * 60 * 1000;

const $ = (s) => document.querySelector(s);
const durum = { urunler: [], guncelleme: 0, magaza: "", ara: "", sirala: "yeni", sonZiyaret: 0 };

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
  $("#bos").hidden = l.length > 0 || !durum.guncelleme;
}

// --- arama / sıralama ---
let araZaman;
$("#ara").addEventListener("input", (e) => { clearTimeout(araZaman); araZaman = setTimeout(() => { durum.ara = e.target.value; ciz(); }, 150); });
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
