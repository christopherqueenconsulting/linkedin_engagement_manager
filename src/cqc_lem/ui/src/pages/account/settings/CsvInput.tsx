import { useEffect, useRef, useState, type InputHTMLAttributes } from 'react'
import { csv, parseCsv } from '../types'
import { inputClass } from './Field'

type Props = {
  value: string[] | undefined | null
  onChange: (values: string[]) => void
} & Omit<InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange' | 'type'>

/**
 * A comma-separated list box you can actually type a comma into. Binding the input straight to
 * `csv(list)` round-tripped every keystroke through `parseCsv`, which trims and drops empty
 * entries — so "ai," and "ai, " both collapsed back to "ai" before the next character landed and
 * the separator could never be entered (issue #638). The raw text lives here; only the parsed
 * array is published upward, and the value is tidied on blur.
 */
export default function CsvInput({ value, onChange, className = inputClass, ...rest }: Props) {
  const incoming = csv(value)
  const [text, setText] = useState(incoming)
  // The canonical form of what WE last published. A prop equal to it is our own edit coming back
  // around, and overwriting the box with it is exactly what ate the comma.
  const echoed = useRef(incoming)

  useEffect(() => {
    if (incoming === echoed.current) return
    echoed.current = incoming
    setText(incoming)
  }, [incoming])

  const publish = (next: string) => {
    setText(next)
    const parsed = parseCsv(next)
    echoed.current = csv(parsed)
    onChange(parsed)
  }

  return (
    <input
      type="text"
      value={text}
      className={className}
      onChange={(e) => publish(e.target.value)}
      onBlur={() => setText(echoed.current)}
      {...rest}
    />
  )
}
