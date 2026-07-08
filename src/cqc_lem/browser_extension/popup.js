// Reads the user's LinkedIn session cookies (li_at is httpOnly, so only the
// chrome.cookies API can see it — not page JS) and POSTs them to the LEM endpoint,
// so automation resumes a trusted session instead of doing a password login.

const baseEl = document.getElementById('base');
const tokenEl = document.getElementById('token');
const btn = document.getElementById('connect');
const statusEl = document.getElementById('status');

const DEFAULT_BASE = 'https://lem.christopherqueenconsulting.com';

// Restore saved settings.
chrome.storage.local.get(['base', 'token'], (s) => {
  baseEl.value = s.base || DEFAULT_BASE;
  if (s.token) tokenEl.value = s.token;
});

function setStatus(msg, cls) {
  statusEl.textContent = msg;
  statusEl.className = cls || '';
}

function getCookie(name) {
  return new Promise((resolve) => {
    chrome.cookies.get({ url: 'https://www.linkedin.com', name }, (c) => resolve(c && c.value));
  });
}

btn.addEventListener('click', async () => {
  const base = (baseEl.value || DEFAULT_BASE).trim().replace(/\/+$/, '');
  const token = (tokenEl.value || '').trim();
  if (!token) return setStatus('Enter your LEM session token first.', 'err');

  chrome.storage.local.set({ base, token });
  btn.disabled = true;
  setStatus('Reading LinkedIn session…');

  try {
    const li_at = await getCookie('li_at');
    if (!li_at) {
      setStatus('No LinkedIn session found. Open linkedin.com, sign in, then retry.', 'err');
      btn.disabled = false;
      return;
    }
    const jsessionid = await getCookie('JSESSIONID');

    setStatus('Sending to LEM…');
    const resp = await fetch(`${base}/api/user/linkedin-cookie`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_token: token, li_at, jsessionid: jsessionid || null }),
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.ok) {
      setStatus('✓ Connected. ' + (data.detail || 'LinkedIn session saved.'), 'ok');
    } else {
      setStatus(`Failed (${resp.status}): ${data.detail || 'see LEM logs'}`, 'err');
    }
  } catch (e) {
    setStatus('Error: ' + e.message, 'err');
  } finally {
    btn.disabled = false;
  }
});
