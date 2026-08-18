import asyncio
import os
import sys

from dotenv import load_dotenv


async def main() -> int:
    load_dotenv()

    if os.environ.get("ANTHROPIC_API_KEY"):
        print("FAIL: ANTHROPIC_API_KEY is set. It outranks the subscription "
              "token, so this spike would give a false pass. Unset it.")
        return 2
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        print("FAIL: CLAUDE_CODE_OAUTH_TOKEN not set. Run: claude setup-token")
        return 2

    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                                  TextBlock, query)

    chunks = []
    async for message in query(prompt="Reply with exactly: OK",
                               options=ClaudeAgentOptions(tools=None)):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)

    reply = "".join(chunks).strip()
    if "OK" in reply:
        print("PASS: subscription token works. Marginal token cost is zero.")
        return 0

    print(f"UNCLEAR: call succeeded but reply was {reply!r}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
