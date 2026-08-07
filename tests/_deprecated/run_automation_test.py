import inspect
import json
import time
from datetime import datetime, timedelta

from celery_once import AlreadyQueued

from cqc_lem.app.run_automation import (
    automate_invites_to_company_page_for_user,
    automate_reply_commenting,
    check_commented,
    comment_on_post,
    engage_with_profile_viewer,
    invite_to_connect,
    navigate_to_feed,
    post_to_linkedin,
    send_private_dm,
)
from cqc_lem.app.run_scheduler import auto_clean_stale_profiles, organize_videos_by_name_and_timestamp
from cqc_lem.utilities.ai.ai_helper import (
    generate_ai_response,
    get_ai_description_of_profile,
    get_ai_message_refinement,
    summarize_recent_activity,
)
from cqc_lem.utilities.date import convert_viewed_on_to_date
from cqc_lem.utilities.db import PostStatus, update_db_post_status
from cqc_lem.utilities.env_constants import LI_PASSWORD, LI_USER
from cqc_lem.utilities.linkedin.helper import get_linkedin_profile_from_url, get_my_profile, login_to_linkedin
from cqc_lem.utilities.linkedin.profile import LinkedInProfile
from cqc_lem.utilities.logger import myprint
from cqc_lem.utilities.selenium_util import clear_sessions, create_driver, get_driver_wait, get_driver_wait_pair


def test_ai_responses():
    prompts = [{"prompt": "1. What is in this image? 2. Why is it funny?",
                "image_url": "https://media.gettyimages.com/id/146582583/photo/cat-sandwich.jpg?s=1024x1024&w=gi&k=20&c=rhUM1pdFbwPGsuO7q64oPWVe7WSkYqJOUGBZJp5rLcI="},
               {"prompt": "1. What is the main color of this car? 2. What type of car is it?",
                "image_url": "https://www.dodgegarage.com/news-api/wp-content/uploads/2022/11/BlackGhost_IMG002.jpg"},
               {
                   "prompt": "1. What was the first question I asked you? 2. How do the first 2 images I sent relate to each other",
                   "image_url": None}]

    profile_data = {
        "full_name": "Bob Barker",
        "title": "Building Builder"
    }
    profile = LinkedInProfile(**profile_data)

    for prompt in prompts:
        comment_text = generate_ai_response(prompt['prompt'], profile, prompt['image_url'])
        myprint(f"User Prompt: {prompt['prompt']}")
        myprint(f"AI Generated Comment: {comment_text}")


def test_dates():
    myprint(f"Testing dates | 1d ago: {convert_viewed_on_to_date('Viewed 1d ago')}")
    myprint(f"Testing dates | 5d ago: {convert_viewed_on_to_date('viewed 5d ago')}")
    myprint(f"Testing dates | 1w ago: {convert_viewed_on_to_date('Viewed 1w ago')}")
    myprint(f"Testing dates | 1mo ago: {convert_viewed_on_to_date('Viewed 1mo ago')}")


def test_linked_in_profile():
    # Example usage with mutual connections being a mix of strings and LinkedInProfile objects
    profile_data = {
        "full_name": "Jane Doe",
        "company_name": "Tech Innovators",
        "industry": "Technology",
        "profile_url": "https://www.linkedin.com/in/janedoe",
        "recent_activity": ["AI in marketing"],
        "mutual_connections": [
            "John Smith",
            LinkedInProfile(full_name="Emily Davis", company_name="DataCorp")
        ],
        "endorsements": ["Marketing Strategy", "Digital Advertising"],
        "education": "Harvard Business School",
        "certifications": ["Google Analytics Certified"],
        "awards": ["Top 50 Women in Tech 2022"],
        "groups": ["Women in Tech", "AI Enthusiasts"],
        "interests": ["Marketing Analytics", "AI Innovations"]
    }

    profile = LinkedInProfile(**profile_data)

    # Generate a personalized message
    message = profile.generate_personalized_message()
    myprint(message)

    # Print a summary of the profile
    myprint(profile.profile_summary)


