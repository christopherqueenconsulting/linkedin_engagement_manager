import axios from 'axios'

const api = axios.create({
  baseURL: '/api',
  headers: { 'Content-Type': 'application/json' },
})

// Build-time API access token. Empty in local/dev (gate disabled server-side);
// set via VITE_API_TOKEN for deployments that enforce bearer auth on /api.
const apiToken = import.meta.env.VITE_API_TOKEN

api.interceptors.request.use((config) => {
  if (apiToken) {
    config.headers['Authorization'] = `Bearer ${apiToken}`
  }
  // Since #745 (2b) the session normally rides in an httpOnly cookie the browser attaches itself,
  // and localStorage holds only the 'cookie' sentinel — there is nothing to put in this header.
  // It is still sent when a real token is held (cookie-less fallback, tutorial capture harness).
  const token = localStorage.getItem('lem_session')
  if (token && token !== 'cookie') {
    config.headers['X-Session-Token'] = token
  }
  return config
})

api.interceptors.response.use(
  (r) => r,
  (error) => {
    if (error.response?.status === 401) {
      localStorage.removeItem('lem_session')
      localStorage.removeItem('lem_email')
      window.location.href = '/'
    }
    return Promise.reject(error)
  }
)

export default api
