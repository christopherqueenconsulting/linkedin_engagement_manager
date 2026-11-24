// LEM Assist service worker. The only two LEM calls this extension ever makes:
//   GET  /api/assist/queue            the comments a human approved
//   POST /api/review/comment-posted   one of them is now visibly posted
// The token it holds is an `assist` session; the server refuses it on every other path.

async function settings() {
  const s = await chrome.storage.local.get(["base", "token"]);
  return { base: (s.base || "").replace(/\/$/, ""), token: s.token || "" };
}

async function lem(path, options) {
  const { base, token } = await settings();
  if (!base || !token) throw new Error("Set the LEM address and assist token in the popup first.");
  const res = await fetch(base + path, {
    ...options,
    headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
  });
  if (!res.ok) throw new Error("LEM answered " + res.status);
  return res.json();
}

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  const run = async () => {
    if (msg.type === "queue") {
      return lem("/api/assist/queue", { method: "GET" });
    }
    if (msg.type === "open") {
      await chrome.storage.session.set({ pending: msg.item });
      await chrome.tabs.create({ url: msg.item.post_url });
      return { ok: true };
    }
    if (msg.type === "pending") {
      const s = await chrome.storage.session.get("pending");
      return { item: s.pending || null };
    }
    if (msg.type === "posted") {
      const out = await lem("/api/review/comment-posted", {
        method: "POST",
        body: JSON.stringify({ candidate_id: msg.candidate_id }),
      });
      await chrome.storage.session.remove("pending");
      return out;
    }
    throw new Error("unknown message");
  };
  run().then(reply, (e) => reply({ error: String(e && e.message ? e.message : e) }));
  return true;
});