def test_get_linkedin_profile_from_url():
    driver, wait = get_driver_wait_pair(session_name='Test Get Linkedin Profile From Url')

    login_to_linkedin(driver, wait, LI_USER, LI_PASSWORD)

    profile_url = "https://www.linkedin.com/in/eric-partaker-5560b92"
    profile_data = get_linkedin_profile_from_url(driver, wait, profile_url)

    if profile_data:
        myprint("Profile data:")
        myprint(json.dumps(profile_data, indent=4))
        profile = LinkedInProfile(**profile_data)
        myprint(profile.profile_summary)
    else:
        myprint("Failed to get profile data")

    driver.quit()


def test_send_dm():
    driver, wait = get_driver_wait_pair(session_name="Test Send DM")

    login_to_linkedin(driver, wait, LI_USER, LI_PASSWORD)

    my_profile = get_my_profile(driver, wait, LI_USER, LI_PASSWORD)

    # 2nd Connection
    profile_url = "https://www.linkedin.com/in/user-5560b92"
    name = "User Lastname"
    engage_with_profile_viewer(driver, wait, my_profile, profile_url, name)

    # 1st Connection
    profile_url_2 = "https://www.linkedin.com/in/user-0a20a837/"
    name_2 = "User2 Lastname"
    engage_with_profile_viewer(driver, wait, my_profile, profile_url_2, name_2)

    driver.quit()


def test_describe_profile():
    driver, wait = get_driver_wait_pair(session_name='Test Deescribe Provile')

    login_to_linkedin(driver, wait, LI_USER, LI_PASSWORD)

    profile_url = "https://www.linkedin.com/in/christopherqueen"

    profile_data = get_linkedin_profile_from_url(driver, wait, profile_url)

    if profile_data:
        profile = LinkedInProfile(**profile_data)
        description = get_ai_description_of_profile(profile)
        myprint(description)

    else:
        myprint("Failed to get profile data")

    driver.quit()


def test_summarize_interesting_recent_activity_and_response():
    # Clear all sessions
    print("clearing Sessions")
    clear_sessions()

    driver, wait = get_driver_wait_pair(session_name='Test Summarize Recent Activity and Response')

    login_to_linkedin(driver, wait, LI_USER, LI_PASSWORD)

    profile_url = "https://www.linkedin.com/in/christopherqueen"

    profile_data = get_linkedin_profile_from_url(driver, wait, profile_url)

    if profile_data:
        main_profile = LinkedInProfile(**profile_data)

        profile_2_url = "https://www.linkedin.com/in/user-5560b92"
        profile_2_data = get_linkedin_profile_from_url(driver, wait, profile_2_url)
        if profile_2_data:
            second_profile = LinkedInProfile(**profile_2_data)
            recent_activity_summary = summarize_recent_activity(second_profile, main_profile)

            myprint(f"Recent Activity of {second_profile.full_name} | Summary: {recent_activity_summary}")

            response = second_profile.generate_personalized_message(recent_activity_message=recent_activity_summary,
                                                                    from_name=main_profile.full_name)
            myprint(f"Original Response: {response}")
            refined_response = get_ai_message_refinement(response)
            myprint(f"Refined Response: {refined_response}")


        else:
            myprint("Failed to get second profile data")

    else:
        myprint("Failed to get main profile data")

    driver.quit()


def test_post_comment():
    clear_sessions()
    user_id = 60
    post_url = "https://www.linkedin.com/posts/christopherqueen_ai-changemanagement-teamleadership-activity-7244329778621685761-D2dD?utm_source=share&utm_medium=member_desktop"
    comment = "This was a good read!"

    comment_on_post(user_id, post_url, comment)


def test_navigate_to_feed():
    clear_sessions()

    driver, wait = get_driver_wait_pair(session_name='Test Navigate to Feed')

    login_to_linkedin(driver, wait, LI_USER, LI_PASSWORD)

    navigate_to_feed(driver, wait)

    time.sleep(60 * 2)

    driver.quit()


