import { useEffect, useRef } from 'react'

export type Placeholder = { token: string; desc: string }

// Clickable placeholder chips. Clicking a chip inserts its {token} at the cursor of whichever
// text field the user last focused WITHIN THIS CARD (scoped to the chips' own card so a chip can
// never write into another section's field). Works with React-controlled inputs by using the
// native value setter + dispatching an input event so onChange fires and state updates.
export default function PlaceholderChips({ placeholders }: { placeholders: Placeholder[] }) {
  const rootRef = useRef<HTMLDivElement | null>(null)
  const cardRef = useRef<HTMLElement | null>(null)
  const lastField = useRef<HTMLInputElement | HTMLTextAreaElement | null>(null)

  useEffect(() => {
    cardRef.current = rootRef.current?.closest('.bg-white') as HTMLElement | null
    const onFocusIn = (e: FocusEvent) => {
      const t = e.target as HTMLElement
      if (!t || !cardRef.current?.contains(t)) return
      const isText = t.tagName === 'TEXTAREA' ||
        (t.tagName === 'INPUT' && ['text', 'url', 'search', ''].includes((t as HTMLInputElement).type))
      if (isText) lastField.current = t as HTMLInputElement | HTMLTextAreaElement
    }
    document.addEventListener('focusin', onFocusIn)
    return () => document.removeEventListener('focusin', onFocusIn)
  }, [])

  const insert = (token: string) => {
    const el = lastField.current
    if (!el || !cardRef.current?.contains(el)) return
    const start = el.selectionStart ?? el.value.length
    const end = el.selectionEnd ?? el.value.length
    const next = el.value.slice(0, start) + token + el.value.slice(end)
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set
    setter?.call(el, next)
    el.dispatchEvent(new Event('input', { bubbles: true }))
    const pos = start + token.length
    requestAnimationFrame(() => { el.focus(); el.setSelectionRange(pos, pos) })
  }

  return (
    <div ref={rootRef} className="flex flex-wrap gap-1.5">
      {placeholders.map((p) => (
        <button
          key={p.token}
          type="button"
          title={p.desc}
          // preventDefault on mousedown keeps the field focused so selectionStart is valid on click
          onMouseDown={(e) => e.preventDefault()}
          onClick={() => insert(p.token)}
          className="inline-flex items-center gap-1 bg-gray-100 hover:bg-blue-100 text-gray-700 hover:text-blue-700 border border-gray-200 rounded-full px-2 py-0.5 text-xs font-mono transition-colors"
        >
          {p.token}
        </button>
      ))}
    </div>
  )
}
