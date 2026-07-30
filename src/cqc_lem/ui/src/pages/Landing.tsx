import { Link } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import TutorialVideos from '../components/TutorialVideos'
import FAQ from './FAQ'

export default function Landing() {
  const { openLoginModal } = useAuth()

  function scrollToFeatures() {
    document.getElementById('features')?.scrollIntoView({ behavior: 'smooth' })
  }

  return (
    <div className="min-h-screen bg-white">
      {/* Minimal nav */}
      <nav className="bg-white border-b border-gray-100 sticky top-0 z-10">
        <div className="max-w-6xl mx-auto px-4 flex items-center justify-between h-14">
          <span className="font-bold text-blue-600 text-lg">LEM</span>
          <button
            onClick={() => openLoginModal()}
            className="bg-blue-600 text-white px-4 py-1.5 rounded-lg text-sm font-semibold hover:bg-blue-700 transition-colors"
          >
            Get Started Free
          </button>
        </div>
      </nav>

      {/* Hero */}
      <section className="bg-gradient-to-br from-blue-600 to-purple-700 text-white py-24 px-4">
        <div className="max-w-4xl mx-auto text-center">
          <h1 className="text-5xl font-bold mb-6 leading-tight">
            LinkedIn Engagement Manager
          </h1>
          <p className="text-2xl font-light opacity-90 mb-4">
            Automate Your LinkedIn Success with AI-Powered Engagement
          </p>
          <p className="text-lg opacity-80 max-w-2xl mx-auto mb-10 leading-relaxed">
            Transform your LinkedIn presence with intelligent automation. Generate AI-powered content,
            schedule posts at optimal times, and engage authentically with your network.
          </p>
          <div className="flex flex-col sm:flex-row gap-4 justify-center">
            <button
              onClick={() => openLoginModal()}
              className="bg-white text-blue-700 px-8 py-3 rounded-lg font-bold text-base hover:bg-blue-50 transition-colors shadow-lg"
            >
              Get Started Free
            </button>
            <button
              onClick={scrollToFeatures}
              className="border-2 border-white text-white px-8 py-3 rounded-lg font-bold text-base hover:bg-white hover:text-blue-700 transition-colors"
            >
              See How It Works
            </button>
          </div>
        </div>
      </section>

      {/* Features */}
      <section id="features" className="py-20 px-4 bg-gray-50">
        <div className="max-w-6xl mx-auto">
          <h2 className="text-3xl font-bold text-center text-gray-800 mb-12">Powerful Features</h2>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-8">
            <div className="bg-white rounded-xl p-8 shadow-sm border border-gray-100">
              <div className="text-3xl mb-4">🤖</div>
              <h3 className="text-xl font-bold text-gray-800 mb-4">AI Content Generation</h3>
              <ul className="space-y-2 text-sm text-gray-600">
                <li className="flex items-start gap-2"><span className="text-green-500 mt-0.5">✓</span><span>Smart Carousels: auto-generate engaging visual content</span></li>
                <li className="flex items-start gap-2"><span className="text-green-500 mt-0.5">✓</span><span>Dynamic Text Posts: AI-crafted posts that resonate</span></li>
                <li className="flex items-start gap-2"><span className="text-green-500 mt-0.5">✓</span><span>Video Scripts: transform ideas into compelling videos</span></li>
                <li className="flex items-start gap-2"><span className="text-green-500 mt-0.5">✓</span><span>Sentiment Analysis: ensure brand-appropriate messaging</span></li>
              </ul>
            </div>
            <div className="bg-white rounded-xl p-8 shadow-sm border border-gray-100">
              <div className="text-3xl mb-4">⏰</div>
              <h3 className="text-xl font-bold text-gray-800 mb-4">Intelligent Scheduling</h3>
              <ul className="space-y-2 text-sm text-gray-600">
                <li className="flex items-start gap-2"><span className="text-green-500 mt-0.5">✓</span><span>AI Best Time: optimal timing predictions per day</span></li>
                <li className="flex items-start gap-2"><span className="text-green-500 mt-0.5">✓</span><span>Bulk Scheduling: plan weeks of content in advance</span></li>
                <li className="flex items-start gap-2"><span className="text-green-500 mt-0.5">✓</span><span>Preview &amp; Approve: review before publishing</span></li>
                <li className="flex items-start gap-2"><span className="text-green-500 mt-0.5">✓</span><span>Easy date-time picker interface</span></li>
              </ul>
            </div>
            <div className="bg-white rounded-xl p-8 shadow-sm border border-gray-100">
              <div className="text-3xl mb-4">🎯</div>
              <h3 className="text-xl font-bold text-gray-800 mb-4">Smart Engagement</h3>
              <ul className="space-y-2 text-sm text-gray-600">
                <li className="flex items-start gap-2"><span className="text-green-500 mt-0.5">✓</span><span>Auto-Comments: thoughtful, contextual responses</span></li>
                <li className="flex items-start gap-2"><span className="text-green-500 mt-0.5">✓</span><span>DM Management: engage with profile visitors</span></li>
                <li className="flex items-start gap-2"><span className="text-green-500 mt-0.5">✓</span><span>Reply Handling: manage post comments intelligently</span></li>
                <li className="flex items-start gap-2"><span className="text-green-500 mt-0.5">✓</span><span>Activity Feed: stay updated on your network</span></li>
              </ul>
            </div>
          </div>
        </div>
      </section>

      {/* Automated feature tutorials (issue #505) — renders nothing until one is produced */}
      <TutorialVideos />

      {/* Stats */}
      <section className="py-16 px-4 bg-white">
        <div className="max-w-4xl mx-auto">
          <h2 className="text-3xl font-bold text-center text-gray-800 mb-12">Trusted by Professionals Worldwide</h2>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-8 text-center">
            <div className="p-6 rounded-xl bg-blue-50">
              <div className="text-4xl font-bold text-blue-600 mb-2">2,500+</div>
              <div className="text-gray-600 font-medium">Active Users</div>
            </div>
            <div className="p-6 rounded-xl bg-purple-50">
              <div className="text-4xl font-bold text-purple-600 mb-2">50K+</div>
              <div className="text-gray-600 font-medium">Posts Automated</div>
            </div>
            <div className="p-6 rounded-xl bg-green-50">
              <div className="text-4xl font-bold text-green-600 mb-2">85%</div>
              <div className="text-gray-600 font-medium">Avg Engagement Boost</div>
            </div>
          </div>
        </div>
      </section>

      {/* Pricing */}
      <section className="py-20 px-4 bg-gray-50">
        <div className="max-w-6xl mx-auto">
          <h2 className="text-3xl font-bold text-center text-gray-800 mb-3">Choose Your Plan</h2>
          <p className="text-center text-gray-500 mb-12">Start with a <strong>free 14-day trial</strong> on any plan. No credit card required.</p>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-6">
            {/* Free */}
            <div className="bg-white rounded-xl border-2 border-gray-200 p-7 flex flex-col">
              <div className="text-center mb-6">
                <div className="text-2xl mb-2">🆓</div>
                <h3 className="text-lg font-bold text-blue-600">Free Trial</h3>
                <div className="text-4xl font-bold text-blue-600 mt-2">$0</div>
                <div className="text-gray-400 text-sm mt-1">14 days free</div>
              </div>
              <ul className="space-y-2 text-sm flex-1">
                <li className="flex items-start gap-2"><span className="text-green-500">✓</span><span>5 posts per week</span></li>
                <li className="flex items-start gap-2"><span className="text-green-500">✓</span><span>Basic AI content generation</span></li>
                <li className="flex items-start gap-2"><span className="text-green-500">✓</span><span>Standard scheduling</span></li>
                <li className="flex items-start gap-2"><span className="text-green-500">✓</span><span>Email support</span></li>
                <li className="flex items-start gap-2"><span className="text-red-400">✗</span><span className="text-gray-400">Advanced engagement</span></li>
                <li className="flex items-start gap-2"><span className="text-red-400">✗</span><span className="text-gray-400">Video creation</span></li>
                <li className="flex items-start gap-2"><span className="text-red-400">✗</span><span className="text-gray-400">Analytics dashboard</span></li>
              </ul>
              <button
                onClick={() => openLoginModal()}
                className="mt-6 w-full border border-blue-600 text-blue-600 py-2 rounded-lg text-sm font-semibold hover:bg-blue-50 transition-colors"
              >
                Get Started
              </button>
            </div>

            {/* Starter */}
            <div className="bg-white rounded-xl border-2 border-green-400 p-7 flex flex-col">
              <div className="text-center mb-6">
                <div className="text-2xl mb-2">🚀</div>
                <h3 className="text-lg font-bold text-green-600">Starter</h3>
                <div className="text-4xl font-bold text-green-600 mt-2">$29</div>
                <div className="text-gray-400 text-sm mt-1">per month</div>
              </div>
              <ul className="space-y-2 text-sm flex-1">
                <li className="flex items-start gap-2"><span className="text-green-500">✓</span><span>20 posts per week</span></li>
                <li className="flex items-start gap-2"><span className="text-green-500">✓</span><span>Full AI content suite</span></li>
                <li className="flex items-start gap-2"><span className="text-green-500">✓</span><span>Smart scheduling</span></li>
                <li className="flex items-start gap-2"><span className="text-green-500">✓</span><span>Basic engagement automation</span></li>
                <li className="flex items-start gap-2"><span className="text-green-500">✓</span><span>Email support</span></li>
                <li className="flex items-start gap-2"><span className="text-red-400">✗</span><span className="text-gray-400">Video creation</span></li>
                <li className="flex items-start gap-2"><span className="text-red-400">✗</span><span className="text-gray-400">Advanced analytics</span></li>
              </ul>
              <button
                onClick={() => openLoginModal()}
                className="mt-6 w-full bg-green-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-green-700 transition-colors"
              >
                Get Started
              </button>
            </div>

            {/* Professional - MOST POPULAR */}
            <div className="bg-white rounded-xl border-2 border-yellow-400 p-7 flex flex-col relative">
              <div className="absolute -top-3 left-1/2 -translate-x-1/2 bg-yellow-400 text-black text-xs font-bold px-4 py-1 rounded-full whitespace-nowrap">
                MOST POPULAR
              </div>
              <div className="text-center mb-6 mt-2">
                <div className="text-2xl mb-2">⭐</div>
                <h3 className="text-lg font-bold text-yellow-600">Professional</h3>
                <div className="text-4xl font-bold text-yellow-600 mt-2">$79</div>
                <div className="text-gray-400 text-sm mt-1">per month</div>
              </div>
              <ul className="space-y-2 text-sm flex-1">
                <li className="flex items-start gap-2"><span className="text-green-500">✓</span><span>Unlimited posts</span></li>
                <li className="flex items-start gap-2"><span className="text-green-500">✓</span><span>Full AI content suite</span></li>
                <li className="flex items-start gap-2"><span className="text-green-500">✓</span><span>Advanced scheduling</span></li>
                <li className="flex items-start gap-2"><span className="text-green-500">✓</span><span>Smart engagement automation</span></li>
                <li className="flex items-start gap-2"><span className="text-green-500">✓</span><span>Video creation</span></li>
                <li className="flex items-start gap-2"><span className="text-green-500">✓</span><span>Analytics dashboard</span></li>
                <li className="flex items-start gap-2"><span className="text-green-500">✓</span><span>Priority support</span></li>
              </ul>
              <button
                onClick={() => openLoginModal()}
                className="mt-6 w-full bg-yellow-400 text-black py-2 rounded-lg text-sm font-bold hover:bg-yellow-500 transition-colors"
              >
                Get Started
              </button>
            </div>

            {/* Enterprise */}
            <div className="bg-white rounded-xl border-2 border-purple-400 p-7 flex flex-col">
              <div className="text-center mb-6">
                <div className="text-2xl mb-2">🏢</div>
                <h3 className="text-lg font-bold text-purple-600">Enterprise</h3>
                <div className="text-4xl font-bold text-purple-600 mt-2">$199</div>
                <div className="text-gray-400 text-sm mt-1">per month</div>
              </div>
              <ul className="space-y-2 text-sm flex-1">
                <li className="flex items-start gap-2"><span className="text-green-500">✓</span><span>Everything in Professional</span></li>
                <li className="flex items-start gap-2"><span className="text-green-500">✓</span><span>Multi-team management</span></li>
                <li className="flex items-start gap-2"><span className="text-green-500">✓</span><span>Custom AI training</span></li>
                <li className="flex items-start gap-2"><span className="text-green-500">✓</span><span>Advanced analytics</span></li>
                <li className="flex items-start gap-2"><span className="text-green-500">✓</span><span>API access</span></li>
                <li className="flex items-start gap-2"><span className="text-green-500">✓</span><span>White-label options</span></li>
                <li className="flex items-start gap-2"><span className="text-green-500">✓</span><span>Dedicated support</span></li>
              </ul>
              <button
                onClick={() => openLoginModal()}
                className="mt-6 w-full bg-purple-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-purple-700 transition-colors"
              >
                Get Started
              </button>
            </div>
          </div>
        </div>
      </section>

      {/* FAQ — served from `faq_entries` and kept current by the auto-FAQ pass (issue #506) */}
      <FAQ />

      {/* CTA */}
      <section className="py-20 px-4 bg-gradient-to-br from-blue-600 to-purple-700 text-white text-center">
        <div className="max-w-2xl mx-auto">
          <h2 className="text-3xl font-bold mb-4">Ready to Transform Your LinkedIn Presence?</h2>
          <p className="text-lg opacity-85 mb-8">
            Join thousands of professionals who are already automating their LinkedIn success.
          </p>
          <button
            onClick={() => openLoginModal()}
            className="bg-white text-blue-700 px-10 py-3 rounded-lg font-bold text-base hover:bg-blue-50 transition-colors shadow-lg"
          >
            Start Free Trial
          </button>
        </div>
      </section>

      {/* Footer */}
      <footer className="bg-gray-900 text-gray-400 text-center py-8 px-4">
        <p className="text-sm">© 2024 Christopher Queen Consulting LLC. All rights reserved.</p>
        <p className="text-xs mt-1 opacity-70">Transform your LinkedIn presence with AI-powered automation.</p>
        <div className="flex items-center justify-center gap-4 mt-4 text-xs">
          <Link to="/privacy-policy" className="hover:text-white transition-colors">
            Privacy Policy
          </Link>
          <Link to="/terms-and-conditions" className="hover:text-white transition-colors">
            Terms and Conditions
          </Link>
        </div>
      </footer>
    </div>
  )
}
