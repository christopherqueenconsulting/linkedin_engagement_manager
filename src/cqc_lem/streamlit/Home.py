#  Copyright (c) 2024. Christopher Queen Consulting LLC (http://www.ChristopherQueenConsulting.com/)
import os

import streamlit as st

from cqc_lem.streamlit.utils import get_custom_css

# Initialize session state variables
# init_session_state()

def render_hero_section():
    """Render the hero section with value proposition"""
    st.markdown("""
    <div style="text-align: center; padding: 60px 0; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border-radius: 15px; margin-bottom: 40px;">
        <h1 style="font-size: 3.5rem; margin-bottom: 20px; font-weight: 700;">
            🚀 LinkedIn Engagement Manager
        </h1>
        <h2 style="font-size: 1.8rem; margin-bottom: 30px; font-weight: 300; opacity: 0.9;">
            Automate Your LinkedIn Success with AI-Powered Engagement
        </h2>
        <p style="font-size: 1.2rem; max-width: 800px; margin: 0 auto 40px; line-height: 1.6; opacity: 0.8;">
            Transform your LinkedIn presence with intelligent automation. Generate AI-powered content, 
            schedule posts at optimal times, and engage authentically with your network—all while you focus on what matters most.
        </p>
    </div>
    """, unsafe_allow_html=True)

def render_features_section():
    """Render the key features section"""
    st.markdown("## 🌟 Powerful Features")
    
    col1, col2, col3 = st.columns(3)
    
    with col1:
        st.markdown("""
        ### 🤖 AI Content Generation
        - **Smart Carousels**: Auto-generate engaging visual content
        - **Dynamic Text Posts**: AI-crafted posts that resonate
        - **Video Creation**: Transform ideas into compelling videos
        - **Sentiment Analysis**: Ensure brand-appropriate messaging
        """)
    
    with col2:
        st.markdown("""
        ### ⏰ Intelligent Scheduling
        - **Optimal Timing**: AI-powered best time predictions
        - **Bulk Scheduling**: Plan weeks of content in advance
        - **Date-Time Picker**: Easy scheduling interface
        - **Preview & Approve**: Review before publishing
        """)
    
    with col3:
        st.markdown("""
        ### 🎯 Smart Engagement
        - **Auto-Comments**: Thoughtful, contextual responses
        - **Profile Messaging**: Engage with profile viewers
        - **Reply Management**: Handle post comments intelligently
        - **Activity Summaries**: Stay updated on your network
        """)

def render_pricing_section():
    """Render the pricing tiers section"""
    st.markdown("## 💎 Choose Your Plan")
    st.markdown("Start with a **free 14-day trial** on any plan. No credit card required.")
    
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.markdown("""
        <div style="border: 2px solid #e1e5e9; border-radius: 12px; padding: 30px; text-align: center; height: 600px; display: flex; flex-direction: column;">
            <h3 style="color: #5865f2; margin-bottom: 15px;">🆓 Free Trial</h3>
            <div style="font-size: 2.5rem; font-weight: bold; color: #5865f2; margin-bottom: 10px;">$0</div>
            <div style="color: #6c757d; margin-bottom: 30px;">14 days free</div>
            <div style="flex-grow: 1;">
                <div style="text-align: left;">
                    ✅ 5 posts per week<br>
                    ✅ Basic AI content generation<br>
                    ✅ Standard scheduling<br>
                    ✅ Email support<br>
                    ❌ Advanced engagement<br>
                    ❌ Video creation<br>
                    ❌ Analytics dashboard<br>
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)
    
    with col2:
        st.markdown("""
        <div style="border: 2px solid #28a745; border-radius: 12px; padding: 30px; text-align: center; height: 600px; display: flex; flex-direction: column;">
            <h3 style="color: #28a745; margin-bottom: 15px;">🚀 Starter</h3>
            <div style="font-size: 2.5rem; font-weight: bold; color: #28a745; margin-bottom: 10px;">$29</div>
            <div style="color: #6c757d; margin-bottom: 30px;">per month</div>
            <div style="flex-grow: 1;">
                <div style="text-align: left;">
                    ✅ 20 posts per week<br>
                    ✅ Full AI content suite<br>
                    ✅ Smart scheduling<br>
                    ✅ Basic engagement automation<br>
                    ✅ Email support<br>
                    ❌ Video creation<br>
                    ❌ Advanced analytics<br>
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)
    
    with col3:
        st.markdown("""
        <div style="border: 3px solid #ffc107; border-radius: 12px; padding: 30px; text-align: center; height: 600px; display: flex; flex-direction: column; position: relative;">
            <div style="position: absolute; top: -12px; left: 50%; transform: translateX(-50%); background: #ffc107; color: #000; padding: 5px 20px; border-radius: 20px; font-size: 0.8rem; font-weight: bold;">MOST POPULAR</div>
            <h3 style="color: #ffc107; margin-bottom: 15px; margin-top: 10px;">⭐ Professional</h3>
            <div style="font-size: 2.5rem; font-weight: bold; color: #ffc107; margin-bottom: 10px;">$79</div>
            <div style="color: #6c757d; margin-bottom: 30px;">per month</div>
            <div style="flex-grow: 1;">
                <div style="text-align: left;">
                    ✅ Unlimited posts<br>
                    ✅ Full AI content suite<br>
                    ✅ Advanced scheduling<br>
                    ✅ Smart engagement automation<br>
                    ✅ Video creation<br>
                    ✅ Analytics dashboard<br>
                    ✅ Priority support<br>
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)
    
    with col4:
        st.markdown("""
        <div style="border: 2px solid #6f42c1; border-radius: 12px; padding: 30px; text-align: center; height: 600px; display: flex; flex-direction: column;">
            <h3 style="color: #6f42c1; margin-bottom: 15px;">🏢 Enterprise</h3>
            <div style="font-size: 2.5rem; font-weight: bold; color: #6f42c1; margin-bottom: 10px;">$199</div>
            <div style="color: #6c757d; margin-bottom: 30px;">per month</div>
            <div style="flex-grow: 1;">
                <div style="text-align: left;">
                    ✅ Everything in Professional<br>
                    ✅ Multi-team management<br>
                    ✅ Custom AI training<br>
                    ✅ Advanced analytics<br>
                    ✅ API access<br>
                    ✅ White-label options<br>
                    ✅ Dedicated support<br>
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)

