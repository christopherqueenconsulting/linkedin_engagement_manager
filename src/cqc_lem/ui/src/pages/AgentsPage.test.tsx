import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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
    revoked: false,
    expired: false,
    last_client_name: 'claude-code',
    ...overrides,
  }
}

let info: { enabled: boolean; mcp_url: string | null; tokens: Row[] } | Error
let strong = true

function mintResponse(token = TOKEN) {
  return {
    data: { detail: { session_token: token, session_id: 9, mcp_url: URL, expires_at: '2026-12-24T00:00:00Z',
      label: 'Claude Code - Linux', scope: 'mcp', ttl_days: 30 } },
  }
}

function factorRefusal() {
  return { response: { status: 403, data: { detail: {
    code: 'mcp_needs_strong_factor',
    message: 'Add a passkey or an authenticator app before connecting an agent.',
  } } } }
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

// MCP W2: the activity card's rows (`GET /user/mcp-token/activity`).
let activity: unknown[] = []

beforeEach(() => {
  get.mockReset()
  post.mockReset()
  captured.length = 0
  flagState.isLoading = false
  flagState.on = true
  strong = true
  info = { enabled: true, mcp_url: URL, tokens: [] }
  activity = []
  get.mockImplementation((path: string) => {
    if (path.startsWith('/user/mcp-token/info')) {
      return info instanceof Error ? Promise.reject(info) : Promise.resolve({ data: { detail: info } })
    }
    if (path.startsWith('/user/auth-factors')) {
      return Promise.resolve({ data: { detail: { has_strong_factor: strong } } })
    }
    if (path.startsWith('/user/settings-proposals')) {
      return Promise.resolve({ data: { detail: { proposals: [], count: 0 } } })
    }
    if (path.startsWith('/user/mcp-token/activity')) {
      return Promise.resolve({ data: { detail: { events: activity } } })
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
  it('sends a direct visit home when mcp-server-enabled is off and nothing is live', async () => {
    flagState.on = false
    info = { enabled: false, mcp_url: URL, tokens: [row({ expired: true }), row({ session_id: 8, revoked: true })] }
    harness()
    await waitFor(() => expect(screen.getByText('Home page')).toBeTruthy())
    expect(screen.queryByRole('heading', { name: 'Agents' })).toBeNull()
    // Nothing that could mint was ever asked for.
    expect(get.mock.calls.some((c) => String(c[0]).startsWith('/user/auth-factors'))).toBe(false)
  })

  it('flag off with a live token: a read-only list that can still revoke it, and nothing else', async () => {
    flagState.on = false
    info = { enabled: false, mcp_url: URL, tokens: [row()] }
    post.mockResolvedValue({ data: { detail: { revoked: 1 } } })
    harness()
    const r = await screen.findByTestId('mcp-row-7')
    expect(screen.getByText(/Agent connections are turned off for your account/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Create connection' })).toBeNull()
    expect(screen.queryByText('Connect your agent')).toBeNull()
    expect(within(r).queryByRole('button', { name: /^Rotate/ })).toBeNull()
    fireEvent.click(within(r).getByRole('button', { name: 'Revoke Claude Code - macOS' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/user/sessions/revoke', {
      session_token: 'cookie', session_id: 7 }))
  })

  it('the off banner states what the server reported, not a cause', async () => {
    info = { enabled: false, mcp_url: URL, tokens: [] }
    harness()
    const banner = await screen.findByText(/Agent connections are turned off for your account/)
    expect(banner.textContent).not.toMatch(/operator|kill|maintenance|outage/i)
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
    // Single-quoted: Claude Code stores the NAME and expands it at load, never the token itself.
    expect(cli).toContain("--header 'Authorization: Bearer ${LEM_MCP_TOKEN}'")
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

  it('403 mcp_needs_strong_factor swaps Create for the enrol hint, with no step-up ceremony', async () => {
    // The server checks the factor BEFORE step-up (P2), so this refusal must never open the modal.
    // The cached factors read said yes (stale); the server, and every read after it, says no.
    post.mockImplementation(() => {
      strong = false
      return Promise.reject(factorRefusal())
    })
    harness()
    await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Create connection' }))
    await waitFor(() => expect(screen.getByRole('link', { name: 'Enrol a passkey or authenticator first' }))
      .toBeTruthy())
    expect(screen.queryByRole('button', { name: 'Create connection' })).toBeNull()
    expect(screen.queryByText("Confirm it's you")).toBeNull()
    expect(post).toHaveBeenCalledTimes(1)
  })

  it('the refusal stands only until a fresher factors read: enrolling brings Create back', async () => {
    post.mockRejectedValue(factorRefusal())
    harness()
    await ready()
    // A refusal the factors read immediately contradicts would hide nothing, so pin the read to
    // "no" for the refetch the 403 triggers, then enrol.
    strong = false
    fireEvent.click(screen.getByRole('button', { name: 'Create connection' }))
    await screen.findByRole('link', { name: 'Enrol a passkey or authenticator first' })

    strong = true
    await act(() => client.invalidateQueries({ queryKey: ['auth-factors'] }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Create connection' })).toBeTruthy())
    expect(screen.queryByRole('link', { name: 'Enrol a passkey or authenticator first' })).toBeNull()
  })

  it('the 403 re-reads the factors rather than trusting a stale cache', async () => {
    post.mockRejectedValue(factorRefusal())
    harness()
    await ready()
    const before = get.mock.calls.filter((c) => String(c[0]).startsWith('/user/auth-factors')).length
    fireEvent.click(screen.getByRole('button', { name: 'Create connection' }))
    await waitFor(() => expect(get.mock.calls.filter((c) => String(c[0]).startsWith('/user/auth-factors')).length)
      .toBeGreaterThan(before))
  })

  it('429 mcp_mint_rate_limited asks the user to wait', async () => {
    post.mockRejectedValue({ response: { status: 429, data: { detail: { code: 'mcp_mint_rate_limited' } } } })
    harness()
    await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Create connection' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/Try again later/))
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
    expect(screen.getByText(/Rotate a live connection below, or create a new one, to get a paste-ready command/)).toBeTruthy()
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
    const inlineHeader = `--header 'Authorization: Bearer ${TOKEN}'`
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

describe('AgentsPage show-once ties to its row', () => {
  async function mintNine() {
    post.mockResolvedValueOnce(mintResponse())
    harness()
    await ready()
    // The list now holds the new connection (session 9), as the refresh after a mint returns it.
    info = { enabled: true, mcp_url: URL, tokens: [row({ session_id: 9, label: 'New one' })] }
    fireEvent.click(screen.getByRole('button', { name: 'Create connection' }))
    await waitFor(() => expect(screen.getByLabelText('Your token').textContent).toBe(TOKEN))
    await screen.findByTestId('mcp-row-9')
  }

  it('revoking the row whose token is on screen takes the token off the screen', async () => {
    await mintNine()
    post.mockResolvedValueOnce({ data: { detail: { revoked: 1 } } })
    fireEvent.click(screen.getByRole('button', { name: 'Revoke New one' }))
    await waitFor(() => expect(screen.queryByLabelText('Your token')).toBeNull())
    expect(document.body.textContent).not.toContain(TOKEN)
  })

  it('a failed rotate of that row clears it too — the server revokes before it mints', async () => {
    await mintNine()
    post.mockRejectedValueOnce({ response: { status: 500, data: { detail: 'Could not create the agent token' } } })
    fireEvent.click(screen.getByRole('button', { name: 'Rotate New one' }))
    await waitFor(() => expect(screen.getByText(/Could not rotate that connection/)).toBeTruthy())
    expect(screen.queryByLabelText('Your token')).toBeNull()
  })

  it('revoking a DIFFERENT row leaves the shown token alone', async () => {
    await mintNine()
    info = { enabled: true, mcp_url: URL, tokens: [row({ session_id: 9, label: 'New one' }), row()] }
    await act(() => client.invalidateQueries({ queryKey: ['mcp-info'] }))
    await screen.findByTestId('mcp-row-7')
    post.mockResolvedValueOnce({ data: { detail: { revoked: 1 } } })
    fireEvent.click(screen.getByRole('button', { name: 'Revoke Claude Code - macOS' }))
    await waitFor(() => expect(screen.getByText(/Revoked Claude Code - macOS/)).toBeTruthy())
    expect(screen.getByLabelText('Your token').textContent).toBe(TOKEN)
  })
})

describe('AgentsPage shells and Claude Desktop', () => {
  const realUA = navigator.userAgent
  afterEach(() => {
    Object.defineProperty(window.navigator, 'userAgent', { value: realUA, configurable: true })
  })

  it('switches the commands to PowerShell on the toggle', async () => {
    post.mockResolvedValue(mintResponse())
    harness()
    await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Create connection' }))
    await waitFor(() => expect(screen.getByText(/Shown once/)).toBeTruthy())
    expect(screen.getByLabelText('Terminal command').textContent).toContain(`export LEM_MCP_TOKEN=${TOKEN}`)
    fireEvent.click(screen.getByRole('radio', { name: 'PowerShell' }))
    const cli = screen.getByLabelText('Terminal command').textContent ?? ''
    expect(cli).toContain(`$env:LEM_MCP_TOKEN = '${TOKEN}'`)
    expect(cli).not.toContain('export ')
  })

  it('defaults to PowerShell on a Windows browser', async () => {
    Object.defineProperty(window.navigator, 'userAgent', {
      value: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)', configurable: true })
    harness()
    await ready()
    expect((screen.getByRole('radio', { name: 'PowerShell' }) as HTMLInputElement).checked).toBe(true)
    expect(screen.getByLabelText('Terminal command').textContent).toContain("$env:LEM_MCP_TOKEN = '<YOUR_TOKEN>'")
  })

  it('Claude Desktop gets the fresh token in its config, and the copy records it as inline', async () => {
    post.mockResolvedValue(mintResponse())
    harness()
    await ready()
    fireEvent.click(screen.getByRole('button', { name: 'Create connection' }))
    await waitFor(() => expect(screen.getByText(/Shown once/)).toBeTruthy())
    fireEvent.click(screen.getByRole('tab', { name: 'Claude Desktop' }))
    const config = screen.getByLabelText('Config file').textContent ?? ''
    expect(JSON.parse(config).mcpServers.lem.env.AUTH_HEADER).toBe(`Bearer ${TOKEN}`)
    expect(screen.getByText(/stores your token in/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Copy Config file' }))
    await waitFor(() => expect(captured).toContainEqual(['mcpCommandCopied', 'claude-desktop', 'inline']))
    expectTokenNowhereButTheDom()
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

  it('greys an expired row with no actions — rotate only replaces a live token', async () => {
    info = { enabled: true, mcp_url: URL, tokens: [row({ expires_at: '2020-01-01T00:00:00Z', expired: true })] }
    harness()
    const r = await screen.findByTestId('mcp-row-7')
    expect(r.className).toContain('opacity-60')
    expect(within(r).getByText('Expired')).toBeTruthy()
    expect(within(r).queryByRole('button')).toBeNull()
    expect(screen.getByText(/None of these connections works any more. Create a new one above/)).toBeTruthy()
  })

  it("trusts the server's expired flag even when the clock disagrees", async () => {
    info = { enabled: true, mcp_url: URL, tokens: [row({ expired: true })] }
    harness()
    const r = await screen.findByTestId('mcp-row-7')
    expect(r.className).toContain('opacity-60')
  })

  it('greys a revoked row, badges it, and offers nothing', async () => {
    info = { enabled: true, mcp_url: URL, tokens: [row({ revoked: true }), row({ session_id: 8, label: 'Live one' })] }
    harness()
    const dead = await screen.findByTestId('mcp-row-7')
    expect(dead.className).toContain('opacity-60')
    expect(within(dead).getByText('Revoked')).toBeTruthy()
    expect(within(dead).queryByRole('button')).toBeNull()
    expect(within(screen.getByTestId('mcp-row-8')).getByRole('button', { name: 'Rotate Live one' })).toBeTruthy()
    // One live row left, so no "none of these works" prompt.
    expect(screen.queryByText(/None of these connections works/)).toBeNull()
  })

  it('names a row whose label is null', async () => {
    info = { enabled: true, mcp_url: URL, tokens: [row({ label: null })] }
    harness()
    const r = await screen.findByTestId('mcp-row-7')
    expect(r.textContent).toContain('Agent connection')
    expect(within(r).getByRole('button', { name: 'Revoke Agent connection' })).toBeTruthy()
  })

  it('a rotate the server answers 404 says the connection is gone and re-reads the list', async () => {
    info = { enabled: true, mcp_url: URL, tokens: [row()] }
    post.mockRejectedValue({ response: { status: 404, data: { detail: 'Agent token not found' } } })
    harness()
    fireEvent.click(await screen.findByRole('button', { name: 'Rotate Claude Code - macOS' }))
    await waitFor(() => expect(screen.getByText(/no longer live. Create a new one above/)).toBeTruthy())
    expect(screen.queryByLabelText('Your token')).toBeNull()
    const infoReads = get.mock.calls.filter((c) => String(c[0]).startsWith('/user/mcp-token/info')).length
    expect(infoReads).toBeGreaterThanOrEqual(2)
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

  it('says why there are no commands when the server has no public URL', async () => {
    info = { enabled: true, mcp_url: null, tokens: [] }
    harness()
    await waitFor(() => expect(screen.getByText(/Connect commands are unavailable right now/)).toBeTruthy())
    expect(screen.queryByText('Connect your agent')).toBeNull()
  })

  it('with no public URL, Create is disabled and says why — no token the user could not use', async () => {
    info = { enabled: true, mcp_url: null, tokens: [] }
    harness()
    await waitFor(() => expect(screen.getAllByText(/no public address to give your agent/).length).toBeGreaterThan(0))
    expect((screen.getByRole('button', { name: 'Create connection' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Create connection' }))
    expect(post).not.toHaveBeenCalled()
  })

  it('a minted token is shown even when the commands cannot render', async () => {
    // mcp-token/info failed, and the mint answered without a URL: the token must still be visible.
    info = new Error('down')
    post.mockResolvedValue({ data: { detail: { ...mintResponse().data.detail, mcp_url: null } } })
    harness()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Create connection' })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Create connection' }))
    await waitFor(() => expect(screen.getByLabelText('Your token').textContent).toBe(TOKEN))
    expect(screen.queryByText('Connect your agent')).toBeNull()
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

describe('AgentsPage activity (MCP W2)', () => {
  it('shows what the agents did once a connection exists', async () => {
    info = { enabled: true, mcp_url: URL, tokens: [row({})] }
    activity = [{ event: 'mcp_write', success: true, client: 'claude-code',
      details: { tool: 'lem_queue_dm', target_id: 5 }, created_at: '2026-11-24T03:00:00+00:00' }]
    harness()
    expect(await screen.findByText('Queued for your review: DM #5')).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Agent activity' })).toBeTruthy()
  })

  it('has no activity card before any connection exists', async () => {
    harness()
    await screen.findByRole('heading', { name: 'Agents' })
    expect(screen.queryByRole('heading', { name: 'Agent activity' })).toBeNull()
    expect(get.mock.calls.some((c) => String(c[0]).startsWith('/user/mcp-token/activity'))).toBe(false)
  })
})
