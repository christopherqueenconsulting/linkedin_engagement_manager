"""The two outside-world research fetchers the content pipeline grounds itself on.

`search_with_perplexity` is `content_research`'s DIRECT fallback for when the proxy's
`lem-research-preferred` route is unavailable; `search_recent_news` is the free GoogleNews path
`get_industry_trend_analysis_based_on_user_profile` drops to when research is disabled or came back
empty. Both are SOURCES, never gates — every caller already treats "nothing found" as "write from
the profile and topic alone", so raising here costs freshness, never the post.

`google_news_tool` and the `chat_*` helpers below are a worked OpenAI tool-calling example. Nothing
in the app calls them.
"""

import os
from typing import Dict

import requests
from GoogleNews import GoogleNews

from cqc_lem.utilities.ai.client import client


def search_with_perplexity(query: str, max_sources: int = 5) -> dict:
    """Search for recent information using Perplexity Sonar (online search-augmented LLM).

    Returns {query, answer, sources} where sources is a list of {url} dicts.
    Raises RuntimeError if PERPLEXITY_API_KEY is not set.
    """
    # Read key at call time so dotenv-late-load scenarios still work
    perplexity_key = os.environ.get("PERPLEXITY_API_KEY")
    if not perplexity_key:
        raise RuntimeError("PERPLEXITY_API_KEY is not set")

    response = requests.post(
        "https://api.perplexity.ai/chat/completions",
        headers={
            "Authorization": f"Bearer {perplexity_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": "sonar",
            "messages": [
                {"role": "system", "content": "Be precise and concise. Cite your sources."},
                {"role": "user", "content": query},
            ],
            "max_tokens": 512,
            "return_citations": True,
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()

    answer = data["choices"][0]["message"]["content"]
    raw_citations = data.get("citations", [])
    sources = [{"url": url} for url in raw_citations[:max_sources]]

    return {"query": query, "answer": answer, "sources": sources}


# Define the tool for GoogleNews search
def search_recent_news(industry: str, days: int = 7) -> dict:
    """Search recent news articles using GoogleNews.

    Parameters:
    - industry (str): The industry or topic to search for.
    - days (int): How many days back to search (default is 7).

    Returns:
    - dict: Contains a list of news articles with titles, dates, and links.
    """
    googlenews = GoogleNews(lang='en', period=f'{days}d')
    googlenews.search(industry)
    articles = googlenews.result()

    news_results = []
    for article in articles:
        news_results.append({
            'title': article.get('title', 'No title available'),
            'date': article.get('date', 'No date available'),
            'link': article.get('link', 'No link available')
        })

    return {
        'industry': industry,
        'days': days,
        'articles': news_results
    }


# Define a function to generate prompts for OpenAI ChatCompletion
def news_analysis_prompt(industry: str, articles: list) -> str:
    """Generate a prompt for OpenAI to analyze news articles and extract keywords/categories.

    Parameters:
    - industry (str): The industry related to the articles.
    - articles (list): List of news articles to analyze.

    Returns:
    - str: A generated prompt for OpenAI.
    """
    articles_text = "\n".join([
        f"- {article['title']} ({article['date']}): {article['link']}"
        for article in articles
    ])

    prompt = (
            f"Here are recent news articles related to the {industry} industry:\n\n"
            f"{articles_text}\n\n" +
            "Analyze these articles and provide:\n" +
            "- A list of key topics or keywords that are trending in this industry.\n" +
            "- Categories these articles belong to (e.g., Innovation, Finance, Policy).\n" +
            "- Suggestions for further exploration based on the news trends."
    )
    return prompt


# Tool definition for GoogleNews search and analysis
def google_news_tool(parameters: Dict) -> Dict:
    """Tool function for searching recent news and performing analysis.
    Expects 'industry' and 'days' in parameters.
    """
    industry = parameters.get("industry", "Technology")
    days = parameters.get("days", 7)

    # Perform news search and analysis
    news_data = search_recent_news(industry, days)
    prompt = news_analysis_prompt(news_data['industry'], news_data['articles'])

    # Call OpenAI ChatCompletion for analysis
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are an expert in industry trends and analysis."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.7
    )

    # Extract and return the response
    analysis = response.choices[0].message.content.strip()
    return {
        "industry": news_data['industry'],
        "articles": news_data['articles'],
        "analysis": analysis
    }


def get_openai_google_news_tool():
    """The GoogleNews tool declaration, with its Python implementation bound under `"function"`.

    Byte-for-byte the module-level `news_tool` below, and unlike that constant it has no callers at
    all — the two `chat_*` demos read `news_tool` directly. Kept as the worked example of how the
    tool would be handed to a completion.
    """
    # Define the tool as an iterable object for ChatCompletion
    return {
        "name": "google_news_tool",
        "description": "Search for recent news articles about an industry and perform trend analysis.",
        "parameters": {
            "type": "object",
            "properties": {
                "industry": {"type": "string", "description": "The industry or topic to search for."},
                "days": {"type": "integer", "description": "How many days back to search.", "default": 7}
            },
            "required": ["industry"]
        },
        "function": google_news_tool  # Bind the tool function
    }


news_tool = {
    "name": "google_news_tool",
    "description": "Search for recent news articles about an industry and perform trend analysis.",
    "parameters": {
        "type": "object",
        "properties": {
            "industry": {"type": "string", "description": "The industry or topic to search for."},
            "days": {"type": "integer", "description": "How many days back to search.", "default": 7}
        },
        "required": ["industry"]
    },
    "function": google_news_tool  # Bind the tool function
}


def chat_with_tools(industry: str, days: int):
    """ChatCompletion with integrated tools for GoogleNews analysis.
    """
    # Messages for the chat session
    messages = [
        {"role": "system", "content": "You are a virtual assistant with access to external tools."},
        {"role": "user", "content": f"Can you find recent news about {industry} and analyze trends?"}
    ]

    # Pass tools to ChatCompletion
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        tools=[news_tool],  # Add your tool here
        function_call="auto",  # Enable dynamic tool invocation
    )

    output = response.choices[0].message.content.strip()

    return output

def chat_about_news(news, industry):
    """ChatCompletion with integrated tools for GoogleNews analysis.
    """
    # Messages for the chat session
    messages = [
        {"role": "system", "content": "You are a virtual assistant with access to external tools."},
        {"role": "user", "content": f"Can you find recent news about {industry} and analyze trends?"}
    ]

    # Pass tools to ChatCompletion
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        tools=[news_tool],  # Add your tool here
        function_call="auto",  # Enable dynamic tool invocation
    )

    output = response.choices[0].message.content.strip()

    return output


if __name__ == "__main__":
    #industry = "Artificial Intelligence"
    #days = 3
    #response = chat_with_tools(industry, days)
    #print(response)
    news = google_news_tool({'industry': 'Artificial Intelligence', 'days': 3})
    #print(news)

