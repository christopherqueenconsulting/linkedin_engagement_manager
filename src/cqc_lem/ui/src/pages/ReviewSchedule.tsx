import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from '../api/client'
import LinkedInPostPreview from '../components/LinkedInPostPreview'
import NewsletterQueue from './review/NewsletterQueue'
import { useAuth } from '../contexts/AuthContext'
import { useUserTimezone } from '../hooks/useUserTimezone'
import { formatInTimezone } from '../utils/datetime'

type View = 'posts' | 'newsletters'
const VIEWS: { key: View; label: string }[] = [
  { key: 'posts', label: 'Posts' },
  { key: 'newsletters', label: 'Newsletters' },
]

type Status = 'ALL' | 'pending' | 'approved' | 'scheduled' | 'posted' | 'rejected'
const STATUSES: { label: string; value: Status }[] = [
  { label: 'ALL', value: 'ALL' },
  { label: 'PENDING', value: 'pending' },
  { label: 'APPROVED', value: 'approved' },
  { label: 'SCHEDULED', value: 'scheduled' },
  { label: 'POSTED', value: 'posted' },
  { label: 'REJECTED', value: 'rejected' },
]

const PAGE_SIZE_OPTIONS = [10, 25, 50]

interface Post {
  post_id: number
  content: string
  video_url: string | null
  scheduled_time: string
  post_type: string
  status: string
  carousel_slides: string[] | null
  post_url?: string | null
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
}

