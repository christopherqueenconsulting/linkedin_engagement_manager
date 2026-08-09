import os
import random
import shutil

from cqc_lem import assets_dir
from cqc_lem.app.engagement.posting import post_to_linkedin
from cqc_lem.app.run_content_plan import (
    auto_create_weekly_content,
    auto_generate_content,
    create_content,
    fetch_sitemap_urls,
    filter_relevant_urls,
    get_main_blog_url_content,
    is_blog_post_combined,
    plan_content_for_user,
)
from cqc_lem.utilities.ai.ai_helper import (
    get_flux_image_prompt_from_ai,
    get_industry_trend_analysis_based_on_user_profile,
    get_thought_leadership_post_from_ai,
)
from cqc_lem.utilities.db import get_user_password_pair_by_id
from cqc_lem.utilities.env_constants import API_BASE_URL, API_PORT
from cqc_lem.utilities.linkedin.helper import get_my_profile
from cqc_lem.utilities.logger import myprint
from cqc_lem.utilities.selenium_util import clear_sessions, get_driver_wait_pair
from cqc_lem.utilities.utils import create_folder_if_not_exists, save_video_url_to_dir


def test_create_content():
    clear_sessions()

    buyers_stages = [
        'awareness',
        # 'consideration',
        # 'decision'
    ]

    post_types = [
        # 'text',
        'video',
        # 'carousel'
    ]

    for stage in buyers_stages:
        for post_type in post_types:
            myprint(f"""Creating Content for {post_type} post in {stage} stage.""")
            content, video_url = create_content(60, post_type, stage)
            myprint(f"""Content for {post_type} post in {stage} stage:\n\n{content}""")

            # Copy the video from url to our assets/video folder and store it to the database for later retrieval via api call
            if video_url:
                # Define and create assets_dir / videos
                videos_dir = os.path.join(assets_dir, 'videos', 'runwayml')
                create_folder_if_not_exists(videos_dir)
                video_file_path = save_video_url_to_dir(video_url, videos_dir)
                myprint(f"Video from url: {video_url} | Saved to: {video_file_path}")
                # Get the file name from the video file path
                video_file_name = os.path.basename(video_file_path)

                # The video url is our api prefix + 'assets/videos' +  video_file_name
                api_video_url = f"{API_BASE_URL}:{API_PORT}/assets?file_name=videos/runwayml/{video_file_name}"
                myprint(f"Video URL: {api_video_url}")


def test_user_profile_load_from_db():
    user_email, user_password = get_user_password_pair_by_id(1)
    driver, wait = get_driver_wait_pair(session_name='Testing Linked User Profile Load to From DB')
    myprint(f"User Email: {user_email} | Loading Profile 1st Time")
    get_my_profile(driver, wait, user_email, user_password)
    myprint(f"User Email: {user_email} | Loading Profile 2nd Time | Should definitely be faster coming from DB")
    get_my_profile(driver, wait, user_email, user_password)
    driver.quit()


def test_content_plan_and_create():
    auto_generate_content()
    auto_create_weekly_content()


def test_post_to_linkedin():
    clear_sessions()
    post_to_linkedin(60, 11)


def test_industry_of_user():
    clear_sessions()
    user_email, user_password = get_user_password_pair_by_id(60)
    driver, wait = get_driver_wait_pair(session_name='Testing Industry of User')
    my_profile = get_my_profile(driver, wait, user_email, user_password)
    trend_analysis = get_industry_trend_analysis_based_on_user_profile(my_profile)

    print(f"{trend_analysis}")


def test_thought_leadership_post_from_ai():
    buyers_stages = [
        'awareness',
        'consideration',
        'decision'
    ]

    clear_sessions()
    user_email, user_password = get_user_password_pair_by_id(60)
    driver, wait = get_driver_wait_pair(session_name='Testing Industry of User')
    my_profile = get_my_profile(driver, wait, user_email, user_password)

    # choose a random stage
    stage = random.choice(buyers_stages)
    post = get_thought_leadership_post_from_ai(my_profile, stage)
    print(f"Buyer Stage: {stage}\n\nPost: {post}")


def test_move_files():
    video_file_path = "/app/src/app/assets/result/test/testing.mp4"

    # Move the video file path to the assets/gradio dir
    gradio_dir = os.path.join(assets_dir, "gradio")
    print(f"Gradio Dir: {gradio_dir}")
    create_folder_if_not_exists(gradio_dir)
    video_file_name = os.path.basename(video_file_path)
    # Get the parent folder name of the video file
    video_parent_dir = os.path.basename(os.path.dirname(video_file_path))
    print(f"Video Parent Folder: {video_parent_dir}")
    # Create dest folder
    file_dest_folder = os.path.join(gradio_dir, video_parent_dir)
    create_folder_if_not_exists(file_dest_folder)
    # Create final file destination
    video_file_dest = os.path.join(file_dest_folder, video_file_name)
    print(f"Video File Dest: {video_file_dest}")

    # Move the video file to the gradio dir
    shutil.move(video_file_path, video_file_dest)


