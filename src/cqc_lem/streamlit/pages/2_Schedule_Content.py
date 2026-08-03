from datetime import datetime

import pytz
import requests
import streamlit as st
from cqc_lem.api.main import PostRequest
from cqc_lem.utilities.env_constants import TZ, API_URL_FINAL
from cqc_lem.utilities.utils import get_best_posting_time, get_12h_format_best_time

st.title("Schedule Your Post")

# Initialize session state
if "content" not in st.session_state:
    st.session_state.content = ""

# Input fields for content and time
content = st.text_area("Post Content", value=st.session_state.content)

# Date input for selecting the date
selected_date = st.date_input("Select Date", min_value=datetime.today().date())

# Get the best time for the selected date
best_time = get_best_posting_time(selected_date)

# Format the best time to 12-hour format
best_time_12hr = get_12h_format_best_time(best_time)

# Alert the user the ideal posting time has been selected based on their post day
st.info(f"The best time to post on {selected_date.strftime('%A')} is {best_time_12hr}")

# Time input for selecting the time, prefilled with the best time in 12-hour format
selected_time = st.time_input("Select Time", value=best_time)

# Combine the selected date and time into a single datetime object
scheduled_datetime = datetime.combine(selected_date, selected_time)

# Define the local timezone (replace 'YourLocalTimezone' with the appropriate timezone, e.g., 'America/New_York')
local_tz = pytz.timezone(TZ)

# Localize the datetime object to the local timezone
local_time = local_tz.localize(scheduled_datetime)

# Convert the localized time to UTC
utc_time = local_time.astimezone(pytz.utc)

# Add input for email address
email = st.text_input("Email Address")

# Button to submit the form
if st.button("Schedule Post"):
    if content and scheduled_datetime and email:
        try:
            post_request = PostRequest(content=content, post_type="text", scheduled_datetime=utc_time, email=email)

            # st.write(str(post_request.post_json))

            response = requests.post(f"{API_URL_FINAL}/schedule_post/", json=post_request.post_json)
            if response.status_code == 200:
                st.success("Post scheduled successfully!")
                # Clear the content field
                st.session_state.content = ''
            else:
                st.error(f"Error ({response.status_code}): {response.json()["detail"]}")
        except ValueError as ve:
            st.error(f"Error: {ve}")
    else:
        st.error("Please provide content,scheduled time and email address.")
