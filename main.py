import asyncio
import os
import pathlib
import sys
from typing import Any, Dict

from dotenv import load_dotenv
from google.adk import Agent, Runner
from google.adk.sessions import DatabaseSessionService
from google.adk.tools import McpToolset, FunctionTool
from google.adk.tools.google_search_tool import GoogleSearchTool
from google.adk.tools.mcp_tool import StdioConnectionParams
from google.genai.types import Content, Part
from rich.console import Console
from rich.panel import Panel
from rich.live import Live
from rich.markdown import Markdown
from prompt_toolkit import PromptSession

from prompt import SYSTEM_PROMPT

# Load environment variables
load_dotenv()

# Initialize Rich console for beautiful terminal output
console = Console()

async def run_terminal_command(command: str) -> Dict[str, Any]:
    """Executes a shell command on the local machine and returns stdout, stderr, and the exit code.

    Args:
        command: The shell command to run (e.g., 'python3 --version', 'git status', 'ls -la').

    Returns:
        A dictionary containing 'stdout', 'stderr', and 'exit_code'.
    """
    console.print(f"[yellow]Executing terminal command:[/yellow] {command}")
    try:
        # Run command asynchronously to avoid blocking the agent loop
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        # Wait for command execution with a 30-second timeout
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30.0)
        
        return {
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "exit_code": process.returncode
        }
    except asyncio.TimeoutError:
        try:
            process.kill()
        except Exception:
            pass
        return {
            "stdout": "",
            "stderr": "Command execution timed out after 30 seconds.",
            "exit_code": -1
        }
    except Exception as e:
        return {
            "stdout": "",
            "stderr": f"Failed to execute command: {str(e)}",
            "exit_code": -1
        }

async def get_user_input(session: PromptSession, multiline: bool = False) -> str:
    """Collects user input supporting standard single-line and multiline editing.
    
    If sys.stdin is not a TTY (interactive terminal), it gracefully falls back 
    to standard input reading.
    """
    # Check if standard input is a real interactive terminal (TTY)
    if not sys.stdin.isatty():
        if multiline:
            console.print("[bold yellow](Non-interactive fallback) Enter multiline text. Type 'EOF' on a new line to submit:[/bold yellow]")
            lines = []
            while True:
                try:
                    line = input()
                    if line.strip() == "EOF":
                        break
                    lines.append(line)
                except EOFError:
                    break
            return "\n".join(lines)
        else:
            return input()

    # Using prompt_toolkit for interactive terminals
    if multiline:
        console.print("[bold blue]You (multiline - Press Alt+Enter or Esc+Enter to submit, or /single to switch):[/bold blue]")
        return await session.prompt_async("> ", multiline=True)
    else:
        # Prompt-toolkit single-line input
        # We print "You:" using rich for proper styling, and then read input with prompt_toolkit
        console.print("[bold green]You:[/bold green] ", end="")
        return await session.prompt_async("")

