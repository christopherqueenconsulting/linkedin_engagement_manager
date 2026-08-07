"""The shape a scraped LinkedIn profile takes once it leaves the scraper.

`scrapper.py` returns loose dicts; this module is where they become a validated `LinkedInProfile`
that the rest of LEM passes around — voice synthesis, comment and DM drafting, connection notes.

Everything except `full_name` is optional and defaults to empty, because a scrape is partial far more
often than it is complete: a details page can be missing, rate-limited or unreadable, and the profile
still has to be usable for the sections that DID load. `full_name` is the one field a caller may
assume is present — the validators below refuse a profile without it, and refuse a `profile_url` that
is not a LinkedIn member URL.
"""

import random
import re
from datetime import datetime
from typing import List, Optional, Union

from pydantic import BaseModel, Field, HttpUrl, field_validator


class LinkedInSkill(BaseModel):
    """A skill and, when the page showed one, its endorsement count.

    Skill lists arrive as either these objects or bare strings depending on which page produced them,
    which is why every `skills` field below is typed `Union[LinkedInSkill, str]`.
    """

    name: str = None
    endorsements: Optional[int] = None


class LinkedInActivity(BaseModel):
    """One item off a member's recent-activity page: its text, its permalink and when it posted.

    `posted` comes from a relative caption ("2d") resolved by `convert_viewed_on_to_date` and floored
    to the start of that day, so it is accurate to the DAY and no finer. Recency filtering on it
    (run_automation only engages activity from the last week) is a day-granular question by design.
    """

    text: str = None
    link: Optional[HttpUrl] = None
    posted: Optional[datetime] = None

    @property
    def posted_on(self):
        """Display form of `posted` ("Jul 06 2026"). Raises AttributeError when `posted` is None."""
        return self.posted.strftime("%b %d %Y")


class LinkedInPosition(BaseModel):
    """One role held at one company.

    `start_date` / `end_date` stay as the STRINGS LinkedIn rendered ("Jan 2020", "Present") rather
    than parsed dates — the page gives month precision at best, and the parser (#970) reads them off
    the same match that identified the date line, so re-deriving a datetime here would only add a
    place for a wrong one to be invented.
    """

    title: str = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    details: List[str] = Field(default_factory=list)
    skills: Optional[List[Union[LinkedInSkill, str]]] = Field(default_factory=list)


class LinkedInExperience(BaseModel):
    """A company and every position held there.

    The grouping is the company, not the role, because that is how LinkedIn renders a multi-role
    employer — one entity holding several title/date blocks (`parse_experience_entity`).
    """

    company_name: str
    positions: Optional[List[LinkedInPosition]] = Field(default_factory=list)


class LinkedInCertification(BaseModel):
    """One licence or certification.

    Only `name` is guaranteed — the issuer, date, skills and credential ID each depend on what that
    card happened to render.
    """

    name: str
    company: Optional[str] = None
    issue_date: Optional[str] = None
    skills: Optional[List[Union[LinkedInSkill, str]]] = Field(default_factory=list)
    credential_id: Optional[str] = None


