const LOCAL_RECEIVER = "http://127.0.0.1:8765/manifest";
const observed = new Set();

function isHotmartManifest(rawUrl) {
  try {
    const url = new URL(rawUrl);
    const hotmartHost =
      url.hostname === "hotmart.com" || url.hostname.endsWith(".hotmart.com");
    return (
      url.protocol === "https:" &&
      hotmartHost &&
      url.pathname.toLowerCase().endsWith(".m3u8")
    );
  } catch {
    return false;
  }
}

function manifestKey(rawUrl) {
  const url = new URL(rawUrl);
  return `${url.hostname}${url.pathname}`;
}

chrome.webRequest.onCompleted.addListener(
  (details) => {
    if (details.tabId === -1 || !isHotmartManifest(details.url)) return;

    const key = manifestKey(details.url);
    if (observed.has(key)) return;
    observed.add(key);

    fetch(LOCAL_RECEIVER, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: details.url }),
    }).catch(() => observed.delete(key));
  },
  { urls: ["https://hotmart.com/*", "https://*.hotmart.com/*"] },
);
