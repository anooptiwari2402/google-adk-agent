import asyncio
import os
import pathlib
import shlex
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

def is_destructive(command: str) -> bool:
    """Checks if a command is potentially destructive (e.g., contains rm, rmdir, or -rf)."""
    try:
        # shlex.split helps parse shell commands correctly including quotes
        tokens = shlex.split(command)
    except Exception:
        tokens = command.split()
        
    if not tokens:
        return False
        
    # 1. Check if the executable itself is rm, rmdir, or ends with /rm, /rmdir
    first_token = tokens[0]
    is_rm_command = (first_token == 'rm' or first_token.endswith('/rm') or 
                     first_token == 'rmdir' or first_token.endswith('/rmdir'))
    
    # Also handle sudo, xargs, etc. before the executable
    for i, token in enumerate(tokens):
        if token in ('sudo', 'xargs') or '=' in token:
            if i + 1 < len(tokens):
                next_tok = tokens[i + 1]
                if next_tok in ('rm', 'rmdir') or next_tok.endswith('/rm') or next_tok.endswith('/rmdir'):
                    is_rm_command = True
                    break
                    
    # 2. Check if 'rm' is used inside evaluation commands like bash -c "rm -rf foo"
    is_eval_with_rm = False
    if first_token in ('bash', 'sh', 'zsh', 'python', 'python3', 'eval', 'exec'):
        for token in tokens[1:]:
            if 'rm ' in token or 'rmdir ' in token or 'rm -' in token:
                is_eval_with_rm = True
                break

    if is_rm_command or is_eval_with_rm:
        return True
        
    # 3. Check for standalone 'rm' or 'rmdir' tokens (e.g. echo foo && rm bar)
    for idx, token in enumerate(tokens):
        if token in ('rm', 'rmdir'):
            # Skip if preceded by commands where rm is an argument (git, grep, echo, etc.)
            # except when separated by shell operators (&&, ;, |, ||)
            if idx > 0:
                prev_token = tokens[idx - 1]
                if prev_token in ('git', 'grep', 'echo', 'cat', 'ag', 'rg', 'nano', 'vim', 'vi'):
                    continue
            return True
            
    return False

async def run_terminal_command(command: str) -> Dict[str, Any]:
    """Executes a shell command on the local machine and returns stdout, stderr, and the exit code.

    Args:
        command: The shell command to run (e.g., 'python3 --version', 'git status', 'ls -la').

    Returns:
        A dictionary containing 'stdout', 'stderr', and 'exit_code'.
    """
    if is_destructive(command):
        console.print(f"\n[bold red]⚠️  WARNING: Potentially destructive terminal command detected![/bold red]")
        console.print(f"[bold yellow]Command:[/bold yellow] {command}")
        console.print("[bold red]This command contains destructive operations (such as rm or -rf).[/bold red]")
        
        # Ask for human intervention/confirmation in a non-blocking way for the event loop
        loop = asyncio.get_running_loop()
        try:
            # Run input() in an executor thread so it doesn't block the main event loop
            user_response = await loop.run_in_executor(
                None, 
                lambda: input("Do you want to allow execution of this command? (type 'yes' to proceed, any other key to cancel): ")
            )
        except Exception as e:
            # Fallback if stdin is not interactive
            user_response = "no"
            console.print(f"[bold red]Failed to get user confirmation (error: {e}). Aborting.[/bold red]")
            
        if user_response.strip().lower() != 'yes':
            console.print("[bold red]🚫 Command execution cancelled by user.[/bold red]\n")
            return {
                "stdout": "",
                "stderr": "Command execution aborted: Guardrails require human confirmation ('yes') for destructive commands (rm, -rf).",
                "exit_code": -1
            }
        console.print("[bold green]✅ User confirmed. Executing command...[/bold green]\n")

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

    # Setup the Groww MCP Toolset via mcp-remote bridge
    console.print(f"[bold info]Connecting to remote Groww MCP Server...[/bold info]")
    groww_toolset = McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command="npx",
                args=["-y", "mcp-remote", "https://mcp.groww.in/mcp"],
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

    # 3. Setup the Agent with all our tools: MCP Filesystem, Groww Portfolio, Terminal Command, and Google Search
    agent = Agent(
        name="SuperAssistant",
        model="gemini-3.5-flash",
        tools=[filesystem_toolset, groww_toolset, terminal_tool, google_search],
        instruction=SYSTEM_PROMPT
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
        await groww_toolset.close()
        await filesystem_toolset.close()

if __name__ == '__main__':
    asyncio.run(main())
