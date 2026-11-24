// LEM Assist content script. It puts the approved text into LinkedIn's comment box and then only
// WATCHES. The human presses Post. Nothing here activates a control or submits anything: it inserts
// text, and it reports a comment once that exact text appears among the post's published comments.

(function () {
  const COMPOSER = '.comments-comment-box [contenteditable="true"], [role="textbox"][contenteditable="true"]';
  const PUBLISHED = ".comments-comment-entity, article.comments-comment-item";
  let item = null;
  let reported = false;

  function insertText(box, text) {
    box.focus();
    document.execCommand("insertText", false, text);
  }

  function watchForPublished() {
    const check = () => {
      if (reported || !item) return;
      const want = item.text.trim();
      for (const el of document.querySelectorAll(PUBLISHED)) {
        if ((el.textContent || "").includes(want)) {
          reported = true;
          chrome.runtime.sendMessage({ type: "posted", candidate_id: item.candidate_id });
          observer.disconnect();
          return;
        }
      }
    };
    const observer = new MutationObserver(check);
    observer.observe(document.body, { childList: true, subtree: true });
    check();
  }

  function waitForComposer() {
    const find = () => document.querySelector(COMPOSER);
    const box = find();
    if (box) return Promise.resolve(box);
    return new Promise((resolve) => {
      const observer = new MutationObserver(() => {
        const b = find();
        if (b) {
          observer.disconnect();
          resolve(b);
        }
      });
      observer.observe(document.body, { childList: true, subtree: true });
    });
  }

  chrome.runtime.sendMessage({ type: "pending" }, async (res) => {
    if (!res || !res.item) return;
    // Only act on the post the popup opened.
    const wanted = res.item.post_url || "";
    if (!wanted || !location.href.startsWith(wanted.split("?")[0])) return;
    item = res.item;
    const box = await waitForComposer();
    insertText(box, item.text);
    watchForPublished();
  });
})();
