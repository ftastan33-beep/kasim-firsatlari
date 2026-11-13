// {{SITE_ADI}} service worker — sürüm {{SURUM}}
const ONBELLEK = "kabuk-{{SURUM}}";
const KABUK = ["./", "index.html", "stil.css?v={{SURUM}}", "app.js?v={{SURUM}}", "manifest.webmanifest",
  "ikon.svg", "ikon-192.png", "ikon-512.png", "yasal.html"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(ONBELLEK).then((c) => c.addAll(KABUK)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((k) => Promise.all(k.filter((x) => x !== ONBELLEK).map((x) => caches.delete(x))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;   // mağaza görselleri tarayıcıya kalsın
  if (url.pathname.endsWith("/veri.json")) {
    // Veri: önce ağ, çevrimdışıysa son kayıtlı liste
    e.respondWith(fetch(e.request).then((r) => {
      const kopya = r.clone();
      caches.open(ONBELLEK).then((c) => c.put("veri.json", kopya));
      return r;
    }).catch(() => caches.match("veri.json")));
    return;
  }
  // Sayfanın kendisi: önce ağ (güncelleme ilk açılışta görünsün), ağ yoksa ya da 4 sn'de cevap gelmezse kayıtlı kopya
  if (e.request.mode === "navigate" || url.pathname.endsWith("/") || url.pathname.endsWith(".html")) {
    e.respondWith(sayfaAl(e.request));
    return;
  }
  // stil.css?v=… ve app.js?v=… sürüm numaralı: kayıtlı kopya yeterli
  e.respondWith(caches.match(e.request).then((r) => r || fetch(e.request)));
});

function sayfaAl(istek) {
  const ag = fetch(istek, { cache: "no-store" }).then((r) => {
    if (r.ok) { const kopya = r.clone(); caches.open(ONBELLEK).then((c) => c.put(istek, kopya)); }
    return r;
  });
  const yedek = () => caches.match(istek, { ignoreSearch: true }).then((r) => r || caches.match("index.html"));
  const sure = new Promise((coz) => setTimeout(coz, 4000)).then(yedek);
  return Promise.race([ag.catch(yedek), sure.then((r) => r || ag)]);
}
