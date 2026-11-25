import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { AlignAccountCard } from './PromptCards'
import { ALIGN_ACCOUNT_PROMPT } from './clientSnippets'

afterEach(cleanup)

describe('AlignAccountCard (#2802)', () => {
  it('shows the card, its prompt, and who stays in control', () => {
    render(<AlignAccountCard />)
    expect(screen.getByRole('heading', { name: 'Align my account' })).toBeTruthy()
    expect(screen.getByText(/You see each change before it is made/)).toBeTruthy()
    expect(screen.getByText(ALIGN_ACCOUNT_PROMPT.text)).toBeTruthy()
  })

  it('names the server prompt and carries no token or connect command', () => {
    expect(ALIGN_ACCOUNT_PROMPT.text).toContain('align_my_account')
    expect(ALIGN_ACCOUNT_PROMPT.text).not.toMatch(
      /lem_mcp_|<YOUR_TOKEN>|LEM_MCP_TOKEN|Bearer|mcp add|--header|export /,
    )
  })
})