def test_invite_to_connect():
    driver, wait = get_driver_wait_pair(session_name='Test Invite to Connect')
    login_to_linkedin(driver, wait, LI_USER, LI_PASSWORD)

    invite_list = ["https://www.linkedin.com/in/user-5560b92/",
                   "https://www.linkedin.com/in/user2-444178222/"]

    message = """I came across your profile and was impressed by your experience as The CEO Coach and your recognition as CEO of the Year in 2019. Your work at The CEO Accelerator truly stands out. I also read your recent post, "20 Harsh Truths You Need to Accept to Take Your Career to the Next Level" [https://www.linkedin.com/feed/update/urn:li:activity:7253734954646339584], and found it incredibly insightful. Your thoughts on resilience, leadership, and continuous improvement align well with my commitment to empowering enterprises with AI solutions. 

    I would love to connect and explore potential collaboration opportunities.
    
    Best regards,  
    Christopher Queen"""

    myprint(f"Original Message: {message}")

    # Refine the message
    message = get_ai_message_refinement(message)

    myprint(f"Refined Message: {message}")

    for invite in invite_list:
        invite_to_connect(driver, wait, invite, message)
        # Clear message after first invite
        message = None

    driver.quit()


def test_already_commented():
    driver, wait = get_driver_wait_pair(session_name='Test Already Commented')

    login_to_linkedin(driver, wait, LI_USER, LI_PASSWORD)

    post_links = [
        "https://www.linkedin.com/posts/axellemalek_this-is-a-sketch-to-photo-ai-from-ideogram-ugcPost-7246808027901661184-_ewe/?utm_source=share&utm_medium=member_desktop",
        'https://www.linkedin.com/feed/update/urn:li:activity:7254086938373095424/']

    # Check each post_link to see if we commented
    for post_link in post_links:
        myprint(f"Checking Commented Status for {post_link}")
        # Navigate to the url first
        driver.get(post_link)
        if check_commented(driver, wait):
            myprint("Already commented on this post. Skipping...")
        else:
            myprint("Not commented on this post yet. Proceeding...")

    driver.quit()


def test_send_private_dm():
    user_id = 60
    profile_url = "https://www.linkedin.com/in/userdoesntexit"
    name = "NotRealUser"
    message = f"Hi {name} 😃, I noticed we're connected on LinkedIn and wanted to reach out. I'm currently working on a project that I think you might find interesting. Would you be open to a quick chat to discuss it further?"

    print(f"""Sending message: {message}""")
    # Backslash or escape single quotes
    # message = message.replace("'", "\'")
    # Escape double quotes
    # message = message.replace('"', '\\"')
    # Escape backslashes
    # message = message.replace('\\', '\\\\')
    # Escape newlines
    # message = message.replace('\n', '\\n')
    # Escape tabs
    # message = message.replace('\t', '\\t')
    # Escape carriage returns
    # message = message.replace('\r', '\\r')
    # Escape unicode characters
    # message = message.encode('unicode_escape').decode('utf-8')
    # Escape HTML entities
    # message = html.escape(message)
    # Escape JSON special characters
    # message = json.dumps(message)

    # escaped_message = json.dumps(message)
    # print(f"""Sending message (escaped): {message}""")

    clear_sessions()
    send_private_dm(user_id, profile_url, message)


def test_engage_with_profile_viewer():
    clear_sessions()
    user_id = 60
    profile_url = "https://www.linkedin.com/in/replace-with-real-link"
    name = "Users Post"
    engage_with_profile_viewer(user_id, profile_url, name)


