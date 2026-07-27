import { useState } from 'react'
import { describe, expect, it, afterEach, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import CsvInput from './CsvInput'

afterEach(cleanup)

// The real wiring: the parsed array is state OWNED BY THE PARENT and handed straight back down,
// which is what used to erase the separator on every keystroke (issue #638).
function Harness({ initial = [], onChange }: { initial?: string[]; onChange?: (v: string[]) => void }) {
  const [values, setValues] = useState<string[]>(initial)
  return (
    <>
      <CsvInput
        aria-label="topics"
        value={values}
        onChange={(v) => {
          setValues(v)
          onChange?.(v)
        }}
      />
      <span data-testid="parsed">{JSON.stringify(values)}</span>
    </>
  )
}

const box = () => screen.getByLabelText('topics') as HTMLInputElement

describe('CsvInput', () => {
  it('lets a comma be typed instead of collapsing it back to the previous entry', () => {
    render(<Harness />)
    fireEvent.change(box(), { target: { value: 'ai' } })
    fireEvent.change(box(), { target: { value: 'ai,' } })
    expect(box().value).toBe('ai,')
  })

  it('keeps the space after a comma while the next entry is being typed', () => {
    render(<Harness initial={['ai']} />)
    fireEvent.change(box(), { target: { value: 'ai, ' } })
    expect(box().value).toBe('ai, ')
    fireEvent.change(box(), { target: { value: 'ai, sales' } })
    expect(box().value).toBe('ai, sales')
    expect(screen.getByTestId('parsed').textContent).toBe('["ai","sales"]')
  })

  it('publishes trimmed values with the empties dropped', () => {
    const onChange = vi.fn()
    render(<Harness onChange={onChange} />)
    fireEvent.change(box(), { target: { value: ' ai ,, sales ' } })
    expect(onChange).toHaveBeenLastCalledWith(['ai', 'sales'])
  })

  it('renders the saved list as comma-separated text', () => {
    render(<Harness initial={['ai', 'sales']} />)
    expect(box().value).toBe('ai, sales')
  })

  it('tidies the text on blur once the user is done typing', () => {
    render(<Harness />)
    fireEvent.change(box(), { target: { value: 'ai,, sales ,' } })
    fireEvent.blur(box())
    expect(box().value).toBe('ai, sales')
  })

  it('adopts a value changed from outside (preferences loading, a one-click fix)', () => {
    function External() {
      const [values, setValues] = useState<string[]>(['ai'])
      return (
        <>
          <CsvInput aria-label="topics" value={values} onChange={setValues} />
          <button onClick={() => setValues(['leadership', 'hiring'])}>replace</button>
        </>
      )
    }
    render(<External />)
    fireEvent.click(screen.getByRole('button', { name: 'replace' }))
    expect(box().value).toBe('leadership, hiring')
  })

  it('starts empty for an unset list rather than rendering null', () => {
    render(<CsvInput aria-label="topics" value={null} onChange={() => {}} />)
    expect(box().value).toBe('')
  })
})