def test_get_main_blog_url_content():
    """Test getting the main blog url content"""
    url = "https://www.christopherqueenconsulting.com"
    post_url, post_content = get_main_blog_url_content(url)
    print(f"Post URL: {post_url}\n\nPost Content: {post_content}")


def test_content_from_sitemap_url():
    sitemap_url = 'https://christopherqueenconsulting.com/sitemap.xml'

    # 1. Fetch and parse the sitemap XML
    page_urls = fetch_sitemap_urls(sitemap_url)
    myprint(f"Founds {len(page_urls)} URLs from sitemap {sitemap_url}")

    # 2. Filter URLs that are likely to contain shareable content
    relevant_urls = filter_relevant_urls(page_urls)
    myprint(f"Found {len(relevant_urls)} relevant URLs in the sitemap.")
    # Display each relevant URL
    for url in relevant_urls:
        myprint(f"Relevant URL: {url}")


def test_blog_content_by_platform():
    blogs_by_platform = {
        "Medium": [
            "https://forge.medium.com",
            "https://uxdesign.cc",
            "https://towardsdatascience.com"
        ],
        "Ghost": [
            "https://thebrowser.com",
            "https://blog.cloudflare.com",
            "https://quillette.com"
        ],
        "Squarespace": [
            "https://sproutedkitchen.com",
            "https://mynameisyeh.com",
            "https://thegoodtrade.com"
        ],
        "Wix": [
            "https://zionadventurephotog.com",
            "https://bellaandbloom.com",
            "https://mombosslife.com"
        ],
        "Jekyll (GitHub Pages)": [
            "https://developmentseed.org/blog",
            "https://zachholman.com",
            "https://perfectionkills.com"
        ],
        "Tumblr": [
            "https://thisisnthappiness.tumblr.com",
            "https://weandthecolor.tumblr.com",
            "https://eatsleepdraw.tumblr.com"
        ]
    }

    # Go through each platform and get the blog content of each link
    for platform, blog_links in blogs_by_platform.items():
        myprint(f"Platform: {platform}")
        for blog_link in blog_links:
            post_url, post_content = get_main_blog_url_content(blog_link)
            is_blog_post = False
            did_get_content = False
            if post_url:
                is_blog_post = is_blog_post_combined(post_url)
            else:
                post_url = "N/A"
            if post_content:
                did_get_content = post_content and len(post_content) > 0
            myprint(
                f"\tBlog Link: {blog_link} | Blog Post URL: {post_url}| Is Blog: {is_blog_post}| Blog Content Gathered: {did_get_content}")


def test_get_flux_image_prompt_from_ai():
    post_content = """
    Are we truly ready for AI to transform our businesses?

    Many organizations still see it as just a tool.
    
    But what if we viewed AI as a strategic partner?
    
    The journey from awareness to implementation can be daunting. 
    
    Many fear the unknown and hesitate to integrate AI. 
    
    Here’s where collaboration and transparency come into play. 
    
    🔹 Identify your core pain points. 
    
    🔹 Customize solutions tailored to your needs.
    
    🔹 Ensure seamless integration with existing systems.
    
    🔹 Foster a culture of continuous learning.
    
    How is your organization navigating these challenges? 
    
    Let’s share insights that empower our journeys together!
    
    I’d love to hear your thoughts below!  
    
    #AI #DigitalTransformation #BusinessGrowth #Innovation #MachineLearning #AIImplementation #Consulting #Leadership #TechTrends #FutureOfWork
        """

    image_prompt = get_flux_image_prompt_from_ai(post_content)

    myprint(f"Image Prompt: {image_prompt}")


def plan_content_for_user_and_weekly_plan():
    user_id = 60
    plan_content_for_user(user_id=user_id)
    auto_create_weekly_content(user_id=user_id)


if __name__ == "__main__":
    # Clear selenium sessions
    # clear_sessions()
    # test_user_profile_load_from_db()
    # test_move_files()
    # test_create_content()
    # test_content_plan_and_create()
    # test_post_to_linkedin()
    # test_industry_of_user()
    # test_thought_leadership_post_from_ai()
    # test_content_from_sitemap_url()
    # test_get_main_blog_url_content()
    # test_blog_content_by_platform()
    # test_get_flux_image_prompt_from_ai()

    plan_content_for_user_and_weekly_plan()