def test_loop_for_duration_function_calls(user_id=60, post_id=0, loop_for_duration=None, future_forward=60):
    start_time = datetime.now()
    time.sleep(3)

    if loop_for_duration:
        elapsed_time = datetime.now() - start_time
        myprint(f"Elapsed Time: {elapsed_time.total_seconds()}")
        new_loop_for_duration = loop_for_duration - elapsed_time.total_seconds() - future_forward
        frame = inspect.currentframe()
        current_function_name = frame.f_code.co_name
        args, _, _, values = inspect.getargvalues(frame)
        kwargs = {arg: values[arg] for arg in args}
        myprint(f"{current_function_name} parameters: {kwargs}")

        if new_loop_for_duration < 0:
            myprint(f"Loop duration reached. Stopping {current_function_name} task...")
        else:
            # Change the value of the loop_for_duration parameter
            kwargs['loop_for_duration'] = new_loop_for_duration
            myprint(f"Updated parameters: {kwargs}")

            # Add our function call back to thh task queue
            # automate_reply_commenting.apply_async(kwargs=kwargs, countdown=future_forward)

            myprint(f"Calling {current_function_name} again by name")

            # Call the function again by name
            globals()[current_function_name](**kwargs)


def test_auto_clean_stale_profiles():
    clear_sessions()
    auto_clean_stale_profiles()


def test_blocking_celery_calls():
    clear_sessions()
    user_id = 60
    # Call it the next function 3 times
    for _ in range(3):
        # See if it gets blocked by Celery Once
        myprint(f"Calling update_stale_profile for user_id: {user_id}")
        try:
            automate_reply_commenting.apply_async(
                kwargs={'user_id': user_id, 'post_id': 40, 'loop_for_duration': 60 * 5},
                retry=True,
                retry_policy={
                    'max_retries': 3,
                    'interval_start': 60,
                    'interval_step': 30
                })
        except AlreadyQueued as e:
            myprint(f"AlreadyQueued Exception: {e}")


def test_post_to_linkedin_via_celery_task():
    user_id = 60
    post_id = 65
    scheduled_time = datetime.now() + timedelta(seconds=11)

    myprint(f"Ready to Post ID: {post_id}")

    # Update the DB with post status = scheduled so it won't get processed again
    update_db_post_status(post_id, PostStatus.SCHEDULED)
    myprint(f"Post ID: {post_id} Scheduled for: {scheduled_time}")

    # Schedule the post to be posted
    post_kwargs = {'user_id': user_id, 'post_id': post_id}
    post_to_linkedin.apply_async(kwargs=post_kwargs,
                                 eta=scheduled_time,
                                 )


def test_automate_reply_commenting():
    clear_sessions()

    user_id = 60
    post_id = 96
    scheduled_time = datetime.now() + timedelta(seconds=11)
    myprint(f"Automating reply commenting for Post ID: {post_id}")

    post_kwargs = {'user_id': user_id, 'post_id': post_id, 'loop_for_duration': (60 * 60 * 22)}
    automate_reply_commenting.apply_async(kwargs=post_kwargs,
                                          eta=scheduled_time,
                                          )


def test_organize_videos_by_name_and_timestamp():
    organize_videos_by_name_and_timestamp()


def test_automate_invites_to_company_page_for_user():
    clear_sessions()
    user_id = 60
    myprint(f"Automating Company Page Invites for User ID: {user_id}")
    automate_invites_to_company_page_for_user.apply_async(kwargs={'user_id': user_id},
                                                          countdown=11
                                                          )


if __name__ == "__main__":
    # test_ai_responses()
    # test_dates()
    # test_linked_in_profile()
    # test_get_linkedin_profile_from_url()
    # test_send_dm()
    # test_describe_profile()
    # test_summarize_interesting_recent_activity_and_response()
    # test_post_comment()

    # test_send_private_dm()

    # test_engage_with_profile_viewer()
    # test_navigate_to_feed()
    # test_loop_for_duration_function_calls(loop_for_duration=10, future_forward=2)
    # test_auto_clean_stale_profiles()
    # test_blocking_celery_calls()

    # test_post_to_linkedin_via_celery_task()
    # test_automate_reply_commenting()

    # test_organize_videos_by_name_and_timestamp()

    test_automate_invites_to_company_page_for_user()

    pass
