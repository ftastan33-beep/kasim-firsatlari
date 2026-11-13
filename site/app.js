"use strict";
// {{SITE_ADI}} — istemci. Veri: veri.json (bilgisayardaki doğrulayıcı 10 dakikada bir günceller).
const ESKI_TEYIT_SN = 12 * 3600;   // teyidi bundan eski ürün gösterilmez
const YENILE_MS = 3 * 60 * 1000;
const SVG = "http://www.w3.org/2000/svg";

const $ = (s, k = document) => k.querySelector(s);
const $$ = (s, k = document) => Array.from(k.querySelectorAll(s));
const durum = { urunler: [], guncelleme: 0, ara: "", kategori: "", magaza: "", sirala: "gercek", sadece: false, sonZiyaret: 0 };
const KATALOG_SAYFA = 30;          // katalog sonuçları bu kadar bu kadar gösterilir
// Katalog: mağazaların ürün listelerinden (feed) gelen ürünler. Yalnızca arama yapılınca indirilir.
const katalog = { ozet: null, urunler: null, yukleniyor: false, yok: false, limit: KATALOG_SAYFA };

const KARAR = {
  dip: { ad: "Gerçek indirim", sinif: "k-dip", sira: 0 },
  iyi: { ad: "İyi fiyat", sinif: "k-iyi", sira: 1 },
  yok: { ad: "Geçmiş az", sinif: "k-yok", sira: 2 },
  sisirilmis: { ad: "Şişirilmiş indirim", sinif: "k-sisirilmis", sira: 3 },
};
const karar = (u) => KARAR[u.karar] || KARAR.yok;
const gercekMi = (u) => u.karar === "dip" || u.karar === "iyi";

