import { describe, expect, it, vi, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import CallIngestPanel from './CallIngestPanel'

const get = vi.fn()
const post = vi.fn()
const del = vi.fn()
vi.mock('../../api/client', () => ({
  default: {
    get: (...a: unknown[]) => get(...a),
    post: (...a: unknown[]) => post(...a),
    delete: (...a: unknown[]) => del(...a),
  },
}))
vi.mock('../../contexts/useAuth', () => ({ useAuth: () => ({ sessionToken: 'tok' }) }))
vi.mock('../../utils/analytics', () => ({ maskProps: (className: string) => ({ className }) }))

const cand = (id: number, ingest_id: number, confidence: number) => ({
  id, ingest_id, kind: 'anecdote', title: `T${id}`, body: `Body ${id}`,
  source_quote: `quote ${id}`, confidence,
})

const serve = (candidates: unknown[], calls: unknown[] = []) =>
  get.mockImplementation((url: string) =>
    Promise.resolve({
      data: { detail: url.includes('/candidates') ? { candidates } : { calls } },
    }))

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

afterEach(() => {
  cleanup(); get.mockReset(); post.mockReset(); del.mockReset(); vi.restoreAllMocks()
})

const upload = () => screen.getByText('Upload transcript') as HTMLButtonElement
const pickFile = () =>
  fireEvent.change(screen.getByLabelText('Transcript file'), {
    target: { files: [new File(['x'], 'call.vtt', { type: 'text/vtt' })] },
  })

describe('CallIngestPanel opt-in gate', () => {
  it('cannot upload until the consent attestation is ticked', async () => {
    serve([])
    harness(<CallIngestPanel />)
    pickFile()
    expect(upload().disabled).toBe(true)
    fireEvent.click(screen.getByLabelText('Consent attestation'))
    expect(upload().disabled).toBe(false)
    expect(post).not.toHaveBeenCalled()
  })

  it('registers the source with the attestation, then uploads', async () => {
    serve([])
    post.mockImplementation((url: string) =>
      Promise.resolve({ data: { detail: url.endsWith('/source') ? { source_id: 7 } : { ingest_id: 1 } } }))
    harness(<CallIngestPanel />)
    pickFile()
    fireEvent.click(screen.getByLabelText('Consent attestation'))
    fireEvent.click(upload())
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2))
    expect(post.mock.calls[0][0]).toBe('/user/call-ingest/source')
    expect(post.mock.calls[0][1]).toMatchObject({ source_type: 'otter', consent_attested: true })
    const form = post.mock.calls[1][1] as FormData
    expect(form.get('source_id')).toBe('7')
  })

  it('asks "Is this you?" on a 409 and re-uploads with the chosen speaker', async () => {
    serve([])
    post.mockResolvedValueOnce({ data: { detail: { source_id: 3 } } })
      .mockRejectedValueOnce(Object.assign(new Error('409'), {
        isAxiosError: true,
        response: { status: 409, data: { detail: { needs_own_speaker: true, speakers: ['Ann', 'Bob'] } } },
      }))
      .mockResolvedValueOnce({ data: { detail: { ingest_id: 2 } } })
    harness(<CallIngestPanel />)
    pickFile()
    fireEvent.click(screen.getByLabelText('Consent attestation'))
    fireEvent.click(upload())
    const speaker = await screen.findByRole('button', { name: 'Bob' }) as HTMLButtonElement
    await waitFor(() => expect(speaker.disabled).toBe(false))
    fireEvent.click(speaker)
    await waitFor(() => expect(post).toHaveBeenCalledTimes(3))
    const form = post.mock.calls[post.mock.calls.length - 1][1] as FormData
    expect(form.get('own_speaker')).toBe('Bob')
    // The source is reused, not re-registered.
    expect(post.mock.calls.filter((c) => String(c[0]).endsWith('/source'))).toHaveLength(1)
  })
})

