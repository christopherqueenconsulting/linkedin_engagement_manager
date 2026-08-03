import { afterEach, describe, expect, it } from 'vitest'
import type { AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import api from './client'

// The CSRF layer restored in issue #957. The server refuses a cookie-authenticated write that does
// not carry this header, and what makes that work is that a cross-origin form cannot set a header
// at all — not that the value is secret. So the thing worth proving here is coverage: it rides on
// EVERY request out of this one client, with or without a session token in hand.

const original = api.defaults.adapter

// Captures what the interceptor chain actually produced, rather than reaching into axios internals.
const sentHeaders = async (method: string, url: string) => {
  let captured: InternalAxiosRequestConfig | null = null
  api.defaults.adapter = async (config: InternalAxiosRequestConfig) => {
    captured = config
    return { data: {}, status: 200, statusText: 'OK', headers: {}, config } as AxiosResponse
  }
  await api.request({ method, url })
  return (captured as unknown as InternalAxiosRequestConfig).headers as Record<string, unknown>
}

afterEach(() => {
  api.defaults.adapter = original
  localStorage.clear()
})

describe('api client', () => {
  it('sends X-LEM-Client on every request, reads and writes alike', async () => {
    for (const method of ['get', 'post', 'put', 'patch', 'delete']) {
      const headers = await sentHeaders(method, '/user/')
      expect(headers['X-LEM-Client']).toBe('spa')
    }
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
