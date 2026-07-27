import { describe, it, expect, beforeEach } from 'vitest'

import { captureAttribution, getAttribution } from './attribution'

const STORAGE_KEY = 'lem_attribution'

function landOn(search: string, referrer = '') {
  window.sessionStorage.clear()
  window.history.replaceState({}, '', `/pricing${search}`)
  Object.defineProperty(document, 'referrer', { value: referrer, configurable: true })
}

beforeEach(() => {
  landOn('')
})

describe('captureAttribution', () => {
  it('reads the UTMs and the referral ref off the landing URL', () => {
    landOn('?utm_source=linkedin&utm_medium=social&utm_campaign=post-12&ref=7')
    expect(captureAttribution()).toEqual({
      utm_source: 'linkedin',
      utm_medium: 'social',
      utm_campaign: 'post-12',
      ref: '7',
      landing_page: '/pricing',
    })
  })

  it('keeps the FIRST touch — a later tagged link in the same session must not overwrite it', () => {
    landOn('?utm_source=linkedin&utm_campaign=post-12')
    captureAttribution()
    window.history.replaceState({}, '', '/pricing?utm_source=youtube&utm_campaign=tutorial-account')
    expect(captureAttribution().utm_source).toBe('linkedin')
    expect(getAttribution().utm_campaign).toBe('post-12')
  })

  it('records an external referrer but ignores an internal one', () => {
    landOn('', 'https://www.linkedin.com/feed/')
    expect(captureAttribution().referrer).toBe('https://www.linkedin.com/feed/')

    landOn('', `${window.location.origin}/dashboard`)
    expect(captureAttribution().referrer).toBeUndefined()
  })

  it('a direct visit still records where it landed', () => {
    expect(captureAttribution()).toEqual({ landing_page: '/pricing' })
  })

  it('getAttribution is empty until something was captured', () => {
    window.sessionStorage.removeItem(STORAGE_KEY)
    expect(getAttribution()).toEqual({})
  })
})
