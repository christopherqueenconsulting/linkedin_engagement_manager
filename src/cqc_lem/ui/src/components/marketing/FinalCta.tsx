import { useAuth } from '../../contexts/useAuth'
import { TRIAL_DAYS } from '../../constants/plans'
import CtaButton from './CtaButton'
import Section from './Section'

// The closing band (issue #1300). It carries its own headline rather than being a bare button on a
// gradient — the version it replaces asked people to "join thousands of professionals", a count
// nobody could source.
export default function FinalCta() {
  const { openLoginModal } = useAuth()

  return (
    <Section analyticsName="final_cta" tone="dark" width="max-w-3xl" className="text-center">
      <h2 className="text-section sm:text-section-lg font-semibold text-fg">
        Start with a month of posts you can actually approve
      </h2>
      <p className="mx-auto mt-4 max-w-xl text-lg leading-relaxed text-fg-muted">
        Sign in with LinkedIn, set your voice and topics, and see the first plan before you decide
        anything.
      </p>
      <div className="mt-8 flex justify-center">
        <CtaButton
          section="final_cta"
          cta="final_trial"
          label="Start free trial from the closing section"
          variant="primary"
          onClick={openLoginModal}
        >
          Start free trial
        </CtaButton>
      </div>
      <p className="mx-auto mt-4 max-w-measure text-sm text-fg-muted">
        {TRIAL_DAYS}-day free trial. No credit card. Cancel any time.
      </p>
    </Section>
  )
}
