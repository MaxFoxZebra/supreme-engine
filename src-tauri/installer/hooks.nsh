; The app runs a separate renderer process (cv-studio-server.exe). NSIS only
; knows to close the main executable, so an orphaned or still-running renderer
; keeps a lock on the DLLs being replaced and the install fails with
; "Error opening file for writing". Close it before touching the files.
;
; This matches the app's own renderer only. The connector an AI client runs
; is a per-version copy elsewhere, named cv-studio-mcp.exe (see "The
; connector's own copy" in server/studio.py), so an update no longer cuts
; Claude Desktop off mid-conversation.

!macro NSIS_HOOK_PREINSTALL
  DetailPrint "Closing the CV Studio renderer..."
  nsExec::Exec 'taskkill /F /IM cv-studio-server.exe /T'
  Sleep 400
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  DetailPrint "Closing the CV Studio renderer..."
  nsExec::Exec 'taskkill /F /IM cv-studio-server.exe /T'
  Sleep 400
!macroend
