// Push notifications only. No fetch handler: API responses are private and must never be cached.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));
self.addEventListener("push", event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = {}; }
  const title = typeof data.title === "string" ? data.title.slice(0, 120) : "ImmoAlert";
  const url = typeof data.url === "string" && data.url.startsWith("/") ? data.url : "/";
  event.waitUntil(self.registration.showNotification(title, {body: typeof data.body === "string" ? data.body.slice(0, 300) : "", icon: "/favicon.svg", data: {url}, tag: data.tag || undefined}));
});
self.addEventListener("notificationclick", event => {
  event.notification.close();
  const url = event.notification.data && event.notification.data.url || "/";
  event.waitUntil(self.clients.matchAll({type: "window", includeUncontrolled: true}).then(list => {
    for (const client of list) if ("focus" in client) { client.navigate(url); return client.focus(); }
    return self.clients.openWindow(url);
  }));
});
