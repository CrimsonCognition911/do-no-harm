"""Create the reusable DO NO HARM examiner in the caller's OpenAI project."""

import os

from openai import OpenAI

from backend.examiner_agent import AGENT_ID_ENV, create_saved_examiner_agent


def main():
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY or OPENAI_KEY is required")
    agent_id = create_saved_examiner_agent(OpenAI(api_key=api_key))
    print(f"{AGENT_ID_ENV}={agent_id}")


if __name__ == "__main__":
    main()
