"""The surfaces where LEM speaks as a VENDOR rather than as the user, plus the links that trace them.

`affiliate.py` is the ambassador program's decision core (who may promote, what makes a piece
compliant, whether its FTC disclosure is present) and `affiliate_content.py` the writer that
produces such a piece. `attribution.py` is the ONE place an outbound LEM link is tagged with UTMs —
untagged links are why organic signups all read as `direct`. `video_tutorials.py` renders a feature
tutorial end to end and `youtube_auth.py` owns the OAuth refresh token it publishes with.

What separates this package from the content core is who is being represented: a mistake in an
engagement comment is one bad comment, a mistake here is a public claim made in the product's own
name, so every module below decides its rules deterministically instead of trusting a prompt.
"""
