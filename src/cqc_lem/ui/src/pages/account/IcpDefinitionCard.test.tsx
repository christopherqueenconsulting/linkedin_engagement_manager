import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import IcpDefinitionCard from './IcpDefinitionCard'

const get = vi.fn()
const put = vi.fn()
vi.mock('../../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    put: (...args: unknown[]) => put(...args),
  },
}))
vi.mock('../../contexts/useAuth', () => ({ useAuth: () => ({ sessionToken: 'tok' }) }))
vi.mock('../../utils/analytics', () => ({
  maskProps: (className: string) => ({ className }),
  capture: vi.fn(),
  EVENTS: { prefsSaved: 'prefs_saved' },
}))

const SAVED = { segments: [{ id: 'segment-1', name: 'Founders', role_titles: ['founder'] }] }

const respond = (detail: Record<string, unknown>) =>
  get.mockResolvedValue({
    data: {
      detail: {
        icp_builder_enabled: false, icp_definition: null, draft: null, draft_sample_size: 0,
        ...detail,
      },
    },
  })

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}><IcpDefinitionCard /></QueryClientProvider>
  )
}

beforeEach(() => {
  get.mockReset()
  put.mockReset()
  put.mockResolvedValue({ data: {} })
})
afterEach(cleanup)

describe('IcpDefinitionCard', () => {
  it('renders the saved definition and flag', async () => {
    respond({ icp_builder_enabled: true, icp_definition: SAVED })
    mount()
    expect(await screen.findByDisplayValue('Founders')).toBeTruthy()
    expect((screen.getByTestId('icp-enabled') as HTMLInputElement).checked).toBe(true)
    expect(screen.queryByTestId('icp-draft-offer')).toBeNull()
  })

  it('offers the pre-fill draft only until the user accepts it', async () => {
    respond({
      draft: { segments: [{ id: 'draft-1', name: 'Draft', role_titles: ['CTO'], industries: ['Software'] }] },
      draft_sample_size: 3,
    })
    mount()
    const offer = await screen.findByTestId('icp-draft-offer')
    expect(offer.textContent).toContain('CTO')
    expect(offer.textContent).toContain('3 people')
    fireEvent.click(screen.getByText('Use this draft'))
    expect(screen.queryByTestId('icp-draft-offer')).toBeNull()
    expect(screen.getByDisplayValue('Draft')).toBeTruthy()
    expect(put).not.toHaveBeenCalled() // a draft is never saved until the user confirms
  })

  it('saves edited segments and the flag', async () => {
    respond({ icp_definition: SAVED })
    mount()
    fireEvent.change(await screen.findByLabelText('Segment name'), { target: { value: 'Operators' } })
    fireEvent.change(screen.getByLabelText('Industries'), { target: { value: 'saas, fintech' } })
    fireEvent.change(screen.getByLabelText('Company size min'), { target: { value: '10' } })
    fireEvent.click(screen.getByTestId('icp-enabled'))
    fireEvent.click(screen.getByText('Save Ideal Customer Profile'))
    await waitFor(() => expect(put).toHaveBeenCalled())
    const [url, body] = put.mock.calls[0]
    expect(url).toBe('/user/icp-definition')
    expect(body.session_token).toBe('tok')
    expect(body.icp_builder_enabled).toBe(true)
    expect(body.icp_definition.segments[0]).toMatchObject({
      id: 'segment-1', name: 'Operators', industries: ['saas', 'fintech'], company_size_min: 10,
    })
    expect(await screen.findByText('Ideal customer profile saved.')).toBeTruthy()
  })

  it('adds a segment with a fresh id and sends exclusions', async () => {
    respond({ icp_definition: SAVED })
    mount()
    await screen.findByDisplayValue('Founders')
    fireEvent.click(screen.getByText('+ Add segment'))
    expect(screen.getByTestId('icp-segment-segment-2')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Competitors (company names)'), { target: { value: 'Initech' } })
    fireEvent.click(screen.getByText('Save Ideal Customer Profile'))
    await waitFor(() => expect(put).toHaveBeenCalled())
    const body = put.mock.calls[0][1]
    expect(body.icp_definition.segments.map((s: { id: string }) => s.id)).toEqual(['segment-1', 'segment-2'])
    expect(body.icp_definition.exclusions.competitors).toEqual(['Initech'])
  })

  it('removing every segment clears the stored definition with {}', async () => {
    respond({ icp_definition: SAVED })
    mount()
    await screen.findByDisplayValue('Founders')
    fireEvent.click(screen.getByLabelText('Remove segment Founders'))
    fireEvent.click(screen.getByText('Save Ideal Customer Profile'))
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0][1].icp_definition).toEqual({})
  })

  it('says so when the save fails', async () => {
    respond({ icp_definition: SAVED })
    put.mockRejectedValue(new Error('422'))
    mount()
    await screen.findByDisplayValue('Founders')
    fireEvent.click(screen.getByText('Save Ideal Customer Profile'))
    expect(await screen.findByText(/Could not save/)).toBeTruthy()
  })
})
