import { Link } from 'react-router-dom'
import { useAppInfo } from '../hooks/useAppInfo'

export default function Footer() {
  const { data } = useAppInfo()
  const year = new Date().getFullYear()

  return (
    <footer className="border-t border-gray-200 bg-white">
      <div className="max-w-5xl mx-auto px-4 py-4 flex flex-col sm:flex-row items-center justify-center gap-2 sm:gap-4 text-gray-400">
        <span className="text-xs">© {year} Christopher Queen Consulting. All rights reserved.</span>
        <div className="flex items-center gap-3 text-xs">
          <Link to="/privacy-policy" className="hover:text-gray-600 transition-colors">
            Privacy Policy
          </Link>
          <span aria-hidden="true" className="text-gray-300">|</span>
          <Link to="/terms-and-conditions" className="hover:text-gray-600 transition-colors">
            Terms and Conditions
          </Link>
        </div>
        {data?.show_version && data.version && (
          <span className="text-[10px] leading-none text-gray-300">v{data.version}</span>
        )}
      </div>
    </footer>
  )
}
