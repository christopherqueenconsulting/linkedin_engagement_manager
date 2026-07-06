import { useAppInfo } from '../hooks/useAppInfo'

export default function Footer() {
  const { data } = useAppInfo()
  const year = new Date().getFullYear()

  return (
    <footer className="border-t border-gray-200 bg-white">
      <div className="max-w-5xl mx-auto px-4 py-4 flex items-center justify-center gap-2 text-gray-400">
        <span className="text-xs">© {year} Christopher Queen Consulting. All rights reserved.</span>
        {data?.show_version && data.version && (
          <span className="text-[10px] leading-none text-gray-300">v{data.version}</span>
        )}
      </div>
    </footer>
  )
}