function sakla(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
function oku(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }

const tamFmt = new Intl.NumberFormat("tr-TR", { maximumFractionDigits: 0 });
const kurusFmt = new Intl.NumberFormat("tr-TR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const tl = (x) => (Number.isInteger(x) ? tamFmt : kurusFmt).format(x) + " TL";
const tarih = (ts) => new Date(ts * 1000).toLocaleDateString("tr-TR", { day: "numeric", month: "short" });
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
function el(etiket, sinif, metin) {
  const e = document.createElement(etiket);
  if (sinif) e.className = sinif;
  if (metin != null) e.textContent = metin;
  return e;
}

// ---------------------------------------------------------------- veri
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
  hashAc();
}

function durumYaz() {
  const e = $("#durum");
  const yas = Date.now() / 1000 - durum.guncelleme;
  const gercek = durum.urunler.filter(gercekMi).length;
  e.classList.toggle("uyari", yas > 2 * 3600);
  e.textContent = yas > 2 * 3600
    ? `Liste ${once(durum.guncelleme)} güncellendi; fiyatlar değişmiş olabilir, mağazada kontrol edin.`
    : `${durum.urunler.length} teyitli fırsat · ${gercek} gerçek indirim · son kontrol ${once(durum.guncelleme)}`;
}

// ---------------------------------------------------------------- grafik
function seri(u) {
  return (u.gecmis || []).map(([t, p]) => [t, p]).concat([[u.teyit, u.fiyat]]);
}
function grafikSvg(noktalar, g, y, secenek = {}) {
  const pad = secenek.pad ?? 6;
  const fiyatlar = noktalar.map((n) => n[1]).concat(secenek.normal ? [secenek.normal] : []);
  const lo = Math.min(...fiyatlar), hi = Math.max(...fiyatlar), aralik = hi - lo || 1;
  const t0 = noktalar[0][0], t1 = noktalar[noktalar.length - 1][0], tAralik = t1 - t0 || 1;
  const xy = (t, p) => [((t - t0) / tAralik) * g, pad + (1 - (p - lo) / aralik) * (y - 2 * pad)];
  const s = document.createElementNS(SVG, "svg");
  s.setAttribute("viewBox", `0 0 ${g} ${y}`);
  s.setAttribute("preserveAspectRatio", "none");
  s.setAttribute("aria-hidden", "true");
  const cizgi = noktalar.map(([t, p]) => xy(t, p).map((v) => v.toFixed(1)).join(",")).join(" ");
  if (secenek.alan) {
    const a = document.createElementNS(SVG, "polygon");
    a.setAttribute("points", `0,${y} ${cizgi} ${g},${y}`);
    a.setAttribute("class", "alan-dolgu");
    s.append(a);
  }
  if (secenek.normal) {
    const yy = xy(t0, secenek.normal)[1].toFixed(1);
    const l = document.createElementNS(SVG, "line");
    l.setAttribute("x1", "0"); l.setAttribute("x2", String(g)); l.setAttribute("y1", yy); l.setAttribute("y2", yy);
    l.setAttribute("class", "normal-cizgi");
    s.append(l);
  }
  const pl = document.createElementNS(SVG, "polyline");
  pl.setAttribute("points", cizgi);
  pl.setAttribute("class", "cizgi");
  s.append(pl);
  return s;
}
function buyukGrafik(kutu, u) {
  kutu.textContent = "";
  const n = seri(u);
  if (n.length < 2) {
    kutu.append(el("div", "grafik-yok", "Bu ürün için henüz fiyat geçmişi yok. Kayıtlar biriktikçe grafik burada görünecek."));
    return;
  }
  const s = grafikSvg(n, 800, 240, { alan: true, normal: u.normal, pad: 16 });
  s.setAttribute("class", "grafik");
  s.removeAttribute("aria-hidden");
  s.setAttribute("role", "img");
  s.setAttribute("aria-label", `Fiyat grafiği: ${tarih(n[0][0])} ile bugün arası, en düşük ${tl(Math.min(...n.map((x) => x[1])))}, en yüksek ${tl(Math.max(...n.map((x) => x[1])))}`);
  kutu.append(s);
  const fiyatlar = n.map((x) => x[1]);
  kutu.append(el("span", "grafik-etiket ust-e", "En yüksek " + tl(Math.max(...fiyatlar))));
  kutu.append(el("span", "grafik-etiket alt-e", "En düşük " + tl(Math.min(...fiyatlar))));
  const t = el("div", "grafik-tarih");
  t.append(el("span", null, tarih(n[0][0])), el("span", null, u.normal ? `Kesikli çizgi: normal fiyat ${tl(u.normal)}` : ""), el("span", null, "Bugün"));
  kutu.after(t);
}

// ---------------------------------------------------------------- kartlar
function ozetSatiri(p, u) {
  p.textContent = "";
  if (u.gercek_yuzde != null) {
    p.append("Gerçek düşüş: ", el("b", null, u.gercek_yuzde > 0 ? "%" + u.gercek_yuzde : "yok"));
    if (u.en_dusuk != null && u.gun) p.append(u.fiyat <= u.en_dusuk ? ` · önceki en düşük ${tl(u.en_dusuk)}` : ` · ${u.gun} günde en düşük ${tl(u.en_dusuk)}`);
  } else if (u.etiket) p.append(u.etiket);
  else if (u.karar == null) p.append("Fiyat mağazada teyit edildi; geçmiş birikiyor.");
}
function notlar(ul, u, etiketiYaz) {
  ul.textContent = "";
  const ekle = (m, uyari) => { const li = el("li", uyari ? "uyari" : null, m); ul.append(li); };
  if (etiketiYaz && u.etiket) ekle(u.etiket);
  if (u.dip && !u.karar) ekle(u.dip);
  if (u.akakce) ekle(u.akakce.fark <= 1 ? "Akakçe'deki en ucuz fiyat seviyesinde" : `Akakçe'de en ucuzu ${tl(u.akakce.en_ucuz)}`);
  if (u.kosul) ekle(u.kosul, true);
}
function gorselDoldur(a, u) {
  let img = $("img", a);
  if (!img) { img = el("img"); img.alt = ""; img.referrerPolicy = "no-referrer"; a.prepend(img); }
  a.classList.remove("yok");
  if (u.resim) {
    img.src = u.resim;
    img.onerror = () => { img.remove(); a.classList.add("yok"); };
  } else { img.remove(); a.classList.add("yok"); }
}

function kart(u) {
  const k = $("#kart-sablon").content.firstElementChild.cloneNode(true);
  const kr = karar(u);
  $$("a", k).forEach((a) => (a.href = u.link));
  const g = $(".gorsel", k);
  g.setAttribute("aria-label", u.baslik);
  gorselDoldur(g, u);
  if (u.yuzde) { const o = $(".oran-rozet", k); o.hidden = false; o.textContent = "%" + u.yuzde; }
  $(".magaza", k).textContent = u.magaza_ad;
  $(".zaman", k).textContent = u.ilk ? once(u.ilk) : "";
  $(".yeni", k).hidden = !(durum.sonZiyaret && u.ilk > durum.sonZiyaret);
  $(".baslik a", k).textContent = u.baslik;
  $(".fiyat", k).textContent = tl(u.fiyat);
  $(".eski", k).textContent = u.eski_fiyat ? tl(u.eski_fiyat) : "";
  const r = $(".rozet", k);
  r.textContent = kr.ad; r.classList.add(kr.sinif);
  if (u.etiket) r.title = u.etiket;
  const mini = $(".mini", k);
  const n = seri(u);
  if (n.length >= 2) { mini.classList.add(kr.sinif); mini.append(grafikSvg(n, 84, 28, { pad: 3 })); }
  ozetSatiri($(".ozet-satir", k), u);
  notlar($(".notlar", k), u, false);
  $(".kart-alt button", k).addEventListener("click", () => detayAc(u.id));
  return k;
}

// ---------------------------------------------------------------- katalog
async function katalogYukle() {
  if (katalog.urunler || katalog.yukleniyor || katalog.yok) return;
  katalog.yukleniyor = true;
  ciz();
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
      ortaklik: !!k[7], dipMi: !!k[8], dusus: k[9] || 0, s: sade(k[1] + " " + k[2]),
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
  const k = kart({ ...u, kosul: null, akakce: null, ilk: 0, teyit: 0, karar: undefined,
    dip: u.dipMi ? "İzlediğimiz dönemin en düşük fiyatı" : (u.dusus >= 3 ? `Son günlerde %${u.dusus} ucuzladı` : null) });
  k.classList.add("katalog");
  k.querySelector(".yeni").hidden = true;
  const r = k.querySelector(".rozet");
  r.className = "rozet k-yok"; r.textContent = "Katalog";
  r.title = "Bu fiyat mağazanın ürün listesinden alındı; mağaza sayfasında tek tek kontrol edilmedi.";
  k.querySelector(".ozet-satir").textContent = "Mağazanın bildirdiği fiyat; tek tek teyit edilmedi.";
  k.querySelector(".kart-alt button").remove();
  return k;
}

// Arama sonuçlarını katalogdan tamamlar. firsatLinkleri: yukarıdaki doğrulanmış listede zaten gösterilenler.
function katalogCiz(q, firsatLinkleri) {
  const bolum = $("#katalog-bolum");
  if (!q.length || (!katalog.urunler && !katalog.yukleniyor)) { bolum.hidden = true; return 0; }
  const liste = $("#katalog"), daha = $("#katalog-daha");
  liste.textContent = "";
  bolum.hidden = false;
  if (!katalog.urunler) {
    $("#katalog-baslik").textContent = "Katalogda aranıyor…";
    $("#katalog-not").textContent = "";
    daha.hidden = true;
    return 0;
  }
  const l = katalog.urunler.filter((u) => q.every((p) => u.s.includes(p)) && !firsatLinkleri.has(u.link) &&
    (!durum.magaza || sade(u.magaza_ad) === sade(magazaAdi(durum.magaza))));
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
function magazaAdi(kod) {
  const u = durum.urunler.find((x) => x.magaza === kod);
  return u ? u.magaza_ad : kod;
}

function siralayici() {
  const g = (u) => (u.gercek_yuzde == null ? -1000 : u.gercek_yuzde);
  if (durum.sirala === "indirim") return (a, b) => (b.yuzde || 0) - (a.yuzde || 0) || b.ilk - a.ilk;
  if (durum.sirala === "ucuz") return (a, b) => a.fiyat - b.fiyat;
  if (durum.sirala === "yeni") return (a, b) => b.ilk - a.ilk;
  return (a, b) => karar(a).sira - karar(b).sira || g(b) - g(a) || (b.yuzde || 0) - (a.yuzde || 0) || b.ilk - a.ilk;
}

function filtreler() {
  // kategori çipleri
  const say = {};
  durum.urunler.forEach((u) => { const k = u.kategori || "diger"; say[k] = say[k] || { ad: u.kategori_ad || "Diğer", n: 0 }; say[k].n++; });
  if (durum.kategori && !say[durum.kategori]) durum.kategori = "";
  const kutu = $("#kategoriler");
  kutu.textContent = "";
  const cip = (kod, ad, n) => {
    const b = el("button", "cip", ad);
    b.type = "button";
    b.setAttribute("aria-pressed", String(durum.kategori === kod));
    b.append(el("span", null, String(n)));
    b.onclick = () => { durum.kategori = kod; ciz(); };
    kutu.append(b);
  };
  cip("", "Tümü", durum.urunler.length);
  Object.entries(say).sort((a, b) => b[1].n - a[1].n).forEach(([k, v]) => cip(k, v.ad, v.n));
  kutu.hidden = Object.keys(say).length < 2;
  // mağaza listesi
  const m = {};
  durum.urunler.forEach((u) => { m[u.magaza] = m[u.magaza] || { ad: u.magaza_ad, n: 0 }; m[u.magaza].n++; });
  const sec = $("#magaza");
  const onceki = durum.magaza;
  sec.length = 1;
  Object.entries(m).sort((a, b) => b[1].n - a[1].n).forEach(([k, v]) => sec.add(new Option(`${v.ad} (${v.n})`, k)));
  durum.magaza = m[onceki] ? onceki : "";
  sec.value = durum.magaza;
}

function oneCikan() {
  const aday = durum.urunler.filter((u) => seri(u).length >= 3 && gercekMi(u));
  aday.sort(siralayiciGercek);
  const u = aday[0];
  const kutu = $("#one");
  kutu.hidden = !u;
  if (!u) return;
  const kr = karar(u);
  const r = $("[data-karar]", kutu);
  r.className = "rozet " + kr.sinif; r.textContent = kr.ad;
  $("[data-magaza]", kutu).textContent = `${u.magaza_ad} · ${once(u.ilk)}`;
  $("[data-baslik]", kutu).textContent = u.baslik;
  $("[data-fiyat]", kutu).textContent = tl(u.fiyat);
  $("[data-eski]", kutu).textContent = u.eski_fiyat ? tl(u.eski_fiyat) : "";
  const gk = $("[data-grafik]", kutu);
  gk.textContent = "";
  const s = grafikSvg(seri(u), 400, 120, { alan: true, normal: u.normal, pad: 10 });
  s.setAttribute("class", "grafik");
  gk.append(s);
  $("[data-magaza-oran]", kutu).textContent = u.yuzde ? "%" + u.yuzde : "—";
  $("[data-gercek]", kutu).textContent = u.gercek_yuzde != null ? (u.gercek_yuzde > 0 ? "%" + u.gercek_yuzde : "yok") : "—";
  $("[data-normal]", kutu).textContent = u.normal ? tl(u.normal) : "—";
  $("[data-detay]", kutu).onclick = () => detayAc(u.id);
}
function siralayiciGercek(a, b) {
  return karar(a).sira - karar(b).sira || (b.gercek_yuzde || 0) - (a.gercek_yuzde || 0) || b.ilk - a.ilk;
}

function ciz() {
  filtreler();
  oneCikan();
  const q = sade(durum.ara).split(/\s+/).filter(Boolean);
  const l = durum.urunler.filter((u) =>
    (!durum.kategori || (u.kategori || "diger") === durum.kategori) &&
    (!durum.magaza || u.magaza === durum.magaza) &&
    (!durum.sadece || gercekMi(u)) &&
    q.every((p) => sade(u.baslik + " " + u.magaza_ad + " " + (u.kategori_ad || "")).includes(p))
  ).sort(siralayici());
  const liste = $("#liste");
  liste.textContent = "";
  const parca = document.createDocumentFragment();
  l.forEach((u) => parca.append(kart(u)));
  liste.append(parca);
  const katalogSayisi = katalogCiz(q, new Set(l.map((u) => u.link)));
  const bos = $("#bos");
  bos.hidden = l.length > 0 || !durum.guncelleme;
  const [bb, bm] = [$("b", bos), $("span", bos)];
  if (q.length) {
    bb.textContent = "Teyitli fırsatlarda bulunamadı";
    bm.textContent = katalog.yukleniyor ? "Katalogda aranıyor…"
      : katalogSayisi ? "Bu ürün için şu an teyitli bir fırsat yok. Aşağıda katalogdaki güncel fiyatları görebilirsin."
      : "Daha kısa yazmayı (ör. yalnızca marka ya da model) dene.";
  } else {
    bb.textContent = "Bu filtrelerle fırsat yok";
    bm.textContent = "Kategoriyi ya da mağazayı değiştir veya “Sadece gerçek indirimler” seçimini kaldır.";
  }
  $("#sayac").textContent = durum.guncelleme
    ? `${l.length} fırsat · fiyatlar son 90 günle karşılaştırıldı` : "";
}

// ---------------------------------------------------------------- ayrıntı
const detay = $("#detay");
function kararMetni(u) {
  const g = u.gercek_yuzde, gun = u.gun;
  if (u.karar === "dip") {
    let m = `Bu fiyat son ${gun} günün en düşüğü.`;
    if (u.normal) m += ` Ürün bu dönemde çoğunlukla ${tl(u.normal)} civarında satıldı; gerçek düşüş %${g}.`;
    if (u.yuzde && g != null && u.yuzde - g >= 5) m += ` Mağazanın gösterdiği oran (%${u.yuzde}) bundan yüksek.`;
    return m;
  }
  if (u.karar === "iyi") {
    let m = `Fiyat, ürünün son ${gun} günde çoğunlukla satıldığı ${tl(u.normal)} seviyesinin %${g} altında.`;
    if (u.en_dusuk != null && u.en_dusuk < u.fiyat) m += ` Bu dönemde ${tl(u.en_dusuk)} fiyatı da görüldü.`;
    return m;
  }
  if (u.karar === "sisirilmis") {
    if (u.normal) {
      return `Mağaza ${u.yuzde ? "%" + u.yuzde + " " : ""}indirim gösteriyor ama ürün son ${gun} günde çoğunlukla ${tl(u.normal)} civarında satıldı. ` +
        (g > 0 ? `Gerçek düşüş yalnızca %${g}.` : "Fiyat her zamanki seviyesinde.");
    }
    return "Üstü çizili fiyat kayıtlarımızda hiç görülmedi; indirim oranı olduğundan yüksek gösteriliyor olabilir.";
  }
  return "Bu ürünün fiyat geçmişi henüz yeterli değil. Fiyat ve stok mağazada teyit edildi; indirimin gerçekliği kayıtlar biriktikçe netleşir.";
}
function detayAc(id, hashGuncelle = true) {
  const u = durum.urunler.find((x) => x.id === id);
  if (!u) return;
  const kr = karar(u);
  gorselDoldur($("[data-gorsel]", detay), u);
  $("[data-magaza]", detay).textContent = `${u.magaza_ad} · ${u.kategori_ad || ""}`.replace(/ · $/, "");
  $("[data-baslik]", detay).textContent = u.baslik;
  $("[data-fiyat]", detay).textContent = tl(u.fiyat);
  $("[data-eski]", detay).textContent = u.eski_fiyat ? tl(u.eski_fiyat) : "";
  $("[data-oran]", detay).textContent = u.yuzde ? "%" + u.yuzde : "";
  const kk = $("[data-karar-kutu]", detay);
  kk.className = "karar-kutu " + kr.sinif;
  kk.textContent = "";
  kk.append(el("span", "rozet " + kr.sinif, kr.ad), el("span", null, kararMetni(u)));
  $("[data-donem]", detay).textContent = u.gun ? `· son ${u.gun} gün` : "";
  const gk = $("[data-grafik]", detay);
  if (gk.nextElementSibling && gk.nextElementSibling.classList.contains("grafik-tarih")) gk.nextElementSibling.remove();
  buyukGrafik(gk, u);
  $("[data-simdi]", detay).textContent = tl(u.fiyat);
  $("[data-en-dusuk]", detay).textContent = u.en_dusuk != null ? tl(Math.min(u.en_dusuk, u.fiyat)) : "—";
  $("[data-normal]", detay).textContent = u.normal ? tl(u.normal) : "—";
  $("[data-en-yuksek]", detay).textContent = u.en_yuksek != null ? tl(Math.max(u.en_yuksek, u.fiyat)) : "—";
  notlar($("[data-notlar]", detay), u, false);
  const t = $("[data-teyit]", detay);
  t.textContent = "✔ " + once(u.teyit) + " mağazada teyit edildi";
  t.title = "Mağaza sayfasında kontrol: " + new Date(u.teyit * 1000).toLocaleString("tr-TR");
  $("[data-git]", detay).href = u.link;
  if (!detay.open) { if (detay.showModal) detay.showModal(); else detay.setAttribute("open", ""); }
  if (hashGuncelle && location.hash !== "#u-" + id) history.pushState(null, "", "#u-" + id);
}
function detayKapat() {
  if (detay.open) detay.close();
}
detay.addEventListener("close", () => {
  if (location.hash.startsWith("#u-")) history.replaceState(null, "", location.pathname + location.search);
});
detay.addEventListener("click", (e) => { if (e.target === detay) detayKapat(); });
$("[data-kapat]", detay).addEventListener("click", detayKapat);
function hashAc() {
  const h = location.hash;
  if (h.startsWith("#u-")) detayAc(h.slice(3), false);
  else if (detay.open) detay.close();
}
window.addEventListener("hashchange", hashAc);
window.addEventListener("popstate", hashAc);

// ---------------------------------------------------------------- kontroller
let araZaman;
$$("[data-ara]").forEach((g) => g.addEventListener("input", (e) => {
  $$("[data-ara]").forEach((d) => { if (d !== e.target) d.value = e.target.value; });
  clearTimeout(araZaman);
  araZaman = setTimeout(() => { aramaYap(e.target.value); }, 150);
}));
function aramaYap(metin) {
  durum.ara = metin;
  katalog.limit = KATALOG_SAYFA;
  if (sade(metin).trim().length >= 2) katalogYukle();
  ciz();
}
$("#katalog-daha").addEventListener("click", () => { katalog.limit += KATALOG_SAYFA; ciz(); });
$("#ara-form").addEventListener("submit", (e) => {
  e.preventDefault();
  aramaYap($("#ara-ust").value);
  $("#firsatlar").scrollIntoView();
});
$("#sirala").addEventListener("change", (e) => { durum.sirala = e.target.value; sakla("sirala", durum.sirala); ciz(); });
$("#magaza").addEventListener("change", (e) => { durum.magaza = e.target.value; ciz(); });
$("#sadece").addEventListener("change", (e) => { durum.sadece = e.target.checked; sakla("sadece", durum.sadece ? "1" : ""); ciz(); });
durum.sirala = ["gercek", "indirim", "yeni", "ucuz"].includes(oku("sirala")) ? oku("sirala") : "gercek";
$("#sirala").value = durum.sirala;
durum.sadece = oku("sadece") === "1";
$("#sadece").checked = durum.sadece;

// "Yeni" rozeti: son ziyaretten sonra eklenenler
durum.sonZiyaret = Number(oku("son_ziyaret")) || 0;
window.addEventListener("pagehide", () => sakla("son_ziyaret", String(Math.floor(Date.now() / 1000))));

// Telegram (kanal ayarlıysa)
if ($("#tg").getAttribute("href").length > "https://t.me/".length) { $("#tg").hidden = false; $("#tg-bant").hidden = false; }

// ---------------------------------------------------------------- uygulama olarak yükleme (PWA)
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
