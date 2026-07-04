import asyncio

from dotenv import load_dotenv
from google.adk import Agent, Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import google_search
from google.genai.types import Content, Part

from prompt import SYSTEM_PROMPT

load_dotenv()

async def main():
    agent = Agent(
        name="Researches",
        model="gemini-3.5-flash",
        tools=[google_search],
        instruction=SYSTEM_PROMPT
    )

    session_service = InMemorySessionService()

    app_name = "my-app"
    user_id = "an-1"
    session_id = "an-2"

    await session_service.create_session(
        app_name=app_name,
        user_id=user_id,
        session_id=session_id
    )

    runner = Runner(
        app_name=app_name,
        agent=agent,
        session_service=session_service
    )

    content = Content(role="user", parts=[Part(text="What is java?")])

    for event in runner.run(
        user_id=user_id,
        session_id=session_id,
        new_message=content
    ):
        print(event.content.parts[0].text)

if __name__ == '__main__':
    asyncio.run(main())