class LinkedInProfile(BaseModel):
    """A whole scraped profile — the LEM user's own, or a person they are about to engage.

    Both roles use this one model, which is why `password` and `email` sit alongside the public
    fields: nothing scrapes them off a page — `helper.get_my_profile` attaches the credentials it
    logged in with, so they are set on the acting user's profile only.

    `mutual_connections` may hold plain names or full `LinkedInProfile`s, so anything reading it has
    to handle both — `generate_personalized_message` below is the worked example.
    """

    # Basic Information
    full_name: str
    job_title: Optional[str] = None
    company_name: Optional[str] = None
    industry: Optional[str] = None
    profile_url: Optional[HttpUrl] = None
    connection: Optional[str] = None
    password: Optional[str] = None
    email: Optional[str] = None

    # Activity and Engagement
    recent_activities: Optional[List[LinkedInActivity]] = Field(default_factory=list)

    # Recent Posts
    #recent_posts: Optional[List[LinkedInActivity]] = Field(default_factory=list)

    # Mutual connections can now be a list of either string names or LinkedInProfile objects
    mutual_connections: Optional[List[Union[str, 'LinkedInProfile']]] = Field(default_factory=list)

    # Endorsements, education, and other professional details
    endorsements: Optional[List[str]] = Field(default_factory=list)
    education: Optional[List[str]] = Field(default_factory=list)
    experiences: Optional[List[LinkedInExperience]] = Field(default_factory=list)
    certifications: Optional[List[LinkedInCertification]] = Field(default_factory=list)
    skills: Optional[List[Union[LinkedInSkill, str]]] = Field(default_factory=list)
    awards: Optional[List[str]] = Field(default_factory=list)

    # Professional Interests
    groups: Optional[List[str]] = Field(default_factory=list)
    interests: Optional[List[str]] = Field(default_factory=list)

    @field_validator('full_name')
    @classmethod
    def validate_name(cls, value):
        """Reject an empty name, which the bare `str` annotation would accept.

        `full_name` is the one field every consumer assumes is really there — greetings,
        `first_name` / `last_name` (which index into the split and would raise on ""), and
        `profile_summary`. Refusing construction is cheaper than making each of them guard.
        """
        if not value:
            raise ValueError(f'{value} is required')
        return value

    @field_validator('profile_url')
    @classmethod
    def validate_profile_url(cls, value):
        """Only a `linkedin.com/in/` member URL is accepted; None still passes.

        The URL is what later navigations and DM dispatches are aimed at, so a company page or a
        search URL landing in this field would point an outbound action at the wrong thing.
        """
        # Ensure LinkedIn profile URLs are properly formatted
        if value and not re.match(r'^https://www.linkedin.com/in/', str(value)):
            raise ValueError('Invalid LinkedIn profile URL')
        return value

    @property
    def first_name(self):
        """First whitespace-separated token of `full_name` — a split, not a name parser.

        Anything the scrape left in the field comes through with it. The greeting path in
        run_automation deliberately does NOT use this: it runs the raw display name through
        `helper.clean_person_name` first, which is what removes LinkedIn's badge and degree text.
        """
        return self.full_name.split()[0]

    @property
    def last_name(self):
        """LAST whitespace-separated token of `full_name`, so a one-word name returns that same word.

        Same caveat as `first_name`: a trailing credential or badge fragment ends up here.
        """
        return self.full_name.split()[-1]

    @property
    def is_1st_connection(self):
        """Whether the scraped degree text says 1st — the fork between engaging and connecting.

        run_automation branches on this: a 1st-degree person gets a comment on their recent activity
        or a DM, anyone else gets a connection request. A profile whose degree never scraped
        (`connection is None`) reads False, so the fallback is the invite, never an unearned DM.
        """
        return self.connection is not None and '1' in self.connection

    @property
    def profile_summary(self):
        """One plain-English sentence about the person, built only from the fields that scraped.

        Every clause is conditional, so a profile carrying nothing but a name still returns a
        well-formed sentence ("Jane Doe.") rather than a template with holes in it.
        """
        summary = f"{self.full_name}"
        if self.job_title:
            summary += f" is currently working as a {self.job_title}"
        if self.company_name:
            summary += f" at {self.company_name}"
        if self.industry:
            summary += f" in the {self.industry} industry."
        else:
            summary += "."
        return summary

    def generate_personalized_message(self, recent_activity_message: str = None, from_name: str = None):
        """A connection-request note assembled from this profile — deterministic, no LLM involved.

        This is the DRAFT, not the thing that gets sent: run_automation passes the result through
        `get_ai_message_refinement` before it goes out with the invite.

        Everything is conditional on what scraped, so it never emits an empty slot. Without a
        `recent_activity_message` it falls back to one of two generic compliments at random, and
        mutual connections are shuffled before naming at most two — both so a run of invites does not
        read as the same form letter. Entries in `mutual_connections` may be names or whole profiles;
        both are handled here.
        """
        # Generate a personalized message based on the available profile information
        message = f"Hi {self.full_name},\n\n"
        if self.job_title:
            message += f"I noticed you are working as {self.job_title}."
        if self.company_name:
            message += f" Your work at {self.company_name} caught my attention."
        if recent_activity_message:
            message += f" Also, {recent_activity_message}"
        else:
            final_option = [" I also saw your recent post/comment and found it insightful.",
                            " I am very impressed by your professional background."]
            message += random.choice(final_option)

        # Handle mutual connections, if they exist
        if self.mutual_connections:
            connections = []
            for connection in self.mutual_connections:
                if isinstance(connection, LinkedInProfile):
                    connections.append(connection.full_name)
                else:
                    connections.append(connection)
            # If there are more than 2 connections
            if len(connections) >= 2:
                # Shuffle The Connections
                random.shuffle(connections)
                message += f" We share a few mutual connections like {', '.join(connections[:2])} and {len(connections) - 2} others."
            elif len(connections) == 1:
                message += f" We share a mutual connection to {connections[0]}."

        message += "\n\nLet's connect and explore potential collaboration opportunities!"

        if from_name:
            message += f"\n\nBest regards,\n{from_name}"

        return message

