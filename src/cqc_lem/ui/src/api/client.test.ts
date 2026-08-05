import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AxiosError } from 'axios'
import type { AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import api from './client'
import { NEW_VERSION_MESSAGE, RELOADING_MESSAGE, resetChunkReloadState } from '../utils/chunkReload'

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

  it('sends it alongside an explicit token too', async () => {
    localStorage.setItem('lem_session', 'a-real-token')
    const headers = await sentHeaders('post', '/create_weekly_content/')
    expect(headers['X-LEM-Client']).toBe('spa')
    expect(headers['X-Session-Token']).toBe('a-real-token')
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
