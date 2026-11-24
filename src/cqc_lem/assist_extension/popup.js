const $ = (id) => document.getElementById(id);
const say = (t) => { $("status").textContent = t; };

chrome.storage.local.get(["base", "token"]).then((s) => {
  $("base").value = s.base || "";
  $("token").value = s.token || "";
});

$("save").addEventListener("click", async () => {
  await chrome.storage.local.set({ base: $("base").value.trim(), token: $("token").value.trim() });
  say("Saved.");
});

function render(items) {
  const ul = $("queue");
  ul.textContent = "";
  for (const item of items) {
    const li = document.createElement("li");
    const who = document.createElement("strong");
    who.textContent = item.author || "Post";
    const text = document.createElement("div");
    text.className = "text";
    text.textContent = item.text;
    const open = document.createElement("a");
    open.href = item.post_url;
    open.textContent = "Open post and fill the box";
    open.addEventListener("click", (e) => {
      e.preventDefault();
      chrome.runtime.sendMessage({ type: "open", item });
    });
    li.append(who, text, open);
    ul.append(li);
  }
}

$("refresh").addEventListener("click", () => {
  say("Loading...");
  chrome.runtime.sendMessage({ type: "queue" }, (res) => {
    if (!res || res.error) { say((res && res.error) || "No answer."); return; }
    const d = res.detail || {};
    say((d.items || []).length + " approved, " + d.remaining_today + " left today.");
    render(d.items || []);
  });
});
