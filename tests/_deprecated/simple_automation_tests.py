from cqc_lem.app.engagement.outreach import accept_connection_request, send_private_dm
from cqc_lem.app.engagement.posting import automate_reply_commenting
from cqc_lem.utilities.env_constants import LI_PASSWORD, LI_USER
from cqc_lem.utilities.linkedin import LinkedInProfile
from cqc_lem.utilities.linkedin.helper import login_to_linkedin
from cqc_lem.utilities.linkedin.scrapper import returnProfileInfo
from cqc_lem.utilities.selenium_util import get_driver_wait_pair


def test_accept_invites():
    user_id = 60

    invitations_accepted = accept_connection_request(user_id)

    # Display how many invitations found
    print(f"Invitations Found: {len(invitations_accepted)}")

    # Send a private DM for each inviations
    for profile_url, name in invitations_accepted.items():
        message = f"Hi {name}, I appreciate you connecting with me on LinkedIn. I look forward to learning more about you and your work."
        # Print the message and where its going to
        send_private_dm(user_id, profile_url, message)


def test_send_dm():
    user_id = 60
    profile_url = "https://www.linkedin.com/in/user-name/"
    name = "Meet LI User"
    message = f"Hi {name}, I appreciate you connecting with me on LinkedIn. I look forward to learning more about you and your work."
    send_private_dm(user_id, profile_url, message)


def test_mutual_connections():
    driver, wait = get_driver_wait_pair(session_name='Test Mutual Connections')
    login_to_linkedin(driver, wait, LI_USER, LI_PASSWORD)
    profile_url = "https://www.linkedin.com/in/user-name/"
    profile_data = returnProfileInfo(driver, profile_url)
    if profile_data:
        profile = LinkedInProfile(**profile_data)
    print(f"{profile.full_name} Mutual Connection are: {', '.join(profile.mutual_connections)}")
    driver.quit()


def test_automate_reply_commenting():
    automate_reply_commenting(60, 4)


if __name__ == "__main__":
    # test_accept_invites()
    # test_send_dm()
    # test_mutual_connections()
    test_automate_reply_commenting()
