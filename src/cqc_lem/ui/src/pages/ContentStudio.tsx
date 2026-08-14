import { useState, useEffect, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from '../api/client'
import type { ApiEnvelope } from '../api/types'
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
import GroupPostQueue from './review/GroupPostQueue'
import ComposePost from './content/ComposePost'
import { useAuth } from '../contexts/useAuth'
import { useUserTimezoneState } from '../hooks/useUserTimezone'
import { formatInTimezone, toZonedInputValue, zonedInputToUtcIso, zonedDateToUtcStart, zonedDateToUtcEnd } from '../utils/datetime'
import { DATE_RANGE_FILTERS, resolveDateRange } from '../utils/dateRange'
import type { DateRangeFilter } from '../utils/dateRange'
import { emptyRunExplanation, isGenerationRunning } from '../utils/generationStatus'
import type { GenerationStatus } from '../utils/generationStatus'
import { EVENTS, capture, maskProps, recordPostApproval } from '../utils/analytics'

// Consolidated content hub: compose posts, schedule DMs, manage newsletters, and review/edit
// existing content — one page, four tabs, synced to ?tab= for deep-linking.
type View = 'posts' | 'dms' | 'connections' | 'catchup' | 'outreach' | 'leads' | 'pipeline' | 'newsletters' | 'groups' | 'review'
const VIEWS: { key: View; label: string }[] = [
  { key: 'posts', label: 'Posts' },
  { key: 'dms', label: 'DMs' },
  { key: 'connections', label: 'Connections' },
  { key: 'catchup', label: 'Catch-up' },
  { key: 'outreach', label: 'Outreach' },
  { key: 'leads', label: 'Leads' },
  { key: 'pipeline', label: 'Pipeline' },
  { key: 'newsletters', label: 'Newsletters' },
  { key: 'groups', label: 'Groups' },
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

type SortBy = 'scheduled_time' | 'status' | 'post_type' | 'id'
const SORT_BY_OPTIONS: { label: string; value: SortBy }[] = [
  { label: 'Scheduled time', value: 'scheduled_time' },
  { label: 'Status', value: 'status' },
  { label: 'Type', value: 'post_type' },
  { label: 'Created (ID)', value: 'id' },
]

const PAGE_SIZE_OPTIONS = [10, 25, 50]

interface Post {
  post_id: number
  content: string
  video_url: string | null
  image_url?: string | null
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
  // An occasion/milestone draft (issue #1074). LinkedIn's "Celebrate an occasion" composer has no
  // API, so LEM never publishes this one — the author copies it across and says when it landed.
  manual_publish?: boolean
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
  const { timezone: userTimezone, isResolved: timezoneResolved } = useUserTimezoneState()
  // Say which clock a typed time is read in. Until the user's own zone has loaded we say so rather
  // than naming the browser's guess — a picker silently labelled with the wrong zone is how a post
  // meant for 9am local got stored as 09:00 UTC and published four hours early (issue #774).
  const timezoneLabel = timezoneResolved ? userTimezone : 'loading your timezone…'

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

  // Selection / bulk state
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [bulkStatus, setBulkStatus] = useState<string>('approved')
  const [bulkDate, setBulkDate] = useState<string>('')

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

  // Single-post edit state
  const [editingPost, setEditingPost] = useState<Post | null>(null)

  // Quick-delete confirmation + regenerate-with-suggestions state
  const [confirmDeletePost, setConfirmDeletePost] = useState<Post | null>(null)
  const [deleteRejectionReason, setDeleteRejectionReason] = useState('')
  const [regenGuidance, setRegenGuidance] = useState('')
  const [regenNotice, setRegenNotice] = useState<string | null>(null)

  // Verdict of the last "Save & re-score" run on the open post (issue #421).
  const [rescoreResult, setRescoreResult] = useState<string | null>(null)

  // Image edits on the open post (issue #1030) — upload/generate/remove write straight to the row,
  // so they are deliberately NOT part of "Save Changes".
  const [imageBusy, setImageBusy] = useState<'upload' | 'generate' | 'remove' | null>(null)
  const [imageError, setImageError] = useState<string | null>(null)
  const imageFileRef = useRef<HTMLInputElement>(null)

  const editingPostId = editingPost?.post_id ?? null

  const { start: filterStartDate, end: filterEndDate } = resolveDateRange(
    dateRange, userTimezone, customStartDate, customEndDate
  )

  const queryKey = [
    'posts', email, page, pageSize, sortOrder, sortBy,
    filterStatus, filterPostType, dateRange, filterStartDate, filterEndDate, searchQuery,
  ]

  const { data, isLoading } = useQuery<ApiEnvelope<PostsResponse>>({
    queryKey,
    queryFn: () => {
      const params = new URLSearchParams({
        session_token: sessionToken!,
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
    enabled: !!sessionToken,
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
        session_token: sessionToken,
        content: post.content,
        video_url: post.video_url,
        post_type: post.post_type,
        scheduled_datetime: post.scheduled_time,
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
        session_token: sessionToken,
        content: post.content,
        video_url: post.video_url,
        post_type: post.post_type,
        scheduled_datetime: post.scheduled_time,
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

  // Image on an existing post (issue #1030). All three actions land on the row immediately —
  // an image is a file, not a form field, so there is nothing sensible to hold until "Save".
  const applyImageUrl = (postId: number, url: string | null) => {
    setEditingPost((p) => (p && p.post_id === postId ? { ...p, image_url: url } : p))
    qc.invalidateQueries({ queryKey: ['posts', email] })
  }
  const imageErrorText = (err: unknown, fallback: string) => {
    const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    setImageError(typeof detail === 'string' ? detail : fallback)
  }

  const uploadImageMutation = useMutation({
    mutationFn: (vars: { post_id: number; file: File }) => {
      const form = new FormData()
      form.append('session_token', sessionToken!)
      form.append('post_id', String(vars.post_id))
      form.append('file', vars.file)
      return api.post('/user/post/image', form)
    },
    onMutate: () => { setImageBusy('upload'); setImageError(null) },
    onSuccess: (res, vars) => applyImageUrl(vars.post_id, res.data.detail.image_url),
    onError: (err) => imageErrorText(err, 'Could not use that image — try another file.'),
    onSettled: () => {
      setImageBusy(null)
      if (imageFileRef.current) imageFileRef.current.value = ''
    },
  })

  const generateImageMutation = useMutation({
    // Send the content that is ON SCREEN: the author is usually looking at an unsaved edit, and an
    // image drawn from the stale stored text is the one way this reads as broken.
    mutationFn: (post: Post) =>
      api.post('/user/post/image/generate', {
        session_token: sessionToken,
        post_id: post.post_id,
        content: post.content,
      }),
    onMutate: () => { setImageBusy('generate'); setImageError(null) },
    onSuccess: (res, post) => applyImageUrl(post.post_id, res.data.detail.image_url),
    onError: (err) => imageErrorText(err, 'Image generation failed — try again.'),
    onSettled: () => setImageBusy(null),
  })

  // "I posted this natively" (issue #1074). Only a manual-publish draft can be marked this way —
  // for every other post 'posted' is written by the task that has the LinkedIn URN to prove it.
  const [nativeCopied, setNativeCopied] = useState(false)
  const [nativeError, setNativeError] = useState<string | null>(null)

  // The re-score verdict, the image error and the native-copy flag all belong to ONE post — drop
  // them the moment a different one is opened. Adjusted during render (React's documented
  // "adjusting state when a prop changes" pattern) rather than in an effect, so the editor never
  // paints one post's verdict over another's.
  const [scratchPostId, setScratchPostId] = useState(editingPostId)
  if (scratchPostId !== editingPostId) {
    setScratchPostId(editingPostId)
    setRescoreResult(null)
    setImageError(null)
    setNativeCopied(false)
    setNativeError(null)
  }

  const markPostedMutation = useMutation({
    mutationFn: (post_id: number) =>
      api.post('/user/post/mark-posted', { session_token: sessionToken, post_id }),
    onMutate: () => setNativeError(null),
    onSuccess: (_res, post_id) => {
      setEditingPost((p) => (p && p.post_id === post_id ? { ...p, status: 'posted' } : p))
      qc.invalidateQueries({ queryKey: ['posts', email] })
    },
    onError: (err) => {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setNativeError(typeof detail === 'string' ? detail : 'Could not mark this post as published.')
    },
  })

  const copyNativeText = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text)
      setNativeCopied(true)
    } catch {
      // A denied clipboard permission is not a failure the user can act on by retrying — say what
      // to do instead, since the text is right there in the editor.
      setNativeError('Copy blocked by your browser — select the text above and copy it manually.')
    }
  }

  const removeImageMutation = useMutation({
    mutationFn: (post_id: number) =>
      api.post('/user/post/image/remove', { session_token: sessionToken, post_id }),
    onMutate: () => { setImageBusy('remove'); setImageError(null) },
    onSuccess: (_res, post_id) => applyImageUrl(post_id, null),
    onError: (err) => imageErrorText(err, 'Could not remove the image — try again.'),
    onSettled: () => setImageBusy(null),
  })

  const bulkUpdateMutation = useMutation({
    mutationFn: (body: { post_ids: number[]; status?: string; scheduled_datetime?: string }) =>
      api.post('/posts/bulk_update/', { ...body, session_token: sessionToken }),
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
    mutationFn: (post_ids: number[]) =>
      api.delete('/posts/', { data: { post_ids, session_token: sessionToken } }),
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
      data: {
        session_token: sessionToken,
        post_ids: [post_id],
        rejection_reason: deleteRejectionReason.trim() || undefined,
      },
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
    // The account is the session's, so there is no id to look up first (issue #914).
    mutationFn: () =>
      api.post(`/create_weekly_content/?session_token=${encodeURIComponent(sessionToken!)}`),
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

  const { data: postUrlData, isLoading: postUrlLoading } = useQuery<ApiEnvelope<{ post_url: string | null }>>({
    // Keyed on the address, like every other cache key on this page — `sessionToken` is the same
    // sentinel for every account since #745 and carries no identity of its own (issue #914).
    queryKey: ['post_url', editingPost?.post_id, email],
    queryFn: () =>
      api
        .get(`/post_url/?post_id=${editingPost!.post_id}&session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data),
    enabled: !!editingPost && editingPost.status === 'posted' && !!sessionToken,
  })

  // Selection helpers
  function toggleSelect(id: number) {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
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

      {view === 'newsletters' && (
        <NewsletterQueue userTimezone={userTimezone} timezoneResolved={timezoneResolved} />
      )}

      {view === 'groups' && <GroupPostQueue userTimezone={userTimezone} />}

      {view === 'dms' && <ScheduledDMs userTimezone={userTimezone} timezoneResolved={timezoneResolved} />}

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
              aria-label={`Bulk schedule date and time (${timezoneLabel})`}
              className="border border-gray-300 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
            <span className="text-xs text-gray-400">{timezoneLabel}</span>
            <button
              onClick={() => {
                // Never convert a typed wall clock against a guessed zone (issue #774).
                if (!timezoneResolved) return
                const utc = zonedInputToUtcIso(bulkDate, userTimezone)
                if (!utc) return
                bulkUpdateMutation.mutate({
                  post_ids: Array.from(selectedIds),
                  scheduled_datetime: utc,
                })
              }}
              disabled={!bulkDate || !timezoneResolved || bulkUpdateMutation.isPending}
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
                      {post.manual_publish && post.status !== 'posted' && (
                        <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-amber-100 text-amber-800">
                          POST NATIVELY
                        </span>
                      )}
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

                {/* Occasion / milestone draft (issue #1074). LinkedIn's occasion composer has no
                    API, so this post never publishes itself — copy it across, then say it landed. */}
                {editingPost.manual_publish && (
                  <div className="border border-amber-300 bg-amber-50 rounded-lg p-4 space-y-3">
                    <p className="text-sm font-semibold text-amber-900">Post natively on LinkedIn</p>
                    <p className="text-xs text-amber-800">
                      LinkedIn's occasion posts can only be created in its own composer, so LEM won't
                      publish this one. On LinkedIn: <span className="font-semibold">Start a post →
                      More → Celebrate an occasion</span>, pick the occasion, then paste the text
                      below.
                    </p>
                    <div className="flex flex-wrap items-center gap-2">
                      <button
                        type="button"
                        onClick={() => copyNativeText(editingPost.content)}
                        className="bg-amber-600 text-white px-3 py-1.5 rounded-lg text-xs font-semibold hover:bg-amber-700 transition-colors"
                      >
                        {nativeCopied ? 'Copied ✓' : 'Copy post text'}
                      </button>
                      <a
                        href="https://www.linkedin.com/feed/"
                        target="_blank"
                        rel="noopener noreferrer"
                        className="border border-amber-400 text-amber-800 px-3 py-1.5 rounded-lg text-xs font-semibold hover:bg-amber-100 transition-colors"
                      >
                        Open LinkedIn ↗
                      </a>
                      <button
                        type="button"
                        onClick={() => markPostedMutation.mutate(editingPost.post_id)}
                        disabled={markPostedMutation.isPending}
                        className="border border-amber-400 text-amber-800 px-3 py-1.5 rounded-lg text-xs font-semibold hover:bg-amber-100 disabled:opacity-50 transition-colors"
                      >
                        {markPostedMutation.isPending ? 'Saving…' : 'I posted this'}
                      </button>
                    </div>
                    {nativeError && <p className="text-xs text-red-600 font-medium">{nativeError}</p>}
                  </div>
                )}

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

                {/* Image on a text post (issue #1030) — upload your own or generate one from the
                    draft. Saved on the spot, so it survives Cancel and needs no "Save Changes". */}
                {editingPost.post_type === 'text' && (
                  <div>
                    <label className="block text-xs font-medium text-gray-600 mb-1">
                      Image <span className="font-normal text-gray-400">(optional)</span>
                    </label>
                    {editingPost.image_url && (
                      <div className="mb-2 flex items-start gap-3">
                        <img
                          src={editingPost.image_url}
                          alt={`Image on post ${editingPost.post_id}`}
                          className="w-24 h-24 object-cover rounded-lg border border-gray-200"
                        />
                        <button
                          type="button"
                          onClick={() => removeImageMutation.mutate(editingPost.post_id)}
                          disabled={imageBusy !== null}
                          className="text-xs text-red-500 hover:text-red-700 font-semibold underline disabled:opacity-50"
                        >
                          {imageBusy === 'remove' ? 'Removing…' : 'Remove image'}
                        </button>
                      </div>
                    )}
                    <div className="flex flex-wrap items-center gap-2">
                      <input
                        ref={imageFileRef}
                        type="file"
                        accept="image/png,image/jpeg"
                        aria-label="Upload post image"
                        disabled={imageBusy !== null}
                        onChange={(e) => {
                          const file = e.target.files?.[0]
                          if (file) uploadImageMutation.mutate({ post_id: editingPost.post_id, file })
                        }}
                        className="text-xs text-gray-600 file:mr-2 file:py-1.5 file:px-3 file:rounded-lg file:border file:border-gray-300 file:text-xs file:font-semibold file:bg-white hover:file:border-blue-400"
                      />
                      <button
                        type="button"
                        onClick={() => generateImageMutation.mutate(editingPost)}
                        disabled={imageBusy !== null}
                        className="bg-indigo-600 text-white px-3 py-1.5 rounded-lg text-xs font-semibold hover:bg-indigo-700 disabled:opacity-50 transition-colors"
                      >
                        {imageBusy === 'generate' ? 'Generating image…' : 'Generate with AI'}
                      </button>
                    </div>
                    {imageBusy === 'upload' && <p className="mt-1 text-xs text-gray-500">Uploading…</p>}
                    {imageError && <p className="mt-1 text-xs text-red-600 font-medium">{imageError}</p>}
                    <p className="mt-1 text-xs text-gray-400">
                      PNG or JPG, at least 400×400. AI generation draws the image from the text above.
                    </p>
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
                    <label className="block text-xs font-medium text-gray-600 mb-1">
                      Scheduled Time <span className="font-normal text-gray-400">({timezoneLabel})</span>
                    </label>
                    <input
                      type="datetime-local"
                      value={timezoneResolved ? toZonedInputValue(editingPost.scheduled_time, userTimezone) : ''}
                      disabled={!timezoneResolved}
                      aria-label={`Scheduled Time (${timezoneLabel})`}
                      onChange={(e) =>
                        setEditingPost({
                          ...editingPost,
                          scheduled_time:
                            zonedInputToUtcIso(e.target.value, userTimezone) ?? editingPost.scheduled_time,
                        })
                      }
                      className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-100"
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
                    Available for pending/rejected posts of EVERY type so users can fix drafts that
                    still need a decision (issue #794 — carousels/documents re-render their slides
                    and videos re-render their clip from the new caption); approved posts already
                    have acceptance (issue #778). */}
                {(editingPost.status === 'pending' || editingPost.status === 'rejected') && (
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
                      Regeneration replaces this post's content
                      {editingPost.post_type === 'carousel' || editingPost.post_type === 'document'
                        ? ' and slides'
                        : editingPost.post_type === 'video'
                          ? ' and video'
                          : ''}
                      , and returns it to PENDING for review.
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
              mediaUrl={editingPost.image_url ?? undefined}
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
          <div className="bg-white rounded-lg shadow-xl max-w-sm w-full p-6 space-y-4 max-h-viewport overflow-y-auto"
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
