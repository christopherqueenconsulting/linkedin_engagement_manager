import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import api from '../../api/client'
import LinkedInPostPreview from '../../components/LinkedInPostPreview'
import TopicAuthorityMeter from '../../components/TopicAuthorityMeter'
import { useAuth } from '../../contexts/AuthContext'
import { useUserTimezone } from '../../hooks/useUserTimezone'
import { zonedInputToUtcIso } from '../../utils/datetime'

const POST_TYPES = ['TEXT', 'VIDEO', 'CAROUSEL'] as const
type PostType = typeof POST_TYPES[number]

const CAROUSEL_STAGES = [
  { value: 'awareness', label: 'Awareness', hint: 'Educational tips, insights, how-tos' },
  { value: 'consideration', label: 'Consideration', hint: 'Step-by-step frameworks, processes' },
  { value: 'decision', label: 'Decision', hint: 'Stats, data reveals, proof points' },
  { value: 'personal', label: 'Personal Story', hint: 'Case studies, transformations, journeys' },
]

const DAY_NAMES = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']

function getBestPostingTime(date: Date): { time: string; display: string; dayName: string } {
  const day = date.getDay()
  let hour: number
  if (day === 1 || day === 5) {
    hour = 8
  } else if (day === 2 || day === 3 || day === 4) {
    hour = 7
  } else {
    hour = 10
  }
  const hh = String(hour).padStart(2, '0')
  const ampm = hour < 12 ? 'AM' : 'PM'
  const display12 = `${hour > 12 ? hour - 12 : hour}:00 ${ampm}`
  return { time: `${hh}:00`, dayName: DAY_NAMES[day], display: display12 }
}

interface CarouselTemplate {
  key: string
  label: string
  description: string
}

