import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import AgentsPage from './AgentsPage'

const get = vi.fn()
const post = vi.fn()
vi.mock('../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
  },
}))
vi.mock('../contexts/useAuth', () => ({ useAuth: () => ({ sessionToken: 'cookie' }) }))

const flagState = vi.hoisted(() => ({ isLoading: false, on: true }))
vi.mock('../hooks/useFeatureFlags', () => ({
  FLAGS: { mcpServer: 'mcp-server-enabled' },
  useFeatureFlags: () => ({
    isLoading: flagState.isLoading,
    data: flagState.isLoading ? undefined : { flags: { 'mcp-server-enabled': flagState.on } },
  }),
}))

// Every analytics call this page can make, recorded with its arguments, so a test can prove the
// token never reaches one (plan §6 "Token handling").
const captured = vi.hoisted(() => [] as unknown[][])
vi.mock('../utils/analytics', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../utils/analytics')>()
  const spy = (name: string) => (...args: unknown[]) => { captured.push([name, ...args]) }
  return {
    ...actual,
    capture: spy('capture'),
    captureException: spy('captureException'),
    recordMcpTokenMinted: spy('mcpTokenMinted'),
    recordMcpTokenRotated: spy('mcpTokenRotated'),
    recordMcpTokenRevoked: spy('mcpTokenRevoked'),
    recordMcpCommandCopied: spy('mcpCommandCopied'),
  }
})

const TOKEN = 'lem_mcp_EXAMPLE'
const URL = 'https://lem.example.com/api/mcp'

type Row = Record<string, unknown>

function row(overrides: Row = {}): Row {
  return {
    session_id: 7,
    label: 'Claude Code - macOS',
    scope: 'mcp',
    created_at: '2026-11-01T00:00:00Z',
    last_seen_at: '2026-11-20T00:00:00Z',
    expires_at: '2099-01-01T00:00:00Z',
    calls_7d: 12,
    last_client_name: 'claude-code',
    ...overrides,
  }
}

let info: { enabled: boolean; mcp_url: string; tokens: Row[] } | Error
let strong = true

function mintResponse(token = TOKEN) {
  return {
    data: { detail: { session_token: token, session_id: 9, mcp_url: URL, expires_at: '2026-12-24T00:00:00Z',
      label: 'Claude Code - Linux', scope: 'mcp' } },
  }
}

function stepUpRefusal() {
  return { response: { status: 403, data: { detail: { code: 'step_up_required', methods: ['totp'] } } } }
}

let client: QueryClient

function harness() {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/agents']}>
        <Routes>
          <Route path="/" element={<p>Home page</p>} />
          <Route path="/agents" element={<AgentsPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

async function ready() {
  await waitFor(() => expect(screen.getByRole('button', { name: 'Create connection' })).toBeTruthy())
  await waitFor(() => expect(screen.getByText('Connect your agent')).toBeTruthy())
}

/** Everything a token must never be in: analytics, both react-query caches, browser storage. */
function expectTokenNowhereButTheDom(token = TOKEN) {
  expect(JSON.stringify(captured)).not.toContain(token)
  const queries = client.getQueryCache().getAll().map((q) => q.state.data)
  const mutations = client.getMutationCache().getAll().map((m) => m.state.data)
  expect(JSON.stringify(queries)).not.toContain(token)
  expect(JSON.stringify(mutations)).not.toContain(token)
  for (const store of [localStorage, sessionStorage]) {
    for (let i = 0; i < store.length; i++) {
      expect(store.getItem(store.key(i)!) ?? '').not.toContain(token)
    }
  }
}

beforeEach(() => {
  get.mockReset()
  post.mockReset()
  captured.length = 0
  flagState.isLoading = false
  flagState.on = true
  strong = true
  info = { enabled: true, mcp_url: URL, tokens: [] }
  get.mockImplementation((path: string) => {
    if (path.startsWith('/user/mcp-info')) {
      return info instanceof Error ? Promise.reject(info) : Promise.resolve({ data: { detail: info } })
    }
    if (path.startsWith('/user/auth-factors')) {
      return Promise.resolve({ data: { detail: { has_strong_factor: strong } } })
    }
    return Promise.reject(new Error(`unexpected GET ${path}`))
  })
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText: vi.fn().mockResolvedValue(undefined) },
    configurable: true,
  })
})
afterEach(cleanup)

