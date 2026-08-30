"""PyInstaller entry point for CV Studio's Python side.

One binary, two modes:
  --mcp            speak MCP over stdio, for Claude Desktop and other AI clients
  (default)        run the local HTTP server the desktop app talks to
"""
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()   # a frozen app will fork-bomb itself without this
    if "--mcp" in sys.argv:
        argv = [a for a in sys.argv[1:] if a != "--mcp"]
        workspace = None
        for i, a in enumerate(argv):
            if a in ("--workspace", "--career-dir") and i + 1 < len(argv):
                workspace = argv[i + 1]
        import mcp_server
        sys.exit(mcp_server.main(workspace))
    from studio import main
    sys.exit(main())