export default function ComposePost({ onNavigateTab }: { onNavigateTab?: (tab: string) => void }) {
  const { user, sessionToken } = useAuth()
  const email = user?.email ?? ''

  const [content, setContent] = useState('')
  const [postType, setPostType] = useState<PostType>('TEXT')
  const [videoUrl, setVideoUrl] = useState('')
  const [slides, setSlides] = useState<string[]>([''])
  const [scheduledAt, setScheduledAt] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [result, setResult] = useState<{ ok: boolean; msg: string } | null>(null)
  const [useAvatar, setUseAvatar] = useState(false)
  const [videoQuality, setVideoQuality] = useState<'standard' | 'premium' | 'premium_top'>('standard')

  // Carousel AI generation state
  const [carouselMode, setCarouselMode] = useState<'manual' | 'ai'>('ai')
  const [selectedStage, setSelectedStage] = useState('awareness')
  const [selectedTemplate, setSelectedTemplate] = useState('')
  const [generatingSlides, setGeneratingSlides] = useState(false)
  const [generatedSlideUrls, setGeneratedSlideUrls] = useState<string[]>([])
  const [generateError, setGenerateError] = useState<string | null>(null)

  const userTimezone = useUserTimezone()

  const { data: avatarData } = useQuery({
    queryKey: ['avatar-credits', sessionToken],
    queryFn: async () => {
      const r = await api.get('/avatar/credits', { params: { session_token: sessionToken } })
      return r.data.detail as { balance: number; active_avatar: { trigger_word: string; status: string } | null }
    },
    enabled: !!sessionToken,
  })

  const { data: videoCreditData } = useQuery({
    queryKey: ['video-credits', sessionToken],
    queryFn: async () => {
      const r = await api.get('/video/credits', { params: { session_token: sessionToken } })
      return r.data.detail as { balance: number }
    },
    enabled: !!sessionToken,
  })
  const videoCredits = videoCreditData?.balance ?? 0

  const { data: templatesData } = useQuery({
    queryKey: ['carousel-templates'],
    queryFn: async () => {
      const r = await api.get('/carousel-templates')
      return r.data.detail.templates as CarouselTemplate[]
    },
  })

  // Focus topics drive the Topic Authority meter below the preview (issue #384). Reuse the same
  // engagement-preferences query key/shape as the Account settings so it's served from cache.
  const { data: engPrefs } = useQuery({
    queryKey: ['engagement-preferences', sessionToken],
    queryFn: () =>
      api
        .get(`/user/engagement-preferences?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as { focus_topics?: string[] }),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })
  const focusTopics = engPrefs?.focus_topics ?? []

  const templates = templatesData ?? []
  const activeAvatar = avatarData?.active_avatar
  const hasActiveAvatar = activeAvatar?.status === 'succeeded'
  const MAX_CHARS = 3000

  const bestTimeSuggestion = scheduledAt ? getBestPostingTime(new Date(scheduledAt)) : null

  function applyBestTime() {
    if (!scheduledAt || !bestTimeSuggestion) return
    const datePart = scheduledAt.slice(0, 10)
    setScheduledAt(`${datePart}T${bestTimeSuggestion.time}`)
  }

  function addSlide() {
    setSlides((prev) => [...prev, ''])
  }

  function removeSlide(idx: number) {
    setSlides((prev) => prev.filter((_, i) => i !== idx))
  }

  function updateSlide(idx: number, value: string) {
    setSlides((prev) => prev.map((s, i) => (i === idx ? value : s)))
  }

  async function handleGenerateSlides() {
    if (!sessionToken) { setGenerateError('Not logged in.'); return }
    setGeneratingSlides(true)
    setGenerateError(null)
    setGeneratedSlideUrls([])
    try {
      const r = await api.post('/generate-carousel', {
        session_token: sessionToken,
        stage: selectedStage,
        template: selectedTemplate || null,
      })
      const { slide_urls, caption } = r.data.detail
      setGeneratedSlideUrls(slide_urls)
      if (caption && !content.trim()) {
        setContent(caption)
      }
      setSlides(slide_urls)
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setGenerateError(msg || 'Slide generation failed. Please try again.')
    } finally {
      setGeneratingSlides(false)
    }
  }

  // "Approve & Schedule" creates the post approved (ready to publish); "Save Draft" holds it as
  // pending for later review. Auto-generated content sets its own status server-side.
  async function handleSubmit(e: React.FormEvent, status: 'pending' | 'approved' = 'approved') {
    e.preventDefault()
    if (!email) { setResult({ ok: false, msg: 'Set your email in Account settings first.' }); return }
    if (!content.trim()) { setResult({ ok: false, msg: 'Post content is required.' }); return }
    if (!scheduledAt) { setResult({ ok: false, msg: 'Scheduled date/time is required.' }); return }
    // The picker is a bare wall clock — read it in the user's timezone, not the browser's.
    const scheduledUtc = zonedInputToUtcIso(scheduledAt, userTimezone)
    if (!scheduledUtc) { setResult({ ok: false, msg: 'Scheduled date/time is not valid.' }); return }
    if (postType === 'CAROUSEL') {
      const activeSlides = carouselMode === 'ai' ? generatedSlideUrls : slides.filter((s) => s.trim())
      if (activeSlides.length === 0) {
        setResult({ ok: false, msg: carouselMode === 'ai' ? 'Generate slides first before scheduling.' : 'At least one slide is required.' })
        return
      }
    }

    setSubmitting(true)
    setResult(null)
    try {
      const payload: Record<string, unknown> = {
        content,
        post_type: postType.toLowerCase(),
        scheduled_datetime: scheduledUtc,
        email,
        status,
        use_avatar: postType !== 'TEXT' && useAvatar && hasActiveAvatar,
      }
      if (postType === 'VIDEO') {
        payload.video_url = videoUrl || null
        payload.video_quality = videoQuality
      }
      if (postType === 'CAROUSEL') {
        payload.carousel_slides = carouselMode === 'ai'
          ? generatedSlideUrls
          : slides.filter((s) => s.trim())
      }
      await api.post('/schedule_post/', payload)
      setResult({ ok: true, msg: status === 'approved' ? 'Post approved & scheduled!' : 'Draft saved.' })
      setContent('')
      setVideoUrl('')
      setSlides([''])
      setScheduledAt('')
      setGeneratedSlideUrls([])
    } catch {
      setResult({ ok: false, msg: 'Failed to schedule post. Please try again.' })
    } finally {
      setSubmitting(false)
    }
  }

  const previewContent =
    postType === 'CAROUSEL'
      ? (content || 'Your carousel caption will appear here…')
      : content || 'Your post content will appear here…'

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <div className="space-y-4">
        {!email && (
          <div className="bg-red-50 border border-red-200 rounded-lg px-4 py-3 text-sm text-red-700">
            No account email set. Please{' '}
            <Link to="/account" className="font-semibold underline hover:text-red-900">
              go to Account settings
            </Link>{' '}
            to configure your email before scheduling posts.
          </div>
        )}

        <div className="bg-blue-50 border border-blue-200 rounded-lg px-4 py-2 text-xs text-blue-700">
          Times are in your profile timezone: <span className="font-semibold">{userTimezone}</span>
        </div>

        <form onSubmit={handleSubmit} className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-4">
          {/* Post type selector */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Post Type</label>
            <div className="flex gap-2 flex-wrap">
              {POST_TYPES.map((t) => (
                <button
                  key={t}
                  type="button"
                  onClick={() => setPostType(t)}
                  className={`px-3 py-1.5 rounded-full text-xs font-semibold border transition-colors ${
                    postType === t
                      ? 'bg-blue-600 text-white border-blue-600'
                      : 'bg-white text-gray-600 border-gray-300 hover:border-blue-400'
                  }`}
                >
                  {t}
                </button>
              ))}
            </div>
          </div>

          {/* Content — caption for all types */}
          <div>
            <div className="flex justify-between mb-1">
              <label className="text-sm font-medium text-gray-700">
                {postType === 'CAROUSEL' ? 'Caption / Intro Text' : 'Post Content'}
              </label>
              <span className={`text-xs ${content.length > MAX_CHARS ? 'text-red-500' : 'text-gray-400'}`}>
                {content.length} / {MAX_CHARS}
              </span>
            </div>
            <textarea
              value={content}
              onChange={(e) => setContent(e.target.value)}
              rows={postType === 'CAROUSEL' ? 3 : 8}
              maxLength={MAX_CHARS}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
              placeholder={postType === 'CAROUSEL' ? 'Optional intro text for the carousel post… (auto-filled after generation)' : 'What would you like to share?'}
            />
          </div>

          {/* VIDEO: video URL field */}
          {postType === 'VIDEO' && (
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Video URL</label>
              <input
                type="url"
                value={videoUrl}
                onChange={(e) => setVideoUrl(e.target.value)}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                placeholder="https://example.com/video.mp4"
              />
              <p className="mt-1 text-xs text-gray-400">Direct URL to the video file that will be uploaded to LinkedIn.</p>
            </div>
          )}

          {/* VIDEO: quality tier (premium tiers spend video credits when LEM generates the video) */}
          {postType === 'VIDEO' && (
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Video Quality</label>
              <div className="space-y-2">
                {[
                  { key: 'standard' as const, label: 'Standard', desc: 'Included free with your plan', cost: 0 },
                  { key: 'premium' as const, label: 'Premium — Veo + audio', desc: 'Realistic video with native sound', cost: 1 },
                  { key: 'premium_top' as const, label: 'Premium Max — top realism', desc: 'Highest-realism Veo + audio', cost: 3 },
                ].map((opt) => {
                  const affordable = opt.cost === 0 || videoCredits >= opt.cost
                  return (
                    <label
                      key={opt.key}
                      className={`flex items-center gap-3 border rounded-lg px-3 py-2 cursor-pointer transition-colors ${
                        videoQuality === opt.key ? 'border-blue-500 bg-blue-50' : 'border-gray-200'
                      } ${!affordable ? 'opacity-50' : ''}`}
                    >
                      <input
                        type="radio"
                        name="videoQuality"
                        value={opt.key}
                        checked={videoQuality === opt.key}
                        disabled={!affordable}
                        onChange={() => setVideoQuality(opt.key)}
                      />
                      <div className="flex-1">
                        <p className="text-sm font-medium text-gray-800">
                          {opt.label}
                          {opt.cost > 0 && (
                            <span className="ml-2 text-xs text-blue-700">
                              {opt.cost} credit{opt.cost > 1 ? 's' : ''}
                            </span>
                          )}
                        </p>
                        <p className="text-xs text-gray-500">{opt.desc}</p>
                      </div>
                    </label>
                  )
                })}
              </div>
              <p className="mt-1 text-xs text-gray-400">
                You have <span className="font-semibold">{videoCredits}</span> premium video credit
                {videoCredits === 1 ? '' : 's'}.{' '}
                <a href="/account" className="text-blue-600 hover:underline">Buy more</a>
              </p>
            </div>
          )}

          {/* CAROUSEL: AI generator + manual fallback */}
          {postType === 'CAROUSEL' && (
            <div className="space-y-4">
              {/* Mode tabs */}
              <div className="flex gap-1 bg-gray-100 rounded-lg p-1 w-fit">
                {(['ai', 'manual'] as const).map((m) => (
                  <button
                    key={m}
                    type="button"
                    onClick={() => setCarouselMode(m)}
                    className={`px-3 py-1 rounded-md text-xs font-semibold transition-colors ${
                      carouselMode === m
                        ? 'bg-white text-blue-700 shadow-sm'
                        : 'text-gray-500 hover:text-gray-700'
                    }`}
                  >
                    {m === 'ai' ? 'AI Generate' : 'Manual Slides'}
                  </button>
                ))}
              </div>

              {carouselMode === 'ai' ? (
                <div className="space-y-4 border border-blue-100 rounded-lg p-4 bg-blue-50">
                  {/* Stage picker */}
                  <div>
                    <label className="block text-xs font-semibold text-gray-600 mb-2">Content Stage</label>
                    <div className="grid grid-cols-2 gap-2">
                      {CAROUSEL_STAGES.map((s) => (
                        <button
                          key={s.value}
                          type="button"
                          onClick={() => setSelectedStage(s.value)}
                          className={`text-left px-3 py-2 rounded-lg border text-xs transition-colors ${
                            selectedStage === s.value
                              ? 'bg-blue-600 text-white border-blue-600'
                              : 'bg-white text-gray-700 border-gray-200 hover:border-blue-300'
                          }`}
                        >
                          <div className="font-semibold">{s.label}</div>
                          <div className={`mt-0.5 ${selectedStage === s.value ? 'text-blue-100' : 'text-gray-400'}`}>
                            {s.hint}
                          </div>
                        </button>
                      ))}
                    </div>
                  </div>

                  {/* Template picker */}
                  {templates.length > 0 && (
                    <div>
                      <label className="block text-xs font-semibold text-gray-600 mb-2">
                        Visual Template <span className="font-normal text-gray-400">(optional — auto-picked if blank)</span>
                      </label>
                      <div className="space-y-1.5">
                        {templates.map((t) => (
                          <button
                            key={t.key}
                            type="button"
                            onClick={() => setSelectedTemplate(selectedTemplate === t.key ? '' : t.key)}
                            className={`w-full text-left px-3 py-2 rounded-lg border text-xs transition-colors ${
                              selectedTemplate === t.key
                                ? 'bg-indigo-600 text-white border-indigo-600'
                                : 'bg-white text-gray-700 border-gray-200 hover:border-indigo-300'
                            }`}
                          >
                            <span className="font-semibold">{t.label}</span>
                            <span className={`ml-2 ${selectedTemplate === t.key ? 'text-indigo-200' : 'text-gray-400'}`}>
                              {t.description}
                            </span>
                          </button>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* Generate button */}
                  <button
                    type="button"
                    onClick={handleGenerateSlides}
                    disabled={generatingSlides}
                    className="w-full bg-indigo-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-indigo-700 disabled:opacity-50 transition-colors flex items-center justify-center gap-2"
                  >
                    {generatingSlides ? (
                      <>
                        <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none">
                          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                        </svg>
                        Generating slides… (30–60 sec)
                      </>
                    ) : (
                      'Generate AI Carousel Slides'
                    )}
                  </button>

                  {generateError && (
                    <p className="text-xs text-red-600 font-medium">{generateError}</p>
                  )}

                  {generatedSlideUrls.length > 0 && (
                    <div className="space-y-2">
                      <p className="text-xs font-semibold text-green-700">
                        {generatedSlideUrls.length} slides generated
                      </p>
                      <div className="flex flex-wrap gap-2">
                        {generatedSlideUrls.map((url, i) => (
                          <a
                            key={i}
                            href={url}
                            target="_blank"
                            rel="noreferrer"
                            className="text-xs text-blue-600 underline hover:text-blue-800"
                          >
                            Slide {i + 1}
                          </a>
                        ))}
                      </div>
                      <p className="text-xs text-gray-400">Click each link to preview the slide image. Not happy? Click Generate again to regenerate.</p>
                    </div>
                  )}
                </div>
              ) : (
                /* Manual slide builder */
                <div className="space-y-3">
                  <label className="block text-sm font-medium text-gray-700">
                    Slides <span className="text-gray-400 font-normal text-xs">(images auto-fetched from Pexels at post time)</span>
                  </label>
                  {slides.map((slide, idx) => (
                    <div key={idx} className="flex gap-2 items-start">
                      <span className="mt-2 text-xs text-gray-400 font-medium w-5 shrink-0">{idx + 1}.</span>
                      <textarea
                        rows={2}
                        value={slide}
                        onChange={(e) => updateSlide(idx, e.target.value)}
                        className="flex-1 border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
                        placeholder={`Slide ${idx + 1} text…`}
                      />
                      {slides.length > 1 && (
                        <button
                          type="button"
                          onClick={() => removeSlide(idx)}
                          className="mt-1.5 text-red-400 hover:text-red-600 text-xs font-semibold px-2 py-1 rounded border border-red-200 hover:border-red-400 transition-colors"
                        >
                          Remove
                        </button>
                      )}
                    </div>
                  ))}
                  <button
                    type="button"
                    onClick={addSlide}
                    className="text-sm text-blue-600 font-semibold hover:text-blue-800 transition-colors"
                  >
                    + Add Slide
                  </button>
                </div>
              )}
            </div>
          )}

          {/* Avatar toggle — only for image/video posts */}
          {postType !== 'TEXT' && (
            <div className="flex items-center justify-between border border-gray-200 rounded-lg px-4 py-3 bg-gray-50">
              <div>
                <p className="text-sm font-medium text-gray-700">Include my avatar</p>
                {hasActiveAvatar ? (
                  <p className="text-xs text-gray-500">
                    Uses <span className="font-mono font-semibold">{activeAvatar?.trigger_word}</span> in the image prompt
                  </p>
                ) : (
                  <p className="text-xs text-gray-400">
                    No active avatar.{' '}
                    <Link to="/avatars" className="text-blue-600 underline hover:text-blue-800">
                      Train one on the Avatars page.
                    </Link>
                  </p>
                )}
              </div>
              <label className="relative inline-flex items-center cursor-pointer">
                <input
                  type="checkbox"
                  className="sr-only peer"
                  checked={useAvatar && hasActiveAvatar}
                  disabled={!hasActiveAvatar}
                  onChange={(e) => setUseAvatar(e.target.checked)}
                />
                <div className="w-10 h-5 bg-gray-200 peer-focus:ring-2 peer-focus:ring-blue-400 rounded-full peer peer-checked:bg-blue-600 peer-disabled:opacity-40 transition-colors after:content-[''] after:absolute after:top-0.5 after:left-0.5 after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:after:translate-x-5" />
              </label>
            </div>
          )}

          {/* Schedule date/time */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Schedule Date &amp; Time <span className="font-normal text-gray-400">({userTimezone})</span>
            </label>
            <input
              type="datetime-local"
              value={scheduledAt}
              onChange={(e) => setScheduledAt(e.target.value)}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            {bestTimeSuggestion && (
              <div className="mt-2 bg-blue-50 border border-blue-200 rounded-lg px-3 py-2 flex items-center justify-between gap-2">
                <p className="text-xs text-blue-700">
                  Best time to post on <strong>{bestTimeSuggestion.dayName}</strong>: <strong>{bestTimeSuggestion.display}</strong>
                </p>
                <button
                  type="button"
                  onClick={applyBestTime}
                  className="text-xs text-blue-700 font-semibold underline hover:text-blue-900 whitespace-nowrap"
                >
                  Use this time
                </button>
              </div>
            )}
          </div>

          {result && (
            <p className={`text-sm font-medium ${result.ok ? 'text-green-600' : 'text-red-600'}`}>
              {result.msg}
              {result.ok && onNavigateTab && (
                <button type="button" onClick={() => onNavigateTab('review')}
                  className="ml-2 font-semibold text-blue-600 hover:underline">
                  View in Review &rarr;
                </button>
              )}
            </p>
          )}

          <div className="grid grid-cols-2 gap-2">
            <button
              type="button"
              onClick={(e) => handleSubmit(e, 'pending')}
              disabled={submitting || content.length > MAX_CHARS}
              className="bg-gray-100 text-gray-700 py-2 rounded-lg text-sm font-semibold hover:bg-gray-200 disabled:opacity-50 transition-colors"
            >
              {submitting ? 'Saving…' : 'Save Draft'}
            </button>
            <button
              type="submit"
              disabled={submitting || content.length > MAX_CHARS}
              className="bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors"
            >
              {submitting ? 'Scheduling…' : 'Approve & Schedule'}
            </button>
          </div>
        </form>
      </div>

      <div className="space-y-4">
        <h2 className="text-xl font-semibold text-gray-700">Preview</h2>
        <LinkedInPostPreview
          content={previewContent}
          author={email.split('@')[0] || 'You'}
          headline="LinkedIn Member"
          postType={postType.toLowerCase()}
          videoUrl={postType === 'VIDEO' ? videoUrl || null : null}
          slides={
            postType === 'CAROUSEL'
              ? (carouselMode === 'ai' ? generatedSlideUrls : slides.filter((s) => s.trim()))
              : null
          }
        />
        {postType === 'TEXT' && (
          <TopicAuthorityMeter content={content} focusTopics={focusTopics} />
        )}
        {postType === 'CAROUSEL' && generatedSlideUrls.length > 0 && (
          <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-4">
            <p className="text-sm font-semibold text-gray-700 mb-3">Slide Previews</p>
            <div className="grid grid-cols-2 gap-3">
              {generatedSlideUrls.slice(0, 4).map((url, i) => (
                <a key={i} href={url} target="_blank" rel="noreferrer" className="block">
                  <img
                    src={url}
                    alt={`Slide ${i + 1}`}
                    className="w-full rounded-lg border border-gray-200 hover:opacity-90 transition-opacity"
                    onError={(e) => { (e.target as HTMLImageElement).style.display = 'none' }}
                  />
                </a>
              ))}
            </div>
            {generatedSlideUrls.length > 4 && (
              <p className="text-xs text-gray-400 mt-2">+{generatedSlideUrls.length - 4} more slides</p>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
