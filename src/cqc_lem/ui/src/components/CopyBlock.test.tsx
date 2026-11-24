import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import CopyBlock from './CopyBlock'
import { MASK_ATTRIBUTE, MASK_CLASS } from '../utils/analytics'

function stubClipboard(writeText: (t: string) => Promise<void>) {
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
}

afterEach(cleanup)

describe('CopyBlock (issue #2687)', () => {
  it('is masked for replay and autocapture by default', () => {
    const { container } = render(<CopyBlock label="Terminal" text="export LEM_MCP_TOKEN=secret" />)
    const root = container.firstElementChild as HTMLElement
    expect(root.getAttribute(MASK_ATTRIBUTE)).toBe('true')
    expect(root.className).toContain(MASK_CLASS)
  })

  it('can opt out of masking explicitly', () => {
    const { container } = render(<CopyBlock label="Prompt" text="hello" mask={false} />)
    const root = container.firstElementChild as HTMLElement
    expect(root.getAttribute(MASK_ATTRIBUTE)).toBeNull()
    expect(root.className).not.toContain(MASK_CLASS)
  })

  it('shows the warning header, the file and the text', () => {
    render(<CopyBlock label="Config file" file="~/.cursor/mcp.json" text="{}" warning="never into chat" />)
    expect(screen.getByText('~/.cursor/mcp.json')).toBeTruthy()
    expect(screen.getByText('never into chat')).toBeTruthy()
    expect(screen.getByLabelText('Config file').textContent).toBe('{}')
  })

  it('copies the exact text and reports success without handing the text to the callback', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    stubClipboard(writeText)
    const onCopied = vi.fn()
    render(<CopyBlock label="Terminal" text="claude mcp add" onCopied={onCopied} />)
    fireEvent.click(screen.getByRole('button', { name: 'Copy Terminal' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Copied Terminal' })).toBeTruthy())
    expect(writeText).toHaveBeenCalledWith('claude mcp add')
    expect(onCopied).toHaveBeenCalledTimes(1)
    expect(onCopied.mock.calls[0]).toEqual([])
    expect(screen.getByRole('status').textContent).toBe('Copied to clipboard.')
  })

  it('a refused clipboard says so, selects the text for a hand copy, and fires no copied event', async () => {
    stubClipboard(vi.fn().mockRejectedValue(new Error('denied')))
    const onCopied = vi.fn()
    render(<CopyBlock label="Terminal" text="claude mcp add" onCopied={onCopied} />)
    fireEvent.click(screen.getByRole('button', { name: 'Copy Terminal' }))
    await waitFor(() => expect(screen.getByRole('status').textContent).toMatch(/Could not copy/))
    expect(onCopied).not.toHaveBeenCalled()
    expect(window.getSelection()?.toString()).toBe('claude mcp add')
  })
})
