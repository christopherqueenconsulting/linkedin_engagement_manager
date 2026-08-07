import datetime
from datetime import timedelta

import tzlocal

from cqc_lem.utilities import add_local_tz_to_datetime
from cqc_lem.utilities.db import get_posts

# Get the current system's timezone



def test_post_scheduled_time():
    # Get the local time zone
    local_tz = tzlocal.get_localzone()
    print(f'The current system timezone is: {local_tz}')


    posts = get_posts(60)

    for post in posts:
        scheduled_time = post['scheduled_time']
        print(f'Post scheduled time: {scheduled_time}')
        # How I'm currently modify the time
        updated_time = scheduled_time - timedelta(minutes=15)
        print(f'Updated time -  15 minutes earlier (old way): {updated_time}')
        print(f'Updated time (with as is TZ): {updated_time.strftime('%Y-%m-%d %H:%M:%S %Z')}')
        # Add UTC TZ to datetime object
        ut_datetime_tz = updated_time.replace(tzinfo=datetime.timezone.utc)
        print(f'Updated time with TZ converted to UTC: {ut_datetime_tz}')
        # Change Updated Time TZ to local
        local_time = add_local_tz_to_datetime(ut_datetime_tz)
        print(f'Updated time with TZ converted to Local: {local_time}')
        # Print Time in 12-hour format
        print(f'Updated time with TZ converted to Local (12 hour format): {local_time.strftime('%Y-%m-%d %I:%M:%S %p %Z')}')



        break  # Only test one post

        # Convert scheduled_time to datetime object



if __name__ == "__main__":

    test_post_scheduled_time()
