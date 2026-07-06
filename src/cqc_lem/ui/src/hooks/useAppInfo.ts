import { useQuery } from '@tanstack/react-query'
import api from '../api/client'

export interface AppInfo {
  version: string
  show_version: boolean
}

// Public app metadata for the footer: the running release version and whether to
// display it. Toggled server-side via the SHOW_VERSION_FOOTER env var.
export function useAppInfo() {
  return useQuery({
    queryKey: ['app-info'],
    queryFn: () => api.get('/app-info').then((r) => r.data.detail as AppInfo),
    staleTime: 60 * 60 * 1000,
  })
}
