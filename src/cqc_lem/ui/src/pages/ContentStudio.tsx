import { useState, useEffect } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from '../api/client'
import LinkedInPostPreview from '../components/LinkedInPostPreview'
import PostGateReason from '../components/PostGateReason'
import { gateHold } from '../utils/gateFindings'
import type { GateFinding } from '../utils/gateFindings'
import NewsletterQueue from './review/NewsletterQueue'
import ScheduledDMs from './review/ScheduledDMs'
import ConnectionRequests from './review/ConnectionRequests'
import OutreachFunnel from './review/OutreachFunnel'
import LeadsInbox from './review/LeadsInbox'
import LeadsPipeline from './review/LeadsPipeline'
import CatchupTouches from './review/CatchupTouches'
import ComposePost from './content/ComposePost'
import { useAuth } from '../contexts/AuthContext'
import { useUserTimezone } from '../hooks/useUserTimezone'
import { formatInTimezone, toZonedInputValue, zonedInputToUtcIso, zonedDateToUtcStart, zonedDateToUtcEnd } from '../utils/datetime'
import { emptyRunExplanation, isGenerationRunning } from '../utils/generationStatus'
import type { GenerationStatus } from '../utils/generationStatus'
import { EVENTS, capture, maskProps, recordPostApproval } from '../utils/analytics'

// Consolidated content hub: compose posts, schedule DMs, manage newsletters, and review/edit
// existing content — one page, four tabs, synced to ?tab= for deep-linking.
type View = 'posts' | 'dms' | 'connections' | 'catchup' | 'outreach' | 'leads' | 'pipeline' | 'newsletters' | 'review'
const VIEWS: { key: View; label: string }[] = [
  { key: 'posts', label: 'Posts' },
  { key: 'dms', label: 'DMs' },
  { key: 'connections', label: 'Connections' },
  { key: 'catchup', label: 'Catch-up' },
  { key: 'outreach', label: 'Outreach' },
  { key: 'leads', label: 'Leads' },
  { key: 'pipeline', label: 'Pipeline' },
  { key: 'newsletters', label: 'Newsletters' },
  { key: 'review', label: 'Review & Edit' },
]
const VIEW_KEYS = VIEWS.map((v) => v.key) as string[]

type Status = 'ALL' | 'pending' | 'approved' | 'scheduled' | 'posted' | 'rejected' | 'error'
const STATUSES: { label: string; value: Status }[] = [
  { label: 'ALL', value: 'ALL' },
  { label: 'PENDING', value: 'pending' },
  { label: 'APPROVED', value: 'approved' },
  { label: 'SCHEDULED', value: 'scheduled' },
  { label: 'POSTED', value: 'posted' },
  { label: 'REJECTED', value: 'rejected' },
  // Generation/posting failed (e.g. media never rendered) — needs a manual fix, so it gets
  // its own filter instead of hiding inside ALL.
  { label: 'ERROR', value: 'error' },
]

type PostTypeFilter = 'ALL' | 'text' | 'video' | 'carousel' | 'document'
const POST_TYPE_FILTERS: { label: string; value: PostTypeFilter }[] = [
  { label: 'All types', value: 'ALL' },
  { label: 'Text', value: 'text' },
  { label: 'Video', value: 'video' },
  { label: 'Carousel', value: 'carousel' },
  { label: 'Document', value: 'document' },
]

// Carousels and native documents share the same generated slide deck — a document post
// is published as a single PDF instead of a multi-image share.
const isDeckType = (postType: string) => postType === 'carousel' || postType === 'document'

type DateRangeFilter = 'ALL' | 'today' | 'yesterday' | 'last7days' | 'last30days' | 'thisMonth' | 'next30days' | 'custom'
const DATE_RANGE_FILTERS: { label: string; value: DateRangeFilter }[] = [
  { label: 'All dates', value: 'ALL' },
  { label: 'Today', value: 'today' },
  { label: 'Yesterday', value: 'yesterday' },
  { label: 'Last 7 days', value: 'last7days' },
  { label: 'Last 30 days', value: 'last30days' },
  { label: 'This month', value: 'thisMonth' },
  { label: 'Next 30 days', value: 'next30days' },
  { label: 'Custom', value: 'custom' },
]

type SortBy = 'scheduled_time' | 'status' | 'post_type' | 'id'
const SORT_BY_OPTIONS: { label: string; value: SortBy }[] = [
  { label: 'Scheduled time', value: 'scheduled_time' },
  { label: 'Status', value: 'status' },
  { label: 'Type', value: 'post_type' },
  { label: 'Created (ID)', value: 'id' },
]

const PAGE_SIZE_OPTIONS = [10, 25, 50]

// Return YYYY-MM-DD for *today* in the user's configured timezone so preset ranges
// line up with the wall clock the rest of the UI uses.
export function getTodayInTz(tz: string): string {
  try {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: tz,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).formatToParts(new Date())
    const get = (type: string) => parts.find((p) => p.type === type)?.value ?? '0'
    return `${get('year')}-${get('month')}-${get('day')}`
  } catch {
    return new Date().toISOString().slice(0, 10)
  }
}

// Given a preset (or custom) date range, return the user-timezone wall dates that bound it.
export function resolveDateRange(
  preset: DateRangeFilter,
  tz: string,
  customStart?: string,
  customEnd?: string,
): { start: string | null; end: string | null } {
  const today = getTodayInTz(tz)
  const [year, month, day] = today.split('-').map(Number)
  switch (preset) {
    case 'ALL':
      return { start: null, end: null }
    case 'today':
      return { start: today, end: today }
    case 'yesterday': {
      const d = new Date(Date.UTC(year, month - 1, day - 1))
      return { start: d.toISOString().slice(0, 10), end: d.toISOString().slice(0, 10) }
    }
    case 'last7days': {
      const d = new Date(Date.UTC(year, month - 1, day - 6))
      return { start: d.toISOString().slice(0, 10), end: today }
    }
    case 'last30days': {
      const d = new Date(Date.UTC(year, month - 1, day - 29))
      return { start: d.toISOString().slice(0, 10), end: today }
    }
    case 'thisMonth': {
      const start = `${year}-${String(month).padStart(2, '0')}-01`
      const end = new Date(Date.UTC(year, month, 0)).toISOString().slice(0, 10)
      return { start, end }
    }
    case 'next30days': {
      const d = new Date(Date.UTC(year, month - 1, day + 29))
      return { start: today, end: d.toISOString().slice(0, 10) }
    }
    case 'custom':
      return { start: customStart || null, end: customEnd || null }
    default:
      return { start: null, end: null }
  }
}