describe('CallIngestPanel audio', () => {
  const selectAudio = () => fireEvent.change(screen.getByLabelText('Source'), { target: { value: 'audio' } })
  const selectAudioFile = (label: string, name: string) => {
    const file = new File(['audio'], name, { type: 'audio/wav' })
    fireEvent.change(screen.getByLabelText(label), { target: { files: [file] } })
    return file
  }
  const recordingUpload = () => screen.getByRole('button', { name: 'Upload recording' }) as HTMLButtonElement
  const enroll = () => screen.getByRole('button', { name: 'Enroll voice' }) as HTMLButtonElement

  it('enrolls a voice sample separately from the call recording', async () => {
    serve([])
    post.mockResolvedValueOnce({ data: { detail: { enrolled: true } } })
    harness(<CallIngestPanel />)
    selectAudio()
    expect(enroll().disabled).toBe(true)
    const sample = selectAudioFile('Voice sample', 'sample.wav')
    fireEvent.click(enroll())
    await screen.findByText('Voice enrolled. You can now upload a recording.')
    expect(post).toHaveBeenCalledTimes(1)
    const [url, form] = post.mock.calls[0]
    expect(url).toBe('/user/call-ingest/enroll')
    expect(form.get('session_token')).toBe('tok')
    expect(form.get('file')).toBe(sample)
  })

  it('registers an audio source with consent and uploads the recording', async () => {
    serve([])
    post.mockResolvedValueOnce({ data: { detail: { source_id: 8 } } })
      .mockResolvedValueOnce({ data: { detail: { ingest_id: 3 } } })
    harness(<CallIngestPanel />)
    selectAudio()
    const recording = selectAudioFile('Recording file', 'call.wav')
    expect(recordingUpload().disabled).toBe(true)
    fireEvent.click(screen.getByLabelText('Consent attestation'))
    fireEvent.click(recordingUpload())
    await screen.findByText(/Recording received/)
    expect(post).toHaveBeenNthCalledWith(1, '/user/call-ingest/source', {
      session_token: 'tok', source_type: 'audio', consent_attested: true,
    })
    const [url, form] = post.mock.calls[1]
    expect(url).toBe('/user/call-ingest/upload')
    expect(form.get('session_token')).toBe('tok')
    expect(form.get('source_id')).toBe('8')
    expect(form.get('source_type')).toBe('audio')
    expect(form.get('file')).toBe(recording)
    expect(form.has('own_speaker')).toBe(false)
  })

  it('prompts enrollment on a 409 and retains the recording for retry', async () => {
    serve([])
    post.mockResolvedValueOnce({ data: { detail: { source_id: 8 } } })
      .mockRejectedValueOnce(Object.assign(new Error('409'), {
        isAxiosError: true,
        response: { status: 409, data: { detail: { needs_enrollment: true } } },
      }))
      .mockResolvedValueOnce({ data: { detail: { enrolled: true } } })
      .mockResolvedValueOnce({ data: { detail: { ingest_id: 3 } } })
    harness(<CallIngestPanel />)
    selectAudio()
    const recording = selectAudioFile('Recording file', 'call.wav')
    fireEvent.click(screen.getByLabelText('Consent attestation'))
    fireEvent.click(recordingUpload())
    expect((await screen.findByRole('alert')).textContent).toContain('Enroll your voice')
    expect(screen.queryByTestId('own-speaker')).toBeNull()
    expect(recordingUpload().disabled).toBe(true)

    selectAudioFile('Voice sample', 'sample.wav')
    fireEvent.click(enroll())
    await screen.findByText('Voice enrolled. You can now upload a recording.')
    expect(screen.queryByRole('alert')).toBeNull()
    fireEvent.click(recordingUpload())
    await screen.findByText(/Recording received/)
    expect(post.mock.calls.map(([url]) => url)).toEqual([
      '/user/call-ingest/source', '/user/call-ingest/upload',
      '/user/call-ingest/enroll', '/user/call-ingest/upload',
    ])
    const retry = post.mock.calls[3][1] as FormData
    expect(retry.get('file')).toBe(recording)
    expect(retry.get('source_id')).toBe('8')
  })
})

