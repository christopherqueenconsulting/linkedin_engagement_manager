import BrandShowcase from '../components/BrandShowcase'
import TutorialVideos from '../components/TutorialVideos'
import ApproachComparison from '../components/marketing/ApproachComparison'
import FeatureNarrative from '../components/marketing/FeatureNarrative'
import FinalCta from '../components/marketing/FinalCta'
import Hero from '../components/marketing/Hero'
import HowItWorks from '../components/marketing/HowItWorks'
import PricingSection from '../components/marketing/PricingSection'
import ProblemSection from '../components/marketing/ProblemSection'
import ProofStrip from '../components/marketing/ProofStrip'
import SafetySection from '../components/marketing/SafetySection'
import FAQ from './FAQ'

// The front page (issue #1300). It is only the section ORDER — every band owns its own copy, its
// own analytics name and its own measure.
//
// Two structural facts this file depends on and must not lose:
// - It renders inside `MarketingLayout`, NOT the app's `Layout`. The nav, the `<main>` landmark and
//   the footer come from there, so nothing here may draw a second one.
// - `id="features"` and `id="pricing"` are contract, not decoration: `TUTORIAL_FLOWS`
//   ('getting-started', `utilities/marketing/video_tutorials.py`) captures `/#features` and
//   `/#pricing` and raises on a missing anchor. `#pricing` never existed before this page, so that
//   flow was already broken at step 3.
export default function Landing() {
  return (
    <>
      <Hero />
      <ProofStrip />
      <ProblemSection />
      <HowItWorks />
      <FeatureNarrative />
      {/* Automated feature tutorials (issue #505) — renders nothing until one is produced. */}
      <TutorialVideos />
      {/* Real brand-account output (issue #1299) — renders nothing until the flag is on. */}
      <BrandShowcase />
      <SafetySection />
      <ApproachComparison />
      <PricingSection />
      {/* Served from `faq_entries` and kept current by the auto-FAQ pass (issue #506). */}
      <FAQ />
      <FinalCta />
    </>
  )
}