interface Post {
  post_id: number
  content: string
  video_url: string | null
  scheduled_time: string
  post_type: string
  status: string
  carousel_slides: string[] | null
  post_url?: string | null
  // The SHAPE the draft was written to (V51 posts.archetype) — reported with the review decision.
  archetype?: string | null
  // Quality-gate verdict (issue #421) — why a PENDING post is being held, and its A1 score.
  authenticity_score?: number | null
  gate_reason?: GateFinding[] | null
  // Why the user rejected/deleted this draft (issue #713) — used when regenerating.
  rejection_reason?: string | null
}

// What POST /user/post/rescore returns after re-running the gates on the saved content.
interface RescoreResult {
  passed: boolean
  status: string | null
  authenticity_score: number | null
  findings: GateFinding[]
  detail: string
}

interface PostsResponse {
  posts: Post[]
  total: number
  page: number
  page_size: number
}

const STATUS_COLORS: Record<string, string> = {
  approved: 'bg-green-100 text-green-700',
  pending: 'bg-yellow-100 text-yellow-700',
  posted: 'bg-blue-100 text-blue-700',
  rejected: 'bg-red-100 text-red-700',
  scheduled: 'bg-purple-100 text-purple-700',
  error: 'bg-red-100 text-red-700',
}

export default function ContentStudio() {
  const { user, sessionToken } = useAuth()
  const email = user?.email ?? ''
  const qc = useQueryClient()
  const userTimezone = useUserTimezone()

  // Top-level view (Posts / Newsletters), synced to the ?tab= query param for deep-linking.
  const [searchParams, setSearchParams] = useSearchParams()
  const tabParam = searchParams.get('tab') ?? ''
  const view: View = (VIEW_KEYS.includes(tabParam) ? tabParam : 'posts') as View
  const setView = (v: View) => setSearchParams(v === 'posts' ? {} : { tab: v }, { replace: true })

  // Filter / sort / pagination state
  const [filterStatus, setFilterStatus] = useState<Status>('ALL')
  const [filterPostType, setFilterPostType] = useState<PostTypeFilter>('ALL')
  const [dateRange, setDateRange] = useState<DateRangeFilter>('ALL')
  const [customStartDate, setCustomStartDate] = useState<string>('')
  const [customEndDate, setCustomEndDate] = useState<string>('')
  const [sortBy, setSortBy] = useState<SortBy>('scheduled_time')
  const [sortOrder, setSortOrder] = useState<'asc' | 'desc'>('asc')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)

  // Keyword search — raw input is debounced into the applied query so we don't
  // refetch on every keystroke. Supports boolean operators (AND / OR / NOT).
  const [searchInput, setSearchInput] = useState('')
  const [searchQuery, setSearchQuery] = useState('')
  useEffect(() => {
    const t = setTimeout(() => {
      setSearchQuery(searchInput.trim())
      setPage(1)
      setSelectedIds(new Set())
    }, 400)
    return () => clearTimeout(t)
  }, [searchInput])

  // Selection / bulk state
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [bulkStatus, setBulkStatus] = useState<string>('approved')
  const [bulkDate, setBulkDate] = useState<string>('')

  // Single-post edit state
  const [editingPost, setEditingPost] = useState<Post | null>(null)

  // Quick-delete confirmation + regenerate-with-suggestions state
  const [confirmDeletePost, setConfirmDeletePost] = useState<Post | null>(null)
  const [deleteRejectionReason, setDeleteRejectionReason] = useState('')
  const [regenGuidance, setRegenGuidance] = useState('')
  const [regenNotice, setRegenNotice] = useState<string | null>(null)

  // Verdict of the last "Save & re-score" run on the open post (issue #421).
  const [rescoreResult, setRescoreResult] = useState<string | null>(null)

  // The re-score verdict belongs to one post — drop it when a different one is opened.
  const editingPostId = editingPost?.post_id ?? null
  useEffect(() => { setRescoreResult(null) }, [editingPostId])

  const { start: filterStartDate, end: filterEndDate } = resolveDateRange(
    dateRange, userTimezone, customStartDate, customEndDate
  )

  const queryKey = [
    'posts', email, page, pageSize, sortOrder, sortBy,
    filterStatus, filterPostType, dateRange, filterStartDate, filterEndDate, searchQuery,
  ]

  const { data, isLoading } = useQuery<{ detail: PostsResponse }>({
    queryKey,
    queryFn: () => {
      const params = new URLSearchParams({
        email,
        page: String(page),
        page_size: String(pageSize),
        sort_order: sortOrder,
        sort_by: sortBy,
      })
      if (filterStatus !== 'ALL') params.set('status_filter', filterStatus)
      if (filterPostType !== 'ALL') params.set('post_type_filter', filterPostType)
      if (filterStartDate) params.set('start_date', zonedDateToUtcStart(filterStartDate, userTimezone) ?? '')
      if (filterEndDate) params.set('end_date', zonedDateToUtcEnd(filterEndDate, userTimezone) ?? '')
      if (searchQuery) params.set('search', searchQuery)
      return api.get(`/posts/?${params.toString()}`).then((r) => r.data)
    },
    enabled: !!email,
  })

  const posts = data?.detail?.posts ?? []
  const total = data?.detail?.total ?? 0
  const totalPages = Math.max(1, Math.ceil(total / pageSize))

  // The review decision is the product moment autocapture can't name: a click on "Save Changes"
  // says nothing about which way the draft went. Only a real STATUS TRANSITION counts — re-saving
  // (or re-deleting) a post that already has that status is an edit, not a second decision.
  const capturePostDecision = (postId: number, status: string, extra: Record<string, unknown> = {}) => {
    if (status !== 'approved' && status !== 'rejected') return
    const post = posts.find((p) => p.post_id === postId)
    if (post?.status === status) return
    const properties = {
      post_id: postId,
      post_type: post?.post_type ?? null,
      archetype: post?.archetype ?? null,
      ...extra,
    }
    // An approval also advances the `posts_approved` person property the CSAT survey is gated on
    // (issue #653) — same event either way, one extra person update on the approve path.
    if (status === 'approved') recordPostApproval(properties)
    else capture(EVENTS.postRejected, properties)
  }

  const updateMutation = useMutation({
    mutationFn: (post: Post) =>
      api.post(`/update_post/?post_id=${post.post_id}`, {
        content: post.content,
        video_url: post.video_url,
        post_type: post.post_type,
        scheduled_datetime: post.scheduled_time,
        email,
        status: post.status,
      }),
    onSuccess: (_res, post) => {
      // post_type is editable in this dialog, so report what was SAVED, not the stale list row.
      capturePostDecision(post.post_id, post.status, { source: 'editor', post_type: post.post_type })
      qc.invalidateQueries({ queryKey: ['posts', email] })
      setEditingPost(null)
    },
  })

  // Save the edit, then re-run the quality gates on it (issue #421). Scoring the STORED content
  // keeps the verdict honest — the gates judge exactly what would publish. A draft that now clears
  // every gate is promoted back to APPROVED server-side, with no full regenerate.
  const rescoreMutation = useMutation({
    mutationFn: async (post: Post) => {
      if (!sessionToken) throw new Error('No active session')
      await api.post(`/update_post/?post_id=${post.post_id}`, {
        content: post.content,
        video_url: post.video_url,
        post_type: post.post_type,
        scheduled_datetime: post.scheduled_time,
        email,
        status: post.status,
      })
      const r = await api.post('/user/post/rescore', {
        session_token: sessionToken,
        post_id: post.post_id,
      })
      return r.data.detail as RescoreResult
    },
    onSuccess: (result) => {
      setRescoreResult(result.detail)
      setEditingPost((p) =>
        p ? { ...p, status: result.status ?? p.status, gate_reason: result.findings,
              authenticity_score: result.authenticity_score } : p)
      qc.invalidateQueries({ queryKey: ['posts', email] })
    },
    onError: () => setRescoreResult('Could not re-score this post — please try again.'),
  })

  const bulkUpdateMutation = useMutation({
    mutationFn: (body: { post_ids: number[]; status?: string; scheduled_datetime?: string }) =>
      api.post('/posts/bulk_update/', body),
    onSuccess: (_res, body) => {
      // One event per post, not one per click — an approval rate broken down by archetype needs
      // the same unit whether the user approved from the editor or from the bulk bar.
      if (body.status) {
        for (const id of body.post_ids) capturePostDecision(id, body.status, { source: 'bulk' })
      }
      qc.invalidateQueries({ queryKey: ['posts', email] })
      setSelectedIds(new Set())
      setBulkDate('')
    },
  })

  const bulkDeleteMutation = useMutation({
    mutationFn: (post_ids: number[]) => api.delete('/posts/', { data: { post_ids } }),
    onSuccess: (_res, post_ids) => {
      // The delete endpoint is a soft delete (status=rejected) — the same decision, another door.
      for (const id of post_ids) capturePostDecision(id, 'rejected', { source: 'bulk_delete' })
      qc.invalidateQueries({ queryKey: ['posts', email] })
      setSelectedIds(new Set())
    },
  })

  // Regenerate a rejected post, seeding guidance with its stored rejection reason.
  const regenerateRejectedMutation = useMutation({
    mutationFn: (post_id: number) => {
      if (!sessionToken) return Promise.reject(new Error('No active session'))
      return api.post('/user/post/regenerate', {
        session_token: sessionToken,
        post_id,
        // Pass the stored reason explicitly so the API uses it as guidance.
        guidance: editingPost?.rejection_reason || undefined,
      })
    },
    onSuccess: () => {
      setRegenNotice('Regenerating… this post will return to PENDING with fresh content shortly.')
      setEditingPost(null)
      setTimeout(() => qc.invalidateQueries({ queryKey: ['posts', email] }), 2500)
      setTimeout(() => setRegenNotice(null), 8000)
    },
    onError: () => setRegenNotice('Could not start regeneration — please try again.'),
  })

  // Quick per-post delete (reuses the same soft-delete endpoint as the bulk flow, one id).
  const deleteMutation = useMutation({
    mutationFn: (post_id: number) => api.delete('/posts/', {
      data: { post_ids: [post_id], rejection_reason: deleteRejectionReason.trim() || undefined },
    }),
    onSuccess: (_res, post_id) => {
      capturePostDecision(post_id, 'rejected', {
        source: 'delete',
        has_rejection_reason: deleteRejectionReason.trim().length > 0,
      })
      qc.invalidateQueries({ queryKey: ['posts', email] })
      setConfirmDeletePost(null)
      setDeleteRejectionReason('')
      setSelectedIds((prev) => { const n = new Set(prev); n.delete(post_id); return n })
      if (editingPost?.post_id === post_id) setEditingPost(null)
    },
  })

  // Regenerate a single post with optional freeform guidance — runs the async generation task
  // server-side (honors saved settings + guidance), then resets the post to PENDING for re-review.
  const regenerateMutation = useMutation({
    mutationFn: (vars: { post_id: number; guidance?: string | null }) => {
      if (!sessionToken) return Promise.reject(new Error('No active session'))
      return api.post('/user/post/regenerate', {
        session_token: sessionToken,
        post_id: vars.post_id,
        guidance: vars.guidance?.trim() || null,
      })
    },
    onSuccess: () => {
      setRegenGuidance('')
      setRegenNotice('Regenerating… this post will return to PENDING with fresh content shortly.')
      setEditingPost(null)
      setTimeout(() => qc.invalidateQueries({ queryKey: ['posts', email] }), 2500)
      setTimeout(() => setRegenNotice(null), 8000)
    },
    onError: () => setRegenNotice('Could not start regeneration — please try again.'),
  })

  // Generation takes minutes (each video post is a ~6-min render), so poll progress while a run
  // is queued/in-progress and stop as soon as it finishes.
  const { data: genStatus } = useQuery<GenerationStatus | null>({
    queryKey: ['content-generation-status', sessionToken],
    queryFn: async () => {
      const r = await api.get('/content_generation_status/', { params: { session_token: sessionToken } })
      return (r.data.detail ?? null) as GenerationStatus | null
    },
    enabled: !!sessionToken,
    refetchInterval: (query) => (isGenerationRunning(query.state.data) ? 5_000 : false),
  })

  // When a run finishes, pull in the posts it just created (finished_at is only set once the
  // run reaches done/failed, so this fires exactly once per run).
  const finishedAt = genStatus?.finished_at ?? null
  useEffect(() => {
    if (finishedAt) qc.invalidateQueries({ queryKey: ['posts', email] })
  }, [finishedAt, email, qc])

  const weeklyMutation = useMutation({
    mutationFn: () =>
      api.get(`/user_id/?email=${encodeURIComponent(email)}`).then((r) =>
        api.post(`/create_weekly_content/?user_id=${r.data.detail}`)
      ),
    onSuccess: () => {
      capture(EVENTS.contentPlanGenerated, { source: 'content_studio' })
      qc.invalidateQueries({ queryKey: ['content-generation-status', sessionToken] })
    },
  })

  // A finished run expires server-side within the hour, so there's no staleness to reason about
  // here — the user can also dismiss the result early.
  const [dismissedRunStartedAt, setDismissedRunStartedAt] = useState<string | null>(null)
  const showGenBanner = !!genStatus && genStatus.started_at !== dismissedRunStartedAt
  const emptyRun = genStatus ? emptyRunExplanation(genStatus, userTimezone) : null

  const generating = weeklyMutation.isPending || isGenerationRunning(genStatus)
  const generateLabel = !generating
    ? 'Generate Weekly Content'
    : genStatus?.state === 'in_progress' && genStatus.total > 0
      ? `Generating ${genStatus.completed + genStatus.failed} of ${genStatus.total}…`
      : 'Generating content…'

  const { data: postUrlData, isLoading: postUrlLoading } = useQuery<{ detail: { post_url: string | null } }>({
    queryKey: ['post_url', editingPost?.post_id, email],
    queryFn: () =>
      api.get(`/post_url/?post_id=${editingPost!.post_id}&email=${encodeURIComponent(email)}`).then((r) => r.data),
    enabled: !!editingPost && editingPost.status === 'posted' && !!email,
  })

  // Selection helpers
  function toggleSelect(id: number) {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }

  function toggleSelectAll() {
    if (posts.every((p) => selectedIds.has(p.post_id))) {
      setSelectedIds((prev) => {
        const next = new Set(prev)
        posts.forEach((p) => next.delete(p.post_id))
        return next
      })
    } else {
      setSelectedIds((prev) => {
        const next = new Set(prev)
        posts.forEach((p) => next.add(p.post_id))
        return next
      })
    }
  }

  function resetPage(newFilter?: Status) {
    setPage(1)
    setSelectedIds(new Set())
    setEditingPost(null)
    if (newFilter !== undefined) setFilterStatus(newFilter)
  }

  const hasActiveFilters =
    filterStatus !== 'ALL' ||
    filterPostType !== 'ALL' ||
    dateRange !== 'ALL' ||
    searchQuery !== ''

  function clearFilters() {
    setFilterStatus('ALL')
    setFilterPostType('ALL')
    setDateRange('ALL')
    setCustomStartDate('')
    setCustomEndDate('')
    setSearchInput('')
    setSearchQuery('')
    setSortBy('scheduled_time')
    setPage(1)
    setSelectedIds(new Set())
    setEditingPost(null)
  }

  const allOnPageSelected = posts.length > 0 && posts.every((p) => selectedIds.has(p.post_id))

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <h1 className="text-2xl font-bold text-gray-800">Content Studio</h1>
        {view === 'review' && (
          <button
            onClick={() => weeklyMutation.mutate()}
            disabled={generating}
            className="bg-green-600 text-white px-4 py-2 rounded-lg text-sm font-semibold hover:bg-green-700 disabled:opacity-50 transition-colors"
          >
            {generateLabel}
          </button>
        )}
        {view === 'review' && (
          <button
            onClick={() => setView('posts')}
            className="border border-blue-600 text-blue-700 px-4 py-2 rounded-lg text-sm font-semibold hover:bg-blue-50 transition-colors"
          >
            + Schedule a post
          </button>
        )}
      </div>

      {/* Generation progress — generation runs for minutes in the background, so show what
          it is doing instead of leaving the page looking unchanged (issue #545). */}
      {showGenBanner && genStatus && (
        <div
          className={`rounded-lg border p-3 text-sm ${
            isGenerationRunning(genStatus)
              ? 'bg-blue-50 border-blue-200 text-blue-800'
              : genStatus.state === 'failed'
                ? 'bg-red-50 border-red-200 text-red-800'
                : emptyRun
                  ? 'bg-amber-50 border-amber-200 text-amber-900'
                  : 'bg-green-50 border-green-200 text-green-800'
          }`}
        >
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <span className="font-semibold">
              {genStatus.state === 'queued' && 'Queued — starting content generation…'}
              {genStatus.state === 'in_progress' &&
                (genStatus.total > 0
                  ? `Generating your posts — ${genStatus.completed + genStatus.failed} of ${genStatus.total} done`
                  : 'Generating your posts…')}
              {genStatus.state === 'done' &&
                (emptyRun
                  ? emptyRun.headline
                  : `${genStatus.completed} ${genStatus.completed === 1 ? 'post is' : 'posts are'} ready to review`)}
              {genStatus.state === 'failed' && 'Content generation failed — no posts were created'}
            </span>
            {isGenerationRunning(genStatus) ? (
              <span className="text-xs">
                This takes a few minutes — video posts render last. You can leave this page; we'll
                email you when it's done.
              </span>
            ) : (
              <button
                onClick={() => setDismissedRunStartedAt(genStatus.started_at)}
                aria-label="Dismiss generation status"
                className="text-xs font-semibold underline"
              >
                Dismiss
              </button>
            )}
          </div>
          {genStatus.total > 0 && (
            <div className="mt-2 h-2 w-full rounded-full bg-white/70 overflow-hidden">
              <div
                className="h-full bg-blue-600 transition-all"
                style={{
                  width: `${Math.min(100, Math.round(
                    ((genStatus.completed + genStatus.failed) / genStatus.total) * 100))}%`,
                }}
              />
            </div>
          )}
          {emptyRun && <p className="mt-2 text-xs">{emptyRun.detail}</p>}
          {genStatus.failed > 0 && (
            <p className="mt-2 text-xs">
              {genStatus.failed} {genStatus.failed === 1 ? 'post' : 'posts'} couldn't be generated
              (usually a media failure) — they stay in your plan and are retried on the next run.
            </p>
          )}
        </div>
      )}

      {/* Content tabs */}
      <div className="flex flex-wrap gap-1 border-b border-gray-200">
        {VIEWS.map((v) => (
          <button
            key={v.key}
            onClick={() => setView(v.key)}
            className={`px-3 py-2 text-sm font-semibold border-b-2 -mb-px transition-colors ${
              view === v.key
                ? 'border-blue-600 text-blue-700'
                : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
            }`}
          >
            {v.label}
          </button>
        ))}
      </div>

      {/* Compose stays MOUNTED (hidden when inactive) so in-progress captions and generated AI
          carousel slides survive a tab switch. */}
      <div className={view === 'posts' ? '' : 'hidden'}>
        <ComposePost onNavigateTab={(t) => setView(t as View)} />
      </div>

      {view === 'newsletters' && <NewsletterQueue userTimezone={userTimezone} />}

      {view === 'dms' && <ScheduledDMs userTimezone={userTimezone} />}

      {view === 'connections' && <ConnectionRequests userTimezone={userTimezone} />}

      {view === 'catchup' && <CatchupTouches userTimezone={userTimezone} />}

      {view === 'outreach' && <OutreachFunnel />}

      {view === 'leads' && <LeadsInbox userTimezone={userTimezone} />}

      {view === 'pipeline' && <LeadsPipeline userTimezone={userTimezone} />}

      {view === 'review' && (
      <>
      {regenNotice && (
        <div className="bg-indigo-50 border border-indigo-200 text-indigo-800 rounded-lg px-4 py-2 text-sm">
          {regenNotice}
        </div>
      )}
      {/* Status tabs */}
      <div className="flex gap-2 flex-wrap">
        {STATUSES.map((s) => (
          <button
            key={s.value}
            onClick={() => resetPage(s.value)}
            className={`px-3 py-1.5 rounded-full text-xs font-semibold transition-colors ${
              filterStatus === s.value
                ? 'bg-blue-600 text-white'
                : 'bg-white text-gray-600 border border-gray-300 hover:border-blue-400'
            }`}
          >
            {s.label}
          </button>
        ))}
      </div>

      {/* Keyword search — boolean operators supported */}
      <div>
        <div className="relative">
          <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 text-sm">🔍</span>
          <input
            type="search"
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            placeholder='Search posts… e.g.  ai AND "case study" NOT hiring'
            aria-label="Search posts by keyword"
            className="w-full border border-gray-300 rounded-lg pl-9 pr-8 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
          {searchInput && (
            <button
              type="button"
              onClick={() => setSearchInput('')}
              aria-label="Clear search"
              className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 text-base leading-none px-1"
            >
              ×
            </button>
          )}
        </div>
        <p className="text-xs text-gray-400 mt-1">
          Combine keywords with <span className="font-mono font-semibold">AND</span>,{' '}
          <span className="font-mono font-semibold">OR</span>,{' '}
          <span className="font-mono font-semibold">NOT</span> and parentheses; wrap phrases in "quotes".
        </p>
      </div>

      {/* Sort + type filter + date range + page-size controls */}
      <div className="flex items-center gap-3 flex-wrap text-sm">
        <label className="flex items-center gap-1.5 text-xs text-gray-600">
          <span>Type</span>
          <select
            value={filterPostType}
            onChange={(e) => { setFilterPostType(e.target.value as PostTypeFilter); setPage(1); setSelectedIds(new Set()) }}
            className="border border-gray-300 rounded-lg px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500"
          >
            {POST_TYPE_FILTERS.map((t) => (
              <option key={t.value} value={t.value}>{t.label}</option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1.5 text-xs text-gray-600">
          <span>Date</span>
          <select
            value={dateRange}
            onChange={(e) => { setDateRange(e.target.value as DateRangeFilter); setPage(1); setSelectedIds(new Set()) }}
            aria-label="Date range preset"
            className="border border-gray-300 rounded-lg px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500"
          >
            {DATE_RANGE_FILTERS.map((d) => (
              <option key={d.value} value={d.value}>{d.label}</option>
            ))}
          </select>
        </label>
        {dateRange === 'custom' && (
          <div className="flex items-center gap-1.5 text-xs text-gray-600">
            <input
              type="date"
              value={customStartDate}
              onChange={(e) => { setCustomStartDate(e.target.value); setPage(1); setSelectedIds(new Set()) }}
              aria-label="Custom start date"
              className="border border-gray-300 rounded-lg px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
            <span>to</span>
            <input
              type="date"
              value={customEndDate}
              onChange={(e) => { setCustomEndDate(e.target.value); setPage(1); setSelectedIds(new Set()) }}
              aria-label="Custom end date"
              className="border border-gray-300 rounded-lg px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
          </div>
        )}
        <label className="flex items-center gap-1.5 text-xs text-gray-600">
          <span>Sort by</span>
          <select
            value={sortBy}
            onChange={(e) => { setSortBy(e.target.value as SortBy); resetPage() }}
            className="border border-gray-300 rounded-lg px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500"
          >
            {SORT_BY_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        </label>
        <button
          onClick={() => { setSortOrder((o) => (o === 'asc' ? 'desc' : 'asc')); resetPage() }}
          className="flex items-center gap-1 text-gray-600 border border-gray-300 rounded-lg px-3 py-1.5 hover:border-blue-400 transition-colors text-xs font-medium"
        >
          {sortBy === 'scheduled_time'
            ? (sortOrder === 'asc' ? '↑ Oldest first' : '↓ Newest first')
            : (sortOrder === 'asc' ? '↑ Ascending' : '↓ Descending')}
        </button>
        <div className="flex items-center gap-1.5 text-xs text-gray-600">
          <span>Show</span>
          {PAGE_SIZE_OPTIONS.map((n) => (
            <button
              key={n}
              onClick={() => { setPageSize(n); resetPage() }}
              className={`px-2 py-1 rounded border transition-colors ${
                pageSize === n ? 'bg-blue-600 text-white border-blue-600' : 'border-gray-300 hover:border-blue-400'
              }`}
            >
              {n}
            </button>
          ))}
          <span>per page</span>
        </div>
        <span className="text-xs text-gray-400">{total} total posts</span>
      </div>

      {/* Bulk action bar */}
      {selectedIds.size > 0 && (
        <div className="bg-blue-50 border border-blue-200 rounded-lg px-4 py-3 flex flex-wrap items-center gap-3">
          <span className="text-sm font-semibold text-blue-800">{selectedIds.size} selected</span>

          <div className="flex items-center gap-2">
            <select
              value={bulkStatus}
              onChange={(e) => setBulkStatus(e.target.value)}
              className="border border-gray-300 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500"
            >
              {['pending', 'approved', 'scheduled', 'posted', 'rejected'].map((s) => (
                <option key={s} value={s}>{s.toUpperCase()}</option>
              ))}
            </select>
            <button
              onClick={() => bulkUpdateMutation.mutate({ post_ids: Array.from(selectedIds), status: bulkStatus })}
              disabled={bulkUpdateMutation.isPending}
              className="bg-blue-600 text-white px-3 py-1 rounded text-xs font-semibold hover:bg-blue-700 disabled:opacity-50"
            >
              Apply Status
            </button>
          </div>

          <div className="flex items-center gap-2">
            <input
              type="datetime-local"
              value={bulkDate}
              onChange={(e) => setBulkDate(e.target.value)}
              className="border border-gray-300 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
            <button
              onClick={() => {
                const utc = zonedInputToUtcIso(bulkDate, userTimezone)
                if (!utc) return
                bulkUpdateMutation.mutate({
                  post_ids: Array.from(selectedIds),
                  scheduled_datetime: utc,
                })
              }}
              disabled={!bulkDate || bulkUpdateMutation.isPending}
              className="bg-blue-600 text-white px-3 py-1 rounded text-xs font-semibold hover:bg-blue-700 disabled:opacity-50"
            >
              Apply Date
            </button>
          </div>

          <button
            onClick={() => bulkDeleteMutation.mutate(Array.from(selectedIds))}
            disabled={bulkDeleteMutation.isPending}
            className="ml-auto bg-red-500 text-white px-3 py-1 rounded text-xs font-semibold hover:bg-red-600 disabled:opacity-50"
          >
            {bulkDeleteMutation.isPending ? 'Deleting…' : 'Delete Selected'}
          </button>
        </div>
      )}

      {isLoading && <p className="text-gray-400 text-sm">Loading posts…</p>}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="space-y-3">
          {/* Select-all header */}
          {posts.length > 0 && (
            <label className="flex items-center gap-2 text-xs text-gray-500 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={allOnPageSelected}
                onChange={toggleSelectAll}
                className="rounded border-gray-300 text-blue-600 focus:ring-blue-500"
              />
              Select all on this page
            </label>
          )}

          {posts.length === 0 && !isLoading && (
            <div className="flex flex-col items-center text-center py-12 px-4 bg-white rounded-lg border border-gray-200">
              <div className="text-4xl mb-4">📅</div>
              <p className="text-gray-600 text-sm mb-6 max-w-xs">
                {hasActiveFilters
                  ? 'No posts match these filters.'
                  : 'Your scheduled posts will appear here. Generate your first week of content to get started.'}
              </p>
              {hasActiveFilters ? (
                <button
                  onClick={clearFilters}
                  className="border border-gray-300 text-gray-600 px-5 py-2 rounded-lg text-sm font-semibold hover:bg-gray-50 transition-colors"
                >
                  Clear filters
                </button>
              ) : (
                <button
                  onClick={() => weeklyMutation.mutate()}
                  disabled={generating}
                  className="bg-green-600 text-white px-6 py-2.5 rounded-lg text-sm font-semibold hover:bg-green-700 disabled:opacity-50 transition-colors"
                >
                  {generateLabel}
                </button>
              )}
            </div>
          )}

          {posts.map((post) => (
            <div
              key={post.post_id}
              onClick={() => setEditingPost(post)}
              className={`bg-white rounded-lg border p-4 cursor-pointer hover:border-blue-400 transition-colors ${
                editingPost?.post_id === post.post_id ? 'border-blue-500 ring-1 ring-blue-500' : 'border-gray-200'
              }`}
            >
              <div className="flex items-start gap-3">
                <input
                  type="checkbox"
                  checked={selectedIds.has(post.post_id)}
                  onChange={(e) => { e.stopPropagation(); toggleSelect(post.post_id) }}
                  onClick={(e) => e.stopPropagation()}
                  className="mt-1 rounded border-gray-300 text-blue-600 focus:ring-blue-500 shrink-0"
                />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center justify-between mb-1 gap-2">
                    <span className="text-xs text-gray-400 truncate">
                      {formatInTimezone(post.scheduled_time, userTimezone)}
                    </span>
                    <div className="flex items-center gap-1.5 shrink-0">
                      <span className="text-xs text-gray-400 uppercase">{post.post_type}</span>
                      <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_COLORS[post.status] ?? 'bg-gray-100 text-gray-600'}`}>
                        {post.status.toUpperCase()}
                      </span>
                      {post.status !== 'posted' && (
                        <button
                          type="button"
                          title="Delete this post"
                          aria-label={`Delete post ${post.post_id}`}
                          onClick={(e) => { e.stopPropagation(); setConfirmDeletePost(post) }}
                          className="text-gray-300 hover:text-red-500 transition-colors px-1 leading-none text-base"
                        >
                          🗑
                        </button>
                      )}
                    </div>
                  </div>
                  <p className="text-sm text-gray-700 line-clamp-2">{post.content}</p>
                  {/* One-line "why" for a gate-held draft — the full reason + fix is in the editor. */}
                  {post.status === 'pending' && gateHold(post.gate_reason).length > 0 && (
                    <p className="text-xs text-amber-700 mt-1 truncate">
                      ⏸ {gateHold(post.gate_reason).map((f) => f.label).join(' · ')} — click to see why
                    </p>
                  )}
                  {post.status === 'rejected' && post.rejection_reason && (
                    <p className="text-xs text-red-700 mt-1 truncate">
                      ✎ {post.rejection_reason}
                    </p>
                  )}
                  {isDeckType(post.post_type) && post.carousel_slides && (
                    <p className="text-xs text-gray-400 mt-1">{post.carousel_slides.length} slides</p>
                  )}
                </div>
              </div>
            </div>
          ))}

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between pt-2">
              <button
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={page <= 1}
                className="px-3 py-1.5 border border-gray-300 rounded-lg text-xs text-gray-600 hover:border-blue-400 disabled:opacity-40 transition-colors"
              >
                ← Prev
              </button>
              <span className="text-xs text-gray-500">Page {page} of {totalPages}</span>
              <button
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                disabled={page >= totalPages}
                className="px-3 py-1.5 border border-gray-300 rounded-lg text-xs text-gray-600 hover:border-blue-400 disabled:opacity-40 transition-colors"
              >
                Next →
              </button>
            </div>
          )}
        </div>

        {editingPost && (
          <div className="sticky top-4 self-start space-y-4 max-h-[calc(100vh-2rem)] overflow-y-auto">
            {editingPost.status === 'posted' ? (
              /* Read-only view for published posts */
              <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-5 space-y-4">
                <div className="flex items-center justify-between">
                  <h3 className="font-semibold text-gray-700">
                    Post #{editingPost.post_id}
                    <span className="ml-2 px-2 py-0.5 rounded-full text-xs font-medium bg-blue-100 text-blue-700">Published</span>
                  </h3>
                </div>

                <p className="text-sm text-gray-700 whitespace-pre-wrap">{editingPost.content}</p>

                <div className="grid grid-cols-2 gap-3 text-xs text-gray-500">
                  <div>
                    <span className="font-medium text-gray-600 block mb-0.5">Type</span>
                    {editingPost.post_type.toUpperCase()}
                  </div>
                  <div>
                    <span className="font-medium text-gray-600 block mb-0.5">Posted</span>
                    {formatInTimezone(editingPost.scheduled_time, userTimezone)}
                  </div>
                </div>

                <div>
                  {postUrlLoading ? (
                    <span className="text-xs text-gray-400">Loading link…</span>
                  ) : postUrlData?.detail?.post_url ? (
                    <a
                      href={postUrlData.detail.post_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1.5 bg-blue-600 text-white px-4 py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 transition-colors"
                    >
                      View on LinkedIn ↗
                    </a>
                  ) : (
                    <span className="text-xs text-gray-400">LinkedIn link not available</span>
                  )}
                </div>

                <button
                  onClick={() => setEditingPost(null)}
                  className="w-full px-4 py-2 border border-gray-300 rounded-lg text-sm text-gray-600 hover:bg-gray-50"
                >
                  Close
                </button>
              </div>
            ) : (
              /* Editable form for non-posted statuses */
              <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-5 space-y-4">
                <h3 className="font-semibold text-gray-700">Edit Post #{editingPost.post_id}</h3>

                {/* Why this draft is pending + how to clear it (issue #421). */}
                <PostGateReason
                  findings={editingPost.gate_reason ?? []}
                  status={editingPost.status}
                  onRescore={() => rescoreMutation.mutate(editingPost)}
                  rescoring={rescoreMutation.isPending}
                  rescoreResult={rescoreResult}
                />

                <textarea
                  rows={6}
                  value={editingPost.content}
                  onChange={(e) => setEditingPost({ ...editingPost, content: e.target.value })}
                  {...maskProps('w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none')}
                />

                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">Post Type</label>
                  <select
                    value={editingPost.post_type}
                    onChange={(e) => {
                      const newType = e.target.value
                      setEditingPost({
                        ...editingPost,
                        post_type: newType,
                        video_url: newType === 'video' ? editingPost.video_url : null,
                        carousel_slides: isDeckType(newType) ? editingPost.carousel_slides : null,
                      })
                    }}
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                  >
                    {['text', 'video', 'carousel', 'document'].map((t) => (
                      <option key={t} value={t}>{t.toUpperCase()}</option>
                    ))}
                  </select>
                </div>

                {editingPost.post_type === 'video' && (
                  <div>
                    <label className="block text-xs font-medium text-gray-600 mb-1">Video URL</label>
                    <input
                      type="url"
                      value={editingPost.video_url ?? ''}
                      onChange={(e) => setEditingPost({ ...editingPost, video_url: e.target.value || null })}
                      className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                      placeholder="https://example.com/video.mp4"
                    />
                  </div>
                )}

                {isDeckType(editingPost.post_type) && editingPost.carousel_slides && (
                  <div>
                    <label className="block text-xs font-medium text-gray-600 mb-1">
                      Slides ({editingPost.carousel_slides.length})
                    </label>
                    <ul className="space-y-1">
                      {editingPost.carousel_slides.map((slide, i) => (
                        <li key={i} className="text-xs text-gray-600 bg-gray-50 rounded px-2 py-1">
                          <span className="text-gray-400 mr-1">{i + 1}.</span>{slide}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-xs font-medium text-gray-600 mb-1">Status</label>
                    <select
                      value={editingPost.status}
                      onChange={(e) => setEditingPost({ ...editingPost, status: e.target.value })}
                      className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                    >
                      {['pending', 'approved', 'scheduled', 'posted', 'rejected'].map((s) => (
                        <option key={s} value={s}>{s.toUpperCase()}</option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-gray-600 mb-1">Scheduled Time</label>
                    <input
                      type="datetime-local"
                      value={toZonedInputValue(editingPost.scheduled_time, userTimezone)}
                      onChange={(e) =>
                        setEditingPost({
                          ...editingPost,
                          scheduled_time:
                            zonedInputToUtcIso(e.target.value, userTimezone) ?? editingPost.scheduled_time,
                        })
                      }
                      className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                    />
                  </div>
                </div>

                <div className="flex gap-2">
                  <button
                    onClick={() => updateMutation.mutate(editingPost)}
                    disabled={updateMutation.isPending}
                    className="flex-1 bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50"
                  >
                    {updateMutation.isPending ? 'Saving…' : 'Save Changes'}
                  </button>
                  <button
                    onClick={() => setEditingPost(null)}
                    className="px-4 py-2 border border-gray-300 rounded-lg text-sm text-gray-600 hover:bg-gray-50"
                  >
                    Cancel
                  </button>
                </div>

                {/* Regenerate with suggestions — mirrors the newsletter flow. Runs the generation
                    pipeline server-side (honors saved voice/tone, focus/goals, emoji/hashtag prefs)
                    PLUS the optional guidance, then resets the post to PENDING for re-review.
                    Available for pending/rejected text posts so users can fix drafts that still
                    need a decision; approved posts already have acceptance (issue #778). */}
                {editingPost.post_type === 'text' && (editingPost.status === 'pending' || editingPost.status === 'rejected') && (
                  <div className="border-t border-gray-100 pt-4 space-y-2">
                    <label className="block text-xs font-medium text-gray-600">
                      Added Guidance <span className="font-normal text-gray-400">(optional)</span>
                    </label>
                    <textarea
                      value={regenGuidance}
                      onChange={(e) => setRegenGuidance(e.target.value)}
                      rows={2}
                      placeholder="e.g. lead with a client result, drop the stat, make it a personal story"
                      className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
                    />
                    <button
                      onClick={() => {
                        const guidance = regenGuidance.trim() || editingPost.rejection_reason || undefined
                        regenerateMutation.mutate({ post_id: editingPost.post_id, guidance })
                      }}
                      disabled={regenerateMutation.isPending}
                      className="w-full bg-indigo-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-indigo-700 disabled:opacity-50 transition-colors"
                    >
                      {regenerateMutation.isPending ? 'Starting…' : 'Re-generate Post'}
                    </button>
                    <p className="text-xs text-gray-400">
                      Regeneration replaces this post's content and returns it to PENDING for review.
                    </p>
                  </div>
                )}

                {/* Rejected posts get a one-click "regenerate with the reason" prompt (issue #713). */}
                {editingPost.status === 'rejected' && editingPost.rejection_reason && (
                  <div className="border-t border-gray-100 pt-4 space-y-2">
                    <p className="text-xs text-gray-600">
                      This post was rejected because:
                      <span className="block mt-1 text-red-700 font-medium">{editingPost.rejection_reason}</span>
                    </p>
                    <button
                      onClick={() => regenerateRejectedMutation.mutate(editingPost.post_id)}
                      disabled={regenerateRejectedMutation.isPending}
                      className="w-full bg-indigo-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-indigo-700 disabled:opacity-50 transition-colors"
                    >
                      {regenerateRejectedMutation.isPending ? 'Starting…' : 'Regenerate using this feedback'}
                    </button>
                    <p className="text-xs text-gray-400">
                      We'll use the rejection reason as guidance so the new draft avoids the same issue.
                    </p>
                  </div>
                )}
              </div>
            )}

            <LinkedInPostPreview
              content={editingPost.content}
              author={email.split('@')[0]}
              headline="LinkedIn Member"
              postType={editingPost.post_type}
              videoUrl={editingPost.video_url}
              slides={editingPost.carousel_slides}
            />
          </div>
        )}
      </div>
      </>
      )}

      {/* Quick-delete confirmation — backend is a soft delete (status=rejected), so the copy
          promises only what actually happens: gone from the queue/views, not purged. */}
      {confirmDeletePost && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
             onClick={() => !deleteMutation.isPending && setConfirmDeletePost(null)}>
          <div className="bg-white rounded-lg shadow-xl max-w-sm w-full p-6 space-y-4"
               onClick={(e) => e.stopPropagation()}>
            <h3 className="text-base font-semibold text-gray-800">Remove this post?</h3>
            <p className="text-sm text-gray-600">
              This will remove post #{confirmDeletePost.post_id} from your queue — it won't be published
              and will disappear from all views. You can't undo this from the UI.
            </p>
            <p className="text-xs text-gray-500 line-clamp-3 bg-gray-50 rounded p-2">{confirmDeletePost.content}</p>
            <div className="space-y-2">
              <label className="block text-xs font-medium text-gray-600">
                Why are you removing this? <span className="font-normal text-gray-400">(optional)</span>
              </label>
              <textarea
                value={deleteRejectionReason}
                onChange={(e) => setDeleteRejectionReason(e.target.value)}
                rows={2}
                placeholder="e.g. too salesy, wrong tone, not relevant to my audience"
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
              />
              <p className="text-xs text-gray-400">
                Your reason will help future drafts avoid the same issue.
              </p>
            </div>
            <div className="flex gap-2 justify-end">
              <button
                onClick={() => setConfirmDeletePost(null)}
                disabled={deleteMutation.isPending}
                className="px-4 py-2 border border-gray-300 rounded-lg text-sm text-gray-600 hover:bg-gray-50 disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                onClick={() => deleteMutation.mutate(confirmDeletePost.post_id)}
                disabled={deleteMutation.isPending}
                className="px-4 py-2 bg-red-600 text-white rounded-lg text-sm font-semibold hover:bg-red-700 disabled:opacity-50"
              >
                {deleteMutation.isPending ? 'Removing…' : 'Remove post'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
