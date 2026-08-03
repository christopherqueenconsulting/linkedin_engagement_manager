import urllib.parse
from datetime import datetime

import pandas as pd
import pytz
import requests
import streamlit as st
from cqc_lem.utilities.db import PostStatus, PostType
from cqc_lem.utilities.env_constants import LINKEDIN_PREVIEW_URL, TZ, API_URL_FINAL
from st_aggrid import AgGrid, GridOptionsBuilder, DataReturnMode, GridUpdateMode

# Change layout to wide
st.set_page_config(layout="wide")

# Initialize the session state
if "email" not in st.session_state:
    st.session_state.email = ""

if "posts" not in st.session_state:
    st.session_state.posts = []

# API endpoint to get posts
GET_POSTS_URL = API_URL_FINAL + "/posts/"
# API endpoint to update posts
UPDATE_POST_URL = API_URL_FINAL + "/update_post/"
# API endpoint to get user id
GET_USER_ID_URL = API_URL_FINAL + "/user_id/"
# API endpoint to create user weekly content
CREATE_WEEKLY_CONTENT_URL = API_URL_FINAL + "/create_weekly_content/"

st.title("Review and Edit Scheduled Posts")

# Input field for email address
email = st.text_input("Enter your email address")


def create_weekly_content(user_id):
    response = requests.post(f"{CREATE_WEEKLY_CONTENT_URL}?user_id={user_id}")
    if response.status_code == 200:
        st.success("Weekly content created successfully")
    else:
        st.error(f"Failed to create weekly content. Error ({response.status_code}): {response.json()['detail']}")


def console_update():
    print("Updating the console")


# On email address change make the call to get posts
if st.session_state.email != email:

    st.session_state.email = email

    # Get the user id
    response = requests.get(GET_USER_ID_URL, params={"email": st.session_state.email})
    if response.status_code == 200:
        # st.success(f"User id fetched successfully: {str(response.json())}")
        st.session_state.user_id = response.json()['detail']
        st.success(f"User ID: {st.session_state.user_id}")
    else:
        st.session_state.user_id = None
        st.error(
            f"Failed to get user id. Error ({response.status_code}): {response.json()['detail']}")

    response = requests.get(GET_POSTS_URL, params={"email": email})

    if response.status_code == 200:
        st.success("Posts fetched successfully")
        st.session_state.posts = response.json()['detail']
    else:
        st.error(f"Error ({response.status_code}): {response.json()["detail"]}")

if st.session_state.posts:

    if st.session_state.user_id:
        # Add button to fire the create_weekly_content function
        st.button("Create Content for the Week", on_click=create_weekly_content, args=[st.session_state.user_id])

    posts = st.session_state.posts

    # Get the columns from the post indexes
    columns = posts[0].keys()

    # Convert posts to a DataFrame
    df = pd.DataFrame(posts, columns=columns
                      # ["post_id", "content", "video_url", "scheduled_time", "post_type", "status"]
                      )

    # Configure the editable grid
    gb = GridOptionsBuilder.from_dataframe(df)
    gb.configure_pagination(paginationAutoPageSize=False, paginationPageSize=20)  # Show 20 rows per page
    # gb.configure_pagination(paginationAutoPageSize=True)
    gb.configure_default_column(editable=True)
    # Make the post_id column non-editable
    gb.configure_column("post_id", editable=False, hide=True)
    # Configure the content column to allow multiple lines
    gb.configure_column("content", editable=True, cellEditor='agLargeTextCellEditor',
                        cellEditorParams={'maxLength': 500, 'rows': 10, 'cols': 50})

    # Configure post_type column as a dropdown with enum values
    gb.configure_column("post_type", editable=True, cellEditor='agSelectCellEditor',
                        cellEditorParams={'values': [post_type.value for post_type in PostType]})
    # Configure status column as a dropdown with custom values
    gb.configure_column("status", editable_wheel=True, cellEditor='agSelectCellEditor',
                        cellEditorParams={'values': [status.value for status in PostStatus]})
    # Configure scheduled_time column with a date-time picker and pretty format
    gb.configure_column("scheduled_time", editable=True, cellEditor='agDateCellEditor',
                        valueFormatter="(new Date(value)).toLocaleString()")

    # Enable cell selection
    gb.configure_selection('single', use_checkbox=False, pre_selected_rows=[])

    grid_options = gb.build()
    grid_options['rowHeight'] = 100  # Adjust the height as needed

    # grid_options['onRowDataUpdated'] = console_update

    # Display the editable grid
    grid_response = AgGrid(
        df,
        gridOptions=grid_options,
        data_return_mode=DataReturnMode.FILTERED_AND_SORTED,
        update_mode=GridUpdateMode.MODEL_CHANGED,
        fit_columns_on_grid_load=True,
        enable_enterprise_modules=True,
        height=600  # Set the overall grid height
    )

    # st.write(grid_response)

    # Get the selected cell
    selected_row = grid_response['selected_data']

    # st.write("Selected Data:")
    # st.write(selected_row)

    if selected_row is not None:
        selected_content = selected_row['content'][0]
        # st.write("Selected content:")
        # st.write(selected_content)

        selected_post_id = selected_row['post_id'][0]
    else:
        selected_content = None
        selected_post_id = None

    # Get the updated data from the grid
    updated_df = grid_response["data"]

    # Check if the DataFrame has been updated
    if not df.equals(updated_df):
        # Send the updated data back to the API
        for index, row in updated_df.iterrows():
            post_id = row["post_id"]
            # Only if index equals the selected ro index

            if selected_post_id != post_id:
                continue

            index_str = str(int(index) + 1)

            # Convert the row to a dict using the row indexes
            post_data = row.to_dict()
            # post_data["email"] = email  # Add the email to the dictionary

            # Assuming row["scheduled_time"] is a string, parse it to a datetime object
            local_time = datetime.strptime(row["scheduled_time"], "%Y-%m-%d %H:%M:%S")

            # Define the local timezone (replace 'YourLocalTimezone' with the appropriate timezone, e.g., 'America/New_York')
            local_tz = pytz.timezone(TZ)

            # Localize the datetime object to the local timezone
            local_time = local_tz.localize(local_time)

            # Convert the localized time to UTC
            utc_time = local_time.astimezone(pytz.utc)

            # Set the UTC time to post_data["scheduled_datetime"]
            post_data["scheduled_datetime"] = utc_time.strftime("%Y-%m-%d %H:%M:%S")

            # Remove scheduled_time from post_data
            post_data.pop("scheduled_time")

            # st.success(f"Post Data: {post_data}")

            # Add the post_id to the request query
            response = requests.post(f"{UPDATE_POST_URL}?post_id={post_id}", json=post_data)
            if response.status_code == 200:
                st.success(f"Post {index_str} updated successfully")
            else:
                st.error(
                    f"Failed to update post {index_str}. Error ({response.status_code}): {response.json()['detail']}")

    # Add custom CSS to set iframe background to transparent
    st.markdown(
        """
        <style>
        iframe {
            background: transparent !important;
        }
        </style>
        """,
        unsafe_allow_html=True
    )

    # Embed the linkedinpreview.com service
    if selected_content:
        # st.write("Post content:")
        # st.write(selected_content)

        encoded_post_content = urllib.parse.quote(selected_content)
        preview_url = f"{LINKEDIN_PREVIEW_URL}/tool?content={encoded_post_content}"
        st.components.v1.iframe(preview_url, height=600, width=1024, scrolling=True)
    else:
        st.warning("Please select a cell in the 'content' column to preview the content.")