async def main():
    console.print(Panel.fit(
        "[bold green]Google ADK Agent Enhancement[/bold green]\n"
        "[blue]Enabling File System MCP Tool & Terminal Tool[/blue]",
        border_style="green"
    ))

    # 1. Setup the Filesystem MCP Toolset
    # We dynamically find the workspace path to expose to the MCP filesystem server.
    # It defaults to the agent's workspace directory.
    workspace_dir = os.getenv("MCP_WORKSPACE_PATH") or str(pathlib.Path(__file__).parent.resolve())
    console.print(f"[bold info]Exposing Workspace to MCP Filesystem Server:[/bold info] {workspace_dir}")
    
    from mcp import StdioServerParameters
    
    filesystem_toolset = McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command="npx",
                args=["-y", "@modelcontextprotocol/server-filesystem", workspace_dir],
            )
        ),
        errlog=open(os.devnull, "w")
    )

    # 2. Setup the Terminal Tool (FunctionTool wrapping our async run_terminal_command function)
    terminal_tool = FunctionTool(run_terminal_command)

    # Setup the Google Search Tool with bypass_multi_tools_limit enabled,
    # as mixing built-in tools with client function-calling is restricted by the Gemini API
    # unless wrapped as a separate sub-agent tool.
    google_search = GoogleSearchTool(bypass_multi_tools_limit=True)

    # 3. Setup the Agent with all our tools: MCP Filesystem, and Terminal Command
    agent = Agent(
        name="SuperAssistant",
        model="gemini-3.5-flash",
        tools=[filesystem_toolset, terminal_tool, google_search],
        instruction=(
            SYSTEM_PROMPT + 
            "\n\nIn addition to deep research, you have direct access to the local filesystem (via MCP tools) "
            "and terminal commands (via execute_terminal_command). You can read/write files and run "
            "commands to assist the user in their programming and research tasks. "
            "Use these tools efficiently to solve problems directly."
        )
    )

    # Use SQLite Database Session Service for persistent session storage
    db_path = os.getenv("SQLITE_DB_URL") or "sqlite+aiosqlite:///adk_agent.db"
    console.print(f"[bold info]Using SQLite database for checkpointer:[/bold info] {db_path}")
    session_service = DatabaseSessionService(db_url=db_path)

    app_name = "enhanced-adk-agent"
    user_id = "user-1"
    session_id = "session-1"

    # Check if a persistent session already exists to allow session resumption
    existing_session = await session_service.get_session(
        app_name=app_name,
        user_id=user_id,
        session_id=session_id
    )
    
    if existing_session is None:
        await session_service.create_session(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id
        )
        console.print(f"[bold green]Created new persistent session:[/bold green] {session_id}")
    else:
        console.print(f"[bold green]Resumed existing persistent session:[/bold green] {session_id}")

    runner = Runner(
        app_name=app_name,
        agent=agent,
        session_service=session_service
    )

    console.print("\n[bold green]Interactive Mode Enabled![/bold green] You can now talk to the agent.")
    console.print("Type '[bold red]exit[/bold red]' or '[bold red]quit[/bold red]' to stop the session.")
    console.print("Type '[bold blue]/multiline[/bold blue]' or '[bold blue]/m[/bold blue]' to toggle multi-line input mode.\n")

    session = PromptSession()
    multiline_mode = False

    try:
        while True:
            user_input = await get_user_input(session, multiline_mode)
            user_input_stripped = user_input.strip()
            
            if not user_input_stripped:
                continue
                
            if user_input_stripped.lower() in ("/multiline", "/m"):
                multiline_mode = True
                console.print("[bold yellow]Switched to Multiline Mode![/bold yellow] Press [bold cyan]Alt+Enter[/bold cyan] (or [bold cyan]Esc then Enter[/bold cyan]) to submit your message. Type [bold yellow]/single[/bold yellow] or [bold yellow]/s[/bold yellow] to switch back.\n")
                continue

            if user_input_stripped.lower() in ("/single", "/s"):
                multiline_mode = False
                console.print("[bold yellow]Switched to Single-line Mode![/bold yellow] Press [bold cyan]Enter[/bold cyan] to submit. Type [bold yellow]/multiline[/bold yellow] or [bold yellow]/m[/bold yellow] to switch back.\n")
                continue

            if user_input_stripped.lower() in ("exit", "quit"):
                console.print("\n[bold yellow]Goodbye![/bold yellow]\n")
                break

            content = Content(role="user", parts=[Part(text=user_input_stripped)])

            console.print("[bold yellow]SuperAssistant:[/bold yellow]")
            
            accumulated_text = ""
            with Live(Markdown(""), console=console, auto_refresh=True) as live:
                # Run the agent asynchronously and iterate over the generated events
                async for event in runner.run_async(
                    user_id=user_id,
                    session_id=session_id,
                    new_message=content
                ):
                    # Inspect event content
                    if event.content and event.content.parts:
                        part = event.content.parts[0]
                        if part.text:
                            accumulated_text += part.text
                            live.update(Markdown(accumulated_text))
            
            # Print a newline at the end of the streaming response
            console.print()
            
    except (KeyboardInterrupt, EOFError):
        console.print("\n[bold yellow]Session cancelled. Goodbye![/bold yellow]\n")
    except Exception as e:
        console.print(f"\n[bold red]Error running agent:[/bold red] {e}", style="red")
    finally:
        # Clean up the database session service if defined
        if "session_service" in locals() and session_service is not None:
            await session_service.close()
        # Clean up the MCP toolset connections
        await filesystem_toolset.close()

if __name__ == '__main__':
    asyncio.run(main())