describe('CallIngestPanel review', () => {
  it('groups candidates by call', async () => {
    serve([cand(1, 10, 0.9), cand(2, 11, 0.5)],
      [{ id: 10, title: 'Intro call', status: 'extracted', pending_count: 1 }])
    harness(<CallIngestPanel />)
    await waitFor(() => expect(screen.getByTestId('call-review')).toBeTruthy())
    expect(screen.getByText('Intro call')).toBeTruthy()
    expect(screen.getByText('Call #11')).toBeTruthy()
  })

  it('accept, edit and reject post the matching decision', async () => {
    serve([cand(1, 10, 0.9), cand(2, 10, 0.5), cand(3, 10, 0.4)])
    post.mockResolvedValue({ data: { detail: {} } })
    harness(<CallIngestPanel />)
    await waitFor(() => expect(screen.getByTestId('candidate-1')).toBeTruthy())
    fireEvent.click(screen.getAllByText('Accept')[0])
    await waitFor(() => expect(post).toHaveBeenCalledWith('/user/call-ingest/review',
      expect.objectContaining({ candidate_id: 1, decision: 'accept' })))
    fireEvent.click(screen.getAllByText('Reject')[0])
    await waitFor(() => expect(post).toHaveBeenCalledWith('/user/call-ingest/review',
      expect.objectContaining({ decision: 'reject' })))
    fireEvent.click(screen.getAllByText('Edit')[0])
    fireEvent.change(screen.getByLabelText('Edit candidate'), { target: { value: 'Better body' } })
    fireEvent.click(screen.getByText('Save edit'))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/user/call-ingest/review',
      expect.objectContaining({ decision: 'edit', edited_body: 'Better body' })))
  })

  it('bulk accepts at 0.8 and counts only the candidates above it', async () => {
    serve([cand(1, 10, 0.9), cand(2, 10, 0.8), cand(3, 10, 0.4)])
    post.mockResolvedValue({ data: { detail: { accepted: 2 } } })
    harness(<CallIngestPanel />)
    const button = await screen.findByText(/Accept all above 0.8 confidence \(2\)/)
    fireEvent.click(button)
    await waitFor(() => expect(post).toHaveBeenCalledWith('/user/call-ingest/accept-bulk',
      expect.objectContaining({ min_confidence: 0.8 })))
  })

  it('keyboard: j moves down, a accepts the focused row, r rejects it', async () => {
    serve([cand(1, 10, 0.9), cand(2, 10, 0.5)])
    post.mockResolvedValue({ data: { detail: {} } })
    harness(<CallIngestPanel />)
    const region = await screen.findByTestId('call-review')
    fireEvent.keyDown(region, { key: 'j' })
    fireEvent.keyDown(region, { key: 'a' })
    await waitFor(() => expect(post).toHaveBeenCalledWith('/user/call-ingest/review',
      expect.objectContaining({ candidate_id: 2, decision: 'accept' })))
    fireEvent.keyDown(region, { key: 'k' })
    fireEvent.keyDown(region, { key: 'r' })
    await waitFor(() => expect(post).toHaveBeenCalledWith('/user/call-ingest/review',
      expect.objectContaining({ candidate_id: 1, decision: 'reject' })))
  })

  it('keys typed into the edit box never review a row', async () => {
    serve([cand(1, 10, 0.9)])
    post.mockResolvedValue({ data: { detail: {} } })
    harness(<CallIngestPanel />)
    fireEvent.click(await screen.findByText('Edit'))
    fireEvent.keyDown(screen.getByLabelText('Edit candidate'), { key: 'a' })
    expect(post).not.toHaveBeenCalled()
  })
})

describe('CallIngestPanel purge', () => {
  it('purges all call data only after confirmation', async () => {
    serve([])
    del.mockResolvedValue({ data: { detail: 'ok' } })
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true)
    harness(<CallIngestPanel />)
    fireEvent.click(screen.getByText('Purge all call data'))
    expect(del).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('Purge all call data'))
    await waitFor(() => expect(del).toHaveBeenCalledWith('/user/call-ingest',
      { data: { session_token: 'tok' } }))
    expect(confirm).toHaveBeenCalledTimes(2)
  })
})
