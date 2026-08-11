import { describe, expect, it } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const indexPath = path.resolve(__dirname, '../index.html')
const publicDir = path.resolve(__dirname, '../public')

function readIndex(baseUrl: string): string {
  const raw = fs.readFileSync(indexPath, 'utf-8')
  return raw.replace(/%VITE_PUBLIC_BASE_URL%/g, baseUrl)
}

function parseMeta(html: string): Record<string, string> {
  const meta: Record<string, string> = {}
  const regex = /<meta\s+(?:name|property)="([^"]+)"\s+content="([^"]+)"/g
  let match
  while ((match = regex.exec(html)) !== null) {
    meta[match[1]] = match[2]
  }
  return meta
}

function parseLink(html: string): Record<string, string> {
  const links: Record<string, string> = {}
  const regex = /<link\s+rel="([^"]+)"\s+href="([^"]+)"/g
  let match
  while ((match = regex.exec(html)) !== null) {
    links[match[1]] = match[2]
  }
  return links
}

function parseJsonLd(html: string): Array<Record<string, unknown>> {
  const blocks: Array<Record<string, unknown>> = []
  const regex = /<script\s+type="application\/ld\+json">([\s\S]*?)<\/script>/g
  let match
  while ((match = regex.exec(html)) !== null) {
    try {
      blocks.push(JSON.parse(match[1].trim()))
    } catch {
      // ignore malformed JSON
    }
  }
  return blocks
}

describe('index.html SEO shell', () => {
  const baseUrl = 'https://lem.example.com'
  const html = readIndex(baseUrl)
  const meta = parseMeta(html)
  const links = parseLink(html)

  it('has a meta description', () => {
    expect(meta.description).toBeDefined()
    expect(meta.description.length).toBeGreaterThan(10)
  })

  it('has theme-color', () => {
    expect(meta['theme-color']).toBe('#054DB1')
  })

  it('has a canonical link pointing at the base URL', () => {
    expect(links.canonical).toBe(`${baseUrl}/`)
  })

  it('has the full Open Graph set with absolute URLs', () => {
    expect(meta['og:title']).toBe('LEM - LinkedIn Engagement Manager')
    expect(meta['og:description']).toBeDefined()
    expect(meta['og:image']).toMatch(/^https:\/\/lem\.example\.com\/brand\/og\.png$/)
    expect(meta['og:url']).toBe(`${baseUrl}/`)
    expect(meta['og:type']).toBe('website')
    expect(meta['og:site_name']).toBe('LinkedIn Engagement Manager')
  })

  it('has the full Twitter card set with absolute image URL', () => {
    expect(meta['twitter:card']).toBe('summary_large_image')
    expect(meta['twitter:title']).toBe('LEM - LinkedIn Engagement Manager')
    expect(meta['twitter:description']).toBeDefined()
    expect(meta['twitter:image']).toMatch(/^https:\/\/lem\.example\.com\/brand\/og\.png$/)
  })

  it('resolves og:image and twitter:image to a file under public/', () => {
    const imagePath = meta['og:image'].replace(`${baseUrl}/`, '')
    const absolute = path.resolve(publicDir, imagePath)
    expect(fs.existsSync(absolute)).toBe(true)
    expect(absolute.endsWith('.png')).toBe(true)
  })

  it('resolves canonical URL path to the SPA root (/)', () => {
    expect(meta['og:url']).toBe(`${baseUrl}/`)
  })

  it('has JSON-LD containing SoftwareApplication and Organization', () => {
    const blocks = parseJsonLd(html)
    expect(blocks.length).toBeGreaterThan(0)
    const graph = blocks[0]['@graph'] as Array<Record<string, unknown>> | undefined
    expect(graph).toBeDefined()
    const types = graph!.map((item) => item['@type'])
    expect(types).toContain('SoftwareApplication')
    expect(types).toContain('Organization')
  })

  it('JSON-LD URLs are absolute and match the base URL', () => {
    const blocks = parseJsonLd(html)
    const graph = blocks[0]['@graph'] as Array<Record<string, unknown>>
    for (const item of graph) {
      for (const key of ['url', 'image', 'logo']) {
        const value = item[key]
        if (typeof value === 'string') {
          expect(value).toMatch(/^https:\/\/lem\.example\.com\//)
        }
      }
    }
  })
})