describe('AgentsPage flag gate', () => {
  it('sends a direct visit home when mcp-server-enabled is off', async () => {
    flagState.on = false
    harness()
    await waitFor(() => expect(screen.getByText('Home page')).toBeTruthy())
    expect(screen.queryByRole('heading', { name: 'Agents' })).toBeNull()
    expect(get).not.toHaveBeenCalled()
  })

  it('does not bounce anyone while the flags payload is still in flight', () => {
    flagState.isLoading = true
    harness()
    expect(screen.queryByText('Home page')).toBeNull()
  })
})

describe('AgentsPage minting (issue #2687)', () => {
  it('mints with the defaults, 30 days and outreach OFF, and shows the token once', async () => {
    post.mockResolvedValue(mintResponse())
    harness()
    await ready()
    expect((screen.getByRole('checkbox', { name: /Include outreach and leads \(PII\)/ }) as HTMLInputElement).checked)
      .toBe(false)
    expect((screen.getByRole('combobox', { name: 'Expires after' }) as HTMLSelectElement).value).toBe('30')

    fireEvent.click(screen.getByRole('button', { name: 'Create connection' }))
    await waitFor(() => expect(screen.getByText(/Shown once/)).toBeTruthy())

    expect(post).toHaveBeenCalledTimes(1)
    const [path, body] = post.mock.calls[0]
    expect(path).toBe('/user/mcp-token')
    expect(body).toMatchObject({ session_token: 'cookie', ttl_days: 30, outreach: false })
    expect(body.label).toMatch(/^Claude Code - /)

    // The raw token block, plus the one export line of the primary command — and nowhere else.
    expect(screen.getByLabelText('Your token').textContent).toBe(TOKEN)
    const cli = screen.getByLabelText('Terminal command').textContent ?? ''
    expect(cli).toContain(`export LEM_MCP_TOKEN=${TOKEN}`)
    expect(cli).toContain('--header "Authorization: Bearer ${LEM_MCP_TOKEN}"')
    expect(screen.getByLabelText('Or, per project').textContent).not.toContain(TOKEN)

    expect(captured).toContainEqual(['mcpTokenMinted', 30, 'mcp'])
    expectTokenNowhereButTheDom()
  })

  it('sends the chosen TTL and the outreach opt-in', async () => {
    post.mockResolvedValue(mintResponse())
    harness()
    await ready()
    fireEvent.change(screen.getByRole('combobox', { name: 'Expires after' }), { target: { value: '90' } })
    fireEvent.click(screen.getByRole('checkbox', { name: /Include outreach and leads \(PII\)/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Create connection' }))
    await waitFor(() => expect(post).toHaveBeenCalled())
    expect(post.mock.calls[0][1]).toMatchObject({ ttl_days: 90, outreach: true })
  })

  it('offers only 7, 30 and 90 days', async () => {
    harness()
    await ready()
    const select = screen.getByRole('combobox', { name: 'Expires after' }) as HTMLSelectElement
    expect(Array.from(select.options).map((o) => o.value)).toEqual(['7', '30', '90'])
  })

  it('opens step-up on a 403 and re-mints once verified — two mint posts', async () => {
    post
      .mockRejectedValueOnce(stepUpRefusal())
      .mockResolvedValueOnce({ data: { detail: { verified: true } } })
      .mockResolvedValueOnce(mintResponse('lem_mcp_STEPUP_EXAMPLE'))
    harness()
    await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Create connection' }))
    await waitFor(() => expect(screen.getByText("Confirm it's you")).toBeTruthy())
    expect(screen.getByText(/An agent connection can read your LEM account/)).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Authenticator code'), { target: { value: '123456' } })
    fireEvent.click(screen.getByText('Confirm'))
    await waitFor(() => expect(post.mock.calls.filter((c) => c[0] === '/user/mcp-token').length).toBe(2))
    await waitFor(() => expect(screen.getByLabelText('Your token').textContent).toBe('lem_mcp_STEPUP_EXAMPLE'))
    expectTokenNowhereButTheDom('lem_mcp_STEPUP_EXAMPLE')
  })

  it('a cancelled step-up changes nothing', async () => {
    post.mockRejectedValueOnce(stepUpRefusal())
    harness()
    await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Create connection' }))
    await waitFor(() => expect(screen.getByText("Confirm it's you")).toBeTruthy())
    fireEvent.click(screen.getByText('Cancel'))
    await waitFor(() => expect(screen.queryByText("Confirm it's you")).toBeNull())
    expect(screen.queryByText(/Shown once/)).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(captured.filter((c) => c[0] === 'mcpTokenMinted')).toEqual([])
  })

  it('a failed mint shows an error and renders no token', async () => {
    post.mockRejectedValue({ response: { status: 500, data: { detail: 'boom' } } })
    harness()
    await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Create connection' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/Could not create the connection/))
    expect(screen.queryByText(/Shown once/)).toBeNull()
    expect(screen.queryByLabelText('Your token')).toBeNull()
  })

  it('409 mcp_token_limit says to revoke one first', async () => {
    post.mockRejectedValue({ response: { status: 409, data: { detail: { code: 'mcp_token_limit' } } } })
    harness()
    await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Create connection' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/Revoke one first/))
  })

  it('403 mcp_needs_strong_factor swaps Create for the enrol hint', async () => {
    post.mockRejectedValue({ response: { status: 403, data: { detail: { code: 'mcp_needs_strong_factor' } } } })
    harness()
    await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Create connection' }))
    await waitFor(() => expect(screen.getByRole('link', { name: 'Enrol a passkey or authenticator first' }))
      .toBeTruthy())
    expect(screen.queryByRole('button', { name: 'Create connection' })).toBeNull()
  })

  it('an account with no strong factor sees the enrol hint instead of Create', async () => {
    strong = false
    harness()
    const link = await screen.findByRole('link', { name: 'Enrol a passkey or authenticator first' })
    expect(link.getAttribute('href')).toBe('/account?section=billing')
    expect(screen.queryByRole('button', { name: 'Create connection' })).toBeNull()
  })
})

describe('AgentsPage show-once window', () => {
  it('hiding the token turns every command back into a placeholder', async () => {
    post.mockResolvedValue(mintResponse())
    harness()
    await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Create connection' }))
    await waitFor(() => expect(screen.getByText(/Shown once/)).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /hide the token/ }))
    expect(document.body.textContent).not.toContain(TOKEN)
    expect(screen.getByLabelText('Terminal command').textContent).toContain('<YOUR_TOKEN>')
    expect(screen.getByText(/Rotate a connection below to get a new paste-ready command/)).toBeTruthy()
  })

  it('a later render never has the token — it lived in component state only', async () => {
    post.mockResolvedValue(mintResponse())
    const first = harness()
    await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Create connection' }))
    await waitFor(() => expect(screen.getByText(/Shown once/)).toBeTruthy())
    first.unmount()

    harness()
    await ready()
    expect(document.body.textContent).not.toContain(TOKEN)
    expect(screen.getByLabelText('Terminal command').textContent).toContain('<YOUR_TOKEN>')
    expectTokenNowhereButTheDom()
  })

  it('the inline-token variant is behind its toggle, with the same warning', async () => {
    post.mockResolvedValue(mintResponse())
    harness()
    await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Create connection' }))
    await waitFor(() => expect(screen.getByText(/Shown once/)).toBeTruthy())
    const inlineHeader = `--header "Authorization: Bearer ${TOKEN}"`
    expect(screen.getByLabelText('Terminal command').textContent).not.toContain(inlineHeader)

    fireEvent.click(screen.getByRole('checkbox', { name: /Show inline token/ }))
    expect(screen.getByLabelText('Terminal command').textContent).toContain(inlineHeader)
    expect(screen.getByText("Paste into your terminal — never into your agent's chat")).toBeTruthy()
  })

  it('a copy records only the client and the variant', async () => {
    post.mockResolvedValue(mintResponse())
    harness()
    await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Create connection' }))
    await waitFor(() => expect(screen.getByText(/Shown once/)).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Copy Terminal command' }))
    await waitFor(() => expect(captured).toContainEqual(['mcpCommandCopied', 'claude-code', 'env']))
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(expect.stringContaining(TOKEN))
    expectTokenNowhereButTheDom()
  })

  it('the agent prompt card never carries the token or a command', async () => {
    post.mockResolvedValue(mintResponse())
    harness()
    await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Create connection' }))
    await waitFor(() => expect(screen.getByText(/Shown once/)).toBeTruthy())
    const prompt = screen.getByLabelText('First message').textContent ?? ''
    expect(prompt).toContain('lem_whoami')
    expect(prompt).not.toContain(TOKEN)
    expect(prompt).not.toMatch(/mcp add|LEM_MCP_TOKEN|Bearer/)
  })

  it('every token-bearing block is masked for replay', async () => {
    post.mockResolvedValue(mintResponse())
    harness()
    await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Create connection' }))
    await waitFor(() => expect(screen.getByText(/Shown once/)).toBeTruthy())
    const bearing = Array.from(document.querySelectorAll('pre')).filter((p) => p.textContent?.includes(TOKEN))
    // The raw-token block and the primary command, at least — never a vacuous pass.
    expect(bearing.length).toBeGreaterThanOrEqual(2)
    for (const pre of bearing) expect(pre.closest('[data-ph-mask="true"]')).toBeTruthy()
    // And no token text sits outside a masked subtree anywhere on the page.
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      if (n.textContent?.includes(TOKEN)) expect(n.parentElement?.closest('[data-ph-mask="true"]')).toBeTruthy()
    }
  })
})

describe('AgentsPage clients', () => {
  it('ChatGPT and the claude.ai connector say not supported yet, with nothing to paste', async () => {
    harness()
    await ready()
    for (const name of ['ChatGPT', 'claude.ai connector']) {
      fireEvent.click(screen.getByRole('tab', { name }))
      const panel = screen.getByRole('tabpanel')
      expect(panel.textContent).toContain('Not supported yet (needs OAuth)')
      expect(within(panel).queryByRole('button', { name: /^Copy/ })).toBeNull()
    }
  })

  it('switching tabs re-prefills the label until the user types their own', async () => {
    post.mockResolvedValue(mintResponse())
    harness()
    await ready()
    const name = screen.getByRole('textbox', { name: 'Name' }) as HTMLInputElement
    fireEvent.click(screen.getByRole('tab', { name: 'Codex' }))
    expect(name.value).toMatch(/^Codex - /)
    fireEvent.change(name, { target: { value: 'Work laptop' } })
    fireEvent.click(screen.getByRole('tab', { name: 'Cursor' }))
    expect(name.value).toBe('Work laptop')
  })

  it('refuses a label the server would reject', async () => {
    harness()
    await ready()
    fireEvent.change(screen.getByRole('textbox', { name: 'Name' }), { target: { value: 'bad/label' } })
    expect((screen.getByRole('button', { name: 'Create connection' }) as HTMLButtonElement).disabled).toBe(true)
  })
})

describe('AgentsPage connected agents', () => {
  it('says so when nothing is connected', async () => {
    harness()
    await waitFor(() => expect(screen.getByText(/No agents connected yet/)).toBeTruthy())
  })

  it('lists a connection with its scope, client, use and expiry', async () => {
    info = { enabled: true, mcp_url: URL, tokens: [row({ scope: 'mcp_outreach', calls_7d: null })] }
    harness()
    const r = await screen.findByTestId('mcp-row-7')
    expect(r.textContent).toContain('Claude Code - macOS')
    expect(r.textContent).toContain('Content + outreach & leads')
    expect(r.textContent).toContain('claude-code')
    // Not counted is not zero.
    expect(r.textContent).toContain('calls not counted')
  })

  it('rotates under step-up and shows the new token once', async () => {
    info = { enabled: true, mcp_url: URL, tokens: [row()] }
    post.mockResolvedValue(mintResponse('lem_mcp_ROTATED_EXAMPLE'))
    harness()
    fireEvent.click(await screen.findByRole('button', { name: 'Rotate Claude Code - macOS' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/user/mcp-token/rotate', {
      session_token: 'cookie', session_id: 7 }))
    await waitFor(() => expect(screen.getByLabelText('Your token').textContent).toBe('lem_mcp_ROTATED_EXAMPLE'))
    expect(captured).toContainEqual(['mcpTokenRotated', 'mcp'])
    expectTokenNowhereButTheDom('lem_mcp_ROTATED_EXAMPLE')
  })

  it('revokes through the existing sessions route, step-up guarded', async () => {
    info = { enabled: true, mcp_url: URL, tokens: [row()] }
    post
      .mockRejectedValueOnce(stepUpRefusal())
      .mockResolvedValueOnce({ data: { detail: { verified: true } } })
      .mockResolvedValueOnce({ data: { detail: { revoked: 1 } } })
    harness()
    fireEvent.click(await screen.findByRole('button', { name: 'Revoke Claude Code - macOS' }))
    await waitFor(() => expect(screen.getByText("Confirm it's you")).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Authenticator code'), { target: { value: '123456' } })
    fireEvent.click(screen.getByText('Confirm'))
    await waitFor(() => expect(screen.getByText(/Revoked Claude Code - macOS/)).toBeTruthy())
    const revokes = post.mock.calls.filter((c) => c[0] === '/user/sessions/revoke')
    expect(revokes.length).toBe(2)
    expect(revokes[1][1]).toEqual({ session_token: 'cookie', session_id: 7 })
    expect(captured).toContainEqual(['mcpTokenRevoked', 'mcp'])
  })

  it('greys an expired row, and asks for a rotate when every row has expired', async () => {
    info = { enabled: true, mcp_url: URL, tokens: [row({ expires_at: '2020-01-01T00:00:00Z' })] }
    harness()
    const r = await screen.findByTestId('mcp-row-7')
    expect(r.className).toContain('opacity-60')
    expect(screen.getByText(/Every connection has expired. Rotate one/)).toBeTruthy()
  })
})

describe('AgentsPage error and off states', () => {
  it('hides the commands with a retry when mcp-info fails', async () => {
    info = new Error('down')
    harness()
    // The hook retries once (about a second) before calling it a failure.
    await waitFor(() => expect(screen.getByText(/connect commands are hidden/)).toBeTruthy(), { timeout: 4000 })
    expect(screen.queryByText('Connect your agent')).toBeNull()
    info = { enabled: true, mcp_url: URL, tokens: [] }
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(screen.getByText('Connect your agent')).toBeTruthy())
  })

  it('shows a banner when the server says connections are switched off', async () => {
    info = { enabled: false, mcp_url: URL, tokens: [] }
    harness()
    await waitFor(() => expect(screen.getByText(/Agent connections are switched off right now/)).toBeTruthy())
  })

  it('never puts the token in a URL — mint is a POST body, info a token-free GET', async () => {
    post.mockResolvedValue(mintResponse())
    harness()
    await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Create connection' }))
    await waitFor(() => expect(screen.getByText(/Shown once/)).toBeTruthy())
    for (const [path] of [...get.mock.calls, ...post.mock.calls]) expect(String(path)).not.toContain(TOKEN)
  })
})
