import { useEffect, useState } from 'react'
import api from '../api/client'
import { FLAGS, useFeatureFlag } from '../hooks/useFeatureFlags'

// Automated feature tutorials (issue #505). The video agent writes a manifest of finished
// tutorials into the shared assets volume; this reads it and embeds whatever is there. Nothing
// produced yet -> renders nothing, so the page never shows an empty "videos" shell.
//
// Gated by the same `tutorial-videos-enabled` flag that gates the PRODUCER (issue #651), so one
// runtime toggle covers both filming the tutorials and showing them — no deploy to pull the
// section. Fallback is OFF: an unreachable API must not publish a marketing section.
interface TutorialRecord {
  flow: string
  title: string
  description?: string | null
  video_url: string
  captions_url?: string | null
  thumbnail_url?: string | null
  youtube_url?: string | null
  duration_seconds?: number
}

const MANIFEST = '/assets?file_name=videos/tutorials/manifest.json'

export default function TutorialVideos() {
  const [videos, setVideos] = useState<TutorialRecord[]>([])
  const enabled = useFeatureFlag(FLAGS.tutorialVideos)

  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    api
      .get(MANIFEST)
      .then((r) => {
        const records: TutorialRecord[] = Object.values(r.data?.videos ?? {})
        if (!cancelled) setVideos(records.filter((v) => v?.video_url))
      })
      // No manifest yet (404) is the normal pre-first-tutorial state, not an error worth showing.
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [enabled])

  if (!enabled || videos.length === 0) return null

  return (
    <section id="tutorials" className="py-16 px-4 bg-white">
      <div className="max-w-5xl mx-auto">
        <h2 className="text-headline sm:text-display font-bold text-center text-ink-900 mb-12">See it in action</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-8">
          {videos.map((video) => (
            <div key={video.flow} className="rounded-card border border-line-200 shadow-card overflow-hidden">
              <video
                className="w-full bg-black"
                controls
                preload="none"
                poster={video.thumbnail_url ?? undefined}
              >
                <source src={video.video_url} type="video/mp4" />
                {video.captions_url && (
                  <track kind="captions" src={video.captions_url} srcLang="en" label="English" default />
                )}
              </video>
              <div className="p-5">
                <h3 className="font-bold text-ink-900 mb-1">{video.title}</h3>
                {video.description && (
                  <p className="text-sm text-ink-600 leading-relaxed line-clamp-3">{video.description}</p>
                )}
                {video.youtube_url && (
                  <a
                    href={video.youtube_url}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-block mt-3 text-sm font-medium text-brand-600 hover:underline"
                  >
                    Watch on YouTube →
                  </a>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  )
}
