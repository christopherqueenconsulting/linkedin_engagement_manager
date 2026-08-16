import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AxiosError } from 'axios'
import type { AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import api from './client'
import { NEW_VERSION_MESSAGE, RELOADING_MESSAGE, resetChunkReloadState } from '../utils/chunkReload'
import { SESSION_ENDED_EVENT } from '../utils/sessionEnd'

// The CSRF layer restored in issue #957. The server refuses a cookie-authenticated write that does
// not carry this header, and what makes that work is that a cross-origin form cannot set a header
// at all — not that the value is secret. So the thing worth proving here is coverage: it rides on
// EVERY request out of this one client, with or without a session token in hand.

const original = api.defaults.adapter

// Captures what the interceptor chain actually produced, rather than reaching into axios internals.
// The adapter is restored in a `finally` so a failing assertion inside a request cannot leak a stub
// into the next test.
const sentHeaders = async (method: string, url: string): Promise<Record<string, unknown>> => {
  let captured: InternalAxiosRequestConfig | null = null
  try {
    api.defaults.adapter = async (config: InternalAxiosRequestConfig) => {
      captured = config
      return { data: {}, status: 200, statusText: 'OK', headers: {}, config } as AxiosResponse
    }
    await api.request({ method, url })
  } finally {
    api.defaults.adapter = original
  }
  return (captured as unknown as InternalAxiosRequestConfig).headers as Record<string, unknown>
}

// A server refusal, shaped exactly as FastAPI serialises `HTTPException(status, detail={...})`.
// A custom adapter is responsible for settling the status itself — axios only runs `validateStatus`
// inside its built-in adapters — so this rejects with a real AxiosError, which is what the response
// interceptor under test actually receives in the browser.
const refuseWith = (status: number, detail: unknown): void => {
  api.defaults.adapter = async (config: InternalAxiosRequestConfig) => {
    const response = {
      data: { detail }, status, statusText: 'Refused', headers: {}, config,
    } as AxiosResponse
    throw new AxiosError(`Request failed with status code ${status}`,
      AxiosError.ERR_BAD_REQUEST, config, null, response)
  }
}

afterEach(() => {
  api.defaults.adapter = original
  localStorage.clear()
  vi.restoreAllMocks()
})

describe('api client', () => {
  it('is same-origin by construction — the baseURL is relative', () => {
    // The whole layer rests on this. An absolute baseURL (a split CDN/API deploy, a dev server
    // pointed straight at :8000) would make X-LEM-Client a PREFLIGHTED header, and no CORS
    // middleware is installed to answer that preflight — every request would fail, not just writes.
    expect(api.defaults.baseURL).toBe('/api')
    expect(api.defaults.baseURL).not.toMatch(/^[a-z]+:\/\//)
  })

  it('sends X-LEM-Client on every request, reads and writes alike', async () => {
    for (const method of ['get', 'post', 'put', 'patch', 'delete']) {
      const headers = await sentHeaders(method, '/user/')
      expect(headers['X-LEM-Client']).toBe('spa')
    }
  })

  // Issue #1030. The client defaults every request to `application/json`, and axios reads that in
  // `transformRequest`: a FormData body sent under a JSON Content-Type is turned into
  // `JSON.stringify(formDataToJSON(data))`, where a File collapses to `{}`. The upload then leaves
  // the browser as JSON with no file in it and the endpoint answers 422 — which is exactly what
  // every FormData call site here does (post images, newsletter covers), because a mocked
  // `api.post` in a component test never runs the transform.
  it('leaves a multipart body alone — the file must survive the JSON default', async () => {
    let captured: InternalAxiosRequestConfig | null = null
    const form = new FormData()
    form.append('session_token', 'cookie')
    form.append('file', new File(['bytes'], 'shot.png', { type: 'image/png' }))
    try {
      api.defaults.adapter = async (config: InternalAxiosRequestConfig) => {
        captured = config
        return { data: {}, status: 200, statusText: 'OK', headers: {}, config } as AxiosResponse
      }
      await api.post('/user/post/image', form)
    } finally {
      api.defaults.adapter = original
    }
    const sent = captured as unknown as InternalAxiosRequestConfig
    expect(sent.data).toBeInstanceOf(FormData)
    expect((sent.data as FormData).get('file')).toBeInstanceOf(File)
    // Never JSON: that is the value that would have eaten the file.
    expect(String(sent.headers['Content-Type'] ?? '')).toBe('multipart/form-data')
    expect(sent.headers['X-LEM-Client']).toBe('spa')
  })

  it('still sends a plain object as JSON', async () => {
    let captured: InternalAxiosRequestConfig | null = null
    try {
      api.defaults.adapter = async (config: InternalAxiosRequestConfig) => {
        captured = config
        return { data: {}, status: 200, statusText: 'OK', headers: {}, config } as AxiosResponse
      }
      await api.post('/schedule_post/', { content: 'hello' })
    } finally {
      api.defaults.adapter = original
    }
    const sent = captured as unknown as InternalAxiosRequestConfig
    expect(sent.data).toBe('{"content":"hello"}')
    expect(String(sent.headers['Content-Type'])).toContain('application/json')
  })

  it('sends it with no session token held — the cookie is the credential it protects', async () => {
    const headers = await sentHeaders('post', '/create_weekly_content/')
    expect(headers['X-LEM-Client']).toBe('spa')
    expect(headers['X-Session-Token']).toBeUndefined()
  })

  // Issue #1357. The header was never read by `get_session_user_id` — an explicit token resolves
  // from the `session_token` FIELD — so it counted only at the edge presence check, which no longer
  // reads it either. Holding a REAL token is the case that used to send it, and is the case that
  // must not: a credential in a header nothing consumes is exposure with nothing bought.
  it('never sends X-Session-Token, not even holding a real token', async () => {
    localStorage.setItem('lem_session', 'a-real-token')
    const headers = await sentHeaders('post', '/create_weekly_content/')
    expect(headers['X-LEM-Client']).toBe('spa')
    expect(headers['X-Session-Token']).toBeUndefined()
  })

  it('sends nothing for the cookie sentinel — it is not a token', async () => {
    localStorage.setItem('lem_session', 'cookie')
    const headers = await sentHeaders('post', '/create_weekly_content/')
    expect(headers['X-LEM-Client']).toBe('spa')
    expect(headers['X-Session-Token']).toBeUndefined()
  })
})

// A bundle cached from before the release that added the header gets 403 `client_header_required`
// on every write. That is a STALE BUNDLE, not a dead session, so the fix is a reload — never a sign
// out, which is why the server answers 403 and not 401.
describe('a 403 client_header_required', () => {
  let reload: ReturnType<typeof vi.fn>

  beforeEach(() => {
    resetChunkReloadState()
    window.sessionStorage.clear()
    reload = vi.fn()
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...window.location, reload },
    })
  })

  it('reloads once so the tab lands on a bundle that sends the header', async () => {
    refuseWith(403, { code: 'client_header_required', message: 'nope' })

    await expect(api.post('/create_weekly_content/')).rejects.toThrow(RELOADING_MESSAGE)
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('surfaces a message instead of looping when the reload did not fix it', async () => {
    refuseWith(403, { code: 'client_header_required', message: 'nope' })

    await expect(api.post('/create_weekly_content/')).rejects.toThrow(RELOADING_MESSAGE)
    // A second refusal inside the cooldown — a proxy stripping the header, say. One reload was the
    // fix worth trying; two is a loop, and a wrong message beats a reload loop.
    await expect(api.post('/create_weekly_content/')).rejects.toThrow(NEW_VERSION_MESSAGE)
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('never signs the user out — that is what the 401 branch is for', async () => {
    localStorage.setItem('lem_session', 'cookie')
    localStorage.setItem('lem_email', 'user@example.com')
    refuseWith(403, { code: 'client_header_required', message: 'nope' })

    await expect(api.post('/create_weekly_content/')).rejects.toThrow()
    expect(localStorage.getItem('lem_session')).toBe('cookie')
    expect(localStorage.getItem('lem_email')).toBe('user@example.com')
  })

  it('leaves any OTHER 403 alone — a step-up or scope refusal is the caller error UI to render', async () => {
    refuseWith(403, { code: 'step_up_required', message: 'verify first' })

    await expect(api.post('/user/li-cookie')).rejects.toMatchObject({ response: { status: 403 } })
    expect(reload).not.toHaveBeenCalled()
  })
})

// Issue #1358. A 401 used to be a global verdict: clear `lem_session`, `window.location.href = '/'`.
// On 2026-08-10 (#1354) routes moved out of main.py by the #1154 split lost cookie resolution while
// `/api/dashboard/stats/` and `/api/activity/` kept answering 200 — and the first 401 discarded a
// session the user had just been handed. A 401 is evidence about the ENDPOINT that answered it;
// only `/auth/session` answers the question "am I signed in?".
describe('a 401', () => {
  const requested: string[] = []
  const probes: InternalAxiosRequestConfig[] = []

  // Routes by URL, so a test can describe a PARTIAL outage — the shape the old interceptor could
  // not tell apart from a dead session.
  const respondBy = (statusFor: (url: string) => number): void => {
    api.defaults.adapter = async (config: InternalAxiosRequestConfig) => {
      const url = config.url ?? ''
      requested.push(url)
      if (url.startsWith('/auth/session')) probes.push(config)
      const status = statusFor(url)
      const response = {
        data: { detail: 'x' }, status, statusText: '', headers: {}, config,
      } as AxiosResponse
      if (status >= 400) {
        throw new AxiosError(`Request failed with status code ${status}`,
          AxiosError.ERR_BAD_REQUEST, config, null, response)
      }
      return response
    }
  }

  const probeCount = () => requested.filter((u) => u.startsWith('/auth/session')).length

  let ended: number
  const onEnded = () => { ended += 1 }

  beforeEach(() => {
    requested.length = 0
    probes.length = 0
    ended = 0
    window.addEventListener(SESSION_ENDED_EVENT, onEnded)
    localStorage.setItem('lem_session', 'cookie')
    localStorage.setItem('lem_email', 'user@example.com')
  })

  afterEach(() => {
    window.removeEventListener(SESSION_ENDED_EVENT, onEnded)
  })

  it('does not clear the session when the session route says the session is fine', async () => {
    respondBy((url) => (url.startsWith('/auth/session') ? 200 : 401))

    await expect(api.get('/user/timezone')).rejects.toMatchObject({ response: { status: 401 } })
    await vi.waitFor(() => expect(probeCount()).toBe(1))

    expect(localStorage.getItem('lem_session')).toBe('cookie')
    expect(localStorage.getItem('lem_email')).toBe('user@example.com')
    expect(ended).toBe(0)
  })

  // The incident, reproduced: a valid session, some endpoints 401, some 200. The user stays in.
  it('survives the #1354 shape — a partial outage is not a sign-out', async () => {
    const broken = ['/user/timezone', '/user/engagement-preferences', '/user/avatars']
    respondBy((url) => (broken.some((b) => url.startsWith(b)) ? 401 : 200))

    const settled = await Promise.allSettled([
      api.get('/user/timezone'),
      api.get('/dashboard/stats/'),
      api.get('/user/engagement-preferences'),
      api.get('/activity/'),
      api.get('/user/avatars'),
    ])
    await vi.waitFor(() => expect(probeCount()).toBeGreaterThan(0))

    expect(settled.filter((s) => s.status === 'fulfilled')).toHaveLength(2)
    expect(localStorage.getItem('lem_session')).toBe('cookie')
    expect(ended).toBe(0)
  })

  it('signs out once the session route agrees the session is gone', async () => {
    respondBy(() => 401)

    await expect(api.get('/user/timezone')).rejects.toMatchObject({ response: { status: 401 } })
    await vi.waitFor(() => expect(ended).toBe(1))

    expect(localStorage.getItem('lem_session')).toBeNull()
    expect(localStorage.getItem('lem_email')).toBeNull()
  })

  it('asks once for a burst — a mounting page fires a dozen requests, not a dozen questions', async () => {
    respondBy((url) => (url.startsWith('/auth/session') ? 200 : 401))

    await Promise.allSettled(
      ['/user/timezone', '/user/avatars', '/user/posts', '/user/newsletter-draft']
        .map((u) => api.get(u)),
    )
    await vi.waitFor(() => expect(probeCount()).toBe(1))
    expect(probeCount()).toBe(1)
  })

  it('leaves a 401 from the session route to the auth layer', async () => {
    // `AuthProvider` boots on this one and `login()` answers it by falling back to holding the
    // token. A teardown from underneath would turn that fallback — which exists so a valid login is
    // never a lockout — into the lockout it prevents.
    respondBy(() => 401)

    await expect(api.get('/auth/session?session_token=cookie')).rejects.toMatchObject({
      response: { status: 401 },
    })
    expect(probeCount()).toBe(1)  // the request itself, no second one behind it
    expect(localStorage.getItem('lem_session')).toBe('cookie')
    expect(ended).toBe(0)
  })

  // The cookie-less fallback: `login()` holds a REAL token because the browser refused the cookie,
  // and every call site sends it in the `session_token` FIELD — which is the only place the server
  // resolves an explicit token from, there being no header form of it since #1357. A probe without
  // that field would carry no credential the resolver reads, 401 about a
  // live session, and make the corroboration the amplifier it exists to remove.
  it('corroborates with the credential the app is actually using', async () => {
    localStorage.setItem('lem_session', 'a-real-token')
    respondBy((url) => (url.startsWith('/auth/session') ? 200 : 401))

    await expect(api.get('/user/timezone')).rejects.toMatchObject({ response: { status: 401 } })
    await vi.waitFor(() => expect(probes).toHaveLength(1))

    expect(probes[0].params).toEqual({ session_token: 'a-real-token' })
    expect(localStorage.getItem('lem_session')).toBe('a-real-token')
    expect(ended).toBe(0)
  })

  it('sends the cookie sentinel when that is what is held — the server reads the cookie instead', async () => {
    respondBy((url) => (url.startsWith('/auth/session') ? 200 : 401))

    await expect(api.get('/user/timezone')).rejects.toMatchObject({ response: { status: 401 } })
    await vi.waitFor(() => expect(probes).toHaveLength(1))

    expect(probes[0].params).toEqual({ session_token: 'cookie' })
  })

  it('asks nothing when no session is held — a signed-out 401 is just a 401', async () => {
    localStorage.clear()
    respondBy(() => 401)

    await expect(api.get('/user/timezone')).rejects.toMatchObject({ response: { status: 401 } })
    expect(probeCount()).toBe(0)
    expect(ended).toBe(0)
  })

  it('never hard-redirects — the teardown is a state change, not a page navigation', async () => {
    const assign = vi.fn()
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...window.location, assign, set href(v: string) { assign(v) }, get href() { return '/' } },
    })
    respondBy(() => 401)

    await expect(api.get('/user/timezone')).rejects.toMatchObject({ response: { status: 401 } })
    await vi.waitFor(() => expect(ended).toBe(1))
    expect(assign).not.toHaveBeenCalled()
  })
})