def render_cta_section():
    """Render the call-to-action section"""
    st.markdown("---")
    
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.markdown("""
        <div style="text-align: center; padding: 40px; background: #f8f9fa; border-radius: 15px;">
            <h2 style="color: #333; margin-bottom: 20px;">Ready to Transform Your LinkedIn Presence?</h2>
            <p style="font-size: 1.1rem; color: #666; margin-bottom: 30px;">
                Join thousands of professionals who are already automating their LinkedIn success.
            </p>
        </div>
        """, unsafe_allow_html=True)
        
        col_a, col_b, col_c = st.columns([1, 2, 1])
        with col_b:
            if st.button("🚀 Start Your Free Trial", type="primary", use_container_width=True):
                st.switch_page("pages/1_My_Account.py")

def render_social_proof():
    """Render social proof and testimonials"""
    st.markdown("## 📊 Trusted by Professionals Worldwide")
    
    col1, col2, col3 = st.columns(3)
    
    with col1:
        st.metric("Active Users", "2,500+", "↗️ 45%")
    
    with col2:
        st.metric("Posts Automated", "50K+", "↗️ 230%")
    
    with col3:
        st.metric("Engagement Boost", "85%", "↗️ Average")

def render_faq_section():
    """Render FAQ section"""
    st.markdown("## ❓ Frequently Asked Questions")
    
    with st.expander("How does the AI content generation work?"):
        st.write("""
        Our AI analyzes your industry, writing style, and audience engagement patterns to create 
        personalized content that matches your brand voice. It uses advanced language models to 
        generate compelling posts, carousels, and video scripts while ensuring appropriate tone and sentiment.
        """)
    
    with st.expander("Is my LinkedIn account safe?"):
        st.write("""
        Absolutely. We use LinkedIn's official API and follow all platform guidelines. Your credentials 
        are encrypted and stored securely. We never perform actions without your explicit approval 
        (unless you enable auto-approval features).
        """)
    
    with st.expander("Can I cancel anytime?"):
        st.write("""
        Yes, you can cancel your subscription at any time. No long-term contracts or cancellation fees. 
        Your account will remain active until the end of your current billing period.
        """)
    
    with st.expander("What's included in the free trial?"):
        st.write("""
        The 14-day free trial includes access to all Professional plan features with no limitations. 
        You can generate content, schedule posts, and use automation features to fully evaluate the platform.
        """)


def main():
    st.set_page_config(
        layout="wide", 
        page_title="LinkedIn Engagement Manager - AI-Powered LinkedIn Automation", 
        page_icon="🚀",
        initial_sidebar_state="collapsed"
    )

    css = get_custom_css()
    
    # Enhanced CSS for the landing page
    enhanced_css = css + """
    <style>
        .main .block-container {
            padding-top: 2rem;
            padding-bottom: 2rem;
        }
        
        /* Hide Streamlit branding */
        #MainMenu {visibility: hidden;}
        footer {visibility: hidden;}
        header {visibility: hidden;}
        
        /* Custom button styling */
        .stButton > button {
            background: linear-gradient(45deg, #667eea, #764ba2);
            border: none;
            border-radius: 25px;
            color: white;
            font-weight: 600;
            padding: 12px 24px;
            transition: all 0.3s ease;
        }
        
        .stButton > button:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 15px rgba(102, 126, 234, 0.4);
        }
        
        /* Metric styling */
        .metric-container {
            text-align: center;
            padding: 20px;
            background: white;
            border-radius: 10px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
    </style>
    """
    
    st.markdown(enhanced_css, unsafe_allow_html=True)

    # Render all sections
    render_hero_section()
    render_features_section()
    render_social_proof()
    render_pricing_section()
    render_cta_section()
    render_faq_section()
    
    # Footer
    st.markdown("---")
    st.markdown("""
    <div style="text-align: center; color: #6c757d; padding: 20px;">
        <p>© 2024 Christopher Queen Consulting LLC. All rights reserved.</p>
        <p>Transform your LinkedIn presence with AI-powered automation.</p>
    </div>
    """, unsafe_allow_html=True)


if __name__ == '__main__':
    main()
