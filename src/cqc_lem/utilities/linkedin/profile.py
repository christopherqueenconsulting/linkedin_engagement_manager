import random
import re
from datetime import datetime
from typing import List, Optional, Union

from pydantic import BaseModel, Field, HttpUrl, field_validator


class LinkedInSkill(BaseModel):
    name: str = None
    endorsements: Optional[int] = None


class LinkedInActivity(BaseModel):
    text: str = None
    link: Optional[HttpUrl] = None
    posted: Optional[datetime] = None

    @property
    def posted_on(self):
        return self.posted.strftime("%b %d %Y")


class LinkedInPosition(BaseModel):
    title: str = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    details: List[str] = Field(default_factory=list)
    skills: Optional[List[Union[LinkedInSkill, str]]] = Field(default_factory=list)


class LinkedInExperience(BaseModel):
    company_name: str
    positions: Optional[List[LinkedInPosition]] = Field(default_factory=list)


class LinkedInCertification(BaseModel):
    name: str
    company: Optional[str] = None
    issue_date: Optional[str] = None
    skills: Optional[List[Union[LinkedInSkill, str]]] = Field(default_factory=list)
    credential_id: Optional[str] = None


class LinkedInProfile(BaseModel):
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
    def validate_name(cls, value):
        if not value:
            raise ValueError(f'{value} is required')
        return value

    @field_validator('profile_url')
    def validate_profile_url(cls, value):
        # Ensure LinkedIn profile URLs are properly formatted
        if value and not re.match(r'^https://www.linkedin.com/in/', str(value)):
            raise ValueError('Invalid LinkedIn profile URL')
        return value

    @property
    def first_name(self):
        return self.full_name.split()[0]

    @property
    def last_name(self):
        return self.full_name.split()[-1]

    @property
    def is_1st_connection(self):
        return self.connection is not None and '1' in self.connection

    @property
    def profile_summary(self):
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