export default function ReviewSchedule() {
  const { user, sessionToken } = useAuth()
  const email = user?.email ?? ''
  const qc = useQueryClient()
  const userTimezone = useUserTimezone()

  // Top-level view (Posts / Newsletters), synced to the ?tab= query param for deep-linking.
  const [searchParams, setSearchParams] = useSearchParams()
  const view: View = searchParams.get('tab') === 'newsletters' ? 'newsletters' : 'posts'
  const setView = (v: View) => setSearchParams(v === 'posts' ? {} : { tab: v }, { replace: true })

  // Filter / sort / pagination state
  const [filterStatus, setFilterStatus] = useState<Status>('ALL')
  const [sortOrder, setSortOrder] = useState<'asc' | 'desc'>('asc')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)

  // Selection / bulk state
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [bulkStatus, setBulkStatus] = useState<string>('approved')
  const [bulkDate, setBulkDate] = useState<string>('')

  // Single-post edit state
  const [editingPost, setEditingPost] = useState<Post | null>(null)

  // Quick-delete confirmation + regenerate-with-suggestions state
  const [confirmDeletePost, setConfirmDeletePost] = useState<Post | null>(null)
  const [regenGuidance, setRegenGuidance] = useState('')
  const [regenNotice, setRegenNotice] = useState<string | null>(null)

  const queryKey = ['posts', email, page, pageSize, sortOrder, filterStatus]

  const { data, isLoading } = useQuery<{ detail: PostsResponse }>({
    queryKey,
    queryFn: () => {
      const params = new URLSearchParams({
        email,
        page: String(page),
        page_size: String(pageSize),
        sort_order: sortOrder,
      })
      if (filterStatus !== 'ALL') params.set('status_filter', filterStatus)
      return api.get(`/posts/?${params.toString()}`).then((r) => r.data)
    },
    enabled: !!email,
  })

  const posts = data?.detail?.posts ?? []
  const total = data?.detail?.total ?? 0
  const totalPages = Math.max(1, Math.ceil(total / pageSize))

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
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['posts', email] })
      setEditingPost(null)
    },
  })

  const bulkUpdateMutation = useMutation({
    mutationFn: (body: { post_ids: number[]; status?: string; scheduled_datetime?: string }) =>
      api.post('/posts/bulk_update/', body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['posts', email] })
      setSelectedIds(new Set())
      setBulkDate('')
    },
  })

  const bulkDeleteMutation = useMutation({
    mutationFn: (post_ids: number[]) => api.delete('/posts/', { data: { post_ids } }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['posts', email] })
      setSelectedIds(new Set())
    },
  })

  // Quick per-post delete (reuses the same soft-delete endpoint as the bulk flow, one id).
  const deleteMutation = useMutation({
    mutationFn: (post_id: number) => api.delete('/posts/', { data: { post_ids: [post_id] } }),
    onSuccess: (_res, post_id) => {
      qc.invalidateQueries({ queryKey: ['posts', email] })
      setConfirmDeletePost(null)
      setSelectedIds((prev) => { const n = new Set(prev); n.delete(post_id); return n })
      if (editingPost?.post_id === post_id) setEditingPost(null)
    },
  })

  // Regenerate a single post with optional freeform guidance — runs the async generation task
  // server-side (honors saved settings + guidance), then resets the post to PENDING for re-review.
  const regenerateMutation = useMutation({
    mutationFn: (vars: { post_id: number; guidance: string }) =>
      api.post('/user/post/regenerate', {
        session_token: sessionToken,
        post_id: vars.post_id,
        guidance: vars.guidance.trim() || null,
      }),
    onSuccess: () => {
      setRegenGuidance('')
      setRegenNotice('Regenerating… this post will return to PENDING with fresh content shortly.')
      setEditingPost(null)
      setTimeout(() => qc.invalidateQueries({ queryKey: ['posts', email] }), 2500)
      setTimeout(() => setRegenNotice(null), 8000)
    },
    onError: () => setRegenNotice('Could not start regeneration — please try again.'),
  })

  const weeklyMutation = useMutation({
    mutationFn: () =>
      api.get(`/user_id/?email=${encodeURIComponent(email)}`).then((r) =>
        api.post(`/create_weekly_content/?user_id=${r.data.detail}`)
      ),
  })

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

  const allOnPageSelected = posts.length > 0 && posts.every((p) => selectedIds.has(p.post_id))

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <h1 className="text-2xl font-bold text-gray-800">Review</h1>
        {view === 'posts' && (
          <button
            onClick={() => weeklyMutation.mutate()}
            disabled={weeklyMutation.isPending}
            className="bg-green-600 text-white px-4 py-2 rounded-lg text-sm font-semibold hover:bg-green-700 disabled:opacity-50 transition-colors"
          >
            {weeklyMutation.isPending ? 'Generating content…' : 'Generate Weekly Content'}
          </button>
        )}
      </div>

      {/* Posts / Newsletters tabs */}
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

      {view === 'newsletters' && <NewsletterQueue userTimezone={userTimezone} />}

      {view === 'posts' && (
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

      {/* Sort + page-size controls */}
      <div className="flex items-center gap-3 flex-wrap text-sm">
        <button
          onClick={() => { setSortOrder((o) => (o === 'asc' ? 'desc' : 'asc')); resetPage() }}
          className="flex items-center gap-1 text-gray-600 border border-gray-300 rounded-lg px-3 py-1.5 hover:border-blue-400 transition-colors text-xs font-medium"
        >
          {sortOrder === 'asc' ? '↑ Oldest first' : '↓ Newest first'}
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
                if (!bulkDate) return
                bulkUpdateMutation.mutate({
                  post_ids: Array.from(selectedIds),
                  scheduled_datetime: new Date(bulkDate).toISOString(),
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
                {filterStatus === 'ALL'
                  ? 'Your scheduled posts will appear here. Generate your first week of content to get started.'
                  : 'No posts match this filter.'}
              </p>
              {filterStatus === 'ALL' && (
                <button
                  onClick={() => weeklyMutation.mutate()}
                  disabled={weeklyMutation.isPending}
                  className="bg-green-600 text-white px-6 py-2.5 rounded-lg text-sm font-semibold hover:bg-green-700 disabled:opacity-50 transition-colors"
                >
                  {weeklyMutation.isPending ? 'Generating content…' : 'Generate Weekly Content'}
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
                  {post.post_type === 'carousel' && post.carousel_slides && (
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

                <textarea
                  rows={6}
                  value={editingPost.content}
                  onChange={(e) => setEditingPost({ ...editingPost, content: e.target.value })}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
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
                        carousel_slides: newType === 'carousel' ? editingPost.carousel_slides : null,
                      })
                    }}
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                  >
                    {['text', 'video', 'carousel'].map((t) => (
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

                {editingPost.post_type === 'carousel' && editingPost.carousel_slides && (
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
                      value={editingPost.scheduled_time?.slice(0, 16) ?? ''}
                      onChange={(e) => setEditingPost({ ...editingPost, scheduled_time: e.target.value })}
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
                    PLUS the optional guidance, then resets the post to PENDING for re-review. */}
                {editingPost.post_type === 'text' && (
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
                      onClick={() => regenerateMutation.mutate({ post_id: editingPost.post_id, guidance: regenGuidance })}
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

      {/* Quick-delete confirmation — permanent, all history lost. */}
      {confirmDeletePost && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
             onClick={() => !deleteMutation.isPending && setConfirmDeletePost(null)}>
          <div className="bg-white rounded-lg shadow-xl max-w-sm w-full p-6 space-y-4"
               onClick={(e) => e.stopPropagation()}>
            <h3 className="text-base font-semibold text-gray-800">Delete this post?</h3>
            <p className="text-sm text-gray-600">
              This will permanently delete post #{confirmDeletePost.post_id} and <span className="font-semibold">ALL of its
              history</span> (stats, logs, generated media). This cannot be undone.
            </p>
            <p className="text-xs text-gray-500 line-clamp-3 bg-gray-50 rounded p-2">{confirmDeletePost.content}</p>
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
                {deleteMutation.isPending ? 'Deleting…' : 'Delete permanently'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
