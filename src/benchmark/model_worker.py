"""Private-process deadline boundary; only public episode data enters stdin."""
import json
import sys
from ..llm import CodexClient, LLMError


def main():
    try:
        response = CodexClient(max_attempts=1).complete(**json.load(sys.stdin))
        result = {"response": response}
    except LLMError as exc:
        result = {"error": str(exc)}
    except Exception as exc:
        # Do not serialize SDK stderr, credentials, environment, or paths.
        result = {"error": f"Codex worker error ({type(exc).__name__})"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
